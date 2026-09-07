import concurrent.futures
import re
import requests
from threading import local

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

FALLBACK_COUNTRIES = [
    "it", "uk", "gb", "de", "es", "pt", "pl", "us", "fr", "br", "ar",
    "ca", "au", "nl", "be", "ch", "at", "se", "no", "fi", "dk", "cz",
    "sk", "ro", "bg", "gr", "tr", "ru", "ua", "in", "jp", "kr", "cn",
    "mx", "cl", "co", "pe", "za", "eg", "sa", "ae", "il", "ie", "nz",
    "hu", "hr", "rs", "si",
]

MAX_WORKERS = 10
INDEX_URL = "https://iptv-org.github.io/epg/guides.json"

thread_local = local()

def get_session():
    if not hasattr(thread_local, "session"):
        thread_local.session = requests.Session()
        thread_local.session.headers.update(HEADERS)
    return thread_local.session

def fetch_guide_xml(guide_url):
    """Scarica un singolo file XML dato il suo URL esatto."""
    session = get_session()
    try:
        r = session.get(guide_url, timeout=20)
        if r.status_code == 200 and r.text.strip():
            return r.text
    except Exception:
        pass
    return None

def extract_channels_and_programmes(xml_text):
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

    print("🔍 Recupero indice guide da iptv-org...")
    try:
        r = session.get(INDEX_URL, timeout=15)
        r.raise_for_status()
        guides_data = r.json()
    except Exception as e:
        print(f"❌ Impossibile scaricare l'indice delle guide: {e}")
        return

    # Mappa gli URL delle guide relativi ai paesi richiesti
    # L'indice contiene oggetti con campi tipo 'lang', 'site', 'url', 'country'
    urls_to_download = []
    countries_set = set(c.lower() for c in FALLBACK_COUNTRIES)
    # Mappa 'uk' -> 'gb'
    if "uk" in countries_set:
        countries_set.add("gb")

    for guide in guides_data:
        guide_country = guide.get("country", "").lower()
        if guide_country in countries_set and "url" in guide:
            urls_to_download.append(guide["url"])

    print(f"📡 Trovate {len(urls_to_download)} guide XML per i paesi selezionati. Avvio scaricamento...")

    all_channels = []
    all_programmes = []
    successful_downloads = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(fetch_guide_xml, url)
            for url in urls_to_download
        ]

        for future in concurrent.futures.as_completed(futures):
            xml_text = future.result()
            if xml_text:
                channels, programmes = extract_channels_and_programmes(xml_text)
                all_channels.extend(channels)
                all_programmes.extend(programmes)
                successful_downloads += 1

    print(f"\n📊 Completato! Scaricate con successo {successful_downloads}/{len(urls_to_download)} guide.")

    output_filename = "combined_epg.xml"
    if all_channels or all_programmes:
        with open(output_filename, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            f.write("<tv>\n")
            for ch in all_channels:
                f.write(ch + "\n")
            for prog in all_programmes:
                f.write(prog + "\n")
            f.write("</tv>\n")
        print(f"✅ Salvato `{output_filename}` con {len(all_channels)} canali e {len(all_programmes)} programmi.")
    else:
        print("⚠️ Nessun dato scaricato.")

if __name__ == "__main__":
    main()
