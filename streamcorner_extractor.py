#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StreamCorner Extractor — CARNETS-style (M3U8 Direct Default)
===========================================================

Estrae canali ed eventi da StreamCorner.st e genera playlist M3U 
con flussi m3u8 diretti gestiti tramite Playwright di default.

Requisiti:
    pip install requests urllib3 playwright
    playwright install chromium

Uso:
    python streamcorner_extractor.py                        # estrazione con m3u8 diretti (default)
    python streamcorner_extractor.py --no-playwright        # solo embed (veloce, senza browser)
    python streamcorner_extractor.py --split                # + M3U separati per provider
    python streamcorner_extractor.py --providers slingtv_channels,nba
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
from concurrent.futures import ThreadPoolExecutor, as_completed

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


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

# Provider supportati: (endpoint, keyField)
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

# -- Playwright -- (importato lazy, solo se richiesto)
PLAYWRIGHT_AVAILABLE = None  # determinato al runtime


# ============================================================
# SALSA20/12 + PRIMITIVE
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
    for _ in range(6):  # Salsa20/12 = 6 double rounds
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
# DECODER VAULT (XOR + sbox custom)
# ============================================================
VAULT_SBOX = [185, 243, 94, 251, 153, 162, 126, 77,
              155, 41, 182, 62, 200, 173, 252, 244]


def decode_vault_payload(payload, key):
    """Decodifica il vault (Ar) con la chiave dr."""
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


# ============================================================
# DECODER d() (specs)
# ============================================================
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
# VAULT LOADER — scarica e decodifica il worker
# ============================================================
def _b64_decode_safe(s):
    s = s.strip().replace("-", "+").replace("_", "/")
    s += "=" * ((4 - len(s) % 4) % 4)
    import base64
    return base64.b64decode(s)


def _extract_array_strings(js, name):
    """Estrai tutti gli elementi di `NAME=[...]`."""
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
    """Carica il vault da cache o lo rigenera dal worker."""
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
# WORKER CALL — invia richiesta cifrata e decodifica risposta
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
        m3 = struct.pack(">I", u)  # BIG ENDIAN
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
    """Estrai stream da embed_url flat, streams[] o vods[]."""
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
    """Arricchisci un item con embed_url."""
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
    """Scarica catalogo + arricchisce con embed_url."""
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
# PLAYWRIGHT RESOLVER — embed_url → m3u8 diretto
# ============================================================
def _check_playwright():
    """Verifica se Playwright è disponibile (una volta sola)."""
    global PLAYWRIGHT_AVAILABLE
    if PLAYWRIGHT_AVAILABLE is not None:
        return PLAYWRIGHT_AVAILABLE
    try:
        from playwright.sync_api import sync_playwright  # noqa
        PLAYWRIGHT_AVAILABLE = True
    except ImportError:
        PLAYWRIGHT_AVAILABLE = False
        print("   ⚠️  Playwright non installato.")
        print("      Esegui: pip install playwright && playwright install chromium")
    return PLAYWRIGHT_AVAILABLE


def resolve_embed_with_playwright(embed_url, timeout=30, verbose=False):
    """
    Apre embed_url in un browser headless e intercetta la richiesta m3u8.
    Ritorna l'URL m3u8 o None.
    """
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
                        print(f"      🎯 {url[:110]}")
            except Exception:
                pass

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
            context = browser.new_context(
                user_agent=UA,
                viewport={"width": 1280, "height": 720},
                locale="en-US",
            )
            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """)
            page = context.new_page()
            page.on("response", on_response)

            try:
                page.goto(embed_url, wait_until="domcontentloaded",
                          timeout=timeout * 1000)
            except Exception:
                pass

            # Aspetta che il player parta (max 15s)
            for _ in range(15):
                if found["m3u8"]:
                    break
                try:
                    page.wait_for_timeout(1000)
                except Exception:
                    break

            # Prova a cliccare play
            if not found["m3u8"]:
                for selector in [
                    "button[aria-label*='play' i]",
                    "button[title*='play' i]",
                    ".play-button",
                    ".vjs-big-play-button",
                    ".jw-icon-playback",
                    "[class*='play']",
                ]:
                    try:
                        page.click(selector, timeout=1500)
                        break
                    except Exception:
                        continue
                for _ in range(10):
                    if found["m3u8"]:
                        break
                    try:
                        page.wait_for_timeout(1000)
                    except Exception:
                        break

            browser.close()
    except Exception as e:
        if verbose:
            print(f"      ❌ Playwright error: {str(e)[:120]}")
        return None

    return found["m3u8"]


def resolve_with_playwright(items, workers=2, verbose=True, cache_file=M3U8_CACHE):
    """
    Risolve in parallelo gli embed_url degli item in m3u8 diretti.
    Usa una cache persistente per evitare di rifare lo stesso lavoro.
    """
    # Carica cache
    cache = {}
    if os.path.exists(cache_file):
        try:
            with open(cache_file, encoding="utf-8") as f:
                cache = json.load(f)
            if verbose:
                print(f"   📂 Cache m3u8: {len(cache)} voci")
        except Exception:
            cache = {}

    # Filtra item da risolvere
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
        print(f"   🔄 Da risolvere: {len(to_resolve)} (già in cache: "
              f"{sum(1 for it in items if it.get('m3u8'))})")

    if not to_resolve:
        return items

    total = len(to_resolve)
    resolved = 0
    done = 0

    def worker(item):
        embed = item["embed_url"]
        m3u8 = resolve_embed_with_playwright(embed, timeout=30, verbose=False)
        return item, embed, m3u8

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(worker, it) for it in to_resolve]
        for fut in as_completed(futs):
            done += 1
            try:
                item, embed, m3u8 = fut.result()
                if m3u8:
                    item["m3u8"] = m3u8
                    cache[embed] = m3u8
                    resolved += 1
                    if verbose:
                        print(f"   🎯 [{done}/{total}] "
                              f"{item.get('channel_name') or item.get('event_name') or '?'}"
                              f" → {m3u8[:80]}")
                elif verbose and done % 10 == 0:
                    print(f"   ·  [{done}/{total}] (risolti: {resolved})")
            except Exception:
                pass

    # Salva cache
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

    if verbose:
        print(f"   ✅ Risolti: {resolved}/{total}")
    return items


# ============================================================
# M3U WRITER
# ============================================================
def write_m3u(items, filename, group_prefix="", prefer_m3u8=False):
    """
    Scrive playlist M3U.
    Se prefer_m3u8=True, scrive solo gli item con m3u8 risolto.
    """
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
        description="StreamCorner Extractor — genera playlist M3U con flussi m3u8 diretti")
    parser.add_argument("--providers", default="all",
                        help="Comma-separated (default: all)")
    parser.add_argument("--output", default="streamcorner_COMPLETE.m3u",
                        help="Output M3U principale")
    parser.add_argument("--output-dir", default=".",
                        help="Cartella output (default: corrente)")
    parser.add_argument("--refresh-vault", action="store_true",
                        help="Forza rigenerazione vault")
    parser.add_argument("--workers", type=int, default=15,
                        help="Thread paralleli per provider (default: 15)")
    parser.add_argument("--split", action="store_true",
                        help="Genera anche M3U per provider")
    # PLAYWRIGHT ATTIVO DI DEFAULT (usare --no-playwright per disabilitarlo)
    parser.add_argument("--no-playwright", action="store_true",
                        help="Disabilita la risoluzione automatica dei flussi m3u8 con Playwright")
    parser.add_argument("--playwright-workers", type=int, default=2,
                        help="Worker Playwright paralleli (default: 2)")
    parser.add_argument("--playwright-limit", type=int, default=0,
                        help="Limita a N item da risolvere (0 = tutti)")
    args = parser.parse_args()

    print("=" * 60)
    print("📡 STREAMCORNER EXTRACTOR (M3U8 Direct)")
    print("=" * 60)

    # 1. Vault
    print(f"\n[1/4] Vault")
    vault = load_vault(force_refresh=args.refresh_vault)
    client = StreamCornerClient(vault)

    # 2. Provider
    if args.providers == "all":
        providers = list(PROVIDERS.items())
    else:
        names = [p.strip() for p in args.providers.split(",")]
        providers = [(p, PROVIDERS[p]) for p in names if p in PROVIDERS]

    print(f"\n[2/4] Fetch {len(providers)} provider")
    all_items = []
    os.makedirs(args.output_dir, exist_ok=True)

    for provider, key in providers:
        print(f"\n   {provider} (key={key})")
        items = fetch_provider(client, provider, key, workers=args.workers)
        if items:
            for it in items:
                it["_provider"] = provider
            all_items.extend(items)

            if args.split and args.no_playwright:
                fname = os.path.join(args.output_dir,
                                     f"streamcorner_{provider}.m3u")
                n = write_m3u(items, fname, group_prefix=provider, prefer_m3u8=False)
                print(f"      💾 {fname} ({n})")

    # 3. Risoluzione m3u8 con Playwright (ATTIVA DI DEFAULT)
    use_m3u8 = not args.no_playwright
    if use_m3u8:
        print(f"\n[3/4] Risoluzione m3u8 con Playwright")
        if args.playwright_limit > 0:
            all_items = all_items[:args.playwright_limit]
            print(f"   ⚠️  Limite attivo: {len(all_items)} item")
        all_items = resolve_with_playwright(
            all_items, workers=args.playwright_workers, verbose=True)
    else:
        print(f"\n[3/4] Risoluzione Playwright saltata (--no-playwright attivo)")

    # 4. Playlist finale
    print(f"\n[4/4] Playlist finale")

    mixed_path = os.path.join(args.output_dir, args.output)
    total_written = write_m3u(all_items, mixed_path, prefer_m3u8=use_m3u8)
    label = "solo m3u8 diretti, VLC-ready" if use_m3u8 else "embed (mixed)"
    print(f"   💾 {args.output}: {total_written} stream ({label})")

    # Split per provider (con preferenza m3u8 se attivo)
    if args.split and use_m3u8:
        print(f"\n   📂 Split per provider (solo m3u8)...")
        by_provider = {}
        for it in all_items:
            p = it.get("_provider", "other")
            by_provider.setdefault(p, []).append(it)
        for p, items in by_provider.items():
            fname = os.path.join(args.output_dir, f"streamcorner_{p}.m3u")
            n = write_m3u(items, fname, group_prefix=p, prefer_m3u8=True)
            if n > 0:
                print(f"      💾 {fname} ({n})")

    # JSON finale
    json_path = os.path.join(args.output_dir, "streamcorner_data.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(all_items, f, indent=2, ensure_ascii=False, default=str)
    print(f"   💾 {json_path}")

    # Riepilogo
    n_with_m3u8 = sum(1 for it in all_items if it.get("m3u8"))
    n_with_embed = sum(1 for it in all_items if it.get("embed_url"))
    print("\n" + "=" * 60)
    print(f"✅ COMPLETATO")
    print(f"   Item totali:       {len(all_items)}")
    print(f"   Con embed_url:     {n_with_embed}")
    print(f"   Con m3u8 diretto:  {n_with_m3u8}")
    print("=" * 60)


if __name__ == "__main__":
    main()
