import requests
import json
import time

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

def http_get(url, referer=None):
    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()
    except:
        return None

def get_event_m3u8(event_id, sd=False):
    url = API_EXTRACT + event_id
    if sd:
        url += "?sd=1"
    data = http_get(url, referer=f"{BASE_URL}/embed/?id={event_id}")
    if data and data.get("success"):
        return data.get("hlsUrl") or data.get("sdUrl")
    return None

def get_channel_m3u8(ch_id):
    data = http_get(API_TV_RESOLVE + ch_id, referer=f"{BASE_URL}/embed/?id={ch_id}")
    if data and (data.get("stream") or data.get("url")):
        return data.get("stream") or data.get("url")
    return None

def build_sports_lines():
    """Genera le righe M3U per sport e live TV."""
    print("📡 Recupero streams da papi/api/streams...")
    data = http_get(API_STREAMS, referer=BASE_URL)
    if not data or not data.get("success"):
        print("Errore API")
        return []

    lines = []
    chno = 1

    # ---- CANALI FISSI ----
    for name, url in FIXED_CHANNELS:
        lines.append(f'#EXTINF:-1 tvg-chno="{chno}" tvg-name="{name}" group-title="Canali Fissi",{name}')
        lines.append(url)
        chno += 1

    # ---- LIVE TV CHANNELS (canali 24/7) ----
    for category in data["streams"]:
        for ev in category["streams"]:
            ev_id = ev.get("id", "")
            title = ev.get("name", "Sconosciuto")
            if ev_id.startswith("247-") or ev.get("always_live") == 1:
                m3u8_url = get_channel_m3u8(ev_id)
                if m3u8_url:
                    display = f"[LIVE TV] {title}"
                    lines.append(f'#EXTINF:-1 tvg-chno="{chno}" tvg-name="{title}" group-title="Live TV",{display}')
                    lines.append(f'#EXTVLCOPT:http-referrer={BASE_URL}/embed/?id={ev_id}')
                    lines.append(f'#EXTVLCOPT:http-origin={BASE_URL}')
                    lines.append(f'#EXTVLCOPT:http-user-agent={USER_AGENT}')
                    lines.append(m3u8_url)
                    chno += 1

    # ---- EVENTI SPORTIVI ----
    for category in data["streams"]:
        for ev in category["streams"]:
            ev_id = ev.get("id", "")
            title = ev.get("name", "Sconosciuto")
            league = ev.get("league", "")
            sport = category.get("category", "")
            logo = ev.get("poster", "")
            sources = ev.get("sources", [])

            # Salta canali 24/7 già gestiti
            if ev_id.startswith("247-") or ev.get("always_live") == 1:
                continue

            # ---- MAIN HD (solo s1) ----
            main_m3u8 = None
            for src in sources:
                if src.get("source") == "hls" and src.get("id") == "s1":
                    main_m3u8 = get_event_m3u8(ev_id)
                    break
            if main_m3u8:
                display = f"[{sport}] {title} (HD)"
                lines.append(f'#EXTINF:-1 tvg-chno="{chno}" tvg-name="{title}" tvg-logo="{logo}" group-title="Main HD",{display}')
                lines.append(f'#EXTVLCOPT:http-referrer={BASE_URL}/embed/?id={ev_id}')
                lines.append(f'#EXTVLCOPT:http-origin={BASE_URL}')
                lines.append(f'#EXTVLCOPT:http-user-agent={USER_AGENT}')
                lines.append(main_m3u8)
                chno += 1

            # ---- ALL SOURCES ----
            for src in sources:
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
                    lines.append(f'#EXTINF:-1 tvg-chno="{chno}" tvg-name="{title} - {src_name}" tvg-logo="{logo}" group-title="{league}",{display}')
                    lines.append(f'#EXTVLCOPT:http-referrer={BASE_URL}/embed/?id={src_id}')
                    lines.append(f'#EXTVLCOPT:http-origin={BASE_URL}')
                    lines.append(f'#EXTVLCOPT:http-user-agent={USER_AGENT}')
                    lines.append(m3u8_url)
                    chno += 1

    return lines

def main():
    lines = build_sports_lines()
    if not lines:
        print("Nessun dato, file non scritto.")
        return

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        f.write("\n".join(lines))

    print(f"\n✅ Salvato {OUTPUT_FILE} con {len(lines)//5} voci")

if __name__ == "__main__":
    main()