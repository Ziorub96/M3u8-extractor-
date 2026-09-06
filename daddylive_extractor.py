# daddylive_mixed_extractor.py
# player5.json  -> solo whitelist top calcio
# player2/6/14  -> tutti i canali
# eventi live   -> opzionale
import requests
import re
import json
import base64
import math
import time
import random
from urllib.parse import urljoin

BASE_URL = "https://daddylive.app"
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
]

# Canali da estrarre da player5.json (whitelist)
WHITELIST_CHANNELS = [
    "CBS Sports Golazo US", "Canale 5 IT", "Italia 1 IT", "Rai 1 IT", "Rai Sport IT",
    "RSI La 1 CH", "RSI La 2 CH", "Starzplay Sports 1 AE", "Starzplay Sports 2 AE",
    "LaLiga TV GB", "Premier Sports 1 GB", "Premier Sports 2 GB", "Sky Sports Football GB",
    "Sky Sports Main Event GB", "Sky Sports Premier League GB", "TNT Sports 1 GB",
    "TNT Sports 2 GB", "TNT Sports 3 GB", "TNT Sports 4 GB", "TNT Sports 5 GB",
    "TNT Sports 6 GB", "Viaplay Sports 1 GB", "Viaplay Sports 2 GB",
    "Canal FR", "Canal Foot FR", "Canal Live 3 FR", "Canal Live 4 FR", "Canal Live 5 FR",
    "Canal Premier League FR", "Canal Sport FR", "Canal Sport360 FR", "RMC Sport 1 FR",
    "RMC Sport 2 FR", "beIN SPORTS 1 FR", "beIN SPORTS 2 FR", "beIN SPORTS 3 FR",
    "beIN SPORTS MAX 4 FR", "beIN SPORTS MAX 5 FR",
    "DAZN F1 ES", "DAZN LaLiga ES", "DAZN LaLiga 2 ES", "Real Madrid TV ES",
    "DAZN 1 DE", "DAZN 2 DE", "Sky Sport 1 DE", "Sky Sport 1 AT", "Sky Sport 2 AT",
    "Sky Sport 2 DE", "Sky Sport Bundesliga 1 DE", "Sky Sport Bundesliga 2 DE",
    "Sky Sport Bundesliga 3 DE", "Sky Sport Bundesliga 4 DE", "Sky Sport Bundesliga 5 DE",
    "Sky Sport Premier League DE", "SportDigital Fussball DE",
    "Canal Sport 2 PL", "Canal Sport 3 PL", "Canal Sport 4 PL", "Canal Sport 5 PL",
    "Eleven Sports 1 PL", "Eleven Sports 2 PL", "Eleven Sports 3 PL", "Eleven Sports 4 PL",
    "Polsat Sport Premium 1 PL", "Polsat Sport Premium 2 PL", "TVP Sport PL",
    "Benfica TV PT", "Canal 11 PT", "DAZN 2 PT", "DAZN 3 PT", "DAZN 4 PT", "DAZN 5 PT",
    "DAZN 6 PT", "Sport TV 1 PT", "Sport TV 2 PT", "Sport TV 3 PT", "Sport TV 4 PT", "Sport TV 5 PT",
    "ESPN NL", "ESPN 2 NL", "ESPN 3 NL", "Ziggo Sport 2 NL", "Ziggo Sport 3 NL",
    "Ziggo Sport 4 NL", "Ziggo Sport 5 NL", "Ziggo Sport 6 NL",
    "ESPN Deportes US", "ESPN Premium AR", "FOX Deportes US", "FOX Soccer Plus US",
    "Premiere 1 BR", "Premiere 2 BR", "Premiere 3 BR", "Premiere 4 BR",
    "TNT Sports CL", "TUDN US", "TyC Sports AR", "Univision US", "beIN SPORTS US",
    "Abu Dhabi Sports 1 AE", "Match Football 1 RU", "Match Football 2 RU", "Match Football 3 RU",
    "Match Premier RU", "V Sport Football SE", "beIN SPORTS 1 TR", "beIN SPORTS 2 TR",
    "beIN SPORTS 3 TR", "beIN SPORTS 4 SA", "beIN SPORTS 5 SA", "beIN SPORTS 6 SA",
    "beIN SPORTS 7 SA", "beIN SPORTS 8 SA", "beIN SPORTS 9 SA"
]

# File da processare
PLAYER_FILES_FULL = ["player2.json", "player6.json", "player14.json"]   # tutti i canali
PLAYER_FILE_WHITELIST = "player5.json"                                   # solo whitelist

# Includi eventi live?
INCLUDE_EVENTS = True

# Session e contatore globale
session = requests.Session()
TOTAL_REQUESTS = 0

def fetch_url(url, headers=None, timeout=15, retries=2, backoff=12.0):
    global TOTAL_REQUESTS
    TOTAL_REQUESTS += 1

    if TOTAL_REQUESTS % 15 == 0:
        print(f"⏸️ Pausa preventiva di 12 secondi (tot richieste: {TOTAL_REQUESTS})...")
        time.sleep(12)
    else:
        time.sleep(random.uniform(0.8, 1.5))

    if headers is None:
        headers = {"User-Agent": random.choice(USER_AGENTS)}
    else:
        headers = headers.copy()
        headers.setdefault("User-Agent", random.choice(USER_AGENTS))

    for attempt in range(retries):
        try:
            r = session.get(url, headers=headers, timeout=timeout)
            if r.status_code == 429:
                wait_time = backoff * (2 ** attempt) + random.uniform(1, 3)
                print(f"⚠️ 429 Rate limited. Attendo {wait_time:.1f}s... (tentativo {attempt+1}/{retries})")
                time.sleep(wait_time)
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if attempt == retries - 1:
                print(f"❌ Errore definitivo su {url}: {e}")
                return None
            time.sleep(3)
    return None

def base64_decode_padded(s):
    s = s.replace('-', '+').replace('_', '/')
    padding = '=' * (-len(s) % 4)
    s += padding
    decoded_bytes = base64.b64decode(s)
    try:
        return decoded_bytes.decode('utf-8')
    except UnicodeDecodeError:
        return decoded_bytes.decode('latin-1')

def extract_m3u8_url_channel(html):
    src_match = re.search(r"source:\{src:([A-Za-z_$][A-Za-z0-9_$]*),format:'hls'\}", html)
    if not src_match:
        return None
    var_name = src_match.group(1)

    pattern = rf"var\s+{re.escape(var_name)}\s*=\s*(.+?);"
    match = re.search(pattern, html, re.DOTALL)
    if not match:
        return None
    expr = match.group(1)

    func_match = re.match(r'\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*\(', expr)
    if not func_match:
        return None
    func_name = func_match.group(1)

    args = re.findall(rf"{re.escape(func_name)}\(([A-Za-z_$][A-Za-z0-9_$]*)\)", expr)
    if not args:
        return None

    decoded_parts = []
    for arg in args:
        var_def = re.search(rf"var\s+{re.escape(arg)}\s*=\s*'([^']*)';", html)
        if not var_def:
            return None
        val = var_def.group(1)
        dec = base64_decode_padded(val)
        decoded_parts.append(dec)

    return ''.join(decoded_parts)

def resolve_channel_stream(php_url):
    headers = {"User-Agent": random.choice(USER_AGENTS), "Referer": BASE_URL}
    r = fetch_url(php_url, headers=headers)
    if not r:
        return None
    return extract_m3u8_url_channel(r.text)

def decode_econfig(econfig_str):
    decoded = base64_decode_padded(econfig_str)
    length = len(decoded)
    chunk_size = math.ceil(length / 4)
    parts = []
    pos = 0
    for _ in range(4):
        part = decoded[pos:pos+chunk_size]
        pos += chunk_size
        part_modified = part[:3] + part[4:]
        parts.append(part_modified)
    order = [1, 3, 0, 2]
    ordered_parts = [parts[i] for i in order]
    joined = ''.join(ordered_parts)
    decoded_joined = base64_decode_padded(joined)
    json_str = base64_decode_padded(decoded_joined)
    return json.loads(json_str)

def resolve_event_stream(embed_url):
    headers = {"User-Agent": random.choice(USER_AGENTS), "Referer": BASE_URL}
    r1 = fetch_url(embed_url, headers=headers)
    if not r1:
        return None
    iframe_match = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', r1.text, re.IGNORECASE)
    if not iframe_match:
        return None
    nontongo_url = iframe_match.group(1)
    if nontongo_url.startswith('//'):
        nontongo_url = 'https:' + nontongo_url

    headers2 = {"User-Agent": random.choice(USER_AGENTS), "Referer": embed_url}
    r2 = fetch_url(nontongo_url, headers=headers2)
    if not r2:
        return None
    play_movie_match = re.search(r"src=['\"]([^'\"]*play-movie\.php\?id=\d+)['\"]", r2.text)
    if not play_movie_match:
        return None
    play_movie_url = play_movie_match.group(1)
    if play_movie_url.startswith('/'):
        play_movie_url = urljoin(nontongo_url, play_movie_url)

    headers3 = {"User-Agent": random.choice(USER_AGENTS), "Referer": nontongo_url, "X-Requested-With": "XMLHttpRequest"}
    r3 = fetch_url(play_movie_url, headers=headers3)
    if not r3:
        return None
    dlive_match = re.search(r'https://dlive\.sx/[^"\'\s]+', r3.text)
    if not dlive_match:
        return None
    dlive_url = dlive_match.group(0)

    headers4 = {"User-Agent": random.choice(USER_AGENTS), "Referer": play_movie_url}
    r4 = fetch_url(dlive_url, headers=headers4)
    if not r4:
        return None
    barecrop_match = re.search(r'<iframe[^>]+src=["\']([^"\']*barecrop\.net[^"\']*)["\']', r4.text, re.IGNORECASE)
    if not barecrop_match:
        return None
    barecrop_url = barecrop_match.group(1)
    if barecrop_url.startswith('//'):
        barecrop_url = 'https:' + barecrop_url

    headers5 = {"User-Agent": random.choice(USER_AGENTS), "Referer": dlive_url}
    r5 = fetch_url(barecrop_url, headers=headers5)
    if not r5:
        return None
    econfig_match = re.search(r"window\._econfig='([^']+)'", r5.text)
    if not econfig_match:
        return None
    econfig_str = econfig_match.group(1)
    try:
        config = decode_econfig(econfig_str)
        return config.get('stream_url') or config.get('stream_url_nop2p')
    except:
        return None

def process_entry(name, php_url, group_title):
    print(f"   ⏳ {name}")
    stream_url = resolve_channel_stream(php_url)
    if stream_url:
        print("      ✅")
        return (name, stream_url, group_title)
    else:
        print("      ❌ non risolto")
        return None

def main():
    all_entries = []

    # === PLAYER5 (whitelist) ===
    print("📡 Estraggo canali whitelist da player5.json...")
    data = fetch_url(f"{BASE_URL}/player/{PLAYER_FILE_WHITELIST}")
    if data:
        try:
            entries = data.json()
        except:
            entries = []
        whitelist_set = set(WHITELIST_CHANNELS)
        print(f"📁 {PLAYER_FILE_WHITELIST}: {len(entries)} voci")
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get('name') or ''
            if name not in whitelist_set:
                continue
            php_url = None
            for key in ['url', 'url1', 'url2', 'url3']:
                val = entry.get(key)
                if val and isinstance(val, str) and val.startswith('http'):
                    php_url = val
                    break
            if not php_url:
                continue
            result = process_entry(name, php_url, "Top Calcio")
            if result:
                all_entries.append(result)
    
    # === PLAYER2/6/14 (tutti i canali) ===
    for pfile in PLAYER_FILES_FULL:
        print(f"\n📡 Estraggo tutti i canali da {pfile}...")
        data = fetch_url(f"{BASE_URL}/player/{pfile}")
        if not data:
            continue
        try:
            entries = data.json()
        except:
            continue
        print(f"📁 {pfile}: {len(entries)} voci")
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get('name') or entry.get('title') or 'Senza nome'
            php_url = None
            for key in ['url', 'url1', 'url2', 'url3']:
                val = entry.get(key)
                if val and isinstance(val, str) and val.startswith('http'):
                    php_url = val
                    break
            if not php_url:
                continue
            result = process_entry(name, php_url, "Altri Canali")
            if result:
                all_entries.append(result)

    # === EVENTI LIVE (opzionale) ===
    if INCLUDE_EVENTS:
        print("\n🎯 Estraggo eventi live...")
        events_data = fetch_url(f"{BASE_URL}/api/events")
        if events_data:
            try:
                events_json = events_data.json()
                popular = events_json.get('popular_events', [])
                print(f"   Trovati {len(popular)} eventi popolari")
                for ev in popular:
                    event_name = ev.get('event', 'Evento')
                    category = ev.get('category', '')
                    # Includi tutti gli eventi? Meglio filtrare sportivi per coerenza
                    if not any(k in category.lower() for k in ['sport', 'football', 'soccer', 'tennis', 'basket', 'fight', 'ufc', 'boxing']):
                        continue
                    for ch in ev.get('channels', []):
                        embed_url = ch.get('url')
                        if not embed_url:
                            continue
                        ch_name = ch.get('channel_name', 'Link')
                        print(f"   ⏳ {event_name} - {ch_name}")
                        stream_url = resolve_event_stream(embed_url)
                        if stream_url:
                            all_entries.append((f"{event_name} [{ch_name}]", stream_url, "Eventi Live"))
                            print("      ✅")
                        else:
                            print("      ❌ non risolto")
            except Exception as e:
                print(f"❌ Errore eventi: {e}")

    # === DEDUPLICA ===
    seen = set()
    unique = []
    for name, url, group in all_entries:
        if url not in seen:
            seen.add(url)
            unique.append((name, url, group))

    print(f"\n🔗 Totale flussi: {len(unique)}")

    output_file = "daddylive_mixed.m3u"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for name, url, group in unique:
            clean = name.replace('"','').replace('\n','').replace(',','-')
            f.write(f'#EXTINF:-1 group-title="{group}",{clean}\n')
            f.write(url + "\n")

    print(f"✅ Salvato {output_file} con {len(unique)} canali/eventi")

if __name__ == "__main__":
    main()