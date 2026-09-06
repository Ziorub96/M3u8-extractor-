# daddylive_direct_extractor.py – estrae URL diretti m3u8 da Daddylive (canali sportivi + eventi live)
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

# File canali da estrarre (player2.json rimosso perché il suo host streamtp-golden1.click non risponde)
PLAYER_FILES = [
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

# Sessione HTTP riusabile e contatore globale richieste
session = requests.Session()
TOTAL_REQUESTS = 0

def fetch_url(url, headers=None, timeout=15, retries=2, backoff=12.0):
    """Scarica URL con gestione rate limiting, backoff e rotazione User-Agent."""
    global TOTAL_REQUESTS
    TOTAL_REQUESTS += 1

    # Pausa preventiva ogni 15 richieste per evitare burst
    if TOTAL_REQUESTS % 15 == 0:
        print(f"⏸️ Pausa preventiva di 12 secondi (tot richieste: {TOTAL_REQUESTS})...")
        time.sleep(12)
    else:
        # Jitter casuale per simulare comportamento umano
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
    """Decodifica base64 gestendo URL-safe e padding."""
    s = s.replace('-', '+').replace('_', '/')
    padding = '=' * (-len(s) % 4)
    s += padding
    decoded_bytes = base64.b64decode(s)
    try:
        return decoded_bytes.decode('utf-8')
    except UnicodeDecodeError:
        return decoded_bytes.decode('latin-1')

def is_sport_channel(name):
    """Verifica se un nome è sportivo, con controllo dei confini parola per keyword brevi."""
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

# ---------- RISOLUZIONE CANALI TV (cdnlivetv) ----------

def extract_m3u8_url_channel(html):
    """Estrae l'URL m3u8 da una pagina player di cdnlivetv."""
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
    """Risolve un URL di canale TV (cdnlivetv.tv) restituendo l'URL m3u8."""
    headers = {"User-Agent": random.choice(USER_AGENTS), "Referer": BASE_URL}
    r = fetch_url(php_url, headers=headers)
    if not r:
        return None
    return extract_m3u8_url_channel(r.text)

# ---------- RISOLUZIONE EVENTI LIVE (embed -> nontongo -> dlive -> barecrop) ----------

def decode_econfig(econfig_str):
    """Decodifica la stringa _econfig di barecrop.net."""
    decoded = base64_decode_padded(econfig_str)
    length = len(decoded)
    chunk_size = math.ceil(length / 4)
    parts = []
    pos = 0
    for _ in range(4):
        part = decoded[pos:pos+chunk_size]
        pos += chunk_size
        part_modified = part[:3] + part[4:]   # rimuove carattere all'indice 3
        parts.append(part_modified)
    order = [1, 3, 0, 2]
    ordered_parts = [parts[i] for i in order]
    joined = ''.join(ordered_parts)
    decoded_joined = base64_decode_padded(joined)
    json_str = base64_decode_padded(decoded_joined)   # terza decodifica
    return json.loads(json_str)

def resolve_event_stream(embed_url):
    """Risolve un URL embed.php?id=NN fino al m3u8 diretto."""
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

# ---------- FUNZIONE PRINCIPALE ----------

def main():
    all_entries = []  # (name, stream_url, group_title)

    # === CANALI SPORTIVI ===
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
        for idx, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            name = entry.get('name') or entry.get('title') or 'Senza nome'
            if not is_sport_channel(name):
                continue
            php_url = None
            for key in ['url', 'url1', 'url2', 'url3']:
                val = entry.get(key)
                if val and isinstance(val, str) and val.startswith('http'):
                    php_url = val
                    break
            if not php_url:
                continue
            print(f"   [{idx+1}/{len(entries)}] {name}")
            # Il throttling è gestito dentro fetch_url, quindi nessun sleep qui
            stream_url = resolve_channel_stream(php_url)
            if stream_url:
                all_entries.append((name, stream_url, "Sport"))
                print(f"      ✅")
            else:
                print(f"      ❌ non risolto")

    # === EVENTI LIVE SPORTIVI ===
    print("\n🎯 Estraggo eventi live sportivi...")
    events_data = fetch_url(f"{BASE_URL}/api/events")
    if events_data:
        try:
            events_json = events_data.json()
            popular = events_json.get('popular_events', [])
            print(f"   Trovati {len(popular)} eventi popolari")
            for ev in popular:
                event_name = ev.get('event', 'Evento sconosciuto')
                category = ev.get('category', '')
                # Controlla sia categoria sia nome evento per non scartare eventi sportivi
                if not (is_sport_channel(category) or is_sport_channel(event_name)):
                    continue
                channels = ev.get('channels', [])
                for ch in channels:
                    ch_name = ch.get('channel_name', 'Link')
                    embed_url = ch.get('url')
                    if not embed_url:
                        continue
                    print(f"   ⏳ {event_name} - {ch_name}")
                    # Throttling già in fetch_url
                    stream_url = resolve_event_stream(embed_url)
                    if stream_url:
                        display_name = f"{event_name} [{ch_name}]"
                        all_entries.append((display_name, stream_url, "Eventi Sportivi"))
                        print(f"      ✅")
                    else:
                        print(f"      ❌ non risolto")
        except Exception as e:
            print(f"❌ Errore parsing eventi: {e}")

    # === DEDUPLICA ===
    seen_urls = set()
    unique_entries = []
    for name, stream_url, group in all_entries:
        if stream_url not in seen_urls:
            seen_urls.add(stream_url)
            unique_entries.append((name, stream_url, group))

    print(f"\n🔗 Totale flussi trovati: {len(unique_entries)}")

    output_file = "daddylive_direct.m3u"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for name, stream_url, group in unique_entries:
            clean_name = name.replace('"', '').replace('\n', '').replace(',', '-')
            f.write(f'#EXTINF:-1 group-title="{group}",{clean_name}\n')
            f.write(stream_url + "\n")

    print(f"✅ Salvato {output_file} con {len(unique_entries)} canali/eventi")

if __name__ == "__main__":
    main()