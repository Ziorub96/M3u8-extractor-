from datetime import datetime
import yt_dlp

# Dizionario multilingua con tutte le parole chiave aggiornate
HIGHLIGHTS_KEYWORDS = {
    "IT": ["highlights", "sintesi", "gol", "summary"],
    "DE": ["höhepunkte", "tore", "zusammenfassung", "highlights"],
    "ES": ["resumen", "goles", "highlights", "gol"],
    "PT": ["resumo", "resumos", "golos", "highlights", "liga portugal", "gols"],
    "BR": ["melhores momentos", "gols", "resumo", "highlights", "compacto"],
    "PL": ["skrót", "bramki", "najlepsze akcje", "highlights"],
    "UK": ["highlights", "goals", "summary"],
    "EN": ["highlights", "goals", "summary"],
    "AR": ["highlights", "goals", "goal collection", "ملخص", "أهداف"]
}

# Canali ufficiali con handle corretti
CHANNELS = [
    ("Sky Sport IT", "https://www.youtube.com/@SkySport/videos", "IT"),
    ("DAZN Italia IT", "https://www.youtube.com/playlist?list=PLNlz0xe3bYHw&si=j74vN3F11HVd9Kkk", "IT"),
    ("Bundesliga DE", "https://www.youtube.com/@Bundesliga/videos", "DE"),
    ("Liga Portugal PT", "https://www.youtube.com/@LigaPortugalOfficial/videos", "PT"),
    ("Liga Profesional AR", "https://www.youtube.com/@LigaProfesional/videos", "ES"),
    ("Brasileirão Highlights BR", "https://www.youtube.com/@Fanatiz/videos", "BR"),
    ("Ekstraklasa PL", "https://www.youtube.com/@Ekstraklasa/videos", "PL"),
    ("Scottish Premiership UK", "https://www.youtube.com/@spflofficial/videos", "UK"),
    ("Como TV Saudi Pro League", "https://www.youtube.com/@comotv_official/videos", "EN"),
]

# Filtro speciale per Sky Sport
SKY_SPECIAL_KEYWORDS = ["gol", "highlights"]

def is_highlight(title, lang, channel_name=None):
    title_lower = title.lower()

    # Se il canale è Sky Sport Italia, usiamo solo "gol" e "highlights"
    if channel_name == "Sky Sport IT":
        return any(kw in title_lower for kw in SKY_SPECIAL_KEYWORDS)

    keywords = HIGHLIGHTS_KEYWORDS.get(lang, HIGHLIGHTS_KEYWORDS["EN"])
    return any(kw in title_lower for kw in keywords)

def get_recent_highlights(channel_url, lang, channel_name):
    """
    Estrae i video recenti e ottiene direttamente l'URL del file MP4
    in un unico passaggio, senza chiamate doppie.
    """
    ydl_opts = {
        'format': 'best[ext=mp4]/best',
        'quiet': True,
        'no_warnings': True,
        'playlistend': 30,
        'extract_flat': False,  # essenziale per avere i formati e l'url diretto
    }

    highlights = []

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(channel_url, download=False)
            if info and 'entries' in info:
                for entry in info['entries']:
                    if entry:
                        title = entry.get('title')
                        if title and is_highlight(title, lang, channel_name):
                            direct_url = entry.get('url')
                            # Se non c'è url diretto, cerchiamo nei formati mp4
                            if not direct_url and 'formats' in entry:
                                formats = [f for f in entry['formats'] if f.get('ext') == 'mp4' and f.get('url')]
                                if formats:
                                    direct_url = formats[-1].get('url')

                            if direct_url:
                                highlights.append((title, direct_url))
    except Exception as e:
        print(f"❌ Errore per {channel_url}: {e}")

    return highlights

def main():
    lines = ["#EXTM3U"]

    for name, url, lang in CHANNELS:
        print(f"📡 Analizzo e converto highlights da {name}...")
        highlights = get_recent_highlights(url, lang, name)
        print(f"   -> Trovati {len(highlights)} video diretti")

        for title, video_url in highlights:
            clean_title = title.replace('"', '').replace('\n', '').replace(',', '-')
            lines.append(f'#EXTINF:-1 tvg-name="{name}" group-title="Calcio Gol 24/7",{name} - {clean_title}')
            lines.append(video_url)

    with open("youtube_highlights.m3u", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print("✅ Playlist M3U con URL diretti generata con successo!")

if __name__ == "__main__":
    main()