#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StreamCorner / CornerStream Extractor — v9.1
=============================================
Adattato al nuovo cornerstream.tech (SPA React con code splitting).

Novità v9.1
-----------
  * Discovery via Playwright: naviga la home, aspetta il bundle, intercetta
    le richieste di rete per trovare il pool workers.dev e il vault.
  * Fallback: se il network non rivela il vault, tenta il crawl statico
    del bundle principale (per retrocompatibilità con vecchi deploy).
  * Priorità candidati: cornerstream.tech in cima.
  * Rilevamento pagine AGCOM (blocco ISP italiano) e skip immediato.
  * fetch_html con Playwright per bypassare Cloudflare.
  * Tutto il resto (Salsa20, client, resolve embed, M3U) invariato.

Requisiti:
    pip install requests pycryptodome
    pip install patchright && patchright install chromium

Uso:
    python sc9_1.py --diagnose
    python sc9_1.py
    python sc9_1.py --proxy http://user:pass@ip-it:porta
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

# Priorità: cornerstream.tech vivo, streamcorner.st bloccato AGCOM (fallback)
PRIORITY_SITES = [
    "https://cornerstream.tech/",
    "https://streamcorner.st/",
    "https://cornerstream.st/",
]

# TLD parcheggiati: rimossi foo, cloud, fun, pro, online, site, top, xyz, app, live, tv, pw, me
TLDS_VALID = ["tech", "st", "is", "to", "ws", "cc", "su"]

PROVIDERS_DEFAULT = "admin"
KEY_FIELDS = ("stream_id", "game_id", "channel_id", "id")
DIRECT_RE = re.compile(r"\.(?:m3u8|mpd)(?:$|\?)", re.I)
KEY_PAIR_RE = re.compile(r"^[0-9a-f]{32}:[0-9a-f]{32}$", re.I)
HOST_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*\.workers\.dev$")
B64_RE = re.compile(r"^[A-Za-z0-9_\-+/=]+$")

SCRIPT_SRC_RE = re.compile(r'<script\b[^>]*\bsrc=["\']([^"\']+\.js)["\']', re.I)
SIBLING_RE = re.compile(r"""["']\./([A-Za-z0-9_\-]+\.js)["']""")
ASSET_RE = re.compile(r"""["'`](?:\./|/)?(assets/[A-Za-z0-9_\-.]+\.js)["'`]""")
PATH_RE = re.compile(r"""["'`](/[A-Za-z0-9_\-][A-Za-z0-9_\-/]{1,40})["'`]""")
PARAM_RE = re.compile(r"""searchParams\.set\(\s*["']([A-Za-z_]\w{0,7})["']""")
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

SPEC_TRANSCRIPT, SPEC_ENDPOINT, SPEC_NAMESPACE = 0, 1, 3
CONST_TRANSCRIPT, CONST_NONCE, CONST_BRAND = 16, 20, 25

CENSUS = {"detail": Counter(), "stream": Counter(),
          "events": 0, "sources": 0, "with_stream_url": 0}


def log(*a):
    print(*a, flush=True)


# ============================================================
# HTTP (requests, per asset statici post-Playwright)
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


def get_text(url, referer=None, proxy=None, timeout=20, ua=UA_IPHONE):
    h = {"User-Agent": ua}
    if referer:
        h["Referer"] = referer
    r = session(proxy).get(url, headers=h, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r.text


def is_agcom_page(html):
    """Rileva la pagina di blocco AGCOM (server Apache italiano)."""
    if not html or len(html) > 5000:
        return False
    low = html.lower()
    return ("agcom" in low or "avviso" in low) and "autorità" in low


# ============================================================
# SALSA20/12 (invariato)
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
# VAULT (invariato)
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


def find_api(code):
    params = PARAM_RE.findall(code)
    if len(params) < 2:
        return [], params
    ctx_paths = [p for p in FETCH_CTX.findall(code)
                 if "." not in p
                 and not p.startswith(("/assets", "/@", "/src", "/static",
                                        "/extra", "/_routes", "/cdn-cgi"))
                 and len(p) > 3]
    return list(dict.fromkeys(ctx_paths)), params


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
# DISCOVERY — Playwright (per SPA + Cloudflare)
# ============================================================
def playwright_discover(site, proxy=None, timeout=45):
    """
    Apre la home con Playwright, aspetta che la SPA carichi i chunk,
    intercetta le richieste di rete per trovare workers.dev e il vault.
    Ritorna dict runtime o None.
    """
    try:
        from patchright.sync_api import sync_playwright
        engine = "patchright"
    except ImportError:
        from playwright.sync_api import sync_playwright
        engine = "playwright"

    log(f"   🌐 Playwright discover: {site}")
    result = {"workers": [], "vault": None, "assets": [],
              "api_path": "/corner", "p_param": "p", "id_param": "id"}

    # Buffer per intercettare le risposte JS
    js_bodies = []
    worker_hosts = set()

    def on_response(resp):
        u = resp.url
        ct = (resp.headers.get("content-type") or "").lower()
        if ".js" in u or "javascript" in ct:
            try:
                body = resp.text()
            except Exception:
                return
            if body and len(body) > 500:
                js_bodies.append((u, body))

    def on_request(req):
        u = req.url
        m = re.search(r"https?://([a-z0-9][a-z0-9\-]*\.workers\.dev)", u)
        if m:
            worker_hosts.add(m.group(1))

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
                user_agent=UA_IPHONE,
                viewport={"width": 1920, "height": 1080},
                locale="it-IT", timezone_id="Europe/Rome",
                ignore_https_errors=True,
            )
            ctx.on("response", on_response)
            ctx.on("request", on_request)

            page = ctx.new_page()
            try:
                page.goto(site, wait_until="domcontentloaded", timeout=timeout * 1000)
            except Exception as e:
                log(f"      ⚠️ goto: {str(e)[:60]}")

            # Attesa SPA: aspetta che carichi i chunk
            for _ in range(8):
                page.wait_for_timeout(2000)
                if worker_hosts and js_bodies:
                    break

            browser.close()
    except Exception as e:
        log(f"      ❌ Playwright: {str(e)[:100]}")
        return None

    log(f"      JS catturati: {len(js_bodies)}  worker visti: {len(worker_hosts)}")

    # Ora cerca vault nei JS catturati
    for url, code in js_bodies:
        if not result["workers"]:
            w = find_workers(code)
            if w:
                result["workers"] = w
                log(f"      ✓ workers pool in {url.rsplit('/', 1)[-1]}: {len(w)} host")
        if result["vault"] is None:
            v = try_vault(code)
            if v:
                result["vault"] = v
                log(f"      ✓ vault in {url.rsplit('/', 1)[-1]}")
                result["assets"].append(url)
        p2, pr2 = find_api(code)
        if p2 and result["api_path"] == "/corner":
            cand = [p for p in p2 if "corner" in p.lower()] or p2
            if cand:
                result["api_path"] = cand[0]
        if pr2 and result["p_param"] == "p":
            if len(pr2) >= 2:
                result["p_param"] = pr2[0]
                result["id_param"] = pr2[1]

    # Aggiungi worker visti via network (non solo dal codice)
    if worker_hosts and not result["workers"]:
        result["workers"] = list(worker_hosts)

    if not result["vault"] and not result["workers"]:
        return None

    return result


def get_runtime(refresh=False, proxy=None, force_site=None):
    if not refresh and not force_site and os.path.exists(RUNTIME_CACHE):
        try:
            rt = json.load(open(RUNTIME_CACHE, encoding="utf-8"))
            age = int(time.time()) - rt.get("discovered_at", 0)
            if rt.get("vault") and rt.get("targets") and age < MAX_RUNTIME_AGE:
                return rt, f"cache ({age // 3600}h)"
        except Exception:
            pass

    sites = [force_site] if force_site else PRIORITY_SITES
    for site in sites:
        # Prima prova requests (fast path)
        try:
            html = get_text(site, proxy=proxy, timeout=15)
            if is_agcom_page(html):
                log(f"   {site:34} BLOCCATO AGCOM — skip")
                continue
            # Se HTML è grande, il sito risponde senza challenge
            if len(html) > 10000:
                log(f"   {site:34} HTML {len(html)} char (no Playwright)")
                # Prova crawl statico veloce
                host = urlparse(site).netloc
                srcs = list(dict.fromkeys(
                    urljoin(site, x) for x in SCRIPT_SRC_RE.findall(html)
                    if urlparse(urljoin(site, x)).netloc == host))
                if srcs:
                    w, v = [], None
                    for u in srcs[:MAX_ASSETS_SCAN]:
                        try:
                            code = get_text(u, referer=site, proxy=proxy, timeout=15)
                        except Exception:
                            continue
                        if not w:
                            w = find_workers(code)
                        if v is None:
                            v = try_vault(code)
                        if w and v:
                            break
                    if w or v:
                        log(f"      → statico: worker={len(w)} vault={'SI' if v else 'NO'}")
                        if v:
                            rt = {"site": site,
                                  "origin": "{0.scheme}://{0.netloc}".format(urlparse(site)),
                                  "workers": w, "vault": v, "assets": srcs[:40],
                                  "api_path": "/corner", "p_param": "p", "id_param": "id",
                                  "mode": "worker" if w else "origin",
                                  "targets": [f"https://{x}" for x in w] if w else
                                             ["{0.scheme}://{0.netloc}".format(urlparse(site))],
                                  "discovered_at": int(time.time())}
                            json.dump(rt, open(RUNTIME_CACHE, "w", encoding="utf-8"),
                                      ensure_ascii=False)
                            return rt, "live-static"
        except Exception:
            pass

        # Fallback: Playwright (SPA + Cloudflare)
        rt = playwright_discover(site, proxy)
        if rt and (rt["vault"] or rt["workers"]):
            rt["site"] = site
            rt["origin"] = "{0.scheme}://{0.netloc}".format(urlparse(site))
            rt["mode"] = "worker" if rt["workers"] else "origin"
            rt["targets"] = ([f"https://{w}" for w in rt["workers"]]
                             if rt["workers"]
                             else [rt["origin"]])
            rt["discovered_at"] = int(time.time())
            json.dump(rt, open(RUNTIME_CACHE, "w", encoding="utf-8"),
                      ensure_ascii=False)
            return rt, "live-playwright"

    # Fallback su cache
    if os.path.exists(RUNTIME_CACHE):
        log("   ⚠️ discovery fallita — uso cache")
        return json.load(open(RUNTIME_CACHE, encoding="utf-8")), "fallback"
    raise RuntimeError("discovery fallita su tutti i candidati")


# ============================================================
# CLIENT (invariato dal v9)
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
        self.rk = hashlib.sha256(self.t + self.e + self.tr).digest()
        self.targets = rt.get("targets") or [rt["origin"]]
        random.shuffle(self.targets)
        self.origin = rt["origin"]
        self.api_path = rt.get("api_path") or "/corner"
        self.p_param = rt.get("p_param") or "p"
        self.id_param = rt.get("id_param") or "id"
        self.proxy = proxy
        self.use_origin = use_origin
        self.bad = {}
        self.lock = threading.Lock()

    def headers(self):
        h = {"User-Agent": UA_IPHONE, "Content-Type": "text/plain",
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
        out, err = self._attempt(p, extra, timeout)
        if out is None and err:
            log(f"   ⚠️ p={p}{' ' + str(extra) if extra else ''} → {err}")
        return out


# ============================================================
# DIAGNOSTICA
# ============================================================
def cmd_diagnose(args):
    log("=" * 66)
    log("DIAGNOSTICA v9.1 — discovery + POST worker")
    log("=" * 66)

    for site in PRIORITY_SITES:
        log(f"\n🔍 {site}")
        try:
            html = get_text(site, proxy=args.proxy, timeout=15)
            if is_agcom_page(html):
                log(f"   ❌ BLOCCATO AGCOM")
                continue
            log(f"   HTTP 200, {len(html)} char")
            if len(html) > 10000:
                log(f"   ✅ HTML reale (SPA o statico)")
                host = urlparse(site).netloc
                srcs = list(dict.fromkeys(
                    urljoin(site, x) for x in SCRIPT_SRC_RE.findall(html)
                    if urlparse(urljoin(site, x)).netloc == host))
                log(f"   script same-origin: {len(srcs)}")
                for s in srcs[:3]:
                    log(f"      {s[:90]}")
            else:
                log(f"   ⚠️ HTML piccolo (challenge Cloudflare?)")
        except Exception as e:
            log(f"   ❌ {type(e).__name__}: {str(e)[:80]}")

    log("\n" + "=" * 66)
    log("Ora prova con Playwright...")
    log("=" * 66)
    for site in PRIORITY_SITES[:1]:
        rt = playwright_discover(site, args.proxy)
        if rt:
            log(f"   workers: {len(rt['workers'])}")
            log(f"   vault:   {'SI' if rt['vault'] else 'NO'}")
            for w in rt["workers"][:5]:
                log(f"      {w}")
        else:
            log("   ❌ nessun risultato")


# ============================================================
# CATALOGO (invariato)
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
        out.append({"url": u, "name": s.get("source_name") or "diretta"})
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
# RESOLVE EMBED (invariato)
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
                """)
            ctx.on("request", on_request)
            ctx.on("response", on_response)
            page = ctx.new_page()
            run_once(page)
            if rec["cloudflare"] and not (rec["m3u8"] or rec["mpd"]):
                for attempt in range(2):
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
    ap = argparse.ArgumentParser(description="StreamCorner Extractor v9.1")
    ap.add_argument("--output-dir", "--output", default=".", dest="output_dir")
    ap.add_argument("--providers", default=PROVIDERS_DEFAULT)
    ap.add_argument("--site", default=None)
    ap.add_argument("--refresh-vault", action="store_true")
    ap.add_argument("--proxy", default=os.environ.get("SC_PROXY"))
    ap.add_argument("--diagnose", action="store_true")
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

    if args.diagnose:
        return cmd_diagnose(args)

    os.makedirs(args.output_dir, exist_ok=True)
    drm_path = os.path.join(args.output_dir, DRM_REPORT)
    if os.path.exists(drm_path):
        os.remove(drm_path)

    log("=" * 66)
    log("📡 STREAMCORNER / CORNERSTREAM EXTRACTOR v9.1")
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
        log("   → prova: python sc9_1.py --diagnose")
        return
    log(f"   site={rt['site']}  mode={rt.get('mode')}  "
        f"targets={len(rt['targets'])}  api={rt.get('api_path')}  ({src})")

    log("\n[2/6] Client")
    try:
        client = SC(rt, args.proxy)
    except RuntimeError as e:
        log(f"❌ {e}")
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
                             {"url": s["url"], "referer": rt["origin"] + "/"}))

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
    drm = sum(1 for r in report if r.get("drm") or r.get("license_url"))
    cf = sum(1 for r in report if r.get("cloudflare"))
    log(f"✅ stream={len(rows)}  report={len(report)} ({drm} DRM, {cf} CF)")
    log("=" * 66)


if __name__ == "__main__":
    main()