import os
from datetime import datetime, timedelta, timezone
import requests

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")

# Canali ufficiali con relativi Channel ID
CHANNELS = {
    "Serie A IT": "UCpC6Fsp7bO9K4yUaH6X9Eyg",
    "Sky Sport IT": "UC-p28jGg_8Jt18_Zvyg5_yA",
    "DAZN Italia IT": "UCm9fP7V7I786r_CgoAnIOfg",
    "Bundesliga DE": "UC5_XWfUP1yREIf6vn5BcSpQ",
    "Liga Profesional AR": "UCaI1vI06Es1q91gVgdXup1A",
    "Brasileirão Play BR": "UCV30a6eR4asA27aZ3K69x5g",
    "Ekstraklasa PL": "UC2qDLJ2jfB6T9F3b3YhGJtA",
    "Scottish Premiership UK": "UCXp8RzG8Dz5kY3f7b8aFJw",
    "Saudi Pro League AR": "UC0fO8wX1nYpK2eZ4t8QvVqA"
}

# Parole chiave per gli highlights in diverse lingue
HIGHLIGHTS_KEYWORDS = {
    "IT": ["highlights", "sintesi", "gol", "resumen", "summary"],
    "DE": ["höhepunkte", "tore", "zusammenfassung", "highlights"],
    "AR": ["resumen", "goles", "highlights", "gol"],
    "BR": ["melhores momentos", "gols", "resumo", "highlights"],
    "PL": ["skrót", "bramki", "najlepsze akcje", "highlights"],
    "UK": ["highlights", "goals", "summary"],
    "EN": ["highlights", "goals", "summary"]
}

def get_language_for_channel(channel_name):
    """Determina la lingua in base al nome del canale."""
    for lang in ["IT", "DE", "AR", "BR", "PL", "UK", "EN"]:
        if lang in channel_name:
            return lang
    return "EN"  # default

def is_highlight(title, lang):
    """Verifica se il titolo contiene parole chiave per gli highlights."""
    title_lower = title.lower()
    keywords = HIGHLIGHTS_KEYWORDS.get(lang, [])
    return any(kw in title_lower for kw in keywords)

def get_latest_videos(channel_name, channel_id):
    if not YOUTUBE_API_KEY:
        print("❌ YOUTUBE_API_KEY non configurata.")
        return []

    one_week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat().replace("+00:00", "Z")

    url = (
        "https://www.googleapis.com/youtube/v3/search"
        f"?key={YOUTUBE_API_KEY}"
        f"&channelId={channel_id}"
        "&part=snippet,id"
        "&order=date"
        "&maxResults=25"
        "&type=video"
        f"&publishedAfter={one_week_ago}"
    )

    try:
        response = requests.get(url, timeout=15).json()
        videos = []
        if "items" in response:
            for item in response["items"]:
                video_id = item.get("id", {}).get("videoId")
                if not video_id:
                    continue
                title = item["snippet"].get("title", "Senza titolo")
                lang = get_language_for_channel(channel_name)
                if is_highlight(title, lang):
                    video_url = f"https://www.youtube.com/watch?v={video_id}"
                    videos.append((title, video_url))
        return videos
    except Exception as e:
        print(f"❌ Errore per {channel_name}: {e}")
        return []

def main():
    lines = ["#EXTM3U"]

    for channel_name, channel_id in CHANNELS.items():
        print(f"📡 Controllo {channel_name}...")
        videos = get_latest_videos(channel_name, channel_id)
        print(f"   -> {len(videos)} highlights trovati")
        for title, url in videos:
            clean_title = title.replace('"', '').replace('\n', '').replace(',', '-')
            lines.append(f'#EXTINF:-1 tvg-name="{channel_name}" group-title="Calcio Gol 24/7",{channel_name} - {clean_title}')
            lines.append(url)

    with open("youtube_highlights.m3u", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print("✅ File youtube_highlights.m3u generato!")

if __name__ == "__main__":
    main()