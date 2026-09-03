import re
import requests

SECTIONS = [
    "https://raw.githubusercontent.com/fmhy/edit/main/docs/video.md"
]

OUTPUT_FILE = "fmhy_streams.m3u"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def fetch_page(url):
    """Scarica il contenuto testuale di una pagina Markdown."""
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"❌ Errore scaricando {url}: {e}")
        return None

def find_live_sports_section(text):
    """
    Individua la sezione 'Live Sports' nel Markdown.
    Supporta qualsiasi livello di heading (##, ###, ecc.) e variazione di maiuscole.
    Se non trova l'header esatto, cerca la prima riga contenente 'live sports'.
    """
    lines = text.splitlines()
    heading_pattern = re.compile(r'^#+\s+live sports\s*$', re.IGNORECASE)

    # 1) Ricerca esatta dell'heading
    for i, line in enumerate(lines):
        if heading_pattern.match(line.strip()):
            return i

    # 2) Fallback: cerca riga che contiene 'live sports'
    for i, line in enumerate(lines):
        if 'live sports' in line.lower():
            return i

    return None

def extract_markdown_links(text):
    """Estrae tutti i link in formato Markdown [testo](url)."""
    pattern = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")
    return pattern.findall(text)

def main():
    all_links = []

    for url in SECTIONS:
        print(f"📡 Scarico {url}...")
        text = fetch_page(url)
        if not text:
            continue

        lines = text.splitlines()

        # Trova l'inizio della sezione Live Sports
        start_idx = find_live_sports_section(text)
        if start_idx is None:
            print("⚠️ Sezione 'Live Sports' non trovata. Proseguo senza filtrare.")
            # Se la sezione non c'è, prendiamo tutto il testo (per non perdere eventuali link)
            section_lines = lines
        else:
            print(f"✅ Sezione 'Live Sports' trovata alla riga {start_idx}")
            section_lines = []
            for line in lines[start_idx+1:]:
                # Interrompi alla prossima sezione dello stesso livello o superiore
                if re.match(r'^#{1,6}\s+', line.strip()):
                    break
                section_lines.append(line)

        section_text = "\n".join(section_lines)

        links = extract_markdown_links(section_text)
        print(f"   -> {len(links)} link Markdown trovati")
        all_links.extend(links)

    # Deduplicazione per URL
    seen = set()
    unique_links = []
    for nome, url in all_links:
        if url not in seen:
            seen.add(url)
            unique_links.append((nome.strip(), url))

    # Scrive SEMPRE il file, anche se vuoto
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for nome, url in unique_links:
            f.write(f"#EXTINF:-1,{nome}\n{url}\n")

    print(f"\n✅ Salvato {OUTPUT_FILE} con {len(unique_links)} link")
    if len(unique_links) == 0:
        print("⚠️ Nessun link trovato; il file è vuoto ma evita errori di commit.")

if __name__ == "__main__":
    main()