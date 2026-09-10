import gzip
import io
import re
import requests
import concurrent.futures

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Fonti EPG da epgshare01.online (file .xml.gz)
EPG_SOURCES = [
    # Generali sport e multi-paese
    "https://epgshare01.online/epgshare01/epg_ripper_IT1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_UK1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_US1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_US_SPORTS1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_US_LOCALS2.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_ES1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_PT1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_PL1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_DE1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_FR1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_BR1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_AR1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_MX1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_NL1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_BE2.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_AT1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_CH1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_SE1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_NO1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_DK1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_FI1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_CZ1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_SK1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_RO1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_BG1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_GR1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_TR1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_HR1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_RS1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_HU1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_IE1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_CA1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_AU1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_IN1.xml.gz",
    # Provider specifici
    "https://epgshare01.online/epgshare01/epg_ripper_BEIN1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_DIRECTVSPORTS1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_DISTROTV1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_PLEX1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_RAKUTEN1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_RAKUTEN_IT1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_POWERNATION1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_SPORTKLUB1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_SSPORTPLUS1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_THESPORTPLUS1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_FANDUEL1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_DRAFTKINGS1.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_PAC-12.xml.gz",
    "https://epgshare01.online/epgshare01/epg_ripper_RALLY_TV1.xml.gz",
]

MAX_WORKERS = 8
OUTPUT_FILE = "combined_epg.xml"


def fetch_and_decompress(url: str, session: requests.Session):
    """Scarica un file .xml.gz e restituisce il testo XML decompresso."""
    try:
        r = session.get(url, timeout=30)
        if r.status_code != 200:
            return url, None
        # Decomprime in memoria
        with gzip.GzipFile(fileobj=io.BytesIO(r.content)) as gz:
            text = gz.read().decode("utf-8", errors="ignore")
        return url, text
    except Exception as e:
        print(f"  ❌ Errore su {url}: {e}")
        return url, None


def extract_channels_and_programmes(xml_text: str):
    """Estrae i blocchi <channel> e <programme>."""
    channels = re.findall(
        r"<channel\b[^>]*>.*?</channel>|<channel\b[^>]*/>",
        xml_text,
        flags=re.DOTALL,
    )
    programmes = re.findall(
        r"<programme\b[^>]*>.*?</programme>|<programme\b[^>]*/>",
        xml_text,
        flags=re.DOTALL,
    )
    return channels, programmes


def main():
    session = requests.Session()
    session.headers.update(HEADERS)

    all_channels = []
    all_programmes = []
    successful = 0

    print(f"📡 Scarico {len(EPG_SOURCES)} fonti EPG da epgshare01.online...")

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {
            ex.submit(fetch_and_decompress, url, session): url
            for url in EPG_SOURCES
        }

        for fut in concurrent.futures.as_completed(futures):
            url, xml_text = fut.result()
            name = url.split("/")[-1].replace("epg_ripper_", "").replace(".xml.gz", "")
            if not xml_text:
                print(f"  ❌ {name}: fallito")
                continue

            channels, programmes = extract_channels_and_programmes(xml_text)
            print(f"  ✓ {name}: {len(channels)} canali, {len(programmes)} programmi")
            all_channels.extend(channels)
            all_programmes.extend(programmes)
            successful += 1

    print(f"\n📊 Completato: {successful}/{len(EPG_SOURCES)} fonti scaricate.")

    if all_channels or all_programmes:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            f.write("<tv>\n")
            for ch in all_channels:
                f.write(ch + "\n")
            for prog in all_programmes:
                f.write(prog + "\n")
            f.write("</tv>\n")
        print(f"✅ Salvato {OUTPUT_FILE} con {len(all_channels)} canali e {len(all_programmes)} programmi.")
    else:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv/>\n')
        print("⚠️ Nessun dato trovato, generato XML vuoto.")


if __name__ == "__main__":
    main()