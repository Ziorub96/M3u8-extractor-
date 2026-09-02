import json
import time
import requests

BASE_URL = "https://ondemand.st"
API_STREAMS = f"{BASE_URL}/papi/api/streams"
API_EXTRACT = f"{BASE_URL}/papi/extract-url/"
API_TV_RESOLVE = f"{BASE_URL}/papi/tv/resolve/"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
OUTPUT_FILE = "damitv_events.m3u"

FIXED_CHANNELS = [
    ("Digi Sport 1", "https://dokagents.site/live/digisport1/mono.m3u8"),
    ("Digi Sport 2 HD", "https://dokagents.site/live/digisport2/mono.m3u8"),
    ("Digi Sport 3", "https://dokagents.site/live/digisport3/mono.m3u8"),
    ("Digi Sport 4", "https://dokagents.site/live/digisport4/mono.m3u8"),
    ("Match!Ultra", "http://stream.mcquack.net/169/index.m3u8"),
]

# Sessione globale per il riutilizzo delle connessioni (Keep-Alive)
session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})

# Cache per evitare chiamate ripetute allo stesso endpoint extract
_event_cache = {}

def http_get_json(url, referer=None):
    headers = {}
    if referer:
        headers["Referer"] = referer
    try:
        r = session.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"❌ Errore richiesta {url}: {e}")
        return None

def get_event_m3u8(event_id, sd=False):
    cache_key = (event_id, sd)
    if cache_key in _event_cache:
        return _event_cache[cache_key]

    url = API_EXTRACT + event_id
    if sd:
        url += "?sd=1"
    
    data = http_get_json(url, referer=f"{BASE_URL}/embed/?id={event_id}")
    result = None
    if data and data.get("success"):
        result = data.get("hlsUrl") or data.get("sdUrl")
    
    _event_cache[cache_key] = result
    return result

def get_channel_m3u8(ch_id):
    cache_key = (ch_id, "channel")
    if cache_key in _event_cache:
        return _event_cache[cache_key]

    data = http_get_json(API_TV_RESOLVE + ch_id, referer=f"{BASE_URL}/embed/?id={ch_id}")
    result = None
    if data and (data.get("stream") or data.get("url")):
        result = data.get("stream") or data.get("url")
    
    _event_cache[cache_key] = result
    return result

def get_24_7_channels(seen_ids):
    print("📡 Recupero canali 24/7 da papi/api/streams...")
    data = http_get_json(API_STREAMS, referer=BASE_URL)
    if not data or not data.get("success"):
        print("❌ API non raggiungibile")
        return []

    lines = []
    for category in data.get("streams", []):
        if not isinstance(category, dict):
            continue

        category_name = category.get("category", "").lower()

        for ev in category.get("streams", []):
            if not isinstance(ev, dict):
                continue

            ev_id = ev.get("id", "")
            title = ev.get("name", "Sconosciuto")
            logo = ev.get("poster", "")

            is_247_category = any(kw in category_name for kw in ["24/7", "channels", "live"])
            is_channel_id = "-" in ev_id and not ev_id.isdigit()

            if not ev_id or not (is_247_category or is_channel_id or ev.get("always_live") == 1):
                continue

            if ev_id in seen_ids:
                continue

            print(f"🔍 Risolvo {title} ({ev_id})...")
            m3u8_url = get_event_m3u8(ev_id)
            if m3u8_url:
                seen_ids.add(ev_id)
                lines.append(f'#EXTINF:-1 tvg-id="{ev_id}" tvg-logo="{logo}",{title}')
                lines.append(m3u8_url)
            else:
                print(f"⚠️ Stream non disponibile per {title}")

    print(f"✅ Canali 24/7 aggiunti: {len(lines)//2}")
    return lines

def get_live_tv_channels(seen_ids):
    ts_url = f"{BASE_URL}/data/ts-channels.json"
    print("📡 Scarico lista canali Live TV da ts-channels.json...")
    data = http_get_json(ts_url, referer=f"{BASE_URL}/livetv")
    if not data or not isinstance(data, dict) or "channels" not in data:
        print("❌ Errore nel recupero di ts-channels.json")
        return []

    channels = data["channels"]
    print(f"🔢 Trovati {len(channels)} canali nel file.")

    lines = []
    for ch in channels:
        if not isinstance(ch, dict):
            continue

        daddy_id = ch.get("daddyId")
        name = ch.get("name", "Sconosciuto")
        logo = ch.get("image", "")

        if not daddy_id or daddy_id in seen_ids:
            continue

        print(f"🔍 Risolvo {name} ({daddy_id})...")
        m3u8_url = get_channel_m3u8(daddy_id)
        if m3u8_url:
            seen_ids.add(daddy_id)
            lines.append(f'#EXTINF:-1 tvg-id="{daddy_id}" tvg-logo="{logo}",{name}')
            lines.append(m3u8_url)
        else:
            print(f"⚠️ Stream non disponibile per {name}")

    print(f"✅ Canali Live TV aggiunti: {len(lines)//2}")
    return lines

def build_sports_lines(seen_ids):
    print("📡 Recupero eventi sportivi...")
    data = http_get_json(API_STREAMS, referer=BASE_URL)
    if not data or not isinstance(data, dict) or not data.get("success"):
        print("❌ API non raggiungibile o dati non validi")
        return []

    streams = data.get("streams", [])
    lines = []

    for category in streams:
        if not isinstance(category, dict):
            continue
        sport = category.get("category", "")
        for ev in category.get("streams", []):
            if not isinstance(ev, dict):
                continue
            ev_id = ev.get("id", "")
            title = ev.get("name", "Sconosciuto")
            sources = ev.get("sources", [])

            if not ev_id or ev_id.startswith("247-") or ev.get("always_live") == 1:
                continue

            # Gestione unificata delle sorgenti evitando richieste duplicate
            for src in sources:
                if not isinstance(src, dict):
                    continue
                src_id = src.get("id")
                src_name = src.get("name", "Sorgente")
                src_type = src.get("source", "")

                if not src_id:
                    continue

                unique_key = f"{ev_id}_{src_id}"
                if unique_key in seen_ids:
                    continue

                m3u8_url = None
                if src_type == "hls":
                    m3u8_url = get_event_m3u8(ev_id)
                elif src_type == "sd":
                    m3u8_url = get_event_m3u8(ev_id, sd=True)
                elif src_type == "dlhd":
                    m3u8_url = get_channel_m3u8(src_id)

                if m3u8_url:
                    seen_ids.add(unique_key)
                    display = f"[{sport}] {title} ({src_name})"
                    lines.append(f'#EXTINF:-1 tvg-id="{unique_key}",{display}')
                    lines.append(m3u8_url)

    print(f"✅ Eventi sportivi aggiunti: {len(lines)//2}")
    return lines

def main():
    seen_ids = set()
    lines = ["#EXTM3U"]

    # Canali fissi
    for name, url in FIXED_CHANNELS:
        lines.append(f'#EXTINF:-1 tvg-id="{name}",{name}')
        lines.append(url)

    # Canali 24/7
    lines.extend(get_24_7_channels(seen_ids))

    # Canali Live TV
    lines.extend(get_live_tv_channels(seen_ids))

    # Eventi sportivi
    lines.extend(build_sports_lines(seen_ids))

    # Scrittura sicura: aggiorna il file solo se ci sono stream effettivi oltre all'intestazione
    if len(lines) > 1:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"\n✅ Salvato {OUTPUT_FILE} con {len(lines)//2} voci totali")
    else:
        print("\n⚠️ Nessun canale trovato. Il file non è stato sovrascritto.")

if __name__ == "__main__":
    main()