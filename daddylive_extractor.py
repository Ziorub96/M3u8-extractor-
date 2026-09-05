# daddylive_direct_extractor.py – estrae URL diretti m3u8 da Daddylive
import requests
import re
import json

BASE_URL = "https://daddylive.app"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

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
    "europa league", "liga", "dazn", "beIN", "movistar", "canale sport", "sport tv",
    "fox sports", "win sports", "dsports", "eleven", "premier", "nascar", "indycar",
    "mls", "a-league", "j-league", "k-league", "super rugby", "nrl", "afl"
]

def fetch_json(url):
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"❌ Errore scaricando {url}: {e}")
        return None

def is_sport_channel(name):
    name_lower = name.lower()
    return any(kw in name_lower for kw in SPORT_KEYWORDS)

def resolve_stream_url(php_url):
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": BASE_URL,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        r = requests.get(php_url, headers=headers, timeout=15)
        if r.status_code != 200:
            return None
        match = re.search(r'var playbackURL\s*=\s*["\']([^"\']+)["\']', r.text)
        if match:
            return match.group(1).replace('\\/', '/')
        m3u8_urls = re.findall(r'https?://[^\s"\']+\.m3u8[^\s"\']*', r.text)
        if m3u8_urls:
            return m3u8_urls[0]
        return None
    except Exception as e:
        print(f"⚠️ Errore risolvendo {php_url}: {e}")
        return None

def main():
    all_entries = []
    for pfile in PLAYER_FILES:
        url = f"{BASE_URL}/player/{pfile}"
        data = fetch_json(url)
        if not data:
            continue
        print(f"📡 {pfile}: {len(data)} voci")
        for entry in data:
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
            direct_url = resolve_stream_url(php_url)
            if direct_url:
                all_entries.append((name, direct_url))
            else:
                print(f"   ❌ Non risolto: {name}")

    seen_urls = set()
    unique_entries = []
    for name, stream_url in all_entries:
        if stream_url not in seen_urls:
            seen_urls.add(stream_url)
            unique_entries.append((name, stream_url))

    print(f"\n🔗 Totale flussi diretti trovati: {len(unique_entries)}")

    output_file = "daddylive_direct.m3u"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for name, stream_url in unique_entries:
            clean_name = name.replace('"', '').replace('\n', '').replace(',', '-')
            f.write(f'#EXTINF:-1 group-title="Daddylive",{clean_name}\n')
            f.write(stream_url + "\n")

    print(f"✅ Salvato {output_file} con {len(unique_entries)} canali")

if __name__ == "__main__":
    main()