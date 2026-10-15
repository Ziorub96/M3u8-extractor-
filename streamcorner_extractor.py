#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StreamCorner Extractor — CARNETS-style (Hybrid: curl_cffi + Playwright)
=======================================================================

Estrae canali ed eventi da StreamCorner.st e genera playlist M3U.

Strategia ibrida:
    1. Pre-check con curl_cffi (TLS impersonate Chrome):
       - Scarica l'embed HTML
       - Cerca m3u8 con regex
       - Valida con HTTP GET
       - Se trovato → NO Playwright (risparmia 15-20s)
    2. Fallback Playwright per embed JS-only
    3. Cache persistente tra run (via actions/cache)

Output:
    streamcorner_COMPLETE.m3u   → mixed
    streamcorner_VLC.m3u        → solo m3u8 diretti
    streamcorner_<provider>.m3u → split (con --split)

Requisiti:
    pip install requests urllib3 playwright curl_cffi
    playwright install chromium

Uso:
    python streamcorner_extractor.py                        # default: 400 item
    python streamcorner_extractor.py --playwright-limit 674 # tutti
    python streamcorner_extractor.py --no-playwright        # solo embed
    python streamcorner_extractor.py --no-quick-probe       # salta curl_cffi
"""

import os
import re
import sys
import json
import time
import struct
import hashlib
import argparse
import requests
import urllib3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# curl_cffi opzionale (fallback a requests se non installato)
try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False


# ============================================================
# CONFIGURAZIONE
# ============================================================
WORKER_HOST = "data.senbon003.workers.dev"
WORKER_URL = f"https://{WORKER_HOST}/corner"
WORKER_ASSETS = [
    "https://streamcorner.st/assets/provider-data.worker-DzCsTezu.js",
    "https://streamcorner.st/assets/provider_worker.js",
]
VAULT_CACHE = ".streamcorner_vault.json"
M3U8_CACHE = ".streamcorner_m3u8_cache.json"

PLAYWRIGHT_DEFAULT_LIMIT = 400
PLAYWRIGHT_DEFAULT_WORKERS = 6

# Priorità basata su dati reali (slingtv è il migliore con 11%, skygo 3%)
PLAYWRIGHT_DEFAULT_PRIORITY = (
    "slingtv_channels,extra004,extra003,channels,mlb,nfl,nba,peacock_channels,skygo"
)

PROVIDERS = {
    "slingtv_channels": "channel_id",
    "peacock_channels": "stream_id",
    "skygo":            "stream_id",
    "extra003":         "stream_id",
    "extra004":         "stream_id",
    "channels":         "stream_id",
    "mlb":              "stream_id",
    "nfl":              "stream_id",
    "nba":              "game_id",
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")

HEADERS_WORKER = {
    "User-Agent": UA,
    "Origin": "https://streamcorner.st",
    "Referer": "https://streamcorner.st/",
    "Content-Type": "text/plain",
    "Accept": "application/octet-stream",
}

RE_M3U8 = re.compile(r'https?://[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*')

PLAYWRIGHT_AVAILABLE = None


# ============================================================
# UTILITY
# ============================================================
def ensure_output_files(output_dir, output_name):
    os.makedirs(output_dir, exist_ok=True)
    for f in [output_name, "streamcorner_VLC.m3u"]:
        path = os.path.join(output_dir, f)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as fp:
                fp.write("#EXTM3U\n")


# ============================================================
# SALSA20/12
# ============================================================
M32 = 0xffffffff


def rotl32(v, c):
    return ((v << c) & M32) | (v >> (32 - c))


def x8(t, e):
    n = 7 & e
    return 255 & ((t << n) | (t >> (8 - n)))


def Y8(t, e):
    n = 7 & e
    return 255 & ((t >> n) | (t << (8 - n)))


def _salsa_qr(s, a, b, c, d):
    s[b] ^= rotl32((s[a] + s[d]) & M32, 7)
    s[c] ^= rotl32((s[b] + s[a]) & M32, 9)
    s[d] ^= rotl32((s[c] + s[b]) & M32, 13)
    s[a] ^= rotl32((s[d] + s[c]) & M32, 18)


def _salsa_block(key32, nonce8, counter):
    k = list(struct.unpack('<8I', key32))
    n0, n1 = struct.unpack('<2I', nonce8)
    state = [
        0x61707865, k[0], k[1], k[2],
        k[3], 0x3320646e, n0, n1,
        counter & M32, (counter >> 32) & M32, 0x79622d32, k[4],
        k[5], k[6], k[7], 0x6b206574,
    ]
    x = state[:]
    for _ in range(6):
        _salsa_qr(x, 0, 4, 8, 12); _salsa_qr(x, 5, 9, 13, 1)
        _salsa_qr(x, 10, 14, 2, 6); _salsa_qr(x, 15, 3, 7, 11)
        _salsa_qr(x, 0, 1, 2, 3);   _salsa_qr(x, 5, 6, 7, 4)
        _salsa_qr(x, 10, 11, 8, 9); _salsa_qr(x, 15, 12, 13, 14)
    return struct.pack('<16I', *[(x[i] + state[i]) & M32 for i in range(16)])


def salsa20_12(key, nonce8, data, counter=0):
    out = bytearray()
    i = 0
    while i < len(data):
        ks = _salsa_block(key, nonce8, counter + i // 64)
        ch = data[i:i + 64]
        out.extend(bytes(a ^ b for a, b in zip(ch, ks)))
        i += 64
    return bytes(out)


# ============================================================
# DECODER VAULT
# ============================================================
VAULT_SBOX = [185, 243, 94, 251, 153, 162, 126, 77,
              155, 41, 182, 62, 200, 173, 252, 244]


def decode_vault_payload(payload, key):
    out = bytearray(len(payload))
    c = 117
    y = 84 ^ key[1]
    for a in range(len(payload)):
        h = (key[15 & a] ^ VAULT_SBOX[15 & a] ^ y
             ^ ((139 * a) & 0xFF)
             ^ ((1293491885 >> (8 * (3 & a))) & 0xFF)
             ^ c) & 0xFF
        out[a] = payload[a] ^ h
        y = payload[a]
        c = x8((c ^ payload[a] ^ key[(a + 7) & 15]) & 0xFF, 1)
    return bytes(out)


def decode_spec(spec):
    t0, t1, t2, t3, t4, t5 = spec[:6]

    def as_list(v):
        return v['v'] if isinstance(v, dict) else v

    t2 = as_list(t2); t3 = as_list(t3); t4 = as_list(t4); t5 = as_list(t5)
    e = bytearray(len(t2))
    n = t0
    for o in range(len(t2)):
        i = t5[o]
        inner = x8(t2[o], 7 & (n ^ t1))
        mid1 = (t3[i] + (n ^ ((13 * o) & 255))) & 255
        mid2 = (t1 + 17 * o) & 255
        r = Y8((inner ^ mid1 ^ mid2) & 255, 7 & (t4[o] ^ n))
        e[o] = r
        n = (x8(n, 1) ^ ((r + t4[o] + o) & 255)) & 255
    return bytes(e)


def get_const(vault, idx):
    c = vault['consts'][idx]
    if isinstance(c, dict):
        t = c.get('t', 'u8'); v = c.get('v')
        if t == 'u8':
            return bytes(v)
        if t == 'u32':
            return b''.join(struct.pack('<I', x) for x in v)
        if t == 's':
            return v.encode()
        if t == 'a':
            return b''.join(s.encode() for s in v)
        if t == 'n':
            return bytes([v])
    return b''


# ============================================================
# VAULT LOADER
# ============================================================
def _b64_decode_safe(s):
    s = s.strip().replace("-", "+").replace("_", "/")
    s += "=" * ((4 - len(s) % 4) % 4)
    import base64
    return base64.b64decode(s)


def _extract_array_strings(js, name):
    m = re.search(rf'\b{re.escape(name)}\s*=\s*\[', js)
    if not m:
        return None
    start = m.end() - 1
    depth = 1
    i = start + 1
    while i < len(js) and depth > 0:
        c = js[i]
        if c == '[': depth += 1
        elif c == ']': depth -= 1
        elif c in ('"', "'"):
            q = c
            i += 1
            while i < len(js) and js[i] != q:
                if js[i] == '\\': i += 1
                i += 1
        i += 1
    arr = js[start:i]
    return re.findall(r'["\']([^"\']+)["\']', arr)


def load_vault(force_refresh=False, verbose=True):
    if not force_refresh and os.path.exists(VAULT_CACHE):
        with open(VAULT_CACHE, encoding="utf-8") as f:
            vault = json.load(f)
        if verbose:
            print(f"   📂 Vault da cache ({len(vault.get('consts', []))} consts)")
        return vault

    if verbose:
        print("   🔄 Rigenerazione vault dal worker...")

    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Referer": "https://streamcorner.st/"})

    worker_js = None
    for url in WORKER_ASSETS:
        try:
            r = session.get(url, timeout=15, verify=False)
            if r.status_code == 200 and len(r.text) > 10000:
                worker_js = r.text
                if verbose:
                    print(f"      ✅ Worker: {url.split('/')[-1]} ({len(worker_js)} char)")
                break
        except Exception:
            continue

    if not worker_js:
        raise RuntimeError("Impossibile scaricare il worker")

    dr_arr = _extract_array_strings(worker_js, "dr")
    Ar_arr = _extract_array_strings(worker_js, "Ar")
    if not dr_arr or not Ar_arr:
        raise RuntimeError("dr/Ar non trovate nel worker")

    key = _b64_decode_safe("".join(dr_arr))
    payload = _b64_decode_safe("".join(Ar_arr))
    if verbose:
        print(f"      🔑 Chiave: {len(key)} byte | Payload: {len(payload)} byte")

    decrypted = decode_vault_payload(payload, key)
    vault = json.loads(decrypted.decode("utf-8"))

    with open(VAULT_CACHE, "w", encoding="utf-8") as f:
        json.dump(vault, f, ensure_ascii=False)

    if verbose:
        print(f"      ✅ Vault: {len(vault.get('consts', []))} consts, "
              f"{len(vault.get('specs', []))} specs")
    return vault


# ============================================================
# WORKER CALL
# ============================================================
class StreamCornerClient:
    def __init__(self, vault):
        self.vault = vault
        self.t = decode_spec(vault['specs'][0])
        self.e = decode_spec(vault['specs'][1])
        self.n = decode_spec(vault['specs'][3])
        self.tr = get_const(vault, 16)
        self.nr = get_const(vault, 20)
        self.Br = get_const(vault, 25)
        self.r_key = hashlib.sha256(self.t + self.e + self.tr).digest()
        self.n_str = self.n.decode("utf-8", "replace")
        self.session = requests.Session()
        self.session.headers.update(HEADERS_WORKER)

    def call(self, provider, extra_query=None, timeout=20):
        nonce = os.urandom(8)
        u = int(time.time() // 300)
        msg_str = json.dumps([self.n_str, provider, u], separators=(",", ":"))
        ct = salsa20_12(self.r_key, nonce, msg_str.encode(), 0)
        tag = hashlib.sha256(self.r_key + nonce + ct + self.nr).digest()[:12]
        body = bytes([0x0A]) + nonce + tag + ct

        url = f"{WORKER_URL}?p={provider}"
        if extra_query:
            for k, v in extra_query.items():
                url += f"&{k}={v}"

        try:
            resp = self.session.post(url, data=body, timeout=timeout, verify=False)
        except Exception:
            return None
        if resp.status_code != 200 or not resp.content or resp.content[0] != 0x04:
            return None

        rn = resp.content[1:9]
        rc = resp.content[21:]
        m0 = self.n_str.encode()
        m1 = nonce
        m2 = provider.encode()
        m3 = struct.pack(">I", u)
        R = hashlib.sha256(self.t + self.e + m0 + m1 + m2 + m3 + self.Br).digest()
        pt = salsa20_12(R, rn, rc, 0)
        try:
            return json.loads(pt.decode("utf-8"))
        except Exception:
            return None


# ============================================================
# ESTRAZIONE STREAM
# ============================================================
def extract_streams(item):
    if not isinstance(item, dict):
        return []
    urls = []
    if item.get("embed_url"):
        urls.append({
            "source_name": item.get("channel_name") or item.get("event_name")
                           or item.get("title") or "?",
            "embed_url": item["embed_url"],
        })
    for key in ("streams", "vods"):
        for s in (item.get(key) or []):
            if isinstance(s, dict) and s.get("embed_url"):
                urls.append({
                    "source_name": s.get("source_name") or "?",
                    "embed_url": s["embed_url"],
                })
    return urls


def enrich_item(client, provider, key_field, item):
    sid = item.get(key_field)
    if not sid:
        return []
    r = client.call(provider, extra_query={"id": sid})
    if not isinstance(r, dict):
        return []
    streams = extract_streams(r)
    out = []
    for s in streams:
        merged = dict(item)
        merged.update(r)
        merged["embed_url"] = s["embed_url"]
        merged["source_name"] = s["source_name"]
        for k in ("streams", "vods"):
            merged.pop(k, None)
        out.append(merged)
    return out


def fetch_provider(client, provider, key_field, workers=15, verbose=True):
    base = client.call(provider)
    if base is None:
        if verbose:
            print(f"      ⚠️  {provider}: vuoto")
        return []

    if isinstance(base, dict):
        base = base.get("sports") or base.get("channels") or []
    if not isinstance(base, list) or not base:
        return []

    enriched = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(enrich_item, client, provider, key_field, item)
                for item in base]
        for fut in as_completed(futs):
            try:
                enriched.extend(fut.result())
            except Exception:
                pass

    if verbose:
        print(f"      ✅ {provider}: {len(enriched)} stream "
              f"(da {len(base)} item)")
    return enriched


# ============================================================
# QUICK PROBE — curl_cffi (o requests come fallback)
# ============================================================
def quick_probe(embed_url, timeout=10, verbose=False):
    """
    Prova a estrarre l'm3u8 direttamente dall'HTML dell'embed.
    Usa curl_cffi (impersonate Chrome) se disponibile.

    Ritorna (m3u8_url, reason) oppure (None, reason).
    """
    try:
        if CURL_CFFI_AVAILABLE:
            session = curl_requests.Session(impersonate="chrome120")
            r = session.get(
                embed_url,
                headers={"Referer": "https://streamcorner.st/"},
                timeout=timeout,
                allow_redirects=True,
            )
        else:
            session = requests.Session()
            session.headers.update({"User-Agent": UA})
            r = session.get(
                embed_url,
                headers={"Referer": "https://streamcorner.st/"},
                timeout=timeout,
                verify=False,
                allow_redirects=True,
            )

        if r.status_code != 200:
            return None, f"http_{r.status_code}"

        html = r.text
        matches = RE_M3U8.findall(html)

        if not matches:
            # Cerca in attributi data-*
            for m in re.finditer(r'data-(?:url|src|stream|hls|m3u8)="([^"]+)"', html, re.I):
                if ".m3u8" in m.group(1):
                    matches.append(m.group(1))

        if not matches:
            return None, "no_m3u8_in_html"

        # Dedup e prendi il primo
        m3u8 = matches[0].replace("\\/", "/")

        # Validazione veloce: HEAD/GET sull'm3u8
        try:
            if CURL_CFFI_AVAILABLE:
                check_session = curl_requests.Session(impersonate="chrome120")
                check = check_session.get(m3u8, timeout=5, allow_redirects=True)
            else:
                check = session.get(m3u8, timeout=5, verify=False,
                                    allow_redirects=True)

            if check.status_code == 200 and "#EXTM3U" in check.text[:500]:
                if verbose:
                    print(f"      ⚡ quick_probe OK: {m3u8[:80]}")
                return m3u8, "quick_probe_ok"
            else:
                return None, f"m3u8_http_{check.status_code}"
        except Exception as e:
            return None, f"validate_err_{str(e)[:30]}"

    except Exception as e:
        return None, f"probe_err_{str(e)[:30]}"


# ============================================================
# PLAYWRIGHT RESOLVER (fallback)
# ============================================================
def _check_playwright():
    global PLAYWRIGHT_AVAILABLE
    if PLAYWRIGHT_AVAILABLE is not None:
        return PLAYWRIGHT_AVAILABLE
    try:
        from playwright.sync_api import sync_playwright  # noqa
        PLAYWRIGHT_AVAILABLE = True
    except ImportError:
        PLAYWRIGHT_AVAILABLE = False
        print("   ⚠️  Playwright non installato.")
    return PLAYWRIGHT_AVAILABLE


def resolve_embed_with_playwright(embed_url, timeout=15, verbose=False):
    if not _check_playwright():
        return None

    from playwright.sync_api import sync_playwright

    found = {"m3u8": None}

    def on_response(response):
        url = response.url
        if ".m3u8" in url and found["m3u8"] is None:
            try:
                body = response.text()
                if "#EXTM3U" in body[:500]:
                    found["m3u8"] = url
                    if verbose:
                        print(f"      🎯 playwright: {url[:80]}")
            except Exception:
                pass

    BLOCK_TYPES = {"image", "font", "stylesheet"}

    def route_handler(route):
        if route.request.resource_type in BLOCK_TYPES:
            return route.abort()
        return route.continue_()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-images",
                    "--blink-settings=imagesEnabled=false",
                ],
            )
            context = browser.new_context(
                user_agent=UA,
                viewport={"width": 1280, "height": 720},
                locale="en-US",
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            context.route("**/*", route_handler)

            page = context.new_page()
            page.on("response", on_response)

            try:
                page.goto(embed_url, wait_until="domcontentloaded",
                          timeout=timeout * 1000)
            except Exception:
                pass

            for _ in range(6):
                if found["m3u8"]:
                    break
                try:
                    page.wait_for_timeout(500)
                except Exception:
                    break

            if not found["m3u8"]:
                for selector in [
                    "button[aria-label*='play' i]",
                    ".vjs-big-play-button",
                    ".jw-icon-playback",
                    ".play-button",
                ]:
                    try:
                        page.click(selector, timeout=800)
                        break
                    except Exception:
                        continue

                for x, y in [(640, 360), (400, 300), (800, 400)]:
                    try:
                        page.mouse.click(x, y)
                        page.wait_for_timeout(500)
                        if found["m3u8"]:
                            break
                    except Exception:
                        continue

                for _ in range(8):
                    if found["m3u8"]:
                        break
                    try:
                        page.wait_for_timeout(500)
                    except Exception:
                        break

            browser.close()
    except Exception:
        return None

    return found["m3u8"]


# ============================================================
# RISOLUZIONE IBRIDA (quick_probe → playwright)
# ============================================================
def resolve_hybrid(embed_url, use_quick=True, use_playwright=True,
                   quick_timeout=10, pw_timeout=15, verbose=False):
    """
    1. Prova quick_probe (curl_cffi, ~0.5s)
    2. Se fallisce e use_playwright → Playwright (~15s)
    """
    if use_quick:
        m3u8, reason = quick_probe(embed_url, timeout=quick_timeout,
                                   verbose=verbose)
        if m3u8:
            return m3u8, f"quick_probe ({reason})"

    if use_playwright:
        m3u8 = resolve_embed_with_playwright(embed_url, timeout=pw_timeout,
                                             verbose=verbose)
        if m3u8:
            return m3u8, "playwright"

    return None, "not_resolved"


def resolve_with_playwright(items, workers=2, verbose=True,
                            cache_file=M3U8_CACHE,
                            use_quick=True, use_playwright=True):
    """Batch resolver con cache persistente."""
    cache = {}
    if os.path.exists(cache_file):
        try:
            with open(cache_file, encoding="utf-8") as f:
                cache = json.load(f)
            if verbose:
                print(f"   📂 Cache m3u8: {len(cache)} voci")
        except Exception:
            cache = {}

    to_resolve = []
    for it in items:
        embed = it.get("embed_url")
        if not embed:
            continue
        if it.get("m3u8"):
            continue
        if embed in cache:
            it["m3u8"] = cache[embed]
            continue
        to_resolve.append(it)

    if verbose:
        already = sum(1 for it in items if it.get("m3u8"))
        print(f"   🔄 Da risolvere: {len(to_resolve)} (già in cache: {already})")

    if not to_resolve:
        return items

    total = len(to_resolve)
    resolved = 0
    done = 0
    quick_hits = 0
    pw_hits = 0

    def worker(item):
        embed = item["embed_url"]
        m3u8, reason = resolve_hybrid(
            embed,
            use_quick=use_quick,
            use_playwright=use_playwright,
            quick_timeout=10,
            pw_timeout=15,
            verbose=False,
        )
        return item, embed, m3u8, reason

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(worker, it) for it in to_resolve]
        for fut in as_completed(futs):
            done += 1
            try:
                item, embed, m3u8, reason = fut.result()
                if m3u8:
                    item["m3u8"] = m3u8
                    cache[embed] = m3u8
                    resolved += 1
                    if reason.startswith("quick_probe"):
                        quick_hits += 1
                    else:
                        pw_hits += 1
                    if verbose:
                        tag = "⚡" if reason.startswith("quick_probe") else "🎯"
                        print(f"   {tag} [{done}/{total}] "
                              f"{item.get('channel_name') or item.get('event_name') or '?'}"
                              f" → {m3u8[:70]}")
                elif verbose and done % 10 == 0:
                    print(f"   ·  [{done}/{total}] (risolti: {resolved})")
            except Exception:
                pass

    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    if verbose:
        print(f"   ✅ Risolti: {resolved}/{total} "
              f"(⚡ quick_probe: {quick_hits}, 🎯 playwright: {pw_hits})")
    return items


# ============================================================
# M3U WRITER
# ============================================================
def write_m3u(items, filename, group_prefix="", prefer_m3u8=False):
    seen = set()
    written = 0
    with open(filename, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        f.write(f"# StreamCorner — {len(items)} item\n\n")
        for it in items:
            if prefer_m3u8:
                url = it.get("m3u8")
                if not url:
                    continue
            else:
                url = it.get("m3u8") or it.get("embed_url")
            if not url or url in seen:
                continue
            seen.add(url)
            written += 1

            name = (it.get("channel_name") or it.get("event_name")
                    or it.get("title") or it.get("name") or "?")
            src = it.get("source_name")
            if src and src != name and src != "?":
                name = f"{name} [{src}]"

            logo = (it.get("channel_logo") or it.get("logo")
                    or it.get("poster") or it.get("hero_thumbnail") or "")
            cat = (it.get("category") or it.get("classification")
                   or it.get("league") or group_prefix or "Other")

            if logo:
                f.write(f'#EXTINF:-1 tvg-logo="{logo}" '
                        f'group-title="{cat}",{name}\n')
            else:
                f.write(f'#EXTINF:-1 group-title="{cat}",{name}\n')
            f.write(url + "\n")
    return written


# ============================================================
# MAIN
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="StreamCorner Extractor — Hybrid curl_cffi + Playwright")
    parser.add_argument("--providers", default="all",
                        help="Comma-separated (default: all)")
    parser.add_argument("--output", default="streamcorner_COMPLETE.m3u",
                        help="Output M3U principale (mixed)")
    parser.add_argument("--output-dir", default=".",
                        help="Cartella output (default: corrente)")
    parser.add_argument("--refresh-vault", action="store_true",
                        help="Forza rigenerazione vault")
    parser.add_argument("--workers", type=int, default=15,
                        help="Thread paralleli per provider (default: 15)")
    parser.add_argument("--split", action="store_true",
                        help="Genera anche M3U per provider")
    parser.add_argument("--no-playwright", action="store_true",
                        help="Disabilita completamente Playwright")
    parser.add_argument("--no-quick-probe", action="store_true",
                        help="Disabilita quick probe (solo Playwright)")
    parser.add_argument("--playwright-workers", type=int,
                        default=PLAYWRIGHT_DEFAULT_WORKERS,
                        help=f"Worker paralleli (default: {PLAYWRIGHT_DEFAULT_WORKERS})")
    parser.add_argument("--playwright-limit", type=int,
                        default=PLAYWRIGHT_DEFAULT_LIMIT,
                        help=f"Max item da risolvere (default: {PLAYWRIGHT_DEFAULT_LIMIT})")
    parser.add_argument("--playwright-priority",
                        default=PLAYWRIGHT_DEFAULT_PRIORITY,
                        help="Provider in ordine di priorità (default: slingtv primo)")
    args = parser.parse_args()

    print("=" * 60)
    print("📡 STREAMCORNER EXTRACTOR (Hybrid)")
    print("=" * 60)

    if CURL_CFFI_AVAILABLE:
        print("   ✅ curl_cffi disponibile (pre-check attivo)")
    else:
        print("   ⚠️  curl_cffi non disponibile, uso requests")

    os.makedirs(args.output_dir, exist_ok=True)
    ensure_output_files(args.output_dir, args.output)

    # 1. Vault
    print(f"\n[1/5] Vault")
    vault = load_vault(force_refresh=args.refresh_vault)
    client = StreamCornerClient(vault)

    # 2. Fetch provider
    if args.providers == "all":
        providers = list(PROVIDERS.items())
    else:
        names = [p.strip() for p in args.providers.split(",")]
        providers = [(p, PROVIDERS[p]) for p in names if p in PROVIDERS]

    print(f"\n[2/5] Fetch {len(providers)} provider")
    all_items = []

    for provider, key in providers:
        print(f"\n   {provider} (key={key})")
        items = fetch_provider(client, provider, key, workers=args.workers)
        if items:
            for it in items:
                it["_provider"] = provider
            all_items.extend(items)

    # 3. Salvataggio anticipato
    print(f"\n[3/5] Salvataggio anticipato (mixed)")
    mixed_path = os.path.join(args.output_dir, args.output)
    total_mixed = write_m3u(all_items, mixed_path, prefer_m3u8=False)
    print(f"   💾 {args.output}: {total_mixed} stream (mixed, salvato)")

    # 4. Risoluzione ibrida
    use_pw = not args.no_playwright
    use_quick = not args.no_quick_probe

    if use_pw or use_quick:
        print(f"\n[4/5] Risoluzione ibrida")
        if use_quick:
            print(f"   ⚡ Quick probe (curl_cffi): attivo")
        if use_pw:
            print(f"   🎯 Playwright fallback: attivo")
        else:
            print(f"   🎯 Playwright fallback: disabilitato")

        priority = [p.strip() for p in args.playwright_priority.split(",")
                    if p.strip()]
        priority_map = {p: i for i, p in enumerate(priority)}

        def sort_key(it):
            p = it.get("_provider", "other")
            return (priority_map.get(p, 999),)

        all_items_sorted = sorted(all_items, key=sort_key)

        limit = args.playwright_limit
        if limit > 0 and limit < len(all_items_sorted):
            print(f"   ⚠️  Limite: {limit} item "
                  f"(catalogo totale: {len(all_items_sorted)})")
            print(f"   📋 Priorità: {priority}")
            to_resolve = all_items_sorted[:limit]
            resolved_ids = {id(it) for it in to_resolve}
            rest = [it for it in all_items if id(it) not in resolved_ids]
        else:
            print(f"   → Tutti i {len(all_items_sorted)} item")
            to_resolve = all_items_sorted
            rest = []

        prov_count = Counter(it.get("_provider", "?") for it in to_resolve)
        print(f"   📊 Provider processati:")
        for p, c in prov_count.most_common():
            print(f"      {p}: {c}")

        to_resolve = resolve_with_playwright(
            to_resolve,
            workers=args.playwright_workers,
            verbose=True,
            use_quick=use_quick,
            use_playwright=use_pw,
        )
        all_items = to_resolve + rest

        total_mixed = write_m3u(all_items, mixed_path, prefer_m3u8=False)
        print(f"\n   💾 {args.output}: {total_mixed} stream (aggiornato)")

        vlc_path = os.path.join(args.output_dir, "streamcorner_VLC.m3u")
        total_vlc = write_m3u(all_items, vlc_path, prefer_m3u8=True)
        print(f"   💾 streamcorner_VLC.m3u: {total_vlc} stream (solo m3u8)")
    else:
        print(f"\n[4/5] Risoluzione saltata")

    # 5. Split
    if args.split:
        print(f"\n[5/5] Split per provider")
        by_provider = {}
        for it in all_items:
            p = it.get("_provider", "other")
            by_provider.setdefault(p, []).append(it)
        for p, items in by_provider.items():
            fname = os.path.join(args.output_dir, f"streamcorner_{p}.m3u")
            n = write_m3u(items, fname, group_prefix=p, prefer_m3u8=False)
            if n > 0:
                print(f"   💾 {fname} ({n})")

    json_path = os.path.join(args.output_dir, "streamcorner_data.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(all_items, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n   💾 {json_path}")

    n_with_m3u8 = sum(1 for it in all_items if it.get("m3u8"))
    n_with_embed = sum(1 for it in all_items if it.get("embed_url"))
    print("\n" + "=" * 60)
    print(f"✅ COMPLETATO")
    print(f"   Item totali:        {len(all_items)}")
    print(f"   Con embed_url:      {n_with_embed}")
    print(f"   Con m3u8 diretto:   {n_with_m3u8}")
    print("=" * 60)


if __name__ == "__main__":
    main()