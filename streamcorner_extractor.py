#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StreamCorner / CornerStream Extractor — v10.0 (HTTP-puro + NinjaCorner protocol)
================================================================================
Ricostruzione completa del protocollo "NinjaCorner":
  * Discovery dinamica del bundle + provider worker
  * Decodifica vault (SBOX custom)
  * Chiavi di sessione (spec_decode)
  * POST cifrato a *.workers.dev/corner?p=admin[&id=...]
  * Salsa20/12 + SHA-256 (pura stdlib Python)

Niente Playwright per il catalogo. Niente Cloudflare, Turnstile, browser.
Playwright rimane SOLO per risolvere gli embed (CDN terze parti).

Output: streamcorner_EVENTS_LIVE.m3u, streamcorner_VLC.m3u, streamcorner_KODI.m3u
"""

from __future__ import annotations
import argparse, hashlib, json, os, re, struct, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

# ============================================================
# CONFIG
# ============================================================
UA_IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile Safari/604.1")

SITE = "https://cornerstream.tech/"
DOMAIN_PREFIX = re.compile(r'data\.[a-zA-Z0-9\-]+\.workers\.dev')

LIVE_MARGIN_BEFORE = 1800
LIVE_MARGIN_AFTER = 3600

M3U8_CACHE = ".sc_m3u8_cache.json"
CAPTURE_FILE = "sc_capture.json"
DRM_REPORT = "sc_drm_report.jsonl"

PROVIDER_DEFAULT = "admin"

# Embed "diretti": già stream utilizzabile, nessun Playwright
DIRECT_EMBED_HINTS = (
    "topembed.pw", "getsugatensho",
)

# ============================================================
# LOG
# ============================================================
def log(*a):
    print(*a, flush=True)


# ============================================================
# HTTP CLIENT
# ============================================================
_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": UA_IPHONE,
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
})


def http_get(url, timeout=20, referer=None):
    h = {}
    if referer:
        h["Referer"] = referer
    try:
        r = _SESSION.get(url, timeout=timeout, headers=h, verify=False)
        return r.status_code, r.content, dict(r.headers)
    except Exception as e:
        return None, str(e).encode(), {}


def http_post_bin(url, body, timeout=20, referer="https://cornerstream.tech/",
                  origin="https://cornerstream.tech"):
    h = {
        "Accept": "application/octet-stream",
        "Content-Type": "text/plain",
        "Origin": origin,
        "Referer": referer,
    }
    try:
        r = _SESSION.post(url, data=body, timeout=timeout, headers=h, verify=False)
        return r.status_code, r.content, dict(r.headers)
    except Exception as e:
        return None, str(e).encode(), {}


# ============================================================
# VAULT DECODER (SBOX custom)
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
             ^ ((MAGIC_VAULT >> (8 * (3 & a))) & 0xFF)
             ^ c) & 0xFF
        w = data[a] ^ g
        out[a] = w
        m = data[a]
        c = rotl8((c ^ data[a] ^ key[(a + 7) & 15]) & 0xFF, 1)
    return bytes(out)


def decode_vault(pt_b64_list, dt_b64_list):
    """Decodifica il vault JSON (specs + consts + k6)."""
    import base64

    def b64d(s):
        s = s.strip().replace("-", "+").replace("_", "/")
        s += "=" * ((4 - len(s) % 4) % 4)
        return base64.b64decode(s)

    pt_bytes = b64d("".join(pt_b64_list))
    dt_bytes = b64d("".join(dt_b64_list))
    raw = _vault_xor_pass(pt_bytes, dt_bytes)
    return json.loads(raw.decode("utf-8"))


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
# SHA-256 + SALSA20/12 (pure stdlib)
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
    for _ in range(6):  # 12 round
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
# DISCOVERY
# ============================================================
def discover_bundle(site=SITE):
    """Trova il bundle main JS dalla home."""
    log(f"→ discovery bundle su {site}")
    st, body, _ = http_get(site, timeout=15)
    if st != 200:
        raise RuntimeError(f"home non 200: {st}")
    html = body.decode("utf-8", "replace")
    m = re.search(r'<script[^>]+src="(/assets/[^"]+\.js)"', html)
    if not m:
        raise RuntimeError("bundle main non trovato in home")
    return m.group(1)


def discover_provider_worker(main_bundle_url, site=SITE):
    """Trova il worker provider dal bundle main."""
    log(f"→ discovery provider worker da {main_bundle_url}")
    full = site.rstrip("/") + main_bundle_url
    st, body, _ = http_get(full, timeout=30)
    if st != 200:
        raise RuntimeError(f"bundle main non 200: {st}")
    txt = body.decode("utf-8", "replace")
    m = re.search(r'(/assets/provider-data\.worker-[A-Za-z0-9_\-]+\.js)', txt)
    if not m:
        raise RuntimeError("provider worker non trovato nel bundle")
    return m.group(1)


def discover_workers(main_bundle_url, site=SITE):
    """Estrai la lista di worker *.workers.dev dal bundle main."""
    log(f"→ discovery worker pool da bundle")
    full = site.rstrip("/") + main_bundle_url
    st, body, _ = http_get(full, timeout=30)
    if st != 200:
        raise RuntimeError(f"bundle main non 200: {st}")
    txt = body.decode("utf-8", "replace")
    found = sorted(set(re.findall(r'data\.[a-zA-Z0-9\-]+\.workers\.dev', txt)))
    log(f"   trovati {len(found)} worker")
    return found


def extract_vault_and_consts(provider_url, site=SITE):
    """Scarica il worker provider e ne estrae vault + specs + consts."""
    log(f"→ scarico provider worker {provider_url}")
    full = site.rstrip("/") + provider_url
    st, body, _ = http_get(full, timeout=30)
    if st != 200:
        raise RuntimeError(f"provider worker non 200: {st}")
    txt = body.decode("utf-8", "replace")

    # Estrai dt
    m = re.search(r'\bdt\s*=\s*\[([^\]]+)\]', txt)
    if not m:
        raise RuntimeError("dt non trovato")
    dt_strs = re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))

    # Estrai pt
    m = re.search(r'\bpt\s*=\s*\[(.*?)\]\s*,', txt, re.DOTALL)
    if not m:
        raise RuntimeError("pt non trovato")
    pt_strs = re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))

    log(f"   dt={len(dt_strs)} pt={len(pt_strs)}")
    vault = decode_vault(pt_strs, dt_strs)

    specs = vault.get("specs") or []
    consts = vault.get("consts") or []
    if len(specs) < 6 or len(consts) < 26:
        raise RuntimeError(f"vault inatteso: specs={len(specs)} consts={len(consts)}")

    # Sanity: consts[16] dovrebbe iniziare con "ninjacorner"
    for idx, name in [(16, "rt"), (20, "nt"), (22, "Ct"), (25, "Mt")]:
        b = const_bytes(consts[idx])
        if not b.startswith(b"ninjacorner"):
            log(f"   ⚠️  consts[{idx}] ({name}) non inizia con 'ninjacorner': {b[:40]}")

    return vault


# ============================================================
# NINJACORNER CLIENT
# ============================================================
class NinjaClient:
    """Client per il protocollo NinjaCorner."""

    def __init__(self, vault, workers):
        specs = vault["specs"]
        consts = vault["consts"]
        self.specs = specs
        self.consts = consts
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

    # ----- cifratura richiesta -----
    def _build_body(self, provider):
        ts = int(time.time()) // 300
        lt_str = self.d_Lt.decode("utf-8", errors="replace")
        plaintext = json.dumps([lt_str, provider, ts]).encode("utf-8")
        nonce = os.urandom(8)
        cipher = salsa20_12(self.invio_key, nonce, plaintext)
        tag = sha256(self.invio_key, nonce, cipher, self.nt)[:12]
        body = b"\x0a" + nonce + tag + cipher
        return body, nonce, ts

    def _build_resp_key(self, nonce_invio, provider, ts):
        ts_be = struct.pack(">I", ts)
        return sha256(self.d_H, self.d_et, self.d_Lt,
                      nonce_invio, provider.encode(), ts_be, self.Mt)

    def _decrypt_resp(self, resp, nonce_invio, provider, ts):
        if len(resp) < 21 or resp[0] != 0x04:
            return None, "bad_magic"
        resp_nonce = resp[1:9]
        resp_tag = resp[9:21]
        cipher = resp[21:]
        key = self._build_resp_key(nonce_invio, provider, ts)
        expected = sha256(key, resp_nonce, cipher, self.Ct)[:12]
        if expected != resp_tag:
            return None, "tag_mismatch"
        return salsa20_12(key, resp_nonce, cipher), None

    # ----- POST con retry su worker pool -----
    def _post_corner(self, provider, extra_id=None, timeout=20):
        body, nonce_invio, ts = self._build_body(provider)
        qs = f"p={provider}"
        if extra_id:
            qs += f"&id={extra_id}"

        # Prova worker noto, poi tutti
        candidates = []
        if self.worker:
            candidates.append(self.worker)
        for w in self.workers:
            if w not in candidates:
                candidates.append(w)

        last_err = None
        for w in candidates:
            url = f"https://{w}/corner?{qs}"
            st, resp, _ = http_post_bin(url, body, timeout=timeout)
            if st == 200 and len(resp) > 21 and resp[0] == 0x04:
                self.worker = w
                plain, err = self._decrypt_resp(resp, nonce_invio, provider, ts)
                if err:
                    last_err = f"{w}: {err}"
                    continue
                return plain, w
            last_err = f"{w}: st={st} len={len(resp) if resp else 0}"
        raise RuntimeError(f"tutti i worker falliti: {last_err}")

    # ----- API pubblica -----
    def get_catalog(self, provider=PROVIDER_DEFAULT):
        raw, w = self._post_corner(provider)
        data = json.loads(raw.decode("utf-8"))
        log(f"   ✅ catalogo via {w}: {len(data)} item")
        return data

    def get_event_detail(self, event_id, provider=PROVIDER_DEFAULT):
        raw, w = self._post_corner(provider, extra_id=event_id)
        return json.loads(raw.decode("utf-8"))


# ============================================================
# EMBED RESOLVER (Playwright, solo per embed non diretti)
# ============================================================
LICENSE_HINTS = ("license", "widevine", "getlicense", "acquirelicense", "drm")
MANIFEST_HINTS = (".m3u8", ".mpd", "manifest", "playlist")
CF_HINTS = ("challenges.cloudflare.com", "cf-chl", "/cdn-cgi/challenge", "turnstile")
DIRECT_RE = re.compile(r"\.(?:m3u8|mpd)(?:$|\?)", re.I)


def resolve_embed(embed_url, origin, proxy=None, timeout=45, tag=""):
    try:
        from patchright.sync_api import sync_playwright
        engine = "patchright"
    except ImportError:
        from playwright.sync_api import sync_playwright
        engine = "playwright"

    rec = {"url": None, "m3u8": None, "mpd": None, "license_url": None,
           "cloudflare": False, "final_url": None, "title": None,
           "requests": [], "engine": engine, "referer": origin + "/"}

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
                user_agent=UA_IPHONE, viewport={"width": 1280, "height": 800},
                locale="it-IT", timezone_id="Europe/Rome",
                extra_http_headers={"Referer": origin + "/"},
                ignore_https_errors=True)
            ctx.on("request", on_request)
            ctx.on("response", on_response)
            page = ctx.new_page()
            try:
                page.goto(embed_url, wait_until="domcontentloaded",
                          timeout=timeout * 1000)
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
            page.wait_for_timeout(9000)
            browser.close()
    except Exception as e:
        log(f"      ❌ {tag}: {str(e)[:100]}")

    rec["url"] = rec["m3u8"] or rec["mpd"]
    return rec


# ============================================================
# M3U
# ============================================================
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
# MAIN
# ============================================================
def main():
    ap = argparse.ArgumentParser(description="StreamCorner Extractor v10.0")
    ap.add_argument("--output", "-o", default="streamcorner_COMPLETE.m3u")
    ap.add_argument("--output-dir", default=".")
    ap.add_argument("--proxy", default=os.environ.get("SC_PROXY"))
    ap.add_argument("--provider", default=PROVIDER_DEFAULT)
    ap.add_argument("--all-events", action="store_true")
    ap.add_argument("--no-embeds", action="store_true")
    ap.add_argument("--embed-workers", "--playwright-workers",
                    type=int, default=4, dest="embed_workers")
    ap.add_argument("--embed-limit", "--playwright-limit",
                    type=int, default=0, dest="embed_limit")
    args = ap.parse_args()

    # /tmp non scrivibile da iOS: fallback
    if args.output_dir == ".":
        args.output_dir = os.path.dirname(args.output) or "."
    os.makedirs(args.output_dir, exist_ok=True)

    log("=" * 66)
    log("📡 STREAMCORNER EXTRACTOR v10.0 (HTTP-puro NinjaCorner)")
    log("=" * 66)

    # ---- 1. Discovery ----
    log("\n[1/5] Discovery")
    try:
        main_bundle = discover_bundle()
        provider_url = discover_provider_worker(main_bundle)
        workers = discover_workers(main_bundle)
        vault = extract_vault_and_consts(provider_url)
    except Exception as e:
        log(f"   ❌ discovery fallita: {e}")
        # placeholder outputs
        for fn in ("streamcorner_EVENTS_LIVE.m3u", "streamcorner_VLC.m3u",
                   "streamcorner_KODI.m3u"):
            open(os.path.join(args.output_dir, fn), "w").write("#EXTM3U\n")
        open(args.output, "w").write("#EXTM3U\n")
        return

    # ---- 2. Client NinjaCorner ----
    log("\n[2/5] Client NinjaCorner")
    client = NinjaClient(vault, workers)
    log(f"   invio_key: {client.invio_key.hex()[:32]}...")

    try:
        catalog = client.get_catalog(args.provider)
    except Exception as e:
        log(f"   ❌ get_catalog: {e}")
        for fn in ("streamcorner_EVENTS_LIVE.m3u", "streamcorner_VLC.m3u",
                   "streamcorner_KODI.m3u"):
            open(os.path.join(args.output_dir, fn), "w").write("#EXTM3U\n")
        open(args.output, "w").write("#EXTM3U\n")
        return

    # ---- 3. Dettagli evento ----
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
                rows.append((f"{base} — {s.get('source_name') or '?'} [DIRETTA]",
                             logo, name, {"url": u, "referer": SITE}))
                continue
            emb = s.get("embed_url")
            if not emb or args.no_embeds:
                continue
            sname = s.get("source_name") or "?"
            if cache.get(emb):
                rows.append((f"{base} — {sname}", logo, name,
                             {"url": cache[emb], "referer": SITE}))
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
            return emb, label, logo, grp, resolve_embed(
                emb, SITE, args.proxy, tag=label[:28])

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
                    log(f"   🎯 [{done}/{len(embed_jobs)}] {label[:45]}")
                elif done % 5 == 0:
                    log(f"   · [{done}/{len(embed_jobs)}]")

        try:
            json.dump(cache, open(M3U8_CACHE, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
        except Exception:
            pass

    if report:
        drm_path = os.path.join(args.output_dir, DRM_REPORT)
        with open(drm_path, "w", encoding="utf-8") as f:
            for r in report:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # ---- Output ----
    n = write_m3u(os.path.join(args.output_dir, "streamcorner_EVENTS_LIVE.m3u"), rows)
    log(f"   💾 EVENTS_LIVE: {n}")
    n = write_m3u(os.path.join(args.output_dir, "streamcorner_VLC.m3u"),
                  [(l, lg, "StreamCorner", r) for l, lg, _, r in rows])
    log(f"   💾 VLC: {n}")
    n = write_m3u(os.path.join(args.output_dir, "streamcorner_KODI.m3u"), rows)
    log(f"   💾 KODI: {n}")
    n = write_m3u(args.output, rows)
    log(f"   💾 {args.output}: {n}")

    # ---- Capture summary ----
    try:
        summary = {
            "site": SITE,
            "provider": args.provider,
            "workers_count": len(workers),
            "winner_worker": client.worker,
            "catalog_size": len(catalog),
            "events_full": len(events_full),
            "events_live": len(live),
            "streams_total": len(rows),
            "embed_jobs": len(embed_jobs),
            "embed_resolved": sum(1 for r in report if r.get("url")),
            "extracted_at": int(time.time()),
        }
        with open(os.path.join(args.output_dir, CAPTURE_FILE), "w",
                  encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
        log(f"   💾 {CAPTURE_FILE} salvato")
    except Exception as e:
        log(f"   ⚠️ capture: {e}")

    log("\n" + "=" * 66)
    log(f"✅ eventi={len(live)}  stream={len(rows)}  report={len(report)}")
    log("=" * 66)


if __name__ == "__main__":
    main()