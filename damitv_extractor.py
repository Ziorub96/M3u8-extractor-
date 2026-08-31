import requests
import json
import time
from datetime import datetime, timedelta

BASE_URL = "https://ondemand.st"
API_STREAMS = f"{BASE_URL}/papi/api/streams"
API_EXTRACT = f"{BASE_URL}/papi/extract-url/"
API_TV_RESOLVE = f"{BASE_URL}/papi/tv/resolve/"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

OUTPUT_FILE = "damitv_events.m3u"

# --- CANALI FISSI ---
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

def is_starting_soon(starts_at, hours_before=1):
    """Ritorna True se l'evento inizia entro 'hours_before' ore."""
    if not starts_at:
        return False
    now = int(time.time())
    start = int(starts_at)
    # Mostra eventi che iniziano tra (ora - 30 min) e (ora + hours_before)
    return (start - now) <= (hours_before * 3600) and (start + 1800) >= now

def main():
    print("📡 Recupero streams da papi/api/streams...")
    data = http_get(API_STREAMS, referer=BASE_URL)
    if not data or not data.get("success"):
        print("Errore API")
        return

    fixed_lines = []
    main_hd_lines = []
    all_sources_lines = []
    chno_fixed = 1
    chno_main = 1
    chno_all = 1

    # ---- CANALI FISSI ----
    for name, url in FIXED_CHANNELS:
        fixed_lines.append(f'#EXTINF:-1 tvg-chno="{chno_fixed}" tvg-name="{name}" group-title="Canali Fissi",{name}')
        fixed_lines.append(url)
        chno_fixed += 1

    for category in data["streams"]:
        for ev in category["streams"]:
            title = ev.get("name", "Sconosciuto")
            league = ev.get("league", "")
            sport = category.get("category", "")
            logo = ev.get("poster", "")
            sources = ev.get("sources", [])
            starts_at = ev.get("starts_at")

            # Filtro: solo eventi che iniziano entro 1 ora (o già live)
            if not is_starting_soon(starts_at, hours_before=1):
                continue

            # ---- MAIN HD ----
            main_m3u8 = None
            for src in sources:
                if src.get("source") == "hls" and src.get("id") == "s1":
                    main_m3u8 = get_event_m3u8(ev["id"])
                    break
            if main_m3u8:
                display = f"[{sport}] {title} (HD)"
                tvg_id = f"{sport}.{league}.main.Dummy.us".replace(" ", ".")
                main_hd_lines.append(f'#EXTINF:-1 tvg-chno="{chno_main}" tvg-id="{tvg_id}" tvg-name="{title}" tvg-logo="{logo}" group-title="Main HD",{display}')
                main_hd_lines.append(f'#EXTVLCOPT:http-referrer={BASE_URL}/embed/?id={ev["id"]}')
                main_hd_lines.append(f'#EXTVLCOPT:http-origin={BASE_URL}')
                main_hd_lines.append(f'#EXTVLCOPT:http-user-agent={USER_AGENT}')
                main_hd_lines.append(main_m3u8)
                chno_main += 1

            # ---- ALL SOURCES ----
            for src in sources:
                src_id = src.get("id")
                src_name = src.get("name", "Sorgente")
                src_type = src.get("source", "")

                if not src_id:
                    continue

                m3u8_url = None
                if src_type == "hls":
                    m3u8_url = get_event_m3u8(ev["id"])
                elif src_type == "sd":
                    m3u8_url = get_event_m3u8(ev["id"], sd=True)
                elif src_type == "dlhd":
                    m3u8_url = get_channel_m3u8(src_id)

                if m3u8_url:
                    display = f"[{sport}] {title} ({src_name})"
                    tvg_id = f"{sport}.{league}.{src_id}.Dummy.us".replace(" ", ".")
                    all_sources_lines.append(f'#EXTINF:-1 tvg-chno="{chno_all}" tvg-id="{tvg_id}" tvg-name="{title} - {src_name}" tvg-logo="{logo}" group-title="{league}",{display}')
                    all_sources_lines.append(f'#EXTVLCOPT:http-referrer={BASE_URL}/embed/?id={src_id}')
                    all_sources_lines.append(f'#EXTVLCOPT:http-origin={BASE_URL}')
                    all_sources_lines.append(f'#EXTVLCOPT:http-user-agent={USER_AGENT}')
                    all_sources_lines.append(m3u8_url)
                    chno_all += 1

    # Combina tutto
    final_lines = ["#EXTM3U"]
    final_lines.append("# ===== CANALI FISSI =====")
    final_lines.extend(fixed_lines)
    final_lines.append("# ===== MAIN HD =====")
    final_lines.extend(main_hd_lines)
    final_lines.append("# ===== ALL SOURCES =====")
    final_lines.extend(all_sources_lines)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(final_lines))

    print(f"\n💾 Salvato {OUTPUT_FILE}")
    print(f"   Canali fissi: {len(FIXED_CHANNELS)}")
    print(f"   Main HD: {len(main_hd_lines)//5} voci")
    print(f"   All Sources: {len(all_sources_lines)//5} voci")

if __name__ == "__main__":
    main()