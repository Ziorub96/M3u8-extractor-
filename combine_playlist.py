import re
import requests
from pathlib import Path

# URL della guida TV combinata (generata da epg_combiner.py)
EPG_URL = "https://raw.githubusercontent.com/Ziorub96/M3u8-extractor-/main/combined_epg.xml"

# Mapping canale -> tvg-id per nazionalità multiple
TVG_ID_MAP = {
    "Digi Sport 1": "DigiSport1.it",
    "Digi Sport 2 HD": "DigiSport2.it",
    "Digi Sport 3": "DigiSport3.it",
    "Digi Sport 4": "DigiSport4.it",
    "Match!Ultra": "MatchTV.ru",
    "DAZN 1 Italia": "DAZN1.it",
    "Sky Sport 24": "SkySport24.it",
    "Sky Sport Calcio": "SkySportCalcio.it",
    "Sky Sport UNO": "SkySportUno.it",
    "Sky Sports Main Event": "SkySportsMainEvent.uk",
    "Sky Sports Premier League": "SkySportsPremierLeague.uk",
    "TNT Sports 1": "TNTSports1.uk",
    "TNT Sports 2": "TNTSports2.uk",
    "ESPN": "ESPN.us",
    "ESPN2": "ESPN2.us",
    "FOX Sports 1": "FoxSports1.us",
    "beIN Sports 1": "beINSports1.fr",
    "Bundesliga 1": "Bundesliga1.de",
    "Movistar Deportes": "MovistarDeportes.es",
    "Sport TV1": "SportTV1.pt",
    "Eleven Sports 1": "ElevenSports1.pl",
}

SOURCES = [
    ("DAMITV", "https://raw.githubusercontent.com/Ziorub96/M3u8-extractor-/main/damitv_events.m3u"),
    ("doms9", "https://s.id/d9M3U8"),
    ("iptv-org sports", "https://iptv-org.github.io/iptv/categories/sports.m3u"),
]

LOCAL_SOURCES = [
    ("FMHY", "fmhy_streams.m3u"),
    ("YouTube Highlights", "youtube_highlights.m3u"),
]

OUTPUT_FILE = "combined_events.m3u"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def fetch_playlist(url):
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
        r.raise_for_status()
        return r.text.splitlines()
    except Exception as e:
        print(f"❌ Errore scaricando {url}: {e}")
        return []

def fetch_local_playlist(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
    except FileNotFoundError:
        print(f"❌ File locale non trovato: {path}")
        return []

def set_group_title(extinf_line, group_title):
    if 'group-title="' in extinf_line:
        return re.sub(r'group-title="[^"]*"', f'group-title="{group_title}"', extinf_line)
    if ',' in extinf_line:
        head, tail = extinf_line.rsplit(',', 1)
        return f'{head} group-title="{group_title}",{tail}'
    return f'{extinf_line} group-title="{group_title}"'

def set_tvg_id(extinf_line, channel_name, group_title):
    if 'tvg-id="' in extinf_line:
        return extinf_line
    tvg_id = TVG_ID_MAP.get(channel_name)
    if not tvg_id:
        return extinf_line
    if 'group-title="' in extinf_line:
        return extinf_line.replace('group-title="', f'tvg-id="{tvg_id}" group-title="')
    if ',' in extinf_line:
        head, tail = extinf_line.rsplit(',', 1)
        return f'{head} tvg-id="{tvg_id}",{tail}'
    return extinf_line

def parse_m3u(lines):
    blocks = []
    current_block = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#EXTINF"):
            if current_block:
                blocks.append(current_block)
            current_block = [stripped]
        elif stripped and not stripped.startswith("#"):
            current_block.append(stripped)
            blocks.append(current_block)
            current_block = []
        elif stripped.startswith("#") and current_block:
            current_block.append(stripped)
    if current_block:
        blocks.append(current_block)
    return blocks

def main():
    all_lines = [f'#EXTM3U url-tvg="{EPG_URL}"']
    seen_urls = set()

    # Processa sorgenti remote
    for name, url in SOURCES:
        print(f"📡 Scarico {name}...")
        lines = fetch_playlist(url)
        blocks = parse_m3u(lines)
        print(f"   -> {len(blocks)} voci trovate")
        if blocks:
            all_lines.append(f"# ===== SORGENTE: {name} =====")
            for block in blocks:
                stream_url = block[-1]
                if stream_url in seen_urls:
                    continue
                seen_urls.add(stream_url)
                if block[0].startswith("#EXTINF"):
                    block[0] = set_group_title(block[0], name)
                    channel_name = block[0].split(",")[-1].strip()
                    block[0] = set_tvg_id(block[0], channel_name, name)
                all_lines.extend(block)

    # Processa sorgenti locali
    for name, path in LOCAL_SOURCES:
        print(f"📂 Leggo file locale {name}...")
        lines = fetch_local_playlist(path)
        blocks = parse_m3u(lines)
        print(f"   -> {len(blocks)} voci trovate")
        if blocks:
            all_lines.append(f"# ===== SORGENTE: {name} =====")
            for block in blocks:
                stream_url = block[-1]
                if stream_url in seen_urls:
                    continue
                seen_urls.add(stream_url)
                if block[0].startswith("#EXTINF"):
                    block[0] = set_group_title(block[0], name)
                    channel_name = block[0].split(",")[-1].strip()
                    block[0] = set_tvg_id(block[0], channel_name, name)
                all_lines.extend(block)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(all_lines))

    print(f"\n✅ Salvato {OUTPUT_FILE} con {len(seen_urls)} flussi unici")

if __name__ == "__main__":
    main()