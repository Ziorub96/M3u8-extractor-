import re
import requests

SECTIONS = [
    "https://raw.githubusercontent.com/fmhy/edit/main/videopiracy.md",
    "https://raw.githubusercontent.com/fmhy/edit/main/non-english.md",
    "https://raw.githubusercontent.com/fmhy/edit/main/videotools.md",
]

OUTPUT_FILE = "fmhy_streams.m3u"
STREAM_KEYWORDS = [".m3u", ".m3u8", ".mp4", ".ts", ".mkv", "playlist", "stream", "iptv", "hls", "dash", "live"]

def is_stream_url(url):
    url_lower = url.lower()
    return any(kw in url_lower for kw in STREAM_KEYWORDS)

def main():
    all_links = []

    for section_url in SECTIONS:
        print(f"📡 Scarico {section_url}...")
        try:
            r = requests.get(section_url, timeout=30)
            r.raise_for_status()
        except Exception as e:
            print(f"❌ Errore: {e}")
            continue

        pattern = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")
        links = pattern.findall(r.text)
        stream_links = [(nome, url) for nome, url in links if is_stream_url(url)]
        print(f"   -> {len(stream_links)} potenziali stream")
        all_links.extend(stream_links)

    # Rimuovi duplicati per URL
    seen = set()
    unique_links = []
    for nome, url in all_links:
        if url not in seen:
            seen.add(url)
            unique_links.append((nome, url))

    # Scrivi SEMPRE il file, anche se vuoto
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for nome, url in unique_links:
            f.write(f"#EXTINF:-1,{nome.strip()}\n{url}\n")

    print(f"\n✅ Salvato {OUTPUT_FILE} con {len(unique_links)} flussi")

if __name__ == "__main__":
    main()