# daddylive_extractor.py – estrae solo canali sportivi
import requests
import json

BASE_URL = "https://daddylive.app"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Parole chiave per canali sportivi (in minuscolo)
SPORT_KEYWORDS = [
    "sport", "espn", "sky sports", "premier league", "nfl", "nba", "nhl", "mlb",
    "ufc", "boxing", "football", "calcio", "soccer", "tennis", "golf", "rugby",
    "cricket", "f1", "motogp", "bundesliga", "serie a", "la liga", "champions",
    "europa league", "liga", "dazn", "beIN", "movistar", "canale sport", "sport tv",
    "fox sports", "win sports", "dsports", "eleven", "premier", "nascar", "indycar",
    "mls", "a-league", "j-league", "k-league", "super rugby", "nrl", "afl"
]

# Solo i file player che contengono sport
PLAYER_FILES = [
    "player2.json",
    "player5.json",
    "player6.json",
    "player14.json",
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
            urls = []
            for key in ['url', 'url1', 'url2', 'url3']:
                val = entry.get(key)
                if val and isinstance(val, str) and val.startswith('http'):
                    urls.append(val)
            if not urls:
                continue
            for stream_url in urls:
                all_entries.append((name, stream_url))

    seen_urls = set()
    unique_entries = []
    for name, stream_url in all_entries:
        if stream_url not in seen_urls:
            seen_urls.add(stream_url)
            unique_entries.append((name, stream_url))

    print(f"\n🔗 Totale flussi sportivi unici: {len(unique_entries)}")

    output_file = "daddylive_streams.m3u"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for name, stream_url in unique_entries:
            clean_name = name.replace('"', '').replace('\n', '').replace(',', '-')
            f.write(f'#EXTINF:-1 group-title="Daddylive Sport",{clean_name}\n')
            f.write(stream_url + "\n")

    print(f"✅ Salvato {output_file} con {len(unique_entries)} canali sportivi")

if __name__ == "__main__":
    main()