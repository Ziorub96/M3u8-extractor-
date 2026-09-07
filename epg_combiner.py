import concurrent.futures
import re
import requests

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

MAX_WORKERS = 15

# URL base corretto: GitHub Pages del progetto EPG
EPG_BASE_URL = "https://iptv-org.github.io/epg/guides/{country}.xml"

def fetch_epg_single(country, session):
    """Scarica il file EPG per un singolo paese usando l'URL corretto."""
    url = EPG_BASE_URL.format(country=country)
    try:
        r = session.get(url, timeout=20)
        if r.status_code == 200 and r.text.strip():
            return country, r.text
    except Exception:
        pass
    # Fallback secondario (alcuni paesi potrebbero avere percorso diverso)
    url_alt = f"https://iptv-org.github.io/epg/guides/{country}/{country}.xml"
    try:
        r = session.get(url_alt, timeout=20)
        if r.status_code == 200 and r.text.strip():
            return country, r.text
    except Exception:
        pass
    return country, None

def extract_channels_and_programmes(xml_text):
    """Estrae i blocchi <channel> e <programme> usando regex efficienti."""
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
    sources = FALLBACK_COUNTRIES.copy()
    all_channels = []
    all_programmes = []
    successful_countries = 0

    print(f"\n📡 Avvio scaricamento EPG per {len(sources)} paesi (in parallelo)...")

    session = requests.Session()
    session.headers.update(HEADERS)

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_epg_single, country, session): country
            for country in sources
        }

        for future in concurrent.futures.as_completed(futures):
            country, xml_text = future.result()
            if xml_text:
                channels, programmes = extract_channels_and_programmes(xml_text)
                print(f"  ✓ {country.upper()}: {len(channels)} canali, {len(programmes)} programmi")
                all_channels.extend(channels)
                all_programmes.extend(programmes)
                successful_countries += 1
            else:
                print(f"  ❌ {country.upper()}: nessun file XML trovato")

    print(f"\n📊 Completato! Scaricati con successo {successful_countries}/{len(sources)} paesi.")

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
        with open(output_filename, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv/>\n')
        print("⚠️ Nessun dato trovato, generato file XML vuoto.")

if __name__ == "__main__":
    main()