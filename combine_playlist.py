import re
import requests

SOURCES = [
    ("DAMITV", "https://raw.githubusercontent.com/Ziorub96/M3u8-extractor-/main/damitv_events.m3u"),
    ("doms9", "https://s.id/d9M3U8"),
    ("iptv-org sports", "https://iptv-org.github.io/iptv/categories/sports.m3u"),
    # Aggiungi altre playlist qui
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

def set_group_title(extinf_line, group_title):
    """
    Modifica la riga #EXTINF per impostare (o sostituire) l'attributo group-title.
    Restituisce la riga aggiornata.
    """
    # Se esiste già group-title, lo sostituiamo
    if 'group-title="' in extinf_line:
        new_line = re.sub(r'group-title="[^"]*"', f'group-title="{group_title}"', extinf_line)
    else:
        # Altrimenti lo aggiungiamo prima della virgola finale
        if ',' in extinf_line:
            head, tail = extinf_line.rsplit(',', 1)
            new_line = f'{head} group-title="{group_title}",{tail}'
        else:
            # Caso anomalo: accodiamo
            new_line = f'{extinf_line} group-title="{group_title}"'
    return new_line

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

                # ✅ Guardia di sicurezza: salta blocchi malformati senza URL
                if stream_url.startswith("#"):
                    continue

                if stream_url in seen_urls:
                    continue

                seen_urls.add(stream_url)

                # Modifica la prima riga (#EXTINF) per aggiungere group-title = nome sorgente
                if block[0].startswith("#EXTINF"):
                    block[0] = set_group_title(block[0], name)

                all_lines.extend(block)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(all_lines))

    print(f"\n✅ Salvato {OUTPUT_FILE} con {len(seen_urls)} flussi unici")

if __name__ == "__main__":
    main()