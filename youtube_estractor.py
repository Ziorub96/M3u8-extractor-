from datetime import datetime, timedelta
import yt_dlp

# Lingue supportate con parole chiave per highlights/goal
HIGHLIGHTS_KEYWORDS = {
    "IT": ["highlights", "sintesi", "gol", "resumen", "summary"],
    "DE": ["höhepunkte", "tore", "zusammenfassung", "highlights"],
    "AR": ["resumen", "goles", "highlights", "gol"],
    "BR": ["melhores momentos", "gols", "resumo", "highlights"],
    "PL": ["skrót", "bramki", "najlepsze akcje", "highlights"],
    "UK": ["highlights", "goals", "summary"],
    "EN": ["highlights", "goals", "summary"]
}

# Elenco dei canali (URL principale del canale, senza /videos)
CHANNELS = [
    ("Serie A IT", "https://www.youtube.com/@seriea", "IT"),
    ("Sky Sport IT", "https://www.youtube.com/@SkySport", "IT"),
    ("DAZN Italia IT", "https://www.youtube.com/@DAZNItalia", "IT"),
    ("Bundesliga DE", "https://www.youtube.com/@Bundesliga", "DE"),
    ("Liga Profesional AR", "https://www.youtube.com/@LigaProfesional", "AR"),
    ("Brasileirão Play BR", "https://www.youtube.com/@Brasileirao", "BR"),
    ("Ekstraklasa PL", "https://www.youtube.com/@EkstraklasaPL", "PL"),
    ("Scottish Premiership UK", "https://www.youtube.com/@spfl", "UK"),
    ("Saudi Pro League AR", "https://www.youtube.com/@SPL", "AR"),
]

def is_highlight(title, lang):
    """Verifica se il titolo corrisponde a highlights/goal nella lingua data."""
    title_lower = title.lower()
    keywords = HIGHLIGHTS_KEYWORDS.get(lang, HIGHLIGHTS_KEYWORDS["EN"])
    return any(kw in title_lower for kw in keywords)

def get_recent_videos(channel_url, days=7):
    """Estrae i video recenti dal canale YouTube gestendo il filtro temporale in Python."""
    ydl_opts = {
        'extract_flat': True,
        'quiet': True,
        'playlistend': 30,  # Analizza gli ultimi 30 video caricati
    }

    limit_date = datetime.now() - timedelta(days=days)
    videos = []

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(channel_url, download=False)
            if info and 'entries' in info:
                for entry in info['entries']:
                    if entry:
                        title = entry.get('title')
                        video_id = entry.get('id')
                        upload_date_str = entry.get('upload_date')  # Formato YYYYMMDD

                        # Controllo della data (se presente nei metadati flat)
                        if upload_date_str:
                            try:
                                upload_date = datetime.strptime(upload_date_str, '%Y%m%d')
                                if upload_date < limit_date:
                                    continue  # Salta i video più vecchi del limite
                            except ValueError:
                                pass

                        if title and video_id:
                            videos.append((title, f"https://www.youtube.com/watch?v={video_id}"))
    except Exception as e:
        print(f"❌ Errore per {channel_url}: {e}")

    return videos

def main():
    lines = ["#EXTM3U"]

    for name, url, lang in CHANNELS:
        print(f"📡 Estraggo highlights da {name}...")
        videos = get_recent_videos(url, days=7)
        highlights = [(t, u) for t, u in videos if is_highlight(t, lang)]
        print(f"   -> {len(highlights)} highlights trovati")

        for title, video_url in highlights:
            clean_title = title.replace('"', '').replace('\n', '').replace(',', '-')
            lines.append(f'#EXTINF:-1 tvg-name="{name}" group-title="Calcio Gol 24/7",{name} - {clean_title}')
            lines.append(video_url)

    with open("youtube_highlights.m3u", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print("✅ File youtube_highlights.m3u generato con successo!")

if __name__ == "__main__":
    main()