# daddylive_extractor.py
import requests
import json

BASE_URL = "https://daddylive.app"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def fetch_json(url):
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"❌ Errore scaricando {url}: {e}")
        return None

def main():
    player_files = [
        "player2.json",
        "player5.json",
        "player6.json",
        "player9.json",
        "player10.json",
        "player14.json",
    ]

    all_entries = []
    for pfile in player_files:
        url = f"{BASE_URL}/player/{pfile}"
        data = fetch_json(url)
        if not data:
            continue
        print(f"📡 {pfile}: {len(data)} voci")
        for entry in data:
            if not isinstance(entry, dict):
                continue
            name = entry.get('name') or entry.get('title') or 'Senza nome'
            urls = []
            for key in ['url', 'url1', 'url2', 'url3']:
                val = entry.get(key)
                if val and isinstance(val, str) and val.startswith('http'):
                    urls.append(val)
            if not urls:
                continue
            for stream_url in urls:
                all_entries.append((name, stream_url))

    # Deduplica per URL
    seen_urls = set()
    unique_entries = []
    for name, stream_url in all_entries:
        if stream_url not in seen_urls:
            seen_urls.add(stream_url)
            unique_entries.append((name, stream_url))

    print(f"\n🔗 Totale flussi unici trovati: {len(unique_entries)}")

    # Scrive il file M3U
    output_file = "daddylive_streams.m3u"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for name, stream_url in unique_entries:
            clean_name = name.replace('"', '').replace('\n', '').replace(',', '-')
            f.write(f'#EXTINF:-1 group-title="Daddylive",{clean_name}\n')
            f.write(stream_url + "\n")

    print(f"✅ Salvato {output_file} con {len(unique_entries)} canali")

if __name__ == "__main__":
    main()