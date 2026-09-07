#!/usr/bin/env python3
# daddylive_extractor.py – estrae URL diretti m3u8 da Daddylive
# Versione unificata con retry su 502/503, blacklist URL obsoleti, multithreading
# Ora include anche player9.json con filtro sportivo più selettivo
# e le partite di calcio da CDN Live TV API.

import re
import json
import base64
import math
import time
import random
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

BASE_CANDIDATES = [
    "https://daddylive.app",
    "https://dlhd.so",
    "https://daddylive.sx",
    "https://daddylive.li",
]

PLAYER_FILES = [
    "player2.json",
    "player5.json",
    "player6.json",
    "player14.json",
]

PLAYER9_FILE = "player9.json"
MAX_PLAYER9_SPORT = 350

ONLY_SPORT = True
MAX_WORKERS = 5
REQUEST_DELAY = (0.4, 1.0)
OUTPUT_FILE = "daddylive_streams.m3u"

BLOCKED_URLS = [
    "http://41.205.93.154",
]

USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
]

SPORT_KEYWORDS = [
    "sport", "espn", "sky sports", "premier league", "nfl", "nba", "nhl", "mlb",
    "ufc", "boxing", "football", "calcio", "soccer", "tennis", "golf", "rugby",
    "cricket", "f1", "motogp", "bundesliga", "serie a", "la liga", "champions",
    "europa league", "dazn", "bein", "movistar", "canale sport", "sport tv",
    "fox sports", "eleven", "premier", "nba tv", "nfl network", "wwe", "aew",
    "tnt sports", "rai sport", "ziggo sport", "polsat sport", "sport tv",
    "sky sport", "canal sport", "rmc sport", "v sport", "match football",
]

SPORT_KEYWORDS_PLAYER9 = [
    "sport", "espn", "sky sports", "nba", "nfl", "nhl", "mlb", "ufc", "boxing",
    "football", "soccer", "tennis", "golf", "rugby", "cricket", "f1", "motogp",
    "premier league", "serie a", "la liga", "champions", "europa league",
    "bundesliga", "dazn", "bein", "movistar", "canale sport", "sport tv",
    "fox sports", "win sports", "dsports", "eleven", "nascar", "indycar",
    "nba tv", "nfl network", "red bull tv", "wwe", "aew", "fight", "wrc",
    "motoamerica", "supercross", "extreme", "outdoor", "fishing", "hunting",
    "poker", "darts", "snooker", "pool", "bowling", "cycling", "triathlon",
    "marathon", "olympics", "world cup", "euro", "copa", "libertadores",
    "sudamericana", "concacaf", "afc", "uefa", "fifa", "nba g league",
    "wnba", "nhl network", "mlb network", "golf", "tennis", "motorsport",
    "formula", "racing"
]

BLACKLIST_WORDS_PLAYER9 = [
    "news", "europe", "europa", "cinema", "movie", "film", "kids", "music",
    "religion", "shalom", "vogue", "velvet", "fashion", "makeover", "conflict",
    "spiegel", "jimjam", "inazuma", "rakuten viki", "myzen", "bloomberg",
    "bbc news", "cctv-4", "ewtn", "tv5monde", "tv5 monde", "rtve", "tve internacional",
    "animation", "cartoon", "anime", "comedy", "drama", "entertainment",
    "documentary", "history", "nature", "wild", "travel", "food", "cooking",
    "reality", "talk", "lifestyle", "wellness", "yoga", "meditation"
]

def b64d(s: str) -> bytes:
    s = s.replace('-', '+').replace('_', '/')
    s += '=' * (-len(s) % 4)
    return base64.b64decode(s)

def is_sport(name: str, use_player9_filter: bool = False) -> bool:
    if not ONLY_SPORT:
        return True
    n = name.lower()
    if use_player9_filter:
        if any(bad in n for bad in BLACKLIST_WORDS_PLAYER9):
            return False
        keywords = SPORT_KEYWORDS_PLAYER9
    else:
        keywords = SPORT_KEYWORDS

    for kw in keywords:
        if len(kw) <= 4:
            if re.search(rf'\b{re.escape(kw)}\b', n):
                return True
        elif kw in n:
            return True
    return False

def get_headers(referer: str | None = None) -> dict:
    h = {"User-Agent": random.choice(USER_AGENTS)}
    if referer:
        h["Referer"] = referer
    return h

def find_working_base() -> str | None:
    for base in BASE_CANDIDATES:
        try:
            r = requests.get(
                f"{base}/player/player5.json",
                headers=get_headers(),
                timeout=12
            )
            if r.status_code == 200 and r.text.strip().startswith('['):
                print(f"✅ Dominio attivo: {base}")
                return base
        except Exception:
            continue
    print("❌ Nessun dominio base funzionante trovato")
    return None

def fetch_url(url, headers=None, timeout=15, retries=3, backoff=12.0):
    for attempt in range(retries):
        session = requests.Session()
        if headers is None:
            headers = get_headers()
        session.headers.update(headers)

        try:
            r = session.get(url, timeout=timeout)
            if r.status_code in (429, 502, 503):
                wait_time = backoff * (2 ** attempt) + random.uniform(1, 3)
                print(f"⚠️ {r.status_code} su {url[:80]} → attendo {wait_time:.1f}s (tentativo {attempt+1}/{retries})")
                time.sleep(wait_time)
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if attempt == retries - 1:
                return None
            time.sleep(3)
    return None

def extract_player2(html: str) -> str | None:
    m = re.search(r'var\s+playbackURL\s*=\s*"([^"]+)"', html)
    if m:
        return m.group(1).replace('\\/', '/')
    return None

def extract_player5(html: str) -> str | None:
    join_match = re.search(
        r'[A-Za-z_$][\w$]*\s*=\s*((?:[A-Za-z_$][\w$]*\([A-Za-z_$][\w$]*\)\s*\+\s*)+[A-Za-z_$][\w$]*\([A-Za-z_$][\w$]*\))',
        html
    )
    if not join_match:
        return None
    args = re.findall(r'[A-Za-z_$][\w$]*\(([A-Za-z_$][\w$]*)\)', join_match.group(1))
    vars_dict = dict(re.findall(r"var\s+([A-Za-z_$][\w$]*)\s*=\s*'([^']*)'", html))
    parts = []
    for arg in args:
        if arg not in vars_dict:
            return None
        try:
            parts.append(b64d(vars_dict[arg]).decode('utf-8', errors='replace'))
        except Exception:
            return None
    return ''.join(parts) if parts else None

def extract_player6(html: str, page_url: str, session: requests.Session) -> str | None:
    iframe_m = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.I)
    if not iframe_m:
        return None

    iframe_url = iframe_m.group(1)
    if iframe_url.startswith('//'):
        iframe_url = 'https:' + iframe_url
    elif iframe_url.startswith('/'):
        iframe_url = urljoin(page_url, iframe_url)

    time.sleep(random.uniform(*REQUEST_DELAY))
    try:
        r = session.get(iframe_url, headers=get_headers(page_url), timeout=15)
        if r.status_code != 200:
            return None
    except Exception:
        return None

    econfig_m = re.search(r"window\._econfig='([^']+)'", r.text)
    if not econfig_m:
        return None

    try:
        decoded = b64d(econfig_m.group(1)).decode('utf-8', errors='replace')
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
    except Exception:
        return None
    return None

def extract_player14(html: str) -> str | None:
    arr_m = re.search(r'var\s+_qb8\s*=\s*\[([^\]]+)\]', html)
    if not arr_m:
        return None

    nums = []
    for x in arr_m.group(1).split(','):
        x = x.strip()
        if x.lstrip('-').isdigit():
            nums.append(int(x))

    sx_m = re.search(r'_sx8\s*=\s*(\d+)', html)
    vq_m = re.search(r'_vq9\s*=\s*(\d+)', html)
    sx8 = int(sx_m.group(1)) if sx_m else 61
    vq9 = int(vq_m.group(1)) if vq_m else 9

    try:
        decoded = ''.join(chr(((n ^ sx8) - vq9 + 256) & 255) for n in nums)
    except Exception:
        return None

    m = re.search(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', decoded)
    return m.group(0) if m else None

def resolve_stream(name: str, url: str) -> tuple | None:
    session = requests.Session()
    time.sleep(random.uniform(*REQUEST_DELAY))

    try:
        r = session.get(url, headers=get_headers(BASE_URL), timeout=15)
        if r.status_code != 200:
            return None
        html = r.text
    except Exception:
        return None

    stream = None

    if "cdnlivetv.tv" in url:
        stream = extract_player5(html)
    elif "streamtp-golden" in url or "global1.php" in url:
        stream = extract_player2(html)
    elif "bolaloca.my" in url:
        stream = extract_player6(html, url, session)
    elif "epiembeds.online" in url:
        stream = extract_player14(html)
    else:
        stream = (extract_player5(html) or
                  extract_player2(html) or
                  extract_player14(html) or
                  extract_player6(html, url, session))

    if stream and stream.startswith("http"):
        return (name, stream)
    return None

def fetch_cdnlivetv_events():
    """Scarica eventi sportivi (calcio) da API cdnlivetv.is."""
    url = "https://api.cdnlivetv.is/api/v1/events/sports/?user=cdnlivetv&plan=free"
    try:
        r = fetch_url(url, headers=get_headers("https://cdnlivetv.is/"), timeout=20)
        if r is None:
            return []
        data = r.json()
        events = data if isinstance(data, list) else data.get("events", [])
        football_events = []
        for ev in events:
            if ev.get("homeTeam") and ev.get("awayTeam"):
                football_events.append(ev)
        return football_events
    except Exception as ex:
        print(f"❌ Errore scaricando eventi CDN Live TV: {ex}")
        return []

def extract_event_streams(event_obj):
    """Estrae URL stream da un evento sportivo."""
    streams = []
    channels = event_obj.get("channels") or []
    for ch in channels:
        for key in ("url", "stream_url", "link"):
            val = ch.get(key)
            if val and val.startswith("http"):
                streams.append(val)
                break
        if not streams:
            for key in ("embed_url", "embed"):
                val = ch.get(key)
                if val and val.startswith("http"):
                    streams.append(val)
                    break
    return streams

def main():
    global BASE_URL

    print("🔍 Cerco dominio attivo...")
    BASE_URL = find_working_base()
    if not BASE_URL:
        return

    all_candidates = []
    # Risultati diretti (canali risolti da Daddylive)
    results = []

    print("\n📡 Scarico le liste player standard...")
    for pfile in PLAYER_FILES:
        try:
            r = fetch_url(
                f"{BASE_URL}/player/{pfile}",
                headers=get_headers(),
                timeout=20
            )
            if r is None:
                print(f"   ❌ {pfile}: fetch fallito")
                continue
            entries = r.json()
            count_sport = 0
            for e in entries:
                if not isinstance(e, dict):
                    continue
                name = e.get("name") or e.get("title") or "Senza nome"
                if not is_sport(name):
                    continue
                php_url = next(
                    (e.get(k) for k in ("url", "url1", "url2", "url3")
                     if e.get(k) and str(e.get(k)).startswith("http")),
                    None
                )
                if php_url:
                    all_candidates.append((name, php_url))
                    count_sport += 1
            print(f"   {pfile}: {len(entries)} totali → {count_sport} sport")
        except Exception as ex:
            print(f"   ❌ {pfile}: {ex}")

    # ===== PLAYER9 CON FILTRO MIGLIORATO =====
    print(f"\n📡 Scarico {PLAYER9_FILE} (limite {MAX_PLAYER9_SPORT} sportivi)...")
    try:
        r = fetch_url(f"{BASE_URL}/player/{PLAYER9_FILE}", headers=get_headers(), timeout=30)
        if r:
            entries = r.json()
            sport9 = []
            for e in entries:
                if not isinstance(e, dict):
                    continue
                name = e.get("name") or ""
                if is_sport(name, use_player9_filter=True):
                    url = e.get("url")
                    if url and url.startswith("http"):
                        sport9.append((name, url))
            sport9 = sport9[:MAX_PLAYER9_SPORT]
            all_candidates.extend(sport9)
            print(f"   player9: {len(entries)} totali → {len(sport9)} sport aggiunti")
        else:
            print("   ❌ player9: fetch fallito")
    except Exception as ex:
        print(f"   ❌ player9: {ex}")

    # ===== EVENTI CALCIO DA CDN LIVE TV API =====
    print("\n📡 Scarico partite di calcio da CDN Live TV API...")
    football_events = fetch_cdnlivetv_events()
    print(f"   Partite di calcio trovate: {len(football_events)}")
    for ev in football_events:
        home = ev.get("homeTeam", "?")
        away = ev.get("awayTeam", "?")
        tournament = ev.get("tournament", "Calcio")
        status = (ev.get("status") or "").lower()
        if status not in ("live", "upcoming"):
            continue
        streams = extract_event_streams(ev)
        if streams:
            display = f"[{tournament}] {home} vs {away}"
            for i, url in enumerate(streams[:1]):  # prendi solo il primo stream
                results.append((display, url))
                print(f"   ✅ {display}")
        else:
            print(f"   ❌ {home} vs {away}: nessuno stream")

    all_candidates = [
        (name, url) for name, url in all_candidates
        if not any(blocked in url for blocked in BLOCKED_URLS)
    ]

    print(f"\n🎯 Canali da risolvere: {len(all_candidates)}")
    print(f"🚀 Avvio con {MAX_WORKERS} worker...\n")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(resolve_stream, name, url): name
            for name, url in all_candidates
        }
        done = 0
        total = len(futures)
        for fut in as_completed(futures):
            done += 1
            name = futures[fut]
            try:
                res = fut.result()
                if res:
                    results.append(res)
                    print(f"[{done}/{total}] ✅ {res[0]}")
                else:
                    print(f"[{done}/{total}] ❌ {name}")
            except Exception:
                print(f"[{done}/{total}] ❌ {name}")

    seen = set()
    unique = []
    for name, url in results:
        if url not in seen:
            seen.add(url)
            unique.append((name, url))

    print(f"\n🔗 Flussi unici trovati: {len(unique)}")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for name, url in unique:
            clean = name.replace('"', '').replace(',', ' -').replace('\n', '')
            f.write(f'#EXTINF:-1 group-title="DaddyLive Sport",{clean}\n')
            f.write(url + "\n")

    print(f"✅ Salvato → {OUTPUT_FILE}")

if __name__ == "__main__":
    main()