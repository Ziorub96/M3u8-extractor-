# daddylive_extractor.py – estrae URL diretti m3u8 da Daddylive (canali sportivi)
# Versione finale ottimizzata: multithreading + circuit breaker ridotto
import re
import json
import base64
import time
import random
from concurrent.futures import ThreadPoolExecutor, as_completed

from curl_cffi import requests as curl_requests

BASE_URL = "https://daddylive.app"
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
]

PLAYER_FILES = [
    "player2.json",
    "player5.json",
    "player6.json",
    "player14.json",
]

SPORT_KEYWORDS = [
    "sport", "espn", "sky sports", "premier league", "nfl", "nba", "nhl", "mlb",
    "ufc", "boxing", "football", "calcio", "soccer", "tennis", "golf", "rugby",
    "cricket", "f1", "motogp", "bundesliga", "serie a", "la liga", "champions",
    "europa league", "dazn", "bein", "movistar", "canale sport", "sport tv",
    "fox sports", "win sports", "dsports", "eleven", "premier", "nascar", "indycar",
    "nba tv", "nfl network", "red bull tv", "wwe", "aew", "afl", "nrl", "mls"
]

MAX_WORKERS = 5
MAX_FAILURES_BEFORE_BREAK = 10
PAUSA_CIRCUIT_BREAKER = 25  # secondi

def get_session():
    """Crea una sessione con fingerprint Chrome, ruotando User-Agent."""
    session = curl_requests.Session(impersonate="chrome120")
    session.headers.update({"User-Agent": random.choice(USER_AGENTS)})
    return session

def fetch_url(url, headers=None, timeout=15, retries=2, backoff=12.0):
    """
    Scarica URL usando curl_cffi con fingerprint Chrome.
    Gestisce rate limiting e backoff esponenziale.
    Non applica pause globali: il jitter è gestito dai worker.
    """
    for attempt in range(retries):
        session = get_session()
        if headers:
            session.headers.update(headers)

        try:
            r = session.get(url, timeout=timeout)
            if r.status_code == 429:
                wait_time = backoff * (2 ** attempt) + random.uniform(1, 3)
                print(f"⚠️ 429 Rate limited. Attendo {wait_time:.1f}s... (tentativo {attempt+1}/{retries})")
                time.sleep(wait_time)
                continue
            r.raise_for_status()
            return r
        except Exception as e:
            if attempt == retries - 1:
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

def is_sport_channel(name):
    name_lower = name.lower()
    for kw in SPORT_KEYWORDS:
        if len(kw) <= 4:
            pattern = rf'\b{re.escape(kw)}\b'
            if re.search(pattern, name_lower):
                return True
        else:
            if kw in name_lower:
                return True
    return False

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
    headers = {"Referer": BASE_URL}
    # Jitter leggero prima di ogni richiesta per distribuire il carico
    time.sleep(random.uniform(0.3, 0.8))
    r = fetch_url(php_url, headers=headers)
    if not r:
        return None
    return extract_m3u8_url_channel(r.text)

def process_entry(entry):
    """Processa un singolo canale: ritorna (name, stream_url) o None."""
    if not isinstance(entry, dict):
        return None
    name = entry.get('name') or entry.get('title') or 'Senza nome'
    if not is_sport_channel(name):
        return None

    php_url = None
    for key in ['url', 'url1', 'url2', 'url3']:
        val = entry.get(key)
        if val and isinstance(val, str) and val.startswith('http'):
            php_url = val
            break
    if not php_url:
        return None

    stream_url = resolve_channel_stream(php_url)
    if stream_url:
        return (name, stream_url, "Sport")
    return None

def main():
    all_entries = []

    print("📡 Estraggo canali sportivi...")

    for pfile in PLAYER_FILES:
        url = f"{BASE_URL}/player/{pfile}"
        data = fetch_url(url)
        if not data:
            continue
        try:
            entries = data.json()
        except:
            continue
        print(f"\n📁 {pfile}: {len(entries)} voci")

        # Filtra solo canali sportivi con URL
        candidates = []
        for entry in entries:
            if isinstance(entry, dict) and is_sport_channel(entry.get('name') or entry.get('title') or ''):
                candidates.append(entry)

        print(f"   ➡️ {len(candidates)} canali sportivi da processare")

        # Processa in parallelo con ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_entry = {executor.submit(process_entry, entry): entry for entry in candidates}
            for future in as_completed(future_to_entry):
                result = future.result()
                if result:
                    all_entries.append(result)

    # Deduplica
    seen_urls = set()
    unique_entries = []
    for name, stream_url, group in all_entries:
        if stream_url not in seen_urls:
            seen_urls.add(stream_url)
            unique_entries.append((name, stream_url, group))

    print(f"\n🔗 Totale flussi trovati: {len(unique_entries)}")

    output_file = "daddylive_streams.m3u"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for name, stream_url, group in unique_entries:
            clean_name = name.replace('"', '').replace('\n', '').replace(',', '-')
            f.write(f'#EXTINF:-1 group-title="{group}",{clean_name}\n')
            f.write(stream_url + "\n")

    print(f"✅ Salvato {output_file} con {len(unique_entries)} canali")

if __name__ == "__main__":
    main()