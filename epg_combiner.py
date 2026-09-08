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
BASE_URL = "https://iptv-org.github.io/epg/"

thread_local = local()

def get_session():
    if not hasattr(thread_local, "session"):
        thread_local.session = requests.Session()
        thread_local.session.headers.update(HEADERS)
    return thread_local.session

def fetch_guide_xml(guide_url):
    session = get_session()
    try:
        r = session.get(guide_url, timeout=20)
        if r.status_code == 200 and r.text.strip():
            return r.text
    except Exception:
        pass
    return None

def extract_channels_and_programmes(xml_text):
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

    countries_set = set(c.lower() for c in FALLBACK_COUNTRIES)
    if "uk" in countries_set:
        countries_set.add("gb")

    urls_to_download = []

    for guide in guides_data:
        # 1. Recupera il percorso da 'url', 'file' o 'site'
        raw_path = guide.get("url") or guide.get("file") or ""
        lang = (guide.get("lang") or "").lower()
        site = (guide.get("site") or "").lower()

        # 2. Determina se la guida appartiene ai paesi selezionati
        # Verifica tramite lang, codice paese nel dominio (es. .it) o percorso (/it/)
        is_matched = (
            lang in countries_set or
            any(f"/guides/{c}/" in raw_path.lower() or f"/{c}/" in raw_path.lower() for c in countries_set) or
            any(site.endswith(f".{c}") for c in countries_set)
        )

        if is_matched and raw_path:
            # 3. Trasforma in URL assoluto valido
            if raw_path.startswith("http://") or raw_path.startswith("https://"):
                full_url = raw_path
            else:
                full_url = f"{BASE_URL}{raw_path.lstrip('/')}"

            if full_url not in urls_to_download:
                urls_to_download.append(full_url)

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
