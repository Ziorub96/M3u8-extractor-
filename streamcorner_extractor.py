#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StreamCorner / CornerStream Extractor — v10.4
==============================================
Basato su v10.3.

Novità v10.4:
  FIX A  Separazione formato (HLS/DASH) da protezione (confirmed/absent/unknown)
  FIX B  Logging diagnostico di OGNI esito (non solo ogni 5)
  FIX C  final_url e title valorizzati
  FIX D  page.on("pageerror") e page.on("requestfailed")
  FIX E  rec["error"] valorizzato su ogni fallimento
  FIX F  Timeout Playwright aumentati (30s totali vs 17s)
  FIX G  UA configurabile: --ua desktop|mobile (default desktop)
  FIX H  entry_lines: manifest_type dinamico; .mpd non-DRM incluso
  FIX I  Cache opzionalmente verificata (--verify-cache)
"""

from __future__ import annotations
import argparse, base64, hashlib, json, os, re, struct, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# CONFIG
# ============================================================
UA_IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile Safari/604.1")

UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")

SITE = "https://cornerstream.tech/"
STATIC_CACHE = ".sc_static_cache.json"
CACHE_TTL = 7 * 24 * 3600

PROVIDER_DEFAULT = "admin"
LIVE_MARGIN_BEFORE = 1800
LIVE_MARGIN_AFTER = 3600

M3U8_CACHE = ".streamcorner_events_m3u8_cache.json"
CAPTURE_FILE = "sc_capture.json"
DRM_REPORT = "sc_drm_report.jsonl"

DIRECT_RE = re.compile(r"\.(?:m3u8|mpd)(?:$|\?)", re.I)

# FIX A: separazione formato / protezione
DRM_SCHEMES = ("edef8ba9", "9a04f079", "1077efecc0b24d02",
               "urn:uuid:edef8ba9", "urn:uuid:9a04f079",
               "urn:uuid:1077efecc0b24d02")
SKIP_HOSTS = {"pandecocogaming.sbs"}


def log(*a):
    print(*a, flush=True)


# ============================================================
# HTTP CLIENT (curl_cffi → requests)
# ============================================================
def _make_session():
    try:
        from curl_cffi import requests as cf
        log("   ✅ curl_cffi attivo (impersonate=chrome)")
        return "curl_cffi", cf.Session(impersonate="chrome")
    except ImportError:
        log("   ⚠️  curl_cffi assente → requests standard")
        import requests
        s = requests.Session()
        s.headers.update({"User-Agent": UA_IPHONE,
                          "Accept-Language": "it-IT,it;q=0.9,en;q=0.8"})
        return "requests", s


_ENGINE, _SESSION = _make_session()


def http_get(url, timeout=20, referer=None, extra_headers=None):
    h = {"User-Agent": UA_IPHONE, "Accept": "*/*",
         "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
         "Accept-Encoding": "gzip, deflate"}
    if referer:
        h["Referer"] = referer
    if extra_headers:
        h.update(extra_headers)
    try:
        r = _SESSION.get(url, timeout=timeout, headers=h, verify=False)
        return r.status_code, r.content, dict(r.headers)
    except Exception as e:
        return None, str(e).encode(), {}


def http_post_bin(url, body, timeout=20,
                  referer="https://cornerstream.tech/",
                  origin="https://cornerstream.tech"):
    h = {
        "User-Agent": UA_IPHONE,
        "Accept": "application/octet-stream",
        "Content-Type": "text/plain",
        "Origin": origin,
        "Referer": referer,
    }
    try:
        r = _SESSION.post(url, data=body, timeout=timeout,
                          headers=h, verify=False)
        return r.status_code, r.content, dict(r.headers)
    except Exception as e:
        return None, str(e).encode(), {}


# ============================================================
# CACHE STATICA
# ============================================================
def cache_load():
    if not os.path.exists(STATIC_CACHE):
        return None
    try:
        with open(STATIC_CACHE, encoding="utf-8") as f:
            c = json.load(f)
        c["_age"] = time.time() - c.get("ts", 0)
        return c
    except Exception:
        return None


def cache_save(main_js_url, provider_js_url, workers, provider_content_b64):
    data = {
        "ts": int(time.time()),
        "main_js_url": main_js_url,
        "provider_js_url": provider_js_url,
        "workers": workers,
        "provider_js_b64": provider_content_b64,
    }
    with open(STATIC_CACHE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ============================================================
# VAULT DECODER (invariato)
# ============================================================
MAGIC32 = 0xFFFFFFFF


def rotl8(v, c):
    c &= 7
    return ((v << c) | (v >> (8 - c))) & 0xFF if c else v & 0xFF


def rotr8(v, c):
    c &= 7
    return ((v >> c) | (v << (8 - c))) & 0xFF if c else v & 0xFF


def rotl32(v, c):
    return ((v << c) & MAGIC32) | (v >> (32 - c))


SBOX_VAULT = [3, 235, 241, 97, 237, 84, 185, 89, 53, 108, 201, 89, 159, 92, 225, 67]
MAGIC_VAULT = (-1237694027) & 0xFFFFFFFF


def _vault_xor_pass(data, key):
    out = bytearray(len(data))
    c = 87
    m = 190 ^ key[4]
    for a in range(len(data)):
        g = (key[15 & a] ^ SBOX_VAULT[15 & a] ^ m ^ (a & 0xFF)
             ^ ((MAGIC_VAULT >> (8 * (3 & a))) & 0xFF) ^ c) & 0xFF
        w = data[a] ^ g
        out[a] = w
        m = data[a]
        c = rotl8((c ^ data[a] ^ key[(a + 7) & 15]) & 0xFF, 1)
    return bytes(out)


def decode_vault(pt_b64_list, dt_b64_list):
    def b64d(s):
        s = s.strip().replace("-", "+").replace("_", "/")
        s += "=" * ((4 - len(s) % 4) % 4)
        return base64.b64decode(s)
    pt_bytes = b64d("".join(pt_b64_list))
    dt_bytes = b64d("".join(dt_b64_list))
    return json.loads(_vault_xor_pass(pt_bytes, dt_bytes).decode("utf-8"))


def spec_decode(spec):
    seed = int(spec[0]); xor1 = int(spec[1])
    b_arr = list(spec[2]); sbox = list(spec[3])
    b2_arr = list(spec[4]); idx = list(spec[5])
    out = bytearray(len(b_arr))
    n = seed
    for o in range(len(b_arr)):
        u = idx[o]
        inner = rotl8(b_arr[o], 7 & (n ^ xor1))
        stuff = ((sbox[u] + (n ^ ((13 * o) & 0xFF))) & 0xFF) ^ ((xor1 + 17 * o) & 0xFF)
        t = rotr8(inner ^ stuff, 7 & (b2_arr[o] ^ n))
        out[o] = t
        n = rotl8(n, 1) ^ ((t + b2_arr[o] + o) & 0xFF)
    return bytes(out)


def const_bytes(c):
    if isinstance(c, dict):
        t, v = c.get("t"), c.get("v")
        if t == "u8": return bytes(v)
        if t == "u32": return b"".join(struct.pack("<I", x) for x in v)
        if t == "s": return v.encode()
        if t == "n": return bytes([v])
        if t == "a": return b"".join(x.encode() for x in v)
        if t == "bi": return int(v).to_bytes(32, "big")
        if isinstance(v, list): return bytes(v)
        if isinstance(v, int): return bytes([v])
        if isinstance(v, str): return v.encode()
    if isinstance(c, list): return bytes(c)
    if isinstance(c, int): return bytes([c])
    if isinstance(c, str): return c.encode()
    return b""


# ============================================================
# SHA-256 + SALSA20/12 (invariato)
# ============================================================
def sha256(*chunks):
    h = hashlib.sha256()
    for c in chunks:
        if isinstance(c, str): c = c.encode()
        h.update(c)
    return h.digest()


def _salsa_qr(s, a, b, c, d):
    s[b] ^= rotl32((s[a] + s[d]) & MAGIC32, 7)
    s[c] ^= rotl32((s[b] + s[a]) & MAGIC32, 9)
    s[d] ^= rotl32((s[c] + s[b]) & MAGIC32, 13)
    s[a] ^= rotl32((s[d] + s[c]) & MAGIC32, 18)


def _salsa_block(key32, nonce8, ctr):
    k = list(struct.unpack("<8I", key32))
    n0, n1 = struct.unpack("<2I", nonce8)
    st = [0x61707865, k[0], k[1], k[2], k[3], 0x3320646e, n0, n1,
          ctr & MAGIC32, (ctr >> 32) & MAGIC32,
          0x79622d32, k[4], k[5], k[6], k[7], 0x6b206574]
    x = st[:]
    for _ in range(6):
        _salsa_qr(x, 0, 4, 8, 12); _salsa_qr(x, 5, 9, 13, 1)
        _salsa_qr(x, 10, 14, 2, 6); _salsa_qr(x, 15, 3, 7, 11)
        _salsa_qr(x, 0, 1, 2, 3); _salsa_qr(x, 5, 6, 7, 4)
        _salsa_qr(x, 10, 11, 8, 9); _salsa_qr(x, 15, 12, 13, 14)
    return struct.pack("<16I", *[(x[i] + st[i]) & MAGIC32 for i in range(16)])


def salsa20_12(key, nonce8, data):
    out = bytearray(); i = 0; ctr = 0
    while i < len(data):
        ks = _salsa_block(key, nonce8, ctr)
        for j in range(64):
            if i + j >= len(data): break
            out.append(data[i + j] ^ ks[j])
        i += 64; ctr += 1
    return bytes(out)


# ============================================================
# DISCOVERY (invariato)
# ============================================================
def _parse_provider(txt):
    m = re.search(r'(/assets/provider-data\.worker-[A-Za-z0-9_\-]+\.js)', txt)
    if not m:
        m = re.search(r'(provider-data\.worker-[A-Za-z0-9_\-]+\.js)', txt)
        if m:
            return "/assets/" + m.group(1)
        return None
    return m.group(1)


def _parse_workers(txt):
    return sorted(set(re.findall(r'data\.[a-zA-Z0-9\-]+\.workers\.dev', txt)))


def _extract_dt_pt(provider_txt):
    m = re.search(r'\bdt\s*=\s*\[([^\]]+)\]', provider_txt)
    if not m:
        raise RuntimeError("dt non trovato")
    dt_strs = re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))
    m = re.search(r'\bpt\s*=\s*\[(.*?)\]\s*,', provider_txt, re.DOTALL)
    if not m:
        raise RuntimeError("pt non trovato")
    pt_strs = re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))
    return dt_strs, pt_strs


def discover_all(site=SITE, force_refresh=False):
    cache = cache_load()
    need_refresh = (cache is None) or (cache.get("_age", 1e9) > CACHE_TTL) or force_refresh

    if need_refresh:
        age_str = "assente" if cache is None else f"{int(cache.get('_age',0))}s"
        log(f"→ tentativo refresh discovery (cache {age_str})")
        try:
            st, body, _ = http_get(site, timeout=15)
            if st != 200:
                raise RuntimeError(f"home non 200: {st}")
            html = body.decode("utf-8", "replace")
            m = re.search(r'<script[^>]+src="(/assets/[^"]+\.js)"', html)
            if not m:
                raise RuntimeError("bundle main non trovato in home")
            main_url = m.group(1)
            log(f"   bundle main: {main_url}")

            st, body, _ = http_get(site.rstrip("/") + main_url, timeout=30)
            if st != 200:
                raise RuntimeError(f"main bundle non 200: {st}")
            main_txt = body.decode("utf-8", "replace")

            provider_url = _parse_provider(main_txt)
            workers = _parse_workers(main_txt)

            if not provider_url or not workers:
                log(f"   → provider/workers non nel main, esploro i chunk lazy")
                chunks = re.findall(r'"(assets/[A-Za-z0-9_\-]+\.js)"', main_txt)
                seen = set()
                chunks_unique = []
                for c in chunks:
                    if c not in seen:
                        seen.add(c)
                        chunks_unique.append(c)
                log(f"   → {len(chunks_unique)} chunk da esplorare")

                for i, chunk in enumerate(chunks_unique, 1):
                    try:
                        st, cbody, _ = http_get(site.rstrip("/") + "/" + chunk, timeout=20)
                        if st != 200:
                            continue
                        ctxt = cbody.decode("utf-8", "replace")
                        if not provider_url:
                            provider_url = _parse_provider(ctxt)
                        if not workers:
                            workers = _parse_workers(ctxt)
                        if provider_url and workers:
                            log(f"   ✅ trovati in {chunk} (iter {i}/{len(chunks_unique)})")
                            break
                    except Exception as e:
                        log(f"   ⚠️ chunk {chunk}: {str(e)[:60]}")

            if not provider_url or not workers:
                raise RuntimeError(
                    f"parse fallito: provider={provider_url} workers={len(workers) if workers else 0}"
                )
            log(f"   provider worker: {provider_url}")
            log(f"   workers trovati: {len(workers)}")

            st, body, _ = http_get(site.rstrip("/") + provider_url, timeout=30)
            if st != 200:
                raise RuntimeError(f"provider worker non 200: {st}")
            provider_txt = body.decode("utf-8", "replace")

            dt_strs, pt_strs = _extract_dt_pt(provider_txt)
            vault = decode_vault(pt_strs, dt_strs)

            cache_save(main_url, provider_url, workers,
                       base64.b64encode(body).decode("ascii"))
            log(f"   ✅ cache aggiornata ({STATIC_CACHE})")
            return vault, workers, {"source": "http", "main": main_url,
                                     "provider": provider_url}

        except Exception as e:
            log(f"   ⚠️ refresh fallito: {e}")
            if cache is None:
                raise RuntimeError(f"refresh fallito e cache assente: {e}")
            log(f"   → uso cache ({int(cache['_age'])}s fa)")

    log(f"→ uso cache statica (età: {int(cache['_age'])}s)")
    provider_b64 = cache.get("provider_js_b64", "")
    if not provider_b64:
        raise RuntimeError("cache senza provider_js_b64")
    provider_txt = base64.b64decode(provider_b64).decode("utf-8", "replace")
    dt_strs, pt_strs = _extract_dt_pt(provider_txt)
    vault = decode_vault(pt_strs, dt_strs)
    workers = cache.get("workers", [])
    return vault, workers, {"source": "cache", "main": cache.get("main_js_url"),
                             "provider": cache.get("provider_js_url")}


# ============================================================
# NINJACORNER CLIENT (invariato)
# ============================================================
class NinjaClient:
    def __init__(self, vault, workers):
        specs = vault["specs"]; consts = vault["consts"]
        self.d_H = spec_decode(specs[0])
        self.d_et = spec_decode(specs[1])
        self.d_ot = spec_decode(specs[2])
        self.d_Lt = spec_decode(specs[3])
        self.d_St = spec_decode(specs[4])
        self.rt = const_bytes(consts[16])
        self.nt = const_bytes(consts[20])
        self.Ct = const_bytes(consts[22])
        self.Mt = const_bytes(consts[25])
        self.invio_key = sha256(self.d_H, self.d_et, self.rt)
        self.workers = workers
        self.worker = None

    def _build_body(self, provider):
        ts = int(time.time()) // 300
        lt_str = self.d_Lt.decode("utf-8", errors="replace")
        plaintext = json.dumps([lt_str, provider, ts]).encode("utf-8")
        nonce = os.urandom(8)
        cipher = salsa20_12(self.invio_key, nonce, plaintext)
        tag = sha256(self.invio_key, nonce, cipher, self.nt)[:12]
        return b"\x0a" + nonce + tag + cipher, nonce, ts

    def _build_resp_key(self, nonce_invio, provider, ts):
        ts_be = struct.pack(">I", ts)
        return sha256(self.d_H, self.d_et, self.d_Lt,
                      nonce_invio, provider.encode(), ts_be, self.Mt)

    def _decrypt_resp(self, resp, nonce_invio, provider, ts):
        if len(resp) < 21 or resp[0] != 0x04:
            return None, "bad_magic"
        rn, rt, ct = resp[1:9], resp[9:21], resp[21:]
        key = self._build_resp_key(nonce_invio, provider, ts)
        if sha256(key, rn, ct, self.Ct)[:12] != rt:
            return None, "tag_mismatch"
        return salsa20_12(key, rn, ct), None

    def _post_corner(self, provider, extra_id=None, timeout=20):
        body, nonce_invio, ts = self._build_body(provider)
        qs = f"p={provider}"
        if extra_id:
            qs += f"&id={extra_id}"
        candidates = ([self.worker] if self.worker else []) + \
                     [w for w in self.workers if w != self.worker]
        last = None
        for w in candidates:
            url = f"https://{w}/corner?{qs}"
            st, resp, _ = http_post_bin(url, body, timeout=timeout)
            if st == 200 and len(resp) > 21 and resp[0] == 0x04:
                self.worker = w
                plain, err = self._decrypt_resp(resp, nonce_invio, provider, ts)
                if err:
                    last = f"{w}: {err}"; continue
                return plain, w
            last = f"{w}: st={st}"
        raise RuntimeError(f"tutti i worker falliti: {last}")

    def get_catalog(self, provider=PROVIDER_DEFAULT):
        raw, w = self._post_corner(provider)
        return json.loads(raw.decode("utf-8"))

    def get_event_detail(self, event_id, provider=PROVIDER_DEFAULT):
        raw, w = self._post_corner(provider, extra_id=event_id)
        return json.loads(raw.decode("utf-8"))


# ============================================================
# EMBED RESOLVER (Playwright) — v10.4 con FIX C/D/E/F/G
# ============================================================
LICENSE_HINTS = ("license", "widevine", "getlicense", "acquirelicense", "drm")
MANIFEST_HINTS = (".m3u8", ".mpd", "manifest", "playlist")
CF_HINTS = ("challenges.cloudflare.com", "cf-chl", "/cdn-cgi/challenge", "turnstile")


def resolve_embed(embed_url, origin, proxy=None, timeout=45, tag="", ua=None):
    """
    Ritorna un dict con:
      url          : manifest trovato (o None)
      m3u8, mpd    : manifest per formato
      license_url  : URL di licenza osservato (indizio DRM)
      cloudflare   : challenge rilevato
      final_url    : URL finale del browser (FIX C)
      title        : titolo pagina (FIX C)
      requests     : indizi di rete
      engine       : engine usato
      error        : motivo del fallimento (FIX E)
      referer      : referer originale
      skipped      : True se scartato a priori
    """
    if has_explicit_drm_hint(embed_url) or host_of(embed_url) in SKIP_HOSTS:
        return {"url": None, "m3u8": None, "mpd": None, "license_url": None,
                "cloudflare": False, "final_url": None, "title": None,
                "requests": [], "engine": "skipped",
                "referer": origin + "/", "skipped": "drm_or_host",
                "error": "skipped_before_resolve"}

    try:
        from patchright.sync_api import sync_playwright
        engine = "patchright"
    except ImportError:
        from playwright.sync_api import sync_playwright
        engine = "playwright"

    user_agent = ua or UA_DESKTOP  # FIX G

    rec = {"url": None, "m3u8": None, "mpd": None, "license_url": None,
           "cloudflare": False, "final_url": None, "title": None,
           "requests": [], "engine": engine, "referer": origin + "/",
           "error": None}

    def on_request(req):
        u = req.url; lu = u.lower()
        if any(k in lu for k in CF_HINTS):
            rec["cloudflare"] = True
        if any(k in lu for k in MANIFEST_HINTS + LICENSE_HINTS):
            if len(rec["requests"]) < 60:
                rec["requests"].append({"m": req.method, "u": u[:200]})
            if req.method == "POST" and any(k in lu for k in LICENSE_HINTS):
                rec["license_url"] = u

    def on_response(resp):
        u = resp.url; lu = u.lower()
        ct = (resp.headers.get("content-type") or "").lower()
        if ".m3u8" in lu or "mpegurl" in ct:
            try:
                t = resp.text()
            except Exception:
                return
            if "#EXTM3U" in t and rec["m3u8"] is None:
                rec["m3u8"] = u
        elif ".mpd" in lu or "dash+xml" in ct:
            try:
                t = resp.text()
            except Exception:
                return
            if "<MPD" in t and rec["mpd"] is None:
                rec["mpd"] = u

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
                user_agent=user_agent, viewport={"width": 1280, "height": 800},
                locale="it-IT", timezone_id="Europe/Rome",
                extra_http_headers={"Referer": origin + "/"},
                ignore_https_errors=True)
            ctx.on("request", on_request)
            ctx.on("response", on_response)
            page = ctx.new_page()

            # FIX D: diagnosi JS e richieste fallite
            page.on("pageerror", lambda exc: log(
                f"      JS ERROR [{tag}]: {str(exc)[:180]}"))
            page.on("requestfailed", lambda req: log(
                f"      REQ FAILED [{tag}]: {req.url[:140]} | {req.failure}"))

            try:
                page.goto(embed_url, wait_until="domcontentloaded",
                          timeout=timeout * 1000)
            except Exception as exc:
                rec["error"] = f"goto: {type(exc).__name__}: {str(exc)[:120]}"

            # FIX F: timeout aumentati
            page.wait_for_timeout(15000)

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

            page.wait_for_timeout(15000)

            # FIX C: valorizza final_url e title prima di chiudere
            try:
                rec["final_url"] = page.url
                rec["title"] = page.title()
                log(f"      PAGE [{tag}]: {rec['final_url'][:180]}")
                log(f"      TITLE [{tag}]: {rec['title'][:120]}")
            except Exception as exc:
                log(f"      PAGE INFO ERROR [{tag}]: {str(exc)[:120]}")

            browser.close()
    except Exception as e:
        rec["error"] = f"playwright: {type(e).__name__}: {str(e)[:150]}"
        log(f"      ❌ {tag}: {rec['error']}")

    rec["url"] = rec["m3u8"] or rec["mpd"]
    if not rec["url"] and not rec.get("error"):
        rec["error"] = "manifest_not_observed"
    return rec


# ============================================================
# FIX A: helper formato/protezione
# ============================================================
def manifest_type(u: str) -> str | None:
    """Ritorna 'hls' | 'dash' | None. NON deduce DRM."""
    path = urlparse(u or "").path.lower()
    if path.endswith(".m3u8"):
        return "hls"
    if path.endswith(".mpd"):
        return "dash"
    return None


def has_explicit_drm_hint(u: str) -> bool:
    """Indizio DRM nell'URL (schemeIdUri, ecc.). NON prova assenza di DRM."""
    v = (u or "").lower()
    return any(s in v for s in DRM_SCHEMES)


def host_of(u: str) -> str:
    return (urlparse(u).hostname or "").lower()


def drm_status(rec: dict) -> str:
    """Ritorna 'confirmed' | 'absent' | 'unknown'."""
    if not rec.get("url"):
        return "unknown"
    if rec.get("license_url"):
        return "confirmed"
    if any("license" in (r.get("u") or "").lower() for r in rec.get("requests", [])):
        return "confirmed"
    if has_explicit_drm_hint(rec["url"]):
        return "confirmed"
    mt = manifest_type(rec["url"])
    if mt == "hls":
        return "absent"
    if mt == "dash":
        # DASH può essere protetto o no; senza info sul manifest, unknown.
        return "unknown"
    return "unknown"


def is_unplayable(rec: dict) -> bool:
    """Sostituisce should_skip(url). Decide sulla base di rec completo."""
    if rec.get("skipped"):
        return True
    if not rec.get("url"):
        return True
    if host_of(rec["url"]) in SKIP_HOSTS:
        return True
    if drm_status(rec) == "confirmed":
        return True
    return False


# ============================================================
# M3U — FIX H (manifest_type dinamico, .mpd non-DRM incluso)
# ============================================================
def entry_lines(rec, label, group, logo=""):
    lo = f' tvg-logo="{logo}"' if logo else ""
    out = [f'#EXTINF:-1{lo} group-title="{group}",{label}']
    if rec.get("referer"):
        out.append(f"#EXTVLCOPT:http-referrer={rec['referer']}")
    out.append(f"#EXTVLCOPT:http-user-agent={UA_DESKTOP}")
    out.append("#KODIPROP:inputstream=inputstream.adaptive")
    mt = manifest_type(rec.get("url"))
    if mt:
        out.append(f"#KODIPROP:inputstream.adaptive.manifest_type={mt}")
    out.append(rec["url"])
    return out


def write_m3u(path, rows):
    seen, n, skipped = set(), 0, 0
    with open(path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for label, logo, grp, rec in rows:
            if not rec.get("url") or rec["url"] in seen:
                continue
            if is_unplayable(rec):
                skipped += 1
                continue
            seen.add(rec["url"])
            f.write("\n".join(entry_lines(rec, label, grp, logo)) + "\n")
            n += 1
    return n, skipped


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


# ============================================================
# FIX I: cache opzionale con verifica
# ============================================================
def verify_cached_url(u: str) -> bool:
    if not u:
        return False
    try:
        st, body, hdr = http_get(u, timeout=10, referer=SITE)
        if st != 200 or not body:
            return False
        ct = (hdr.get("content-type") or "").lower()
        return ("mpegurl" in ct or "dash+xml" in ct
                or body[:7] == b"#EXTM3U" or b"<MPD" in body[:2048])
    except Exception:
        return False


# ============================================================
# MAIN
# ============================================================
def main():
    ap = argparse.ArgumentParser(description="StreamCorner Extractor v10.4")
    ap.add_argument("--output", "-o", default="streamcorner_COMPLETE.m3u")
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--proxy", default=os.environ.get("SC_PROXY"))
    ap.add_argument("--provider", default=PROVIDER_DEFAULT)
    ap.add_argument("--all-events", action="store_true")
    ap.add_argument("--no-embeds", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--verify-cache", action="store_true",
                    help="Verifica HTTP i cached URL prima di riusarli")
    ap.add_argument("--embed-workers", "--playwright-workers",
                    type=int, default=4, dest="embed_workers")
    ap.add_argument("--embed-limit", "--playwright-limit",
                    type=int, default=0, dest="embed_limit")
    ap.add_argument("--ua", choices=["desktop", "mobile"], default="desktop",
                    help="User-Agent Playwright (FIX G, default desktop)")
    args, _unknown = ap.parse_known_args()

    if args.output_dir is None:
        args.output_dir = os.path.dirname(args.output) or "."
    os.makedirs(args.output_dir, exist_ok=True)

    ua = UA_DESKTOP if args.ua == "desktop" else UA_IPHONE

    log("=" * 66)
    log(f"📡 STREAMCORNER EXTRACTOR v10.4 (UA={args.ua})")
    log("=" * 66)

    # ---- 1. Discovery ----
    log("\n[1/5] Discovery")
    try:
        vault, workers, meta = discover_all(force_refresh=args.refresh)
        log(f"   vault: {len(vault.get('specs', []))} specs, "
            f"{len(vault.get('consts', []))} consts")
        log(f"   workers: {len(workers)}  (fonte: {meta['source']})")
    except Exception as e:
        log(f"   ❌ discovery fallita: {e}")
        for fn in ("streamcorner_EVENTS_LIVE.m3u", "streamcorner_VLC.m3u",
                   "streamcorner_KODI.m3u"):
            open(os.path.join(args.output_dir, fn), "w").write("#EXTM3U\n")
        open(args.output, "w").write("#EXTM3U\n")
        return

    # ---- 2. Client ----
    log("\n[2/5] Client NinjaCorner")
    client = NinjaClient(vault, workers)
    log(f"   invio_key: {client.invio_key.hex()[:32]}...")

    try:
        catalog = client.get_catalog(args.provider)
        log(f"   ✅ catalogo via {client.worker}: {len(catalog)} item")
    except Exception as e:
        log(f"   ❌ get_catalog: {e}")
        for fn in ("streamcorner_EVENTS_LIVE.m3u", "streamcorner_VLC.m3u",
                   "streamcorner_KODI.m3u"):
            open(os.path.join(args.output_dir, fn), "w").write("#EXTM3U\n")
        open(args.output, "w").write("#EXTM3U\n")
        return

    # ---- 3. Dettagli ----
    log(f"\n[3/5] Dettagli per {len(catalog)} eventi")
    events_full = []
    for i, ev in enumerate(catalog, 1):
        sid = ev.get("stream_id")
        name = ev.get("event_name", "?")
        if not sid:
            continue
        try:
            detail = client.get_event_detail(sid, args.provider)
            streams = detail.get("streams") or []
            if streams:
                ev["streams"] = streams
            events_full.append(ev)
            log(f"   [{i}/{len(catalog)}] {name[:45]:45}  streams={len(streams)}")
        except Exception as e:
            log(f"   [{i}/{len(catalog)}] {name[:45]:45}  ❌ {str(e)[:50]}")
            events_full.append(ev)

    # ---- 4. Filtro live ----
    log(f"\n[4/5] Filtro live")
    if args.all_events:
        live = events_full
    else:
        live = [e for e in events_full if is_live(e)]
        log(f"   🔴 {len(live)}/{len(events_full)} in onda adesso")

    # ---- 5. Sorgenti ----
    log(f"\n[5/5] Sorgenti")
    cache = {}
    if os.path.exists(M3U8_CACHE):
        try:
            cache = json.load(open(M3U8_CACHE, encoding="utf-8"))
        except Exception:
            cache = {}

    rows, embed_jobs = [], []
    skipped_pre = 0

    for ev in live:
        name = ev.get("event_name") or ev.get("title") or "?"
        league = ev.get("league") or ""
        logo = ev.get("poster") or ev.get("hero_thumbnail") or ""
        base = f"{name} ({league})" if league else name

        for s in ev.get("streams") or []:
            if not isinstance(s, dict):
                continue
            u = str(s.get("stream_url") or "").strip()
            if u and DIRECT_RE.search(u.split("?")[0]):
                if host_of(u) in SKIP_HOSTS or has_explicit_drm_hint(u):
                    skipped_pre += 1
                    continue
                rec = {"url": u, "referer": SITE, "requests": [],
                       "license_url": None, "skipped": None}
                rows.append((f"{base} — {s.get('source_name') or '?'} [DIRETTA]",
                             logo, name, rec))
                continue
            emb = s.get("embed_url")
            if not emb or args.no_embeds:
                continue
            sname = s.get("source_name") or "?"
            cached = cache.get(emb)
            if cached and not (args.verify_cache and not verify_cached_url(cached)):
                rec = {"url": cached, "referer": SITE, "requests": [],
                       "license_url": None, "skipped": None}
                rows.append((f"{base} — {sname}", logo, name, rec))
            else:
                embed_jobs.append((emb, f"{base} — {sname}", logo, name))

    if skipped_pre:
        log(f"   🚫 {skipped_pre} stream DRM/host-skip scartati prima del resolve")
    log(f"   dirette: {len(rows)}   embed da risolvere: {len(embed_jobs)}")
    if args.embed_limit:
        embed_jobs = embed_jobs[:args.embed_limit]

    report = []
    if embed_jobs:
        done = 0

        def job(item):
            emb, label, logo, grp = item
            return emb, label, logo, grp, resolve_embed(
                emb, SITE, args.proxy, tag=label[:28], ua=ua)

        with ThreadPoolExecutor(max_workers=args.embed_workers) as ex:
            for f in as_completed([ex.submit(job, i) for i in embed_jobs]):
                done += 1
                try:
                    emb, label, logo, grp, r = f.result()
                except Exception as e:
                    log(f"   ❌ [{done}/{len(embed_jobs)}] {str(e)[:90]}")
                    continue
                r["embed"] = emb; r["label"] = label
                report.append(r)

                # FIX B + E: log di OGNI esito con causa
                if r.get("url"):
                    cache[emb] = r["url"]
                    rows.append((label, logo, grp, r))
                    st = drm_status(r)
                    mt = manifest_type(r["url"]) or "?"
                    log(f"   🎯 [{done}/{len(embed_jobs)}] {label[:45]} "
                        f"| {mt} | drm={st}")
                else:
                    reason = r.get("error") or "manifest_not_observed"
                    if r.get("cloudflare"):
                        reason = "cloudflare_challenge"
                    elif r.get("requests"):
                        reason = f"network({len(r['requests'])})_no_manifest"
                    log(f"   ⚠️ [{done}/{len(embed_jobs)}] {label[:45]} — {reason}")

        try:
            json.dump(cache, open(M3U8_CACHE, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
        except Exception:
            pass

    if report:
        drm_path = os.path.join(args.output_dir, DRM_REPORT)
        with open(drm_path, "w", encoding="utf-8") as f:
            for r in report:
                # FIX E: includi status sintetico
                r2 = dict(r)
                r2["drm_status"] = drm_status(r)
                r2["manifest_type"] = manifest_type(r.get("url"))
                f.write(json.dumps(r2, ensure_ascii=False) + "\n")

    # ---- Output ----
    n, sk = write_m3u(os.path.join(args.output_dir, "streamcorner_EVENTS_LIVE.m3u"), rows)
    log(f"   💾 EVENTS_LIVE: {n} (skipped: {sk})")
    n, sk = write_m3u(os.path.join(args.output_dir, "streamcorner_VLC.m3u"),
                      [(l, lg, "StreamCorner", r) for l, lg, _, r in rows])
    log(f"   💾 VLC: {n} (skipped: {sk})")
    n, sk = write_m3u(os.path.join(args.output_dir, "streamcorner_KODI.m3u"), rows)
    log(f"   💾 KODI: {n} (skipped: {sk})")
    n, sk = write_m3u(args.output, rows)
    log(f"   💾 {args.output}: {n} (skipped: {sk})")

    # ---- Summary ----
    try:
        summary = {
            "site": SITE,
            "provider": args.provider,
            "discovery_source": meta.get("source"),
            "main_js_url": meta.get("main"),
            "provider_js_url": meta.get("provider"),
            "workers_count": len(workers),
            "winner_worker": client.worker,
            "catalog_size": len(catalog),
            "events_full": len(events_full),
            "events_live": len(live),
            "streams_skipped_pre_resolve": skipped_pre,
            "embed_jobs": len(embed_jobs),
            "embed_resolved": sum(1 for r in report if r.get("url")),
            "embed_failed": sum(1 for r in report if not r.get("url")),
            "failures_by_reason": _summarize(report),
            "ua_used": args.ua,
            "verify_cache": args.verify_cache,
            "extracted_at": int(time.time()),
        }
        with open(os.path.join(args.output_dir, CAPTURE_FILE), "w",
                  encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        log(f"   💾 {CAPTURE_FILE} salvato")
    except Exception as e:
        log(f"   ⚠️ capture: {e}")

    log("\n" + "=" * 66)
    log(f"✅ eventi={len(live)}  resolved={summary['embed_resolved']}  "
        f"failed={summary['embed_failed']}")
    log("=" * 66)


def _summarize(report):
    out = {}
    for r in report:
        if r.get("url"):
            continue
        key = r.get("error") or "manifest_not_observed"
        if r.get("cloudflare"):
            key = "cloudflare_challenge"
        elif r.get("requests"):
            key = f"network_no_manifest"
        out[key] = out.get(key, 0) + 1
    return out


if __name__ == "__main__":
    main()