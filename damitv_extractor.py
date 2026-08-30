import requests
import json
import time

BASE_URL = "https://ondemand.st"
API_EVENTS = f"{BASE_URL}/papi/matches/all-today"
API_EXTRACT = f"{BASE_URL}/papi/extract-url/"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def fetch_events():
    headers = {"User-Agent": USER_AGENT, "Referer": BASE_URL}
    try:
        r = requests.get(API_EVENTS, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"Errore nel recupero eventi: {e}")
        return []

def get_m3u8_url(stream_id):
    headers = {"User-Agent": USER_AGENT, "Referer": f"{BASE_URL}/embed/?id={stream_id}"}
    try:
        r = requests.get(API_EXTRACT + stream_id, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("success"):
            return data.get("hlsUrl") or data.get("sdUrl")
        else:
            print(f"  Errore API per {stream_id}: {data.get('error')}")
    except Exception as e:
        print(f"  Errore richiesta m3u8 per {stream_id}: {e}")
    return None

def main():
    print("📡 Recupero eventi da ondemand.st...")
    events = fetch_events()
    if not events:
        print("Nessun evento trovato.")
        return

    print(f"🏟️ Trovati {len(events)} eventi totali. Estraggo i link m3u8...")

    m3u_lines = ["#EXTM3U"]
    for i, ev in enumerate(events, 1):
        stream_id = ev.get("id")
        title = ev.get("title", "Sconosciuto")
        league = ev.get("league", "")
        sport = ev.get("sport", "")
        date_ts = ev.get("date")
        logo = ev.get("poster", "")

        if not stream_id or stream_id.lower().startswith("dl-"):
            continue  # salta eventi non live o non validi

        print(f"🔄 {i}/{len(events)} - {title} ({league})")
        m3u8_url = get_m3u8_url(stream_id)
        if m3u8_url:
            display_name = f"[{sport}] {title} ({league})"
            referer = f"{BASE_URL}/embed/?id={stream_id}"
            m3u_lines.append(f'#EXTINF:-1 tvg-name="{title}" tvg-logo="{logo}" group-title="{league}",{display_name}')
            m3u_lines.append(f'#EXTVLCOPT:http-referrer={referer}')
            m3u_lines.append(f'#EXTVLCOPT:http-user-agent={USER_AGENT}')
            m3u_lines.append(m3u8_url)
            print(f"   ✅ {m3u8_url}")
        else:
            print(f"   ❌ Nessun m3u8 disponibile")

    output_file = "damitv_events.m3u"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("\n".join(m3u_lines))
    print(f"\n💾 Playlist salvata in {output_file}")
    print("   Scarica il file e aprilo con VLC/GSE IPTV.")

if __name__ == "__main__":
    main()