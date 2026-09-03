import requests

# Fonti EPG da diverse nazioni
EPG_SOURCES = [
    "https://raw.githubusercontent.com/iptv-org/epg/master/guides/it.xml",
    "https://raw.githubusercontent.com/iptv-org/epg/master/guides/uk.xml",
    "https://raw.githubusercontent.com/iptv-org/epg/master/guides/de.xml",
    "https://raw.githubusercontent.com/iptv-org/epg/master/guides/es.xml",
    "https://raw.githubusercontent.com/iptv-org/epg/master/guides/pt.xml",
    "https://raw.githubusercontent.com/iptv-org/epg/master/guides/pl.xml",
    "https://raw.githubusercontent.com/iptv-org/epg/master/guides/us.xml",
]

OUTPUT_FILE = "combined_epg.xml"

def download_epg(url):
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"❌ Errore scaricando {url}: {e}")
        return None

def main():
    channels = []
    programmes = []

    for url in EPG_SOURCES:
        print(f"📡 Scarico EPG da {url}...")
        xml_text = download_epg(url)
        if not xml_text:
            continue

        # Estrai i blocchi <channel> e <programme>
        import re
        chan = re.findall(r"<channel .*?</channel>", xml_text, re.DOTALL)
        prog = re.findall(r"<programme .*?</programme>", xml_text, re.DOTALL)

        channels.extend(chan)
        programmes.extend(prog)

    if not channels:
        print("❌ Nessun canale EPG trovato.")
        return

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write("<tv>\n")
        for c in channels:
            f.write(c + "\n")
        for p in programmes:
            f.write(p + "\n")
        f.write("</tv>\n")

    print(f"✅ Salvato {OUTPUT_FILE} con {len(channels)} canali e {len(programmes)} programmi.")

if __name__ == "__main__":
    main()