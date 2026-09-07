import concurrent.futures
import re
import requests

# User-Agent per evitare blocchi anti-scraping
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Lista di fallback con oltre 50 paesi (ISO 3166-1 alpha-2)
FALLBACK_COUNTRIES = [
    "it",
    "uk",
    "gb",
    "de",
    "es",
    "pt",
    "pl",
    "us",
    "fr",
    "br",
    "ar",
    "ca",
    "au",
    "nl",
    "be",
    "ch",
    "at",
    "se",
    "no",
    "fi",
    "dk",
    "cz",
    "sk",
    "ro",
    "bg",
    "gr",
    "tr",
    "ru",
    "ua",
    "in",
    "jp",
    "kr",
    "cn",
    "mx",
    "cl",
    "co",
    "pe",
    "za",
    "eg",
    "sa",
    "ae",
    "il",
    "ie",
    "nz",
    "hu",
    "hr",
    "rs",
    "si",
]

# Numero di thread simultanei per il download
MAX_WORKERS = 15


def discover_epg_sources():
    """Scopre dinamicamente tutte le fonti EPG disponibili su GitHub API.

    In caso di errore o rate-limit, ripiega sulla lista FALLBACK_COUNTRIES.
    """
    api_url = "https://api.github.com/repos/iptv-org/epg/contents/guides"
    sources = {}

    try:
        print("🔍 Ricerca dinamica delle fonti EPG su repository GitHub...")
        r = requests.get(api_url, headers=HEADERS, timeout=15)
        if r.status_code == 200:
            items = r.json()
            for item in items:
                name = item.get("name", "")
                if item.get("type") == "file" and name.endswith(".xml"):
                    country = name.replace(".xml", "").lower()
                    sources[country] = [item.get("download_url")]
                elif item.get("type") == "dir":
                    country = name.lower()
                    sources[country] = [
                        f"https://raw.githubusercontent.com/iptv-org/epg/master/guides/{country}/{country}.xml",
                        f"https://raw.githubusercontent.com/iptv-org/epg/main/guides/{country}/{country}.xml",
                        f"https://raw.githubusercontent.com/iptv-org/epg/master/guides/{country}.xml",
                        f"https://raw.githubusercontent.com/iptv-org/epg/main/guides/{country}.xml",
                    ]
            if sources:
                print(
                    f"✅ Trovate {len(sources)} fonti EPG disponibili via API!"
                )
                return sources
    except Exception as e:
        print(
            f"⚠️ Impossibile interrogare l'API di GitHub ({e}). Uso della lista estesa predefinita."
        )

    # Generazione struttura URL per la lista di fallback
    for country in FALLBACK_COUNTRIES:
        sources[country] = [
            f"https://raw.githubusercontent.com/iptv-org/epg/master/guides/{country}.xml",
            f"https://raw.githubusercontent.com/iptv-org/epg/main/guides/{country}.xml",
            f"https://raw.githubusercontent.com/iptv-org/epg/master/guides/{country}/{country}.xml",
            f"https://raw.githubusercontent.com/iptv-org/epg/main/guides/{country}/{country}.xml",
        ]
    return sources


def fetch_epg_single(country, urls, session):
    """Scarica il file EPG per un singolo paese provando gli URL a disposizione."""
    for url in urls:
        if not url:
            continue
        try:
            r = session.get(url, timeout=20)
            if r.status_code == 200 and r.text.strip():
                return country, r.text
        except Exception:
            continue
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
    sources = discover_epg_sources()
    all_channels = []
    all_programmes = []
    successful_countries = 0

    print(
        f"\n📡 Avvio scaricamento EPG per {len(sources)} paesi (in parallelo)..."
    )

    session = requests.Session()
    session.headers.update(HEADERS)

    # Esecuzione dei download in parallelo
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:
        futures = {
            executor.submit(
                fetch_epg_single, country, urls, session
            ): country
            for country, urls in sources.items()
        }

        for future in concurrent.futures.as_completed(futures):
            country, xml_text = future.result()
            if xml_text:
                channels, programmes = extract_channels_and_programmes(
                    xml_text
                )
                print(
                    f"  ✓ {country.upper()}: {len(channels)} canali, {len(programmes)} programmi"
                )
                all_channels.extend(channels)
                all_programmes.extend(programmes)
                successful_countries += 1
            else:
                print(f"  ❌ {country.upper()}: nessun file XML trovato")

    print(
        f"\n📊 Completato! Scaricati con successo {successful_countries}/{len(sources)} paesi."
    )

    # Scrittura del file combinato
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
        print(
            f"✅ Salvato `{output_filename}` con {len(all_channels)} canali e {len(all_programmes)} programmi in totale."
        )
    else:
        with open(output_filename, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv/>\n')
        print("⚠️ Nessun dato trovato, generato file XML vuoto.")


if __name__ == "__main__":
    main()
