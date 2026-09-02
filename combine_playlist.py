import requests

SOURCES = [
    ("DAMITV", "https://raw.githubusercontent.com/Ziorub96/M3u8-extractor-/main/damitv_events.m3u"),
    ("doms9", "https://s.id/d9M3U8"),
    ("iptv-org sports", "https://iptv-org.github.io/iptv/categories/sports.m3u"),
    # Aggiungi altre playlist qui, ad esempio:
    # ("Nome Sorgente", "https://url/della/playlist.m3u"),
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

def parse_m3u(lines):
    """
    Estrae blocchi composti da #EXTINF + eventuali tag successivi + URL finale.
    Ritorna lista di blocchi (liste di righe) dove l'URL è l'ultima riga.
    """
    blocks = []
    current_block = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#EXTINF"):
            if current_block:
                blocks.append(current_block)
            current_block = [stripped]
        elif stripped and not stripped.startswith("#"):
            # URL del flusso
            current_block.append(stripped)
            blocks.append(current_block)
            current_block = []
        elif stripped.startswith("#") and current_block:
            # Tag aggiuntivo (es. #EXTVLCOPT)
            current_block.append(stripped)
    if current_block:
        blocks.append(current_block)
    return blocks

def main():
    all_lines = ["#EXTM3U"]
    seen_urls = set()

    for name, url in SOURCES:
        print(f"📡 Scarico {name}...")
        lines = fetch_playlist(url)
        blocks = parse_m3u(lines)
        print(f"   -> {len(blocks)} voci trovate")

        if blocks:
            all_lines.append(f"# ===== SORGENTE: {name} =====")
            for block in blocks:
                # L'URL è l'ultima riga del blocco
                stream_url = block[-1]
                if stream_url not in seen_urls:
                    seen_urls.add(stream_url)
                    all_lines.extend(block)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(all_lines))

    print(f"\n✅ Salvato {OUTPUT_FILE} con {len(seen_urls)} flussi unici")

if __name__ == "__main__":
    main()