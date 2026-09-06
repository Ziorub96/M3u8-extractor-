import requests
import re
from xml.etree import ElementTree

# URL base corretti: guides/<country>/<country>.xml
EPG_BASE_MAIN = "https://raw.githubusercontent.com/iptv-org/epg/main/guides/{}/{}.xml"
EPG_BASE_MASTER = "https://raw.githubusercontent.com/iptv-org/epg/master/guides/{}/{}.xml"

COUNTRIES = ["it", "uk", "de", "es", "pt", "pl", "us", "fr", "br", "ar"]
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})

def fetch_epg(country):
    """Prova prima main, poi master, con la struttura guides/{country}/{country}.xml."""
    urls = [
        EPG_BASE_MAIN.format(country, country),
        EPG_BASE_MASTER.format(country, country),
    ]
    for url in urls:
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                return r.text
        except Exception:
            continue
    print(f"❌ Impossibile scaricare EPG per {country}")
    return None

def extract_channels_and_programmes(xml_text):
    # Regex efficiente per estrarre blocchi <channel> e <programme>
    channels = re.findall(r"<channel\b[^>]*>.*?</channel>|<channel\b[^>]*/>", xml_text, flags=re.DOTALL)
    programmes = re.findall(r"<programme\b[^>]*>.*?</programme>|<programme\b[^>]*/>", xml_text, flags=re.DOTALL)
    return channels, programmes

def main():
    all_channels = []
    all_programmes = []

    for country in COUNTRIES:
        print(f"📡 Scarico EPG per {country}...")
        xml = fetch_epg(country)
        if not xml:
            continue
        channels, programmes = extract_channels_and_programmes(xml)
        print(f"   -> {len(channels)} canali, {len(programmes)} programmi")
        all_channels.extend(channels)
        all_programmes.extend(programmes)

    if all_channels or all_programmes:
        with open("combined_epg.xml", "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            f.write("<tv>\n")
            for ch in all_channels:
                f.write(ch + "\n")
            for prog in all_programmes:
                f.write(prog + "\n")
            f.write("</tv>\n")
        print(f"\n✅ Salvato combined_epg.xml con {len(all_channels)} canali e {len(all_programmes)} programmi")
    else:
        with open("combined_epg.xml", "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv/>\n')
        print("⚠️ Nessun canale EPG trovato, generato file vuoto")

if __name__ == "__main__":
    main()