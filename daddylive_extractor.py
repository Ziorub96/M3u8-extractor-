#!/usr/bin/env python3
# daddylive_extractor.py - Ottimizzato per Samsung TV IPTV

import re
import base64
import math
import time
import random
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

BASE_CANDIDATES = [
    "https://daddylive.app",
    "https://dlhd.so",
    "https://dlive.sx",
    "https://dlhd.st",
    "https://daddylive.li",
]

PLAYER_FILES = ["player2.json", "player5.json", "player6.json", "player14.json"]
ONLY_SPORT = True
MAX_WORKERS = 6
REQUEST_DELAY = (0.35, 0.9)
OUTPUT_FILE = "daddylive_streams.m3u"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
]

SPORT_KEYWORDS = [
    "sport", "espn", "sky sports", "premier league", "nfl", "nba", "nhl", "mlb",
    "ufc", "boxing", "football", "calcio", "soccer", "tennis", "golf", "rugby",
    "cricket", "f1", "motogp", "bundesliga", "serie a", "la liga", "champions",
    "europa league", "dazn", "bein", "movistar", "fox sports", "eleven",
    "tnt sports", "rai sport", "sky sport", "wwe", "aew"
]

def b64d(s: str) -> bytes:
    s = s.replace('-', '+').replace('_', '/')
    s += '=' * (-len(s) % 4)
    return base64.b64decode(s)

def is_sport(name: str) -> bool:
    if not ONLY_SPORT:
        return True
    n = name.lower()
    for kw in SPORT_KEYWORDS:
        if kw in n:
            return True
    return False

def get_headers(referer: str = None) -> dict:
    h = {"User-Agent": random.choice(USER_AGENTS)}
    if referer:
        h["Referer"] = referer
        parsed = urlparse(referer)
        h["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
    return h

def find_working_base() -> str | None:
    for base in BASE_CANDIDATES:
        try:
            r = requests.get(f"{base}/player/player5.json", headers=get_headers(), timeout=10)
            if r.status_code == 200 and r.text.strip().startswith('['):
                print(f"✅ Dominio attivo: {base}")
                return base
        except:
            continue
    return None

def decode_econfig(encoded: str) -> str | None:
    try:
        decoded = b64d(encoded).decode('utf-8', errors='replace')
        length = len(decoded)
        chunk = math.ceil(length / 4)
        parts = []
        pos = 0
        for _ in range(4):
            part = decoded[pos:pos + chunk]
            pos += chunk
            parts.append(part[:3] + part[4:])
        ordered = [parts[i] for i in [1, 3, 0, 2]]
        joined = ''.join(ordered)
        d2 = b64d(joined).decode('utf-8', errors='replace')
        d3 = b64d(d2).decode('utf-8', errors='replace')
        m = re.search(r'"stream_url(?:_nop2p)?"\s*:\s*"([^"]+)"', d3)
        if m:
            return m.group(1).replace('\\/', '/')
    except:
        pass
    return None

def get_premium_token(session, playlist_url: str) -> str:
    try:
        m = re.match(r"^(?P<edge>https?://[^/]+)/(?P<slug>premium\d+)/index\.m3u8", playlist_url.split("?")[0])
        if not m:
            return playlist_url
        edge, slug = m.group("edge"), m.group("slug")
        r = session.get(
            f"{edge}/api/playlist-token?slug={slug}",
            headers={
                "User-Agent": random.choice(USER_AGENTS),
                "Referer": "https://dembed.st/",
                "Origin": "https://dembed.st"
            },
            timeout=6
        )
        tok = str(r.json().get("token") or "").strip()
        if tok:
            base, _, query = playlist_url.partition("?")
            params = [(k, v) for k, v in parse_qsl(query) if k.lower() not in ("t", "_")]
            params.append(("t", tok))
            return base + "?" + urlencode(params)
    except:
        pass
    return playlist_url

def extract_from_html(html: str, page_url: str, session: requests.Session) -> str | None:
    # 1. _econfig
    m = re.search(r"window\._econfig\s*=\s*['\"]([^'\"]{80,})['\"]", html)
    if m:
        link = decode_econfig(m.group(1))
        if link:
            return link

    # 2. atob
    m = re.search(r"window\.atob\(['\"]([^'\"]+)['\"]\)", html)
    if m:
        try:
            link = b64d(m.group(1)).decode('utf-8', errors='replace')
            if '.m3u8' in link:
                return link
        except:
            pass

    # 3. SRC
    m = re.search(r"(?:const|let|var)\s+SRC\s*=\s*[\"'](https?://[^\"']+)[\"']", html)
    if m:
        return m.group(1).strip()

    # 4. playbackURL
    m = re.search(r'var\s+playbackURL\s*=\s*"([^"]+)"', html)
    if m:
        return m.group(1).replace('\\/', '/')

    # 5. ricerca generica
    m = re.search(r'(https?://[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*)', html)
    if m:
        return m.group(1)

    # 6. iframe
    iframe_m = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.I)
    if iframe_m:
        iframe_url = iframe_m.group(1)
        if iframe_url.startswith('//'):
            iframe_url = 'https:' + iframe_url
        elif iframe_url.startswith('/'):
            iframe_url = urljoin(page_url, iframe_url)
        try:
            r = session.get(iframe_url, headers=get_headers(page_url), timeout=12)
            if r.status_code == 200:
                return extract_from_html(r.text, iframe_url, session)
        except:
            pass
    return None

def resolve_stream(name: str, url: str) -> tuple | None:
    session = requests.Session()
    time.sleep(random.uniform(*REQUEST_DELAY))
    try:
        r = session.get(url, headers=get_headers(BASE_URL), timeout=14)
        if r.status_code != 200:
            return None
        stream = extract_from_html(r.text, url, session)
        if stream and stream.startswith("http"):
            if "/premium" in stream and "index.m3u8" in stream:
                stream = get_premium_token(session, stream)
            return (name, stream)
    except:
        pass
    return None

def clean_name(name: str) -> str:
    name = re.sub(r'\[.*?\]', '', name)
    name = re.sub(r'\(.*?\)', '', name)
    name = name.replace('  ', ' ').strip()
    return name

def main():
    global BASE_URL
    print("🔍 Cerco dominio attivo...")
    BASE_URL = find_working_base()
    if not BASE_URL:
        print("❌ Nessun dominio trovato")
        return

    candidates = []
    print("\n📡 Scarico liste player...")
    for pfile in PLAYER_FILES:
        try:
            r = requests.get(f"{BASE_URL}/player/{pfile}", headers=get_headers(), timeout=15)
            if r.status_code != 200:
                continue
            for e in r.json():
                if not isinstance(e, dict):
                    continue
                name = e.get("name") or e.get("title") or "Canale"
                if not is_sport(name):
                    continue
                php_url = next((e.get(k) for k in ("url", "url1", "url2", "url3") if e.get(k) and str(e.get(k)).startswith("http")), None)
                if php_url:
                    candidates.append((name, php_url))
        except:
            pass

    print(f"🎯 Canali da risolvere: {len(candidates)}")
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(resolve_stream, name, url): name for name, url in candidates}
        for i, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            if res:
                results.append(res)
                print(f"[{i}/{len(futures)}] ✅ {clean_name(res[0])}")
            else:
                print(f"[{i}/{len(futures)}] ❌")

    # Deduplica
    seen = set()
    unique = []
    for name, url in results:
        if url not in seen:
            seen.add(url)
            unique.append((clean_name(name), url))

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for name, url in unique:
            f.write(f'#EXTINF:-1 tvg-name="{name}" group-title="DaddyLive",{name}\n')
            f.write(f"{url}\n")

    print(f"\n✅ Salvato {len(unique)} canali → {OUTPUT_FILE}")

if __name__ == "__main__":
    main()