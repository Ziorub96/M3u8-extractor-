from datetime import datetime
import yt_dlp

# Dizionario multilingua delle parole chiave per intercettare highlights e gol
HIGHLIGHTS_KEYWORDS = {
    "IT": ["highlights", "sintesi", "gol", "summary"],
    "DE": ["höhepunkte", "tore", "zusammenfassung", "highlights"],
    "ES": ["resumen", "goles", "highlights", "gol"],
    "BR": ["melhores momentos", "gols", "resumo", "highlights", "compacto"],
    "PL": ["skrót", "bramki", "najlepsze akcje", "highlights"],
    "UK": ["highlights", "goals", "summary"],
    "EN": ["highlights", "goals", "summary"],
    "AR": ["highlights", "goals", "goal collection", "ملخص", "أهداف"]
}

# Elenco dei canali ufficiali verificati (con il Brasileirão corretto su Fanatiz)
CHANNELS = [
    ("Serie A IT", "https://www.youtube.com/@seriea/videos", "IT"),
    ("Sky Sport IT", "https://www.youtube.com/@SkySport/videos", "IT"),
    ("DAZN Italia IT", "https://www.youtube.com/@DAZNIT/videos", "IT"),
    ("Bundesliga DE", "https://www.youtube.com/@Bundesliga/videos", "DE"),
    ("Liga Profesional AR", "https://www.youtube.com/@LigaProfesional/videos", "ES"),
    ("Brasileirão Highlights BR", "https://www.youtube.com/@Fanatiz/videos", "BR"),  # Canale ufficiale partner per il Brasileirão
    ("Ekstraklasa PL", "https://www.youtube.com/@Ekstraklasa/videos", "PL"),
    ("Scottish Premiership UK", "https://www.youtube.com/@spflofficial/videos", "UK"),
    ("Como TV Saudi Pro League", "https://www.youtube.com/@comotv_official/videos", "EN"),
]

def is_highlight(title, lang):
    title_lower = title.lower()
    keywords = HIGHLIGHTS_KEYWORDS.get(lang, HIGHLIGHTS_KEYWORDS["EN"])
    return any(kw in title_lower for kw in keywords)

def get_recent_videos(channel_url):
    ydl_opts = {
        'extract_flat': True,
        'quiet': True,
        'playlistend': 30, # Analizza gli ultimi 30 caricamenti recenti
    }

    videos = []

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(channel_url, download=False)
            if info and 'entries' in info:
                for entry in info['entries']:
                    if entry:
                        title = entry.get('title')
                        video_id = entry.get('id')
                        if title and video_id:
                            videos.append((title, f"https://www.youtube.com/watch?v={video_id}"))
    except Exception as e:
        print(f"❌ Errore di connessione per {channel_url}: {e}")

    return videos

def main():
    lines = ["#EXTM3U"]

    for name, url, lang in CHANNELS:
        print(f"📡 Analizzo il canale: {name}...")
        videos = get_recent_videos(url)
        highlights = [(t, u) for t, u in videos if is_highlight(t, lang)]
        print(f"   -> Trovati {len(highlights)} video validi")

        for title, video_url in highlights:
            clean_title = title.replace('"', '').replace('\n', '').replace(',', '-')
            lines.append(f'#EXTINF:-1 tvg-name="{name}" group-title="Calcio Gol 24/7",{name} - {clean_title}')
            lines.append(video_url)

    with open("youtube_highlights.m3u", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print("✅ Playlist M3U generata correttamente!")

if __name__ == "__main__":
    main()
