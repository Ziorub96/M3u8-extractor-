#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StreamCorner / CornerStream Extractor — v9
==========================================
Fusione di v8 (scan mirror, fallback same-origin, crawler profondo, profili
header) con le robustezze di v7.2 (workflow aliases, cache con scadenza,
range-safety, validazione vault, decadimento penalità).

Novità v9
---------
  * --scan / --diagnose funzionanti, con classificazione COMPLETO/VIVO+VAULT/VIVO
  * mode="worker" | "origin": usa same-origin se il mirror non ha workers.dev
  * Path API e parametri scoperti dal bundle (searchParams.set non minificato)
  * Crawler profondo (assets/*.js) con deadline per non bloccare il run
  * 3 profili header HTML + 3 profili POST con rotazione
  * Alias workflow: --output, --playwright-workers, --playwright-limit
  * Cache runtime con MAX_RUNTIME_AGE e invalidazione per sito dismesso
  * gc() range-safe, _vault_is_valid() decodifica davvero le specs
  * Penalità worker con decadimento temporale
  * KODIPROP license_key sanificato da '|'

Requisiti:
    pip install requests pycryptodome
    pip install patchright && patchright install chromium

Uso:
    python sc9.py --scan
    python sc9.py --scan --proxy http://utente:pass@ip-it:porta
    python sc9.py --site https://streamcorner.foo/
    python sc9.py
    python sc9.py --diagnose
"""

from __future__ import annotations
import argparse, base64, hashlib, json, os, random, re, socket, struct, sys, threading, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# CONFIG
# ============================================================
UA_IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile Safari/604.1")
UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

BROWSER_COMMON = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
}

HTML_PROFILES = [
    ("desktop", {"User-Agent": UA_DESKTOP, **BROWSER_COMMON}),
    ("iphone", {"User-Agent": UA_IPHONE, **BROWSER_COMMON}),
    ("minimo", {"User-Agent": UA_IPHONE}),
]

POST_PROFILES = [
    ("origin+iphone", UA_IPHONE, True),
    ("origin+desktop", UA_DESKTOP, True),
    ("plain+iphone", UA_IPHONE, False),
]

PRIORITY_SITES = [
    "https://streamcorner.st/",
    "https://streamcorner.foo/",
    "https://streamcorner.cloud/",
    "https://streamcorner.fun/",
    "https://cornerstream.tech/",
]
TLDS = ["st", "foo", "cloud", "fun", "tech", "is", "to", "ws", "cc", "su",
        "pro", "online", "site", "top", "xyz", "app", "live", "tv", "pw", "me"]
BASES = ["streamcorner", "cornerstream"]


def build_candidates():
    out = list(PRIORITY_SITES)
    for b in BASES:
        for t in TLDS:
            u = f"https://{b}.{t}/"
            if u not in out:
                out.append(u)
    return out


PROVIDERS_DEFAULT = "admin"
KEY_FIELDS = ("stream_id", "game_id", "channel_id", "id")
DIRECT_RE = re.compile(r"\.(?:m3u8|mpd)(?:$|\?)", re.I)
KEY_PAIR_RE = re.compile(r"^[0-9a-f]{32}:[0-9a-f]{32}$", re.I)
HOST_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*\.workers\.dev$")
B64_RE = re.compile(r"^[A-Za-z0-9_\-+/=]+$")

SCRIPT_SRC_RE = re.compile(r'<script\b[^>]*\bsrc=["\']([^"\']+\.js)["\']', re.I)
SIBLING_RE = re.compile(r"""["']\./([A-Za-z0-9_\-]+\.js)["']""")
ASSET_RE = re.compile(r"""["'`](?:\./|/)?(assets/[A-Za-z0-9_\-.]+\.js)["'`]""")
BAREJS_RE = re.compile(r"""["'`]([A-Za-z0-9_\-]{4,}\.js)["'`]""")
PATH_RE = re.compile(r"""["'`](/[A-Za-z0-9_\-][A-Za-z0-9_\-/]{1,40})["'`]""")
PARAM_RE = re.compile(r"""searchParams\.set\(\s*["']([A-Za-z_]\w{0,7})["']""")
# prossimità a fetch/open per filtrare i path API da quelli statici
FETCH_CTX = re.compile(r"""(?:fetch|open|url|URL)\([^)]{0,200}?["'`](/[^"'`]+)["'`]""")

RUNTIME_CACHE = ".sc_runtime.json"
M3U8_CACHE = ".sc_m3u8_cache.json"
DRM_REPORT = "sc_drm_report.jsonl"

LIVE_MARGIN_BEFORE = 1800
LIVE_MARGIN_AFTER = 3600
WORKER_TRIES = 4
MAX_ASSETS = 44
MAX_ASSETS_SCAN = 12
CRAWL_BUDGET = 30
MAX_SCAN_CRAWLS = 6
MAX_RUNTIME_AGE = 7 * 86400
BAD_DECAY = 300
MAX_REQUESTS_CAPTURED = 120

# indici magic nel vault (reverse-engineered)
SPEC_TRANSCRIPT, SPEC_ENDPOINT, SPEC_NAMESPACE = 0, 1, 3
CONST_TRANSCRIPT, CONST_NONCE, CONST_BRAND = 16, 20, 25

CENSUS = {"detail": Counter(), "stream": Counter(),
          "events": 0, "sources": 0, "with_stream_url": 0}


def log(*a):
    print(*a, flush=True)


# ============================================================
# HTTP
# ============================================================
_tls = threading.local()


def session(proxy=None):
    s = getattr(_tls, "s", None)
    if s is None:
        s = requests.Session()
        s.verify = False
        if proxy:
            s.proxies = {"http": proxy, "https": proxy}
        _tls.s = s
    return s


def fetch_html(url, proxy=None, timeout=15):
    """Prova i profili HTML in ordine. Ritorna (status_finale, testo|None)."""
    last = None
    for _name, h in HTML_PROFILES:
        try:
            r = session(proxy).get(url, headers=h, timeout=timeout,
                                   allow_redirects=True)
        except requests.exceptions.ConnectionError:
            return None, None
        except requests.exceptions.Timeout:
            return "timeout", None
        except requests.exceptions.SSLError:
            return "ssl", None
        except Exception:
            last = last or "err"
            continue
        if r.status_code == 200 and r.text:
            return 200, r.text
        last = r.status_code
    return last, None


def get_text(url, referer=None, proxy=None, timeout=20, ua=UA_IPHONE):
    h = {"User-Agent": ua}
    if referer:
        h["Referer"] = referer
    r = session(proxy).get(url, headers=h, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r.text


def site_status(site, proxy=None, timeout=15):
    """Classifica un candidato in una stringa leggibile."""
    st, html = fetch_html(site, proxy, timeout)
    if st is None:
        return "conn", None
    if st == "timeout":
        return "timeout", None
    if st == "ssl":
        return "ssl", None
    return st, html


# ============================================================
# SALSA20/12
# ============================================================
M32 = 0xFFFFFFFF


def rotl32(v, c):
    return ((v << c) & M32) | (v >> (32 - c))


def x8(t, e):
    n = 7 & e
    return 255 & ((t << n) | (t >> (8 - n)))


def Y8(t, e):
    n = 7 & e
    return 255 & ((t >> n) | (t << (8 - n)))


def _qr(s, a, b, c, d):
    s[b] ^= rotl32((s[a] + s[d]) & M32, 7); s[c] ^= rotl32((s[b] + s[a]) & M32, 9)
    s[d] ^= rotl32((s[c] + s[b]) & M32, 13); s[a] ^= rotl32((s[d] + s[c]) & M32, 18)


def _blk(k32, n8, ctr):
    k = list(struct.unpack("<8I", k32)); n0, n1 = struct.unpack("<2I", n8)
    st = [0x61707865, k[0], k[1], k[2], k[3], 0x3320646e, n0, n1,
          ctr & M32, (ctr >> 32) & M32, 0x79622d32, k[4], k[5], k[6], k[7], 0x6b206574]
    x = st[:]
    for _ in range(6):
        _qr(x, 0, 4, 8, 12); _qr(x, 5, 9, 13, 1); _qr(x, 10, 14, 2, 6); _qr(x, 15, 3, 7, 11)
        _qr(x, 0, 1, 2, 3); _qr(x, 5, 6, 7, 4); _qr(x, 10, 11, 8, 9); _qr(x, 15, 12, 13, 14)
    return struct.pack("<16I", *[(x[i] + st[i]) & M32 for i in range(16)])


def salsa20_12(k, n8, d, ctr=0):
    out = bytearray(); i = 0
    while i < len(d):
        ks = _blk(k, n8, ctr + i // 64)
        out.extend(bytes(a ^ b for a, b in zip(d[i:i + 64], ks))); i += 64
    return bytes(out)


# ============================================================
# VAULT
# ============================================================
SBOX = [185, 243, 94, 251, 153, 162, 126, 77, 155, 41, 182, 62, 200, 173, 252, 244]


def dec_payload(p, k):
    out = bytearray(len(p)); c = 117; y = 84 ^ k[1]
    for a in range(len(p)):
        h = (k[15 & a] ^ SBOX[15 & a] ^ y ^ ((139 * a) & 0xFF)
             ^ ((1293491885 >> (8 * (3 & a))) & 0xFF) ^ c) & 0xFF
        out[a] = p[a] ^ h; y = p[a]; c = x8((c ^ p[a] ^ k[(a + 7) & 15]) & 0xFF, 1)
    return bytes(out)


def dec_spec(s):
    t0, t1, t2, t3, t4, t5 = s[:6]
    al = lambda v: v["v"] if isinstance(v, dict) else v
    t2, t3, t4, t5 = al(t2), al(t3), al(t4), al(t5)
    e = bytearray(len(t2)); n = t0
    for o in range(len(t2)):
        i = t5[o]; inner = x8(t2[o], 7 & (n ^ t1))
        m1 = (t3[i] + (n ^ ((13 * o) & 255))) & 255; m2 = (t1 + 17 * o) & 255
        r = Y8((inner ^ m1 ^ m2) & 255, 7 & (t4[o] ^ n)); e[o] = r
        n = (x8(n, 1) ^ ((r + t4[o] + o) & 255)) & 255
    return bytes(e)


def gc(v, i):
    consts = v.get("consts") or []
    if not isinstance(consts, list) or i < 0 or i >= len(consts):
        raise IndexError(f"consts[{i}] fuori range (len={len(consts)})")
    c = consts[i]
    if isinstance(c, dict):
        t = c.get("t", "u8"); val = c.get("v")
        if t == "u8":  return bytes(val)
        if t == "u32": return b"".join(struct.pack("<I", x) for x in val)
        if t == "s":   return val.encode()
        if t == "a":   return b"".join(x.encode() for x in val)
        if t == "n":   return bytes([val])
    return b""


def b64d(s):
    s = s.strip().replace("-", "+").replace("_", "/")
    s += "=" * ((4 - len(s) % 4) % 4)
    return base64.b64decode(s)


# ============================================================
# LETTURA ARRAY (senza nomi)
# ============================================================
def read_array(code, st):
    if st >= len(code) or code[st] != "[":
        return None
    d, i = 1, st + 1
    while i < len(code) and d > 0:
        c = code[i]
        if c == "[": d += 1
        elif c == "]": d -= 1
        elif c in ("'", '"'):
            q = c; i += 1
            while i < len(code) and code[i] != q:
                if code[i] == "\\": i += 1
                i += 1
        i += 1
    if d != 0:
        return None
    return re.findall(r"""["']([^"']*)["']""", code[st:i])


def string_arrays(code):
    out, seen = [], set()
    for m in re.finditer(r"""\[\s*["']""", code):
        st = code.index("[", m.start())
        if st in seen:
            continue
        seen.add(st)
        arr = read_array(code, st)
        if arr and len(arr) >= 2:
            out.append(arr)
    return out


def find_workers(code):
    best = []
    for arr in string_arrays(code):
        hosts = [h for h in arr if h and HOST_RE.match(h)]
        if len(hosts) >= 3 and len(hosts) > len(best):
            best = hosts
    return best


def _vault_is_valid(v):
    if not (isinstance(v, dict)
            and isinstance(v.get("specs"), list)
            and isinstance(v.get("consts"), list)
            and len(v["specs"]) >= 4):
        return False
    try:
        s0 = dec_spec(v["specs"][SPEC_TRANSCRIPT])
        s1 = dec_spec(v["specs"][SPEC_ENDPOINT])
    except Exception:
        return False
    return len(s0) >= 8 and len(s1) >= 8


def try_vault(code):
    blobs = []
    for arr in string_arrays(code):
        if not all(B64_RE.match(x or "") for x in arr):
            continue
        try:
            raw = b64d("".join(arr))
        except Exception:
            continue
        if raw:
            blobs.append(raw)
    keys = [b for b in blobs if 16 <= len(b) <= 64]
    pays = [b for b in blobs if len(b) >= 128]
    for k in keys:
        for p in pays:
            try:
                v = json.loads(dec_payload(p, k).decode())
            except Exception:
                continue
            if _vault_is_valid(v):
                return v
    return None


def find_api(code):
    """Path API + nomi parametri. searchParams.set non è minificato (DOM API)."""
    params = PARAM_RE.findall(code)
    # path in prossimità di fetch/open/URL — segnale forte
    ctx_paths = [p for p in FETCH_CTX.findall(code)
                 if "." not in p and not p.startswith(("/assets", "/@", "/src", "/static"))]
    # fallback: tutti i path brevi
    loose = [p for p in PATH_RE.findall(code)
             if "." not in p and not p.startswith(("/assets", "/@", "/src", "/static"))]
    return list(dict.fromkeys(ctx_paths + loose)), params


def vault_strings(v):
    out = []
    for c in v.get("consts", []):
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, dict) and isinstance(c.get("v"), str):
            out.append(c["v"])
        elif isinstance(c, dict) and isinstance(c.get("v"), list):
            out.extend(x for x in c["v"] if isinstance(x, str))
    return out


# ============================================================
# CRAWLER
# ============================================================
def asset_names(code):
    names = list(SIBLING_RE.findall(code))
    names += [n.split("/", 1)[1] for n in ASSET_RE.findall(code)]
    names += [n for n in BAREJS_RE.findall(code) if "/" not in n]
    return list(dict.fromkeys(names))


def crawl(start_urls, referer, proxy, deadline, max_assets=MAX_ASSETS):
    seen, queue, assets = set(), list(start_urls), []
    workers, vault = [], None
    paths, params = [], []

    while queue and len(seen) < max_assets and time.time() < deadline:
        u = queue.pop(0)
        if u in seen:
            continue
        seen.add(u)
        try:
            code = get_text(u, referer=referer, proxy=proxy, timeout=15)
        except Exception:
            continue
        assets.append(u)
        short = u.rsplit("/", 1)[-1]

        if not workers:
            w = find_workers(code)
            if w:
                workers = w
                log(f"      ✓ pool worker: {len(w)} host (da {short})")
        if vault is None:
            v = try_vault(code)
            if v:
                vault = v
                log(f"      ✓ vault decodificato (da {short})")
                paths += [s for s in vault_strings(v)
                          if isinstance(s, str) and s.startswith("/")]
        p2, pr2 = find_api(code)
        paths += p2
        params += pr2
        if workers and vault:
            break

        for n in asset_names(code):
            nu = urljoin(u, "./" + n)
            if nu not in seen:
                queue.append(nu)

    # scelta path API
    cand = [p for p in paths if "corner" in p.lower()]
    if not cand:
        cand = paths
    path = Counter(cand).most_common(1)[0][0] if cand else "/corner"
    ps = list(dict.fromkeys(params))
    pparam = ps[0] if ps else "p"
    idparam = ps[1] if len(ps) > 1 else "id"
    return workers, vault, assets, path, pparam, idparam


# ============================================================
# DISCOVERY
# ============================================================
def discover(proxy=None, force_site=None, verbose=True):
    sites = [force_site] if force_site else build_candidates()
    if verbose:
        log(f"   scansione {len(sites)} candidati...")

    # fase 1: fetch HTML parallelo
    scanned = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(fetch_html, s, proxy): s for s in sites}
        for f in as_completed(futs):
            s = futs[f]
            try:
                st, html = f.result()
            except Exception:
                st, html = None, None
            scanned[s] = (st, html)

    order = {s: i for i, s in enumerate(sites)}
    ordered = sorted(scanned.items(), key=lambda kv: order.get(kv[0], 999))

    # fase 2: crawl dei soli candidati vivi
    results = []
    crawls_done = 0
    for s, (st, html) in ordered:
        if not html:
            continue
        if crawls_done >= MAX_SCAN_CRAWLS:
            if verbose:
                log(f"   ⏹ limite crawl ({MAX_SCAN_CRAWLS}) raggiunto")
            break
        origin = "{0.scheme}://{0.netloc}".format(urlparse(s))
        host = urlparse(s).netloc
        srcs = list(dict.fromkeys(
            urljoin(s, x) for x in SCRIPT_SRC_RE.findall(html)
            if urlparse(urljoin(s, x)).netloc == host))
        if not srcs:
            if verbose:
                log(f"   {s:34} HTTP 200 ma nessuno script")
            continue
        crawls_done += 1
        if verbose:
            log(f"   → crawl {s}")
        deadline = time.time() + CRAWL_BUDGET
        w, v, assets, path, pp, ip = crawl(srcs, s, proxy, deadline)
        results.append({"site": s, "origin": origin, "workers": w, "vault": v,
                        "assets": assets, "api_path": path, "p_param": pp,
                        "id_param": ip, "discovered_at": int(time.time())})
        if verbose:
            log(f"      worker={len(w)} vault={'SI' if v else 'NO'} "
                f"api={path} p={pp} id={ip}")
        if w and v:
            break

    if not results:
        errs = []
        for s, (st, _) in ordered[:6]:
            errs.append(f"{s} {st if st is not None else 'conn'}")
        raise RuntimeError("nessun candidato con script utilizzabili → "
                           + " | ".join(errs))

    def score(r):
        return 2 if (r["workers"] and r["vault"]) else (1 if r["vault"] else 0)

    results.sort(key=score, reverse=True)
    best = results[0]
    if not best["vault"]:
        raise RuntimeError("nessun candidato con vault decodificabile")

    if best["workers"]:
        best["mode"] = "worker"
        best["targets"] = [f"https://{w}" for w in best["workers"]]
    else:
        best["mode"] = "origin"
        best["targets"] = [best["origin"]]
        if verbose:
            log(f"   ⚠️ nessun pool worker: uso API same-origin {best['origin']}")
    return best


def get_runtime(refresh=False, proxy=None, force_site=None):
    if not refresh and not force_site and os.path.exists(RUNTIME_CACHE):
        try:
            rt = json.load(open(RUNTIME_CACHE, encoding="utf-8"))
            age = int(time.time()) - rt.get("discovered_at", 0)
            site_ok = (rt.get("site") in build_candidates()) or rt.get("site") == force_site
            if (rt.get("vault") and rt.get("targets") and rt.get("origin")
                    and site_ok and age < MAX_RUNTIME_AGE):
                return rt, f"cache ({age // 3600}h)"
            if not site_ok:
                log(f"   ⏳ cache punta a sito dismesso ({rt.get('site')}) — refresh")
            elif age >= MAX_RUNTIME_AGE:
                log(f"   ⏳ cache scaduta ({age // 86400}g) — refresh")
        except Exception:
            pass
    try:
        rt = discover(proxy, force_site)
        json.dump(rt, open(RUNTIME_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
        return rt, "live"
    except Exception as e:
        if os.path.exists(RUNTIME_CACHE):
            try:
                cached = json.load(open(RUNTIME_CACHE, encoding="utf-8"))
                if cached.get("vault") and cached.get("targets"):
                    log(f"   ⚠️ discovery fallita ({str(e)[:140]}) — uso cache")
                    return cached, "fallback"
            except Exception:
                pass
        raise


# ============================================================
# CLIENT
# ============================================================
class SC:
    def __init__(self, rt, proxy=None, use_origin=True):
        v = rt["vault"]
        try:
            self.t = dec_spec(v["specs"][SPEC_TRANSCRIPT])
            self.e = dec_spec(v["specs"][SPEC_ENDPOINT])
            self.ns = dec_spec(v["specs"][SPEC_NAMESPACE]).decode("utf-8", "replace")
            self.tr = gc(v, CONST_TRANSCRIPT)
            self.nr = gc(v, CONST_NONCE)
            self.Br = gc(v, CONST_BRAND)
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            raise RuntimeError(f"vault incompatibile: {type(e).__name__}: {e}")
        if not (self.t and self.e and self.ns):
            raise RuntimeError("vault: materiale di sessione vuoto")
        self.rk = hashlib.sha256(self.t + self.e + self.tr).digest()
        self.targets = rt.get("targets") or [rt["origin"]]
        random.shuffle(self.targets)
        self.origin = rt["origin"]
        self.api_path = rt.get("api_path") or "/corner"
        self.p_param = rt.get("p_param") or "p"
        self.id_param = rt.get("id_param") or "id"
        self.proxy = proxy
        self.use_origin = use_origin
        self.pidx = 0
        self.bad = {}
        self.lock = threading.Lock()

    def headers(self):
        _n, ua, use_origin = POST_PROFILES[self.pidx % len(POST_PROFILES)]
        h = {"User-Agent": ua, "Content-Type": "text/plain",
             "Accept": "application/octet-stream"}
        if use_origin and self.use_origin:
            h["Origin"] = self.origin
            h["Referer"] = self.origin + "/"
        return h

    def build(self, p):
        n = os.urandom(8); u = int(time.time() // 300)
        msg = json.dumps([self.ns, p, u], separators=(",", ":"))
        ct = salsa20_12(self.rk, n, msg.encode())
        tag = hashlib.sha256(self.rk + n + ct + self.nr).digest()[:12]
        return bytes([0x0A]) + n + tag + ct, n, u

    def _bad_score(self, host):
        entry = self.bad.get(host)
        if not entry:
            return 0
        score, ts = entry
        return 0 if time.time() - ts > BAD_DECAY else score

    def _mark(self, host, penalty):
        with self.lock:
            entry = self.bad.get(host)
            now = time.time()
            if entry is None:
                score = 0
            else:
                old_score, old_ts = entry
                score = 0 if now - old_ts > BAD_DECAY else old_score
            score = score + penalty if penalty > 0 else max(0, score - 1)
            self.bad[host] = (score, now)

    def _attempt(self, p, extra, timeout):
        body, n, u = self.build(p)
        R = hashlib.sha256(self.t + self.e + self.ns.encode() + n + p.encode()
                           + struct.pack(">I", u) + self.Br).digest()
        with self.lock:
            order = sorted(self.targets, key=self._bad_score)
        last = None
        for base in order[:WORKER_TRIES]:
            url = f"{base}{self.api_path}?{self.p_param}={p}"
            if extra:
                for k, v in extra.items():
                    url += f"&{k}={v}"
            try:
                r = session(self.proxy).post(url, data=body,
                                             headers=self.headers(), timeout=timeout)
            except Exception as e:
                last = f"{base}: {type(e).__name__}"; self._mark(base, 3); continue
            if r.status_code != 200 or not r.content:
                last = f"{base}: HTTP {r.status_code}"; self._mark(base, 1); continue
            for rn, rc in ((r.content[1:9], r.content[21:]),
                           (r.content[:8], r.content[20:])):
                if len(rc) < 2:
                    continue
                try:
                    out = json.loads(salsa20_12(R, rn, rc).decode())
                    self._mark(base, 0)
                    return out, None
                except Exception:
                    continue
            last = f"{base}: decode"; self._mark(base, 2)
        return None, last

    def call(self, p, extra=None, timeout=15):
        last = None
        for _ in range(len(POST_PROFILES)):
            out, err = self._attempt(p, extra, timeout)
            if out is not None:
                return out
            last = err
            self.pidx += 1
        if last:
            log(f"   ⚠️ p={p}{' ' + str(extra) if extra else ''} → {last}")
        return None


# ============================================================
# --scan
# ============================================================
def cmd_scan(args):
    log("=" * 66)
    log("SCAN MIRROR — chi risponde, chi ha vault, chi ha worker")
    log("=" * 66)
    sites = [args.site] if args.site else build_candidates()
    log(f"{len(sites)} candidati\n")

    rows = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(fetch_html, s, args.proxy): s for s in sites}
        for f in as_completed(futs):
            s = futs[f]
            try:
                st, html = f.result()
            except Exception:
                st, html = None, None
            rows[s] = (st, html)

    order = {s: i for i, s in enumerate(sites)}
    crawls = 0
    found_complete = False
    for s, (st, html) in sorted(rows.items(), key=lambda kv: order.get(kv[0], 999)):
        if not html:
            if st:
                log(f"  {s:34} HTTP {st}")
            else:
                log(f"  {s:34} irraggiungibile")
            continue
        host = urlparse(s).netloc
        srcs = list(dict.fromkeys(
            urljoin(s, x) for x in SCRIPT_SRC_RE.findall(html)
            if urlparse(urljoin(s, x)).netloc == host))
        if not srcs:
            log(f"  {s:34} HTTP 200, 0 script")
            continue
        if crawls >= MAX_SCAN_CRAWLS:
            log(f"  {s:34} HTTP 200 (crawl saltato: limite raggiunto)")
            continue
        crawls += 1
        deadline = time.time() + CRAWL_BUDGET
        w, v, _assets, path, pp, ip = crawl(srcs, s, args.proxy, deadline,
                                            max_assets=MAX_ASSETS_SCAN)
        tag = "VIVO"
        if v:
            tag = "VIVO+VAULT"
        if w and v:
            tag = "COMPLETO"
        log(f"  {s:34} {tag:12} worker={len(w):<3} vault={'SI' if v else 'NO':<3} "
            f"api={path} p={pp} id={ip}")
        if tag == "COMPLETO" and not args.deep:
            log("  → target completo trovato, mi fermo")
            found_complete = True
            break

    log("\nVerdetto:")
    log("  COMPLETO   = pool worker + vault → funziona come prima")
    log("  VIVO+VAULT = niente pool → lo script usa same-origin")
    log("  VIVO       = risponde ma non è un deploy utilizzabile")
    if not found_complete and crawls >= MAX_SCAN_CRAWLS:
        log(f"\n⚠️  Limite crawl raggiunto ({MAX_SCAN_CRAWLS}). "
            f"Rilancia con --deep per continuare.")


# ============================================================
# --diagnose
# ============================================================
def cmd_diagnose(args):
    log("=" * 66)
    log("DIAGNOSTICA — header vs IP")
    log("=" * 66)
    rt = None
    try:
        rt, src = get_runtime(args.refresh_vault, args.proxy, args.site)
        log(f"runtime: {src}  mode={rt.get('mode')}  targets={len(rt['targets'])}")
    except Exception as e:
        log(f"⚠️ discovery non riuscita: {str(e)[:170]}")

    if not rt:
        for s in (PRIORITY_SITES if not args.site else [args.site]):
            st, _ = site_status(s, args.proxy)
            log(f"  {s:34} {('HTTP %s' % st) if isinstance(st, int) else st}")
        return

    targets = rt["targets"][:3]
    variants = [
        ("nessun Origin/Referer", UA_IPHONE, False),
        ("Origin/Referer reali + iphone", UA_IPHONE, True),
        ("Origin/Referer reali + desktop", UA_DESKTOP, True),
        ("Origin legacy streamcorner.st", UA_IPHONE, True),
    ]

    for label, proxy in (("DIRETTO", None), ("PROXY", args.proxy)):
        if label == "PROXY" and not args.proxy:
            continue
        log(f"\n{'#' * 22} {label} {'#' * 22}")
        for t in targets:
            log(f"\n[{t}]")
            body = None
            try:
                body, _, _ = SC(rt, proxy, True).build("admin")
            except Exception:
                body = b"\x0a" + b"\x00" * 32
            for vlabel, ua, use_origin in variants:
                h = {"User-Agent": ua, "Content-Type": "text/plain"}
                if use_origin:
                    o = "https://streamcorner.st" if "legacy" in vlabel else rt["origin"]
                    h["Origin"] = o
                    h["Referer"] = o + "/"
                try:
                    r = session(proxy).post(
                        f"{t}{rt.get('api_path', '/corner')}?"
                        f"{rt.get('p_param', 'p')}=admin",
                        data=body, headers=h, timeout=15)
                    ok = bool(r.content[:1] == b"\x04")
                    log(f"   {vlabel:32} HTTP {r.status_code}  {len(r.content):>6}B"
                        f"  {'✅ framing' if ok else ''}")
                except Exception as e:
                    log(f"   {vlabel:32} {type(e).__name__}: {str(e)[:44]}")

    log("\nLettura:")
    log("  qualche variante con framing → problema header")
    log("  403 su tutte                → problema IP: --proxy italiano")


# ============================================================
# CATALOGO
# ============================================================
def key_of(row):
    for f in KEY_FIELDS:
        v = row.get(f)
        if v:
            return str(v).strip()
    return ""


def fetch_events(client, providers, max_events=400):
    jobs, seen = [], set()
    for p in providers:
        res = client.call(p)
        rows = res if isinstance(res, list) else (res or {}).get("channels") or []
        log(f"   {p}: {len(rows)} voci a catalogo")
        for r in rows:
            if not isinstance(r, dict):
                continue
            k = key_of(r)
            if not k or (p, k) in seen:
                continue
            seen.add((p, k))
            jobs.append((p, k, r))
    if not jobs:
        return []
    jobs = jobs[:max_events]
    log(f"   da arricchire: {len(jobs)}")

    out, done = [], 0

    def one(job):
        p, k, _ = job
        d = client.call(p, {"id": k})
        if isinstance(d, list) and d:
            d = d[0]
        return d if isinstance(d, dict) else None

    with ThreadPoolExecutor(max_workers=8) as ex:
        for f in as_completed([ex.submit(one, j) for j in jobs]):
            done += 1
            try:
                d = f.result()
            except Exception:
                d = None
            if d:
                out.append(d)
                for kk in d.keys():
                    CENSUS["detail"][kk] += 1
                for s in d.get("streams") or []:
                    if isinstance(s, dict):
                        for kk in s.keys():
                            CENSUS["stream"][kk] += 1
            if done % 25 == 0:
                log(f"   · {done}/{len(jobs)} ({len(out)} ok)")
    log(f"   ✅ {len(out)} eventi arricchiti")
    return out


def is_live(ev, now=None):
    if now is None:
        now = int(time.time())
    st = ev.get("start_time") or ev.get("timestamp") or 0
    et = ev.get("end_time") or 0
    if not st:
        return True
    if not et:
        et = st + 4 * 3600
    return (st - LIVE_MARGIN_BEFORE) <= now <= (et + LIVE_MARGIN_AFTER)


def direct_sources(ev):
    out = []
    for s in ev.get("streams") or []:
        if not isinstance(s, dict):
            continue
        u = str(s.get("stream_url") or "").strip()
        CENSUS["sources"] += 1
        if u:
            CENSUS["with_stream_url"] += 1
        if not u or not DIRECT_RE.search(u.split("?")[0]):
            continue
        kk = str(s.get("stream_keys") or "").strip()
        if kk and not KEY_PAIR_RE.match(kk):
            kk = ""
        out.append({"url": u, "kidkey": kk,
                    "name": s.get("source_name") or "diretta"})
    return out


def extract_pssh(text):
    if not text:
        return None
    for m in re.findall(r"[A-Za-z0-9+/]{40,}={0,2}", text):
        try:
            d = base64.b64decode(m + "=" * ((4 - len(m) % 4) % 4))
            if len(d) >= 12 and d[4:8] == b"pssh":
                return m
        except Exception:
            continue
    return None


# ============================================================
# RESOLVE EMBED
# ============================================================
LICENSE_HINTS = ("license", "widevine", "getlicense", "acquirelicense", "drm")
MANIFEST_HINTS = (".m3u8", ".mpd", "manifest", "playlist")
CF_HINTS = ("challenges.cloudflare.com", "cf-chl", "/cdn-cgi/challenge", "turnstile")


def resolve_embed(embed_url, origin, proxy=None, timeout=45, tag=""):
    try:
        from patchright.sync_api import sync_playwright
        engine = "patchright"
    except ImportError:
        from playwright.sync_api import sync_playwright
        engine = "playwright"

    rec = {"url": None, "m3u8": None, "mpd": None, "license_url": None,
           "license_method": None, "license_headers": None, "license_body_len": 0,
           "pssh": None, "kid": None, "drm": None, "key_line": None,
           "cloudflare": False, "final_url": None, "title": None,
           "requests": [], "engine": engine, "referer": origin + "/"}

    def on_request(req):
        u = req.url; lu = u.lower()
        if any(k in lu for k in CF_HINTS):
            rec["cloudflare"] = True
        if any(k in lu for k in MANIFEST_HINTS + LICENSE_HINTS):
            if len(rec["requests"]) < MAX_REQUESTS_CAPTURED:
                rec["requests"].append({"m": req.method, "u": u[:220]})
            if req.method == "POST" and any(k in lu for k in LICENSE_HINTS):
                rec["license_url"] = u
                rec["license_method"] = "POST"
                try:
                    rec["license_headers"] = dict(req.headers)
                except Exception:
                    pass
                try:
                    b = req.post_data_buffer()
                    rec["license_body_len"] = len(b) if b else 0
                except Exception:
                    pass

    def on_response(resp):
        u = resp.url; lu = u.lower()
        ct = (resp.headers.get("content-type") or "").lower()
        if ".m3u8" in lu or "mpegurl" in ct:
            try:
                txt = resp.text()
            except Exception:
                return
            if "#EXTM3U" in txt and rec["m3u8"] is None:
                rec["m3u8"] = u
                for line in txt.splitlines():
                    if not line.startswith("#EXT-X-KEY"):
                        continue
                    rec["key_line"] = line[:300]
                    m = re.search(r"KEYID=0x([0-9a-fA-F]+)", line)
                    if m:
                        rec["kid"] = m.group(1)
                    m = re.search(r'KEYFORMAT="([^"]+)"', line)
                    if m:
                        rec["drm"] = m.group(1)
                    m = re.search(r'URI="data:[^,]*,(.+)"', line)
                    if m:
                        rec["pssh"] = m.group(1)
        elif ".mpd" in lu or "dash+xml" in ct:
            try:
                txt = resp.text()
            except Exception:
                return
            if "<MPD" in txt and rec["mpd"] is None:
                rec["mpd"] = u
                p = extract_pssh(txt)
                if p:
                    rec["pssh"] = p
        if rec["pssh"] is None and ("json" in ct or "octet" in ct or "text" in ct):
            try:
                p = extract_pssh(resp.text()[:20000])
            except Exception:
                p = None
            if p:
                rec["pssh"] = p

    def run_once(page):
        try:
            page.goto(embed_url, wait_until="domcontentloaded", timeout=timeout * 1000)
        except Exception as e:
            log(f"      ⚠️ goto {tag}: {str(e)[:70]}")
        try:
            rec["final_url"] = page.url
            rec["title"] = page.title()
        except Exception:
            pass
        page.wait_for_timeout(8000)
        for sel in ("button[aria-label*='play' i]", ".vjs-big-play-button",
                    ".jw-icon-playback", ".bitmovinplayer-ui",
                    ".clappr-play-button", "video"):
            try:
                if page.locator(sel).count() > 0:
                    page.click(sel, timeout=2000)
                    page.wait_for_timeout(2500)
                    break
            except Exception:
                continue
        try:
            page.evaluate("() => { const v=document.querySelector('video');"
                          " if (v){ v.muted=true; v.play().catch(()=>{}); } }")
        except Exception:
            pass
        page.wait_for_timeout(9000)

    try:
        with sync_playwright() as p:
            kw = {"headless": True,
                  "args": ["--no-sandbox", "--disable-dev-shm-usage",
                           "--use-gl=swiftshader", "--enable-unsafe-swiftshader",
                           "--autoplay-policy=no-user-gesture-required",
                           "--mute-audio"]}
            if proxy:
                kw["proxy"] = {"server": proxy}
            browser = p.chromium.launch(**kw)
            ctx = browser.new_context(
                user_agent=UA_IPHONE, viewport={"width": 1920, "height": 1080},
                locale="it-IT", timezone_id="Europe/Rome",
                extra_http_headers={"Referer": origin + "/"},
                ignore_https_errors=True)
            if engine == "playwright":
                ctx.add_init_script("""
                    Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
                    Object.defineProperty(navigator,'languages',{get:()=>['it-IT','it','en-US','en']});
                    window.chrome={runtime:{}};
                """)
            ctx.on("request", on_request)
            ctx.on("response", on_response)
            page = ctx.new_page()
            run_once(page)
            if rec["cloudflare"] and not (rec["m3u8"] or rec["mpd"]):
                for attempt in range(2):
                    log(f"      🛡️ challenge, retry {attempt + 1}/2 {tag}")
                    try:
                        page.reload(wait_until="domcontentloaded",
                                    timeout=timeout * 1000)
                        page.wait_for_timeout(8000 + attempt * 4000)
                    except Exception:
                        pass
                    if rec["m3u8"] or rec["mpd"]:
                        break
            browser.close()
    except Exception as e:
        log(f"      ❌ {tag}: {str(e)[:100]}")

    rec["url"] = rec["m3u8"] or rec["mpd"]
    return rec


# ============================================================
# M3U
# ============================================================
def _kodi_safe(v):
    return str(v).replace("|", " ")


def entry_lines(rec, label, group, logo=""):
    lo = f' tvg-logo="{logo}"' if logo else ""
    out = [f'#EXTINF:-1{lo} group-title="{group}",{label}']
    if rec.get("referer"):
        out.append(f"#EXTVLCOPT:http-referrer={rec['referer']}")
    out.append(f"#EXTVLCOPT:http-user-agent={UA_IPHONE}")
    out.append("#KODIPROP:inputstream=inputstream.adaptive")
    out.append("#KODIPROP:inputstream.adaptive.manifest_type=hls")
    if rec.get("license_url"):
        out.append("#KODIPROP:inputstream.adaptive.license_type=com.widevine.alpha")
        drop = {"host", "content-length", "content-type", "accept-encoding",
                "connection", ":authority", "accept"}
        hs = "&".join(f"{k}={_kodi_safe(v)}"
                      for k, v in (rec.get("license_headers") or {}).items()
                      if k.lower() not in drop)
        out.append("#KODIPROP:inputstream.adaptive.license_key="
                   f"{rec['license_url']}|{hs}|R{{SSM}}|")
    elif rec.get("kidkey"):
        out.append("#KODIPROP:inputstream.adaptive.license_type=org.w3.clearkey")
        out.append(f"#KODIPROP:inputstream.adaptive.license_key={rec['kidkey']}")
    out.append(rec["url"])
    return out


def write_m3u(path, rows):
    seen, n = set(), 0
    with open(path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for label, logo, grp, rec in rows:
            if not rec.get("url") or rec["url"] in seen:
                continue
            seen.add(rec["url"])
            f.write("\n".join(entry_lines(rec, label, grp, logo)) + "\n")
            n += 1
    return n


# ============================================================
# MAIN
# ============================================================
def main():
    ap = argparse.ArgumentParser(description="StreamCorner / CornerStream Extractor v9")
    ap.add_argument("--output-dir", "--output", default=".", dest="output_dir",
                    help="directory di output, oppure file .m3u combinato")
    ap.add_argument("--providers", default=PROVIDERS_DEFAULT)
    ap.add_argument("--site", default=None, help="forza un dominio")
    ap.add_argument("--refresh-vault", action="store_true")
    ap.add_argument("--proxy", default=os.environ.get("SC_PROXY"))
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--deep", action="store_true",
                    help="scan: non fermarti al primo COMPLETO")
    ap.add_argument("--diagnose", action="store_true")
    ap.add_argument("--no-origin", action="store_true")
    ap.add_argument("--no-embeds", action="store_true")
    ap.add_argument("--no-direct", action="store_true")
    ap.add_argument("--all-events", action="store_true")
    ap.add_argument("--max-events", type=int, default=400)
    ap.add_argument("--embed-workers", "--playwright-workers", type=int, default=4,
                    dest="embed_workers")
    ap.add_argument("--embed-limit", "--playwright-limit", type=int, default=0,
                    dest="embed_limit")
    args = ap.parse_args()

    combined_path = None
    if args.output_dir.lower().endswith((".m3u", ".m3u8")):
        combined_path = args.output_dir
        args.output_dir = os.path.dirname(combined_path) or "."

    if args.scan:
        return cmd_scan(args)
    if args.diagnose:
        return cmd_diagnose(args)

    os.makedirs(args.output_dir, exist_ok=True)
    drm_path = os.path.join(args.output_dir, DRM_REPORT)
    if os.path.exists(drm_path):
        os.remove(drm_path)

    log("=" * 66)
    log("📡 STREAMCORNER / CORNERSTREAM EXTRACTOR v9")
    log("=" * 66)

    try:
        from patchright.sync_api import sync_playwright  # noqa
        log("   ✅ patchright attivo")
    except ImportError:
        log("   ⚠️ patchright assente — fallback playwright")

    log("\n[1/6] Discovery")
    try:
        rt, src = get_runtime(args.refresh_vault, args.proxy, args.site)
    except Exception as e:
        log(f"❌ discovery fallita: {e}")
        log("   → prova: python sc9.py --scan")
        return
    log(f"   site={rt['site']}  mode={rt.get('mode')}  "
        f"targets={len(rt['targets'])}  api={rt.get('api_path')}  ({src})")

    log("\n[2/6] Client")
    try:
        client = SC(rt, args.proxy, use_origin=not args.no_origin)
    except RuntimeError as e:
        log(f"❌ {e}")
        log("   → esegui --refresh-vault, il vault potrebbe essere cambiato.")
        return

    log("\n[3/6] Catalogo + dettagli")
    events = fetch_events(client,
                          [p.strip() for p in args.providers.split(",") if p.strip()],
                          args.max_events)
    if not events:
        log("❌ nessun evento. Prova: --scan oppure --diagnose")
        for fn in ("streamcorner_VLC.m3u", "streamcorner_KODI.m3u",
                   "streamcorner_EVENTS_LIVE.m3u"):
            open(os.path.join(args.output_dir, fn), "w").write("#EXTM3U\n")
        if combined_path:
            open(combined_path, "w").write("#EXTM3U\n")
        return

    log("\n[4/6] Filtro live")
    if args.all_events:
        live = events
    else:
        live = [e for e in events if is_live(e)]
        log(f"   🔴 {len(live)}/{len(events)} in onda adesso")

    log("\n[5/6] Sorgenti")
    cache = {}
    if os.path.exists(M3U8_CACHE):
        try:
            cache = json.load(open(M3U8_CACHE, encoding="utf-8"))
        except Exception:
            cache = {}

    rows, embed_jobs = [], []
    for ev in live:
        CENSUS["events"] += 1
        name = ev.get("event_name") or "?"
        league = ev.get("league") or ""
        logo = ev.get("poster") or ev.get("hero_thumbnail") or ""
        base = f"{name} ({league})" if league else name

        if not args.no_direct:
            for s in direct_sources(ev):
                rows.append((f"{base} — {s['name']} [DIRETTA]", logo, name,
                             {"url": s["url"], "kidkey": s["kidkey"],
                              "referer": rt["origin"] + "/"}))

        if args.no_embeds:
            continue
        for s in ev.get("streams") or []:
            if not isinstance(s, dict):
                continue
            emb = s.get("embed_url")
            if not emb:
                continue
            sname = s.get("source_name") or "?"
            if cache.get(emb):
                rows.append((f"{base} — {sname}", logo, name,
                             {"url": cache[emb], "referer": rt["origin"] + "/"}))
            else:
                embed_jobs.append((emb, f"{base} — {sname}", logo, name))

    log(f"   dirette: {len(rows)}   embed da risolvere: {len(embed_jobs)}")
    if args.embed_limit:
        embed_jobs = embed_jobs[:args.embed_limit]

    report = []
    if embed_jobs:
        done = 0

        def job(item):
            emb, label, logo, grp = item
            return emb, label, logo, grp, resolve_embed(emb, rt["origin"], args.proxy,
                                                        tag=label[:28])

        with ThreadPoolExecutor(max_workers=args.embed_workers) as ex:
            for f in as_completed([ex.submit(job, i) for i in embed_jobs]):
                done += 1
                try:
                    emb, label, logo, grp, r = f.result()
                except Exception as e:
                    log(f"   ❌ {str(e)[:90]}"); continue
                r["embed"] = emb; r["label"] = label
                report.append(r)
                if r.get("url"):
                    cache[emb] = r["url"]
                    rows.append((label, logo, grp, r))
                    extra = " [WIDEVINE]" if r.get("license_url") else (
                        " [DRM]" if r.get("kid") else "")
                    log(f"   🎯 [{done}/{len(embed_jobs)}] {label[:45]}{extra}")
                elif done % 5 == 0:
                    log(f"   · [{done}/{len(embed_jobs)}]")

        try:
            json.dump(cache, open(M3U8_CACHE, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
        except Exception:
            pass

    if report:
        with open(drm_path, "w", encoding="utf-8") as f:
            for r in report:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    log("\n[6/6] Output")
    n = write_m3u(os.path.join(args.output_dir, "streamcorner_EVENTS_LIVE.m3u"), rows)
    log(f"   💾 EVENTS_LIVE: {n}")
    n = write_m3u(os.path.join(args.output_dir, "streamcorner_VLC.m3u"),
                  [(l, lg, "StreamCorner", r) for l, lg, _, r in rows])
    log(f"   💾 VLC: {n}")
    n = write_m3u(os.path.join(args.output_dir, "streamcorner_KODI.m3u"), rows)
    log(f"   💾 KODI: {n}")
    if combined_path:
        n = write_m3u(combined_path, rows)
        log(f"   💾 COMBINED ({combined_path}): {n}")
    json.dump(live, open(os.path.join(args.output_dir, "streamcorner_events.json"),
                         "w", encoding="utf-8"), indent=2,
              ensure_ascii=False, default=str)

    log("\n" + "=" * 66)
    log(f"CENSUS  eventi={CENSUS['events']}  sorgenti={CENSUS['sources']}  "
        f"con stream_url={CENSUS['with_stream_url']}")
    if CENSUS["detail"]:
        log("campi dettagli: " + ", ".join(
            f"{k}({v})" for k, v in CENSUS["detail"].most_common(12)))
    if CENSUS["stream"]:
        log("campi stream:   " + ", ".join(
            f"{k}({v})" for k, v in CENSUS["stream"].most_common(12)))
    if CENSUS["sources"] and CENSUS["with_stream_url"] == 0:
        log("→ stream_url assente: usa --no-direct")
    drm = sum(1 for r in report if r.get("drm") or r.get("license_url"))
    cf = sum(1 for r in report if r.get("cloudflare"))
    log(f"✅ stream={len(rows)}  report={len(report)} ({drm} DRM, {cf} CF)")
    log("=" * 66)


if __name__ == "__main__":
    main()