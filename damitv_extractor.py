import requests
import json
import time
import sys

BASE_URL = "https://ondemand.st"
API_STREAMS = f"{BASE_URL}/papi/api/streams"
API_EXTRACT = f"{BASE_URL}/papi/extract-url/"
API_TV_RESOLVE = f"{BASE_URL}/papi/tv/resolve/"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

OUTPUT_FILE = "damitv_events.m3u"

# --- CANALI FISSI (sempre presenti) ---
FIXED_CHANNELS = [
    ("Digi Sport 1", "https://dokagents.site/live/digisport1/mono.m3u8"),
    ("Digi Sport 2 HD", "https://dokagents.site/live/digisport2/mono.m3u8"),
    ("Digi Sport 3", "https://dokagents.site/live/digisport3/mono.m3u8"),
    ("Digi Sport 4", "https://dokagents.site/live/digisport4/mono.m3u8"),
    ("Match!Ultra", "http://stream.mcquack.net/169/index.m3u8"),
]

def http_get_json(url, referer=None):
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    try:
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"❌ Errore richiesta {url}: {e}")
        return None

def get_event_m3u8(event_id, sd=False):
    url = API_EXTRACT + event_id
    if sd:
        url += "?sd=1"
    data = http_get_json(url, referer=f"{BASE_URL}/embed/?id={event_id}")
    if data and data.get("success"):
        return data.get("hlsUrl") or data.get("sdUrl")
    return None

def get_channel_m3u8(ch_id):
    data = http_get_json(API_TV_RESOLVE + ch_id, referer=f"{BASE_URL}/embed/?id={ch_id}")
    if data and (data.get("stream") or data.get("url")):
        return data.get("stream") or data.get("url")
    return None

def get_live_tv_channels():
    """Scarica i canali TimStreams e restituisce righe minimali."""
    ts_url = f"{BASE_URL}/data/ts-channels.json"
    print("📡 Scarico lista canali Live TV...")
    data = http_get_json(ts_url, referer=f"{BASE_URL}/livetv")
    if data is None:
        print("❌ Impossibile scaricare ts-channels.json")
        return []
    if not isinstance(data, list):
        print(f"⚠️ Formato inatteso per ts-channels.json: {type(data)}")
        return []

    lines = []
    for idx, ch in enumerate(data):
        if isinstance(ch, str):
            print(f"Ignoro voce stringa: {ch[:80]}")
            continue
        if not isinstance(ch, dict):
            print(f"Ignoro voce non dizionario: {type(ch)}")
            continue

        daddy_id = ch.get("daddyId") or ch.get("id") or ch.get("channel_id")
        name = ch.get("name") or ch.get("title") or "Canale"
        if not daddy_id:
            continue

        m3u8_url = get_channel_m3u8(daddy_id)
        if m3u8_url:
            lines.append(f'#EXTINF:-1 tvg-id="{daddy_id}",{name}')
            lines.append(m3u8_url)
        else:
            print(f"⚠️ Stream non disponibile per {name}")

    print(f"✅ Live TV aggiunti: {len(lines)//2}")
    return lines

def build_sports_lines():
    """Eventi sportivi in formato minimale con debug."""
    print("📡 Recupero eventi sportivi...")
    data = http_get_json(API_STREAMS, referer=BASE_URL)
    if data is None:
        print("❌ API non raggiungibile")
        return []
    if not isinstance(data, dict):
        print(f"❌ Risposta non dizionario: {type(data)}")
        return []
    if not data.get("success"):
        print(f"❌ success=false: {data}")
        return []

    streams = data.get("streams", [])
    print(f"🔢 Numero categorie: {len(streams)}")
    for cat in streams:
        if isinstance(cat, dict):
            cat_name = cat.get("category", "?")
            cat_streams = cat.get("streams", [])
            print(f"   - {cat_name}: {len(cat_streams)} eventi")
        else:
            print(f"   - Categoria non valida: {type(cat)}")

    lines = []
    for category in streams:
        if not isinstance(category, dict):
            continue
        for ev in category.get("streams", []):
            if not isinstance(ev, dict):
                continue
            ev_id = ev.get("id", "")
            title = ev.get("name", "Sconosciuto")
            sport = category.get("category", "")
            sources = ev.get("sources", [])

            # Main HD
            main_m3u8 = None
            for src in sources:
                if isinstance(src, dict) and src.get("source") == "hls" and src.get("id") == "s1":
                    main_m3u8 = get_event_m3u8(ev_id)
                    break
            if main_m3u8:
                display = f"[{sport}] {title} (HD)"
                lines.append(f'#EXTINF:-1 tvg-id="{ev_id}",{display}')
                lines.append(main_m3u8)

            # All sources
            for src in sources:
                if not isinstance(src, dict):
                    continue
                src_id = src.get("id")
                src_name = src.get("name", "Sorgente")
                src_type = src.get("source", "")

                if not src_id:
                    continue

                m3u8_url = None
                if src_type == "hls":
                    m3u8_url = get_event_m3u8(ev_id)
                elif src_type == "sd":
                    m3u8_url = get_event_m3u8(ev_id, sd=True)
                elif src_type == "dlhd":
                    m3u8_url = get_channel_m3u8(src_id)

                if m3u8_url:
                    display = f"[{sport}] {title} ({src_name})"
                    lines.append(f'#EXTINF:-1 tvg-id="{src_id}",{display}')
                    lines.append(m3u8_url)

    print(f"✅ Eventi sportivi aggiunti: {len(lines)//2}")
    return lines

def main():
    lines = ["#EXTM3U"]

    # Canali fissi
    for name, url in FIXED_CHANNELS:
        lines.append(f'#EXTINF:-1 tvg-id="{name}",{name}')
        lines.append(url)

    # Eventi sportivi
    sports_lines = build_sports_lines()
    if sports_lines:
        lines.extend(sports_lines)

    # Live TV
    live_tv_lines = get_live_tv_channels()
    if live_tv_lines:
        lines.extend(live_tv_lines)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\n✅ Salvato {OUTPUT_FILE} con {len(lines)//2} voci totali")

if __name__ == "__main__":
    main()