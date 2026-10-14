#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StreamCorner Extractor — CARNETS-style
=======================================

Estrae canali e eventi da StreamCorner.st e genera playlist M3U.

Funziona completamente offline dopo il primo run (il vault viene
salvato in `.streamcorner_vault.json`).

Uso:
    python streamcorner_extractor.py                    # tutto
    python streamcorner_extractor.py --providers slingtv_channels,nba
    python streamcorner_extractor.py --refresh-vault     # ri-forza il vault
    python streamcorner_extractor.py --output streamcorner.m3u
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

    # Estrai `dr` (chiave) e `Ar` (payload cifrato)
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
        self.t = decode_spec(vault['specs'][0])   # "ninjacorner.api...."
        self.e = decode_spec(vault['specs'][1])   # "ninjacorner.transport...."
        self.n = decode_spec(vault['specs'][3])   # "ninjacorner.token...."
        self.tr = get_const(vault, 16)            # "ninjacorner:req:outer:v4"
        self.nr = get_const(vault, 20)            # "ninjacorner:req:guard:v4"
        self.Br = get_const(vault, 25)            # "ninjacorner:outer:v3"
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
# M3U WRITER
# ============================================================
def write_m3u(items, filename, group_prefix=""):
    """Scrive playlist M3U da lista di item."""
    seen = set()
    with open(filename, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        f.write(f"# StreamCorner — {len(items)} stream\n\n")
        for it in items:
            url = it.get("embed_url")
            if not url or url in seen:
                continue
            seen.add(url)

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
    return len(seen)


# ============================================================
# MAIN
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="StreamCorner Extractor — genera playlist M3U")
    parser.add_argument("--providers", default="all",
                        help="Comma-separated (default: all)")
    parser.add_argument("--output", default="streamcorner_COMPLETE.m3u",
                        help="Output M3U principale")
    parser.add_argument("--output-dir", default=".",
                        help="Cartella output (default: corrente)")
    parser.add_argument("--refresh-vault", action="store_true",
                        help="Forza rigenerazione vault")
    parser.add_argument("--workers", type=int, default=15,
                        help="Thread paralleli (default: 15)")
    parser.add_argument("--split", action="store_true",
                        help="Genera anche M3U per provider")
    args = parser.parse_args()

    print("=" * 60)
    print("📡 STREAMCORNER EXTRACTOR")
    print("=" * 60)

    # 1. Vault
    print(f"\n[1/3] Vault")
    vault = load_vault(force_refresh=args.refresh_vault)
    client = StreamCornerClient(vault)

    # 2. Provider
    if args.providers == "all":
        providers = list(PROVIDERS.items())
    else:
        names = [p.strip() for p in args.providers.split(",")]
        providers = [(p, PROVIDERS[p]) for p in names if p in PROVIDERS]

    print(f"\n[2/3] Fetch {len(providers)} provider")
    all_items = []
    os.makedirs(args.output_dir, exist_ok=True)

    for provider, key in providers:
        print(f"\n   {provider} (key={key})")
        items = fetch_provider(client, provider, key, workers=args.workers)
        if items:
            for it in items:
                it["_provider"] = provider
            all_items.extend(items)

            if args.split:
                fname = os.path.join(args.output_dir,
                                     f"streamcorner_{provider}.m3u")
                n = write_m3u(items, fname, group_prefix=provider)
                print(f"      💾 {fname} ({n})")

    # 3. Playlist finale
    print(f"\n[3/3] Playlist finale")
    total = write_m3u(all_items, os.path.join(args.output_dir, args.output))
    print(f"\n   💾 {args.output}: {total} stream unici")

    # Salva JSON per usi futuri
    json_path = os.path.join(args.output_dir, "streamcorner_data.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(all_items, f, indent=2, ensure_ascii=False, default=str)
    print(f"   💾 {json_path}")

    print("\n" + "=" * 60)
    print(f"✅ COMPLETATO: {total} stream")
    print("=" * 60)


if __name__ == "__main__":
    main()