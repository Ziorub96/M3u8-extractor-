#!/usr/bin/env python3
# daddylive_extractor_github.py – Versione Definitiva (Massima Resa / Anti-Timeout)
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

ONLY_SPORT = True
MAX_WORKERS = 16  # Ottimale per l'I/O bound su GitHub Actions
OUTPUT_FILE = "daddylive_streams.m3u"

# Timeout bilanciati: (timeout_connessione, timeout_lettura)
FAST_TIMEOUT = (5, 10)

BLOCKED_URLS = [
    "http://41.205.93.154",
]

USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
]

# Ampliata per non perdere nessun broadcaster sportivo importante
SPORT_KEYWORDS = [
    "sport", "espn", "sky sports", "premier league", "nfl", "nba", "nhl", "mlb",
    "ufc", "boxing", "football", "calcio", "soccer", "tennis", "golf", "rugby",
    "cricket", "f1", "motogp", "bundesliga", "serie a", "la liga", "champions",
    "europa league", "dazn", "bein", "movistar", "canale sport", "sport tv",
    "fox sports", "eleven", "premier", "nba tv", "nfl network", "wwe", "aew",
    "tnt sports", "rai sport", "ziggo sport", "polsat sport", "sky sport", 
    "canal sport", "rmc sport", "v sport", "match football", "supersport", 
    "arena sport", "optus", "tsn", "sportsnet", "bally", "viaplay", "setanta"
]

SPORT_KEYWORDS_PLAYER9 = SPORT_KEYWORDS + [
    "win sports", "dsports", "nascar", "indycar", "red bull tv", "fight", "wrc",
    "motoamerica", "supercross", "extreme", "outdoor", "poker", "darts", "snooker",
    "pool", "cycling", "triathlon", "marathon", "olympics", "world cup", "euro",
    "copa", "libertadores", "sudamericana", "concacaf", "afc", "uefa", "fifa",
    "wnba", "nhl network", "mlb network", "motorsport", "formula", "racing"
]

BLACKLIST_WORDS_PLAYER9 = [
    "news", "cinema", "movie", "film", "kids", "music", "religion", "fashion",
    "bloomberg", "bbc news", "cartoon", "anime", "comedy", "drama", "documentary"
]

resolved_cache = {}

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
                timeout=FAST_TIMEOUT
            )
            if r.status_code == 200 and r.text.strip().startswith('['):
                print(f"✅ Dominio attivo: {base}")
                return base
        except Exception:
            continue
    print("❌ Nessun dominio base funzionante trovato")
    return None

def fetch_url(url, headers=None, timeout=FAST_TIMEOUT, retries=2):
    """Fetch con doppio tentativo per micro-down del server."""
    for attempt in range(retries):
        session = requests.Session()
        if headers is None:
            headers = get_headers()
        session.headers.update(headers)

        try:
            r = session.get(url, timeout=timeout)
            if r.status_code == 200:
                return r
            # Se è un errore temporaneo, riprova
            elif r.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                time.sleep(0.5)
                continue
            else:
                return None # Per 404, 403, ecc. scarta subito
        except requests.RequestException:
            if attempt == retries - 1:
                return None
            time.sleep(0.5)
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

    try:
        r = session.get(iframe_url, headers=get_headers(page_url), timeout=FAST_TIMEOUT)
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

def generic_m3u8_fallback(html: str) -> str | None:
    """Fallback generico con filtro anti-spam/ads."""
    candidates = re.findall(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', html)
    for candidate in candidates:
        if not any(bad in candidate.lower() for bad in ["doubleclick", "google", "ads", "analytics"]):
            return candidate
    return None

def resolve_stream(name: str, url: str) -> tuple:
    if url in resolved_cache:
        return (name, resolved_cache[url], "cached")

    session = requests.Session()
    time.sleep(random.uniform(0, 0.1))  # Micro-delay anti-burst

    r = None
    error_reason = "unknown"

    # Doppio tentativo per recuperare i canali lenti/bloccati
    for attempt in range(2):
        try:
            r_temp = session.get(url, headers=get_headers(BASE_URL), timeout=FAST_TIMEOUT)
            if r_temp.status_code == 200:
                r = r_temp
                break
            elif r_temp.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                error_reason = f"HTTP {r_temp.status_code}"
                time.sleep(0.5)
                continue
            else:
                return (name, None, f"HTTP {r_temp.status_code}")
        except requests.Timeout:
            error_reason = "timeout"
            if attempt == 0:
                time.sleep(0.5)
                continue
        except requests.ConnectionError:
            error_reason = "connection error"
            if attempt == 0:
                time.sleep(0.5)
                continue
        except Exception as e:
            return (name, None, f"error: {type(e).__name__}")

    if not r:
        return (name, None, error_reason)

    html = r.text

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

    if not stream:
        stream = generic_m3u8_fallback(html)

    if stream and stream.startswith("http"):
        resolved_cache[url] = stream
        return (name, stream, "OK")
    else:
        return (name, None, "no stream found in HTML")

def fetch_cdnlivetv_events():
    api_endpoints = [
        "https://api.cdnlivetv.is/api/v1/events/sports/?user=cdnlivetv&plan=free",
        "https://cdnlivetv.is/api/events/sports",
        "https://daddylive.mp/schedule/schedule-stream.json"
    ]
    
    for url in api_endpoints:
        try:
            r = fetch_url(url, headers=get_headers("https://cdnlivetv.is/"), timeout=FAST_TIMEOUT, retries=2)
            if r is None:
                continue
            data = r.json()
            events = data if isinstance(data, list) else data.get("events", [])
            valid_events = [ev for ev in events if isinstance(ev, dict) and (ev.get("homeTeam") or ev.get("channels") or ev.get("event"))]
            if valid_events:
                return valid_events
        except Exception:
            continue
    return []

def extract_event_streams(event_obj):
    streams = []
    channels = event_obj.get("channels") or []
    for ch in channels:
        if isinstance(ch, dict):
            for key in ("url", "stream_url", "link", "embed_url", "embed"):
                val = ch.get(key)
                if val and str(val).startswith("http"):
                    streams.append(val)
                    break
    return streams

def download_player_json(pfile, base_url):
    r = fetch_url(f"{base_url}/player/{pfile}", headers=get_headers(), timeout=FAST_TIMEOUT, retries=2)
    if r is None:
        return (pfile, None)
    try:
        return (pfile, r.json())
    except Exception:
        return (pfile, None)

def main():
    global BASE_URL

    print("🔍 Cerco dominio attivo...")
    BASE_URL = find_working_base()
    if not BASE_URL:
        return

    all_candidates = []
    results = []

    player_files_to_download = PLAYER_FILES + [PLAYER9_FILE]
    print("\n📡 Scarico le liste player in parallelo...")
    with ThreadPoolExecutor(max_workers=len(player_files_to_download)) as pool:
        futures = [pool.submit(download_player_json, pf, BASE_URL) for pf in player_files_to_download]
        for future in as_completed(futures):
            pfile, entries = future.result()
            if entries is None:
                continue
            count_sport = 0
            for e in entries:
                if not isinstance(e, dict):
                    continue
                name = e.get("name") or e.get("title") or "Senza nome"
                use_p9 = (pfile == PLAYER9_FILE)
                if not is_sport(name, use_player9_filter=use_p9):
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

    print("\n📡 Scarico eventi sportivi da API Live TV...")
    football_events = fetch_cdnlivetv_events()
    print(f"   Eventi sportivi trovati: {len(football_events)}")
    for ev in football_events:
        home = ev.get("homeTeam") or ev.get("event") or "?"
        away = ev.get("awayTeam", "")
        tournament = ev.get("tournament") or ev.get("category") or "Calcio/Sport"
        title = f"[{tournament}] {home} vs {away}" if away else f"[{tournament}] {home}"
        
        streams = extract_event_streams(ev)
        if streams:
            results.append((title, streams[0]))

    candidates_to_resolve = [
        (name, url) for name, url in all_candidates
        if not any(blocked in url for blocked in BLOCKED_URLS)
    ]

    print(f"\n🎯 Canali da risolvere: {len(candidates_to_resolve)}")
    print(f"🚀 Avvio con {MAX_WORKERS} worker (Timeout {FAST_TIMEOUT}s, 1 retry per channel)...\n")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(resolve_stream, name, url): (name, url)
            for name, url in candidates_to_resolve
        }
        done = 0
        total = len(futures)
        for fut in as_completed(futures):
            done += 1
            name, _ = futures[fut]
            try:
                res = fut.result()
                if res[1]:  # Stream OK
                    results.append((res[0], res[1]))
                    print(f"[{done}/{total}] ✅ {res[0]}")
                else:
                    print(f"[{done}/{total}] ❌ {res[0]} ({res[2]})")
            except Exception as e:
                print(f"[{done}/{total}] ❌ {name} (error: {e})")

    # Deduplicazione pulita: associa l'URL allo stream (scartando i duplicati invisibili)
    final_entries = {}
    for name, stream in results:
        if stream not in final_entries:
            final_entries[stream] = name

    print(f"\n🔗 Flussi unici trovati: {len(final_entries)}")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for stream, name in final_entries.items():
            clean = name.replace('"', '').replace(',', ' -').replace('\n', '')
            f.write(f'#EXTINF:-1 group-title="DaddyLive Sport",{clean}\n')
            f.write(stream + "\n")

    print(f"✅ Salvato → {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
