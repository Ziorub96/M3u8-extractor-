#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StreamCorner / CornerStream Extractor — v7.2
============================================
Discovery strutturale: nessun nome di variabile minificato assunto.
  - worker pool -> array di stringhe che matchano HOST_RE (*.workers.dev)
  - vault        -> bruteforce di coppie (blob 16-64B, blob >=128B) finche'
                    esce un JSON con specs+consts **e** le specs si decodificano
                    davvero.

Novita' v7.2
------------
  * CANDIDATE_SITES aggiornati: i vecchi cornerstream.* e streamcorner.tech
    sono morti (NXDOMAIN / HTTPError). Il servizio ora vive su:
        streamcorner.st   (canonico)
        streamcorner.foo  (mirror)
        streamcorner.cloud (mirror)
        streamcorner.fun  (mirror)
  * Log discovery piu' informativo: HTTP <code> invece di "HTTPError".
  * Cache runtime invalidata automaticamente se `site` non e' piu' tra i
    CANDIDATE_SITES (evita di usare un runtime salvato da domini morti).

Requisiti:
    pip install requests pycryptodome
    pip install patchright && patchright install chromium
"""

from __future__ import annotations
import argparse, base64, hashlib, json, os, random, re, struct, sys, threading, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# CONFIG
# ============================================================
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile Safari/604.1")
UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Domini vivi al 2026-10-16. I vecchi cornerstream.* e streamcorner.tech
# sono morti (NXDOMAIN o HTTPError). streamninja.* sono elencati come mirror
# dal sito ma al momento non rispondono: NON metterli qui finche' non tornano.
CANDIDATE_SITES = [
    "https://streamcorner.st/",
    "https://streamcorner.foo/",
    "https://streamcorner.cloud/",
    "https://streamcorner.fun/",
]

PROVIDERS_DEFAULT = "admin"

KEY_FIELDS = ("stream_id", "game_id", "channel_id", "id")

# Indici "magic" dentro specs/consts del vault (reverse-engineered).
SPEC_TRANSCRIPT  = 0
SPEC_ENDPOINT    = 1
SPEC_NAMESPACE   = 3
CONST_TRANSCRIPT = 16
CONST_NONCE      = 20
CONST_BRAND      = 25

DIRECT_RE     = re.compile(r"\.(?:m3u8|mpd)(?:$|\?)", re.I)
KEY_PAIR_RE   = re.compile(r"^[0-9a-f]{32}:[0-9a-f]{32}$", re.I)
HOST_RE       = re.compile(r"^[a-z0-9][a-z0-9\-]*\.workers\.dev$")
B64_RE        = re.compile(r"^[A-Za-z0-9_\-+/=]+$")
SCRIPT_SRC_RE = re.compile(r'<script\b[^>]*\bsrc=["\']([^"\']+\.js)["\']', re.I)
SIBLING_RE    = re.compile(r"""["']\./([A-Za-z0-9_\-]+\.js)["']""")

RUNTIME_CACHE = ".sc_runtime.json"
M3U8_CACHE    = ".sc_m3u8_cache.json"
DRM_REPORT    = "sc_drm_report.jsonl"

LIVE_MARGIN_BEFORE     = 1800
LIVE_MARGIN_AFTER      = 3600
WORKER_TRIES           = 3
MAX_ASSETS             = 26
MAX_REQUESTS_CAPTURED  = 120
MAX_RUNTIME_AGE        = 7 * 86400
BAD_DECAY              = 300

CENSUS = {"detail": Counter(), "stream": Counter(),
          "events": 0, "sources": 0, "with_stream_url": 0}


def log(*a):
    print(*a, flush=True)


# ============================================================
# HTTP  (session per thread)
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


def get_text(url, referer=None, proxy=None, timeout=20, ua=UA):
    h = {"User-Agent": ua}
    if referer:
        h["Referer"] = referer
    r = session(proxy).get(url, headers=h, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r.text


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
# LETTURA ARRAY
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


# ============================================================
# DISCOVERY
# ============================================================
def crawl(start_urls, referer, proxy, max_assets=MAX_ASSETS):
    seen, queue, assets = set(), list(start_urls), []
    workers, vault = [], None
    while queue and len(seen) < max_assets:
        u = queue.pop(0)
        if u in seen:
            continue
        seen.add(u)
        try:
            code = get_text(u, referer=referer, proxy=proxy, timeout=20)
        except Exception:
            continue
        assets.append(u)
        if not workers:
            w = find_workers(code)
            if w:
                workers = w
                log(f"   ✓ pool worker: {len(w)} host (da {u.rsplit('/', 1)[-1]})")
        if vault is None:
            v = try_vault(code)
            if v:
                vault = v
                log(f"   ✓ vault decodificato (da {u.rsplit('/', 1)[-1]}, "
                    f"{len(v['specs'])} specs, {len(v['consts'])} consts)")
        if workers and vault:
            break
        for n in SIBLING_RE.findall(code):
            nu = urljoin(u, "./" + n)
            if nu not in seen:
                queue.append(nu)
    return workers, vault, assets


def _site_error(site, proxy):
    """Classifica l'errore di un sito candidato in una stringa leggibile."""
    try:
        r = session(proxy).get(site, headers={"User-Agent": UA},
                               timeout=20, allow_redirects=True)
        return f"{site} HTTP {r.status_code}"
    except requests.exceptions.ConnectionError:
        return f"{site} conn"
    except requests.exceptions.Timeout:
        return f"{site} timeout"
    except requests.exceptions.SSLError:
        return f"{site} ssl"
    except requests.exceptions.HTTPError as e:
        code = getattr(e.response, "status_code", "?")
        return f"{site} HTTP {code}"
    except Exception as e:
        return f"{site} {type(e).__name__}"


def discover(proxy=None):
    errors = []
    for site in CANDIDATE_SITES:
        try:
            html = get_text(site, proxy=proxy, timeout=20)
        except Exception:
            errors.append(_site_error(site, proxy)); continue
        host = urlparse(site).netloc
        srcs = [urljoin(site, s) for s in SCRIPT_SRC_RE.findall(html)]
        srcs = list(dict.fromkeys(s for s in srcs if urlparse(s).netloc == host))
        if not srcs:
            errors.append(f"{site} nessuno script"); continue

        origin = "{0.scheme}://{0.netloc}".format(urlparse(site))
        log(f"   sito candidato: {site} ({len(srcs)} script)")
        workers, vault, assets = crawl(srcs, site, proxy)
        if not workers:
            errors.append(f"{site} pool worker assente"); continue
        if not vault:
            errors.append(f"{site} vault non decodificato"); continue

        return {"site": site, "origin": origin, "workers": workers, "vault": vault,
                "assets": assets[:40], "discovered_at": int(time.time())}
    raise RuntimeError("discovery fallita → " + " | ".join(errors))


def get_runtime(refresh=False, proxy=None):
    if not refresh and os.path.exists(RUNTIME_CACHE):
        try:
            rt = json.load(open(RUNTIME_CACHE, encoding="utf-8"))
            age = int(time.time()) - rt.get("discovered_at", 0)
            # Rifiuta cache che puntano a siti non piu' in CANDIDATE_SITES
            # (es. runtime salvato da cornerstream.tech, ora morto).
            site_ok = rt.get("site") in CANDIDATE_SITES
            if (rt.get("vault") and rt.get("workers") and rt.get("origin")
                    and site_ok and age < MAX_RUNTIME_AGE):
                return rt, f"cache ({age // 3600}h)"
            if not site_ok:
                log(f"   ⏳ runtime cache punta a sito dismesso ({rt.get('site')}) — refresh")
            elif age >= MAX_RUNTIME_AGE:
                log(f"   ⏳ runtime cache scaduta ({age // 86400}g) — refresh")
        except Exception:
            pass
    try:
        rt = discover(proxy)
        json.dump(rt, open(RUNTIME_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
        return rt, "live"
    except Exception as e:
        if os.path.exists(RUNTIME_CACHE):
            try:
                cached = json.load(open(RUNTIME_CACHE, encoding="utf-8"))
                if cached.get("site") in CANDIDATE_SITES:
                    log(f"   ⚠️ discovery fallita ({str(e)[:140]}) — uso cache")
                    return cached, "fallback"
                log("   ⚠️ discovery fallita e cache stantia — niente da usare")
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
            self.t  = dec_spec(v["specs"][SPEC_TRANSCRIPT])
            self.e  = dec_spec(v["specs"][SPEC_ENDPOINT])
            self.ns = dec_spec(v["specs"][SPEC_NAMESPACE]).decode("utf-8", "replace")
            self.tr = gc(v, CONST_TRANSCRIPT)
            self.nr = gc(v, CONST_NONCE)
            self.Br = gc(v, CONST_BRAND)
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            raise RuntimeError(f"vault incompatibile: {type(e).__name__}: {e}")
        if not (self.t and self.e and self.ns):
            raise RuntimeError("vault: materiale di sessione vuoto dopo il decode")

        self.rk = hashlib.sha256(self.t + self.e + self.tr).digest()
        self.workers = rt["workers"][:]
        random.shuffle(self.workers)
        self.origin = rt["origin"]
        self.proxy = proxy
        self.use_origin = use_origin
        self.bad = {}
        self.lock = threading.Lock()

    def headers(self):
        h = {"User-Agent": UA, "Content-Type": "text/plain",
             "Accept": "application/octet-stream"}
        if self.use_origin:
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
        if time.time() - ts > BAD_DECAY:
            return 0
        return score

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

    def call(self, p, extra=None, timeout=15):
        body, n, u = self.build(p)
        R = hashlib.sha256(self.t + self.e + self.ns.encode() + n + p.encode()
                           + struct.pack(">I", u) + self.Br).digest()
        with self.lock:
            order = sorted(self.workers, key=self._bad_score)
        last = None
        for host in order[:WORKER_TRIES]:
            url = f"https://{host}/corner?p={p}"
            if extra:
                for k, v in extra.items():
                    url += f"&{k}={v}"
            try:
                r = session(self.proxy).post(url, data=body,
                                             headers=self.headers(), timeout=timeout)
            except Exception as e:
                last = f"{host}: {type(e).__name__}"; self._mark(host, 3); continue
            if r.status_code != 200 or not r.content:
                last = f"{host}: HTTP {r.status_code}"; self._mark(host, 1); continue
            tries = [(r.content[1:9], r.content[21:]), (r.content[:8], r.content[20:])]
            for rn, rc in tries:
                if len(rc) < 2:
                    continue
                try:
                    out = json.loads(salsa20_12(R, rn, rc).decode())
                    self._mark(host, 0)
                    return out
                except Exception:
                    continue
            last = f"{host}: decode fallito"; self._mark(host, 2)
        if last:
            log(f"   ⚠️ p={p}{' ' + str(extra) if extra else ''} → {last}")
        return None


# ============================================================
# DIAGNOSTICA
# ============================================================
def probe(url, headers, proxy, method="GET", data=None, timeout=15):
    try:
        kw = {"headers": headers, "timeout": timeout, "allow_redirects": True}
        if data is not None:
            kw["data"] = data
            r = session(proxy).post(url, **kw)
        else:
            r = session(proxy).get(url, **kw)
        return {"status": r.status_code, "len": len(r.content),
                "server": r.headers.get("server", ""),
                "cfray": r.headers.get("cf-ray", ""),
                "cfmit": r.headers.get("cf-mitigated", ""),
                "ct": (r.headers.get("content-type") or "")[:36],
                "framing": bool(r.content[:1] == b"\x04")}
    except Exception as e:
        return {"status": None, "err": f"{type(e).__name__}: {str(e)[:55]}"}


def pline(p):
    if p.get("err"):
        return f"❌ {p['err']}"
    t = ""
    if p.get("cfmit"):
        t = f"   ⛔ cf-mitigated={p['cfmit']}"
    elif p["status"] == 403:
        t = "   ⛔ 403 perimetro"
    return f"HTTP {p['status']}  {p['len']:>7}B  srv={p['server'][:10]}  ct={p['ct']}{t}"


def cmd_diagnose(args):
    log("=" * 64)
    log("DIAGNOSTICA — header vs IP vs servizio giu'")
    log("=" * 64)

    rt = None
    try:
        rt, src = get_runtime(args.refresh_vault, args.proxy)
        log(f"runtime: {src}  site={rt['site']}  worker={len(rt['workers'])}")
    except Exception as e:
        log(f"⚠️ discovery non riuscita: {str(e)[:170]}")

    sites   = [rt["site"]] if rt else CANDIDATE_SITES[:2]
    workers = (rt or {}).get("workers", [])[:3]
    origin  = (rt or {}).get("origin", "https://streamcorner.st")

    body = None
    if rt:
        try:
            body, _, _ = SC(rt, args.proxy, use_origin=False).build("admin")
        except Exception:
            body = None

    variants = [
        ("nessun Origin/Referer", {}),
        ("Origin/Referer reali", {"Origin": origin, "Referer": origin + "/"}),
        ("Origin legacy streamcorner.st", {"Origin": "https://streamcorner.st",
                                           "Referer": "https://streamcorner.st/"}),
        ("Origin reale + UA desktop", {"Origin": origin, "Referer": origin + "/",
                                       "User-Agent": UA_DESKTOP}),
    ]

    res = {}
    for label, proxy in (("DIRETTO", None), ("PROXY", args.proxy)):
        if label == "PROXY" and not args.proxy:
            continue
        log(f"\n{'#' * 20} {label} {'#' * 20}")
        bucket = {"site": [], "wget": [], "post": {}}

        log("\n[1] sito (perimetro principale)")
        for s in sites:
            p = probe(s, {"User-Agent": UA}, proxy)
            bucket["site"].append(p)
            log(f"   {s:40} {pline(p)}")

        if not workers:
            log("\n[2] nessun worker noto — passa --refresh-vault")
        else:
            log("\n[2] worker — GET crudo (perimetro workers.dev)")
            for w in workers:
                p = probe(f"https://{w}/", {"User-Agent": UA}, proxy)
                bucket["wget"].append(p)
                log(f"   {w:40} {pline(p)}")

            log("\n[3] worker — POST /corner?p=admin (varianti header)")
            for w in workers:
                for vlabel, vh in variants:
                    h = {"User-Agent": UA, "Content-Type": "text/plain"}
                    h.update(vh)
                    p = probe(f"https://{w}/corner?p=admin", h, proxy,
                              method="POST", data=body or b"\x0a" + b"\x00" * 32)
                    bucket["post"].setdefault(vlabel, []).append(p)
                    flag = "  ✅ framing valido" if p.get("framing") else ""
                    log(f"   {w[:26]:26} {vlabel:30} {pline(p)}{flag}")
        res[label] = bucket

    log("\n" + "=" * 64)
    log("VERDETTO")
    log("=" * 64)

    d  = res.get("DIRETTO", {})
    pr = res.get("PROXY", {})

    def any_ok(bucket):
        return [v for v, ps in bucket.get("post", {}).items()
                if any(p.get("status") == 200 and p.get("framing") for p in ps)]

    ok_direct = any_ok(d)
    ok_proxy  = any_ok(pr)

    if ok_direct:
        log(f"✅ HEADER: la variante «{ok_direct[0]}» passa.")
    elif ok_proxy:
        log(f"✅ IP: diretto fallisce, via proxy passa («{ok_proxy[0]}»).")
        log("   → gira sempre con --proxy su IP italiano")
    else:
        sites_403 = all(p.get("status") == 403 for p in d.get("site", []))
        w_403     = all(p.get("status") == 403 for p in d.get("wget", []))
        errs      = all(p.get("status") is None for p in d.get("site", []) + d.get("wget", []))
        if errs:
            log("❌ Servizio giu' o discovery da rifare → --refresh-vault")
        elif sites_403 and w_403:
            log("❌ IP: 403 sia sul sito sia sui worker, tutte le varianti.")
            log("   → è l'ASN. Serve un IP residenziale/italiano.")
        else:
            log("⚠️ Misto: nessuna variante passa ma i perimetri non sono tutti 403.")
    log("")


# ============================================================
# CATALOGO / DETTAGLI
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
        p, k, _row = job
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
                log(f"   · {done}/{len(jobs)} arricchiti ({len(out)} ok)")
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
LICENSE_HINTS  = ("license", "widevine", "getlicense", "acquirelicense", "drm")
MANIFEST_HINTS = (".m3u8", ".mpd", "manifest", "playlist")
CF_HINTS       = ("challenges.cloudflare.com", "cf-chl", "/cdn-cgi/challenge", "turnstile")


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
                user_agent=UA, viewport={"width": 1920, "height": 1080},
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
    out.append(f"#EXTVLCOPT:http-user-agent={UA}")
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
    ap = argparse.ArgumentParser(description="StreamCorner / CornerStream Extractor v7.2")
    ap.add_argument("--output-dir", "--output", default=".",
                    dest="output_dir",
                    help="directory di output (o, con --output FILE.m3u, "
                         "il file combinato; la dir diventa la sua parent)")
    ap.add_argument("--providers", default=PROVIDERS_DEFAULT)
    ap.add_argument("--refresh-vault", action="store_true")
    ap.add_argument("--proxy", default=os.environ.get("SC_PROXY"))
    ap.add_argument("--diagnose", action="store_true")
    ap.add_argument("--no-origin", action="store_true")
    ap.add_argument("--no-embeds", action="store_true")
    ap.add_argument("--no-direct", action="store_true")
    ap.add_argument("--all-events", action="store_true")
    ap.add_argument("--max-events", type=int, default=400)
    ap.add_argument("--embed-workers", "--playwright-workers", type=int, default=4,
                    dest="embed_workers",
                    help="worker paralleli per resolve_embed")
    ap.add_argument("--embed-limit", "--playwright-limit", type=int, default=0,
                    dest="embed_limit",
                    help="max embed da risolvere (0 = tutti)")
    args = ap.parse_args()

    # Normalizza --output: se punta a un file .m3u, la dir e' la parent
    combined_path = None
    if args.output_dir.lower().endswith((".m3u", ".m3u8")):
        combined_path = args.output_dir
        args.output_dir = os.path.dirname(combined_path) or "."

    if args.diagnose:
        return cmd_diagnose(args)

    os.makedirs(args.output_dir, exist_ok=True)
    drm_path = os.path.join(args.output_dir, DRM_REPORT)
    if os.path.exists(drm_path):
        os.remove(drm_path)

    log("=" * 64)
    log("📡 STREAMCORNER / CORNERSTREAM EXTRACTOR v7.2")
    log("=" * 64)

    try:
        from patchright.sync_api import sync_playwright  # noqa
        log("   ✅ patchright attivo")
    except ImportError:
        log("   ⚠️ patchright assente — fallback playwright")

    log("\n[1/6] Discovery runtime")
    try:
        rt, src = get_runtime(args.refresh_vault, args.proxy)
    except Exception as e:
        log(f"❌ discovery fallita: {e}")
        return
    log(f"   site={rt['site']}  worker={len(rt['workers'])}  ({src})")

    log("\n[2/6] Client worker")
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
        log("❌ nessun evento. Prova: --diagnose")
        for fn in ("streamcorner_VLC.m3u", "streamcorner_KODI.m3u",
                   "streamcorner_EVENTS_LIVE.m3u"):
            open(os.path.join(args.output_dir, fn), "w").write("#EXTM3U\n")
        if combined_path:
            open(combined_path, "w").write("#EXTM3U\n")
        return

    log("\n[4/6] Filtro live")
    if args.all_events:
        live = events
        log("   (filtro disattivato)")
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
        name   = ev.get("event_name") or "?"
        league = ev.get("league") or ""
        logo   = ev.get("poster") or ev.get("hero_thumbnail") or ""
        base   = f"{name} ({league})" if league else name

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

    log("\n" + "=" * 64)
    log("CENSUS CAMPI (dati, non ipotesi)")
    log(f"  eventi: {CENSUS['events']}   sorgenti viste: {CENSUS['sources']}"
        f"   con stream_url: {CENSUS['with_stream_url']}")
    if CENSUS["detail"]:
        log("  campi nei dettagli: " + ", ".join(
            f"{k}({v})" for k, v in CENSUS["detail"].most_common(14)))
    if CENSUS["stream"]:
        log("  campi negli stream: " + ", ".join(
            f"{k}({v})" for k, v in CENSUS["stream"].most_common(14)))
    if CENSUS["sources"] and CENSUS["with_stream_url"] == 0:
        log("  → stream_url NON compare: il path diretto è zavorra. Usa --no-direct.")
    elif CENSUS["with_stream_url"]:
        log("  → stream_url ESISTE: il path diretto serve, non è codice morto.")

    drm = sum(1 for r in report if r.get("drm") or r.get("license_url"))
    cf  = sum(1 for r in report if r.get("cloudflare"))
    log(f"\n✅ stream scritti: {len(rows)}   report: {len(report)} embed"
        f" ({drm} DRM, {cf} Cloudflare)")
    log("=" * 64)


if __name__ == "__main__":
    main()