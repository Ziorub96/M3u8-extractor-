import json
import time
import requests

BASE_URL = "https://ondemand.st"
API_STREAMS = f"{BASE_URL}/papi/api/streams"
API_MATCHES_TODAY = f"{BASE_URL}/papi/matches/all-today"
API_EXTRACT = f"{BASE_URL}/papi/extract-url/"
API_TV_RESOLVE = f"{BASE_URL}/papi/tv/resolve/"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
OUTPUT_FILE = "damitv_events.m3u"

PAST_MINUTES = 30
UPCOMING_MINUTES = 180

# Rimossa la lista FIXED_CHANNELS. Ora i canali dokagents vengono scoperti dinamicamente.

# Lista di possibili nomi canale su dokagents.site/live
DOKAGENTS_CANDIDATES = [
    "digisport1", "digisport2", "digisport3", "digisport4",
    "digisport5", "digisport6", "digisportplus", "digisportnews",
    "eurosport", "eurosport1", "eurosport2", "eurosport2hd",
    "sportklub1", "sportklub2", "sportklub3", "sportklub4", "sportklub5", "sportklub6",
    "arenasport1", "arenasport2", "arenasport3", "arenasport4", "arenasport5", "arenasport6",
    "maxsport1", "maxsport2", "maxsport3", "maxsport4",
    "matchtv", "matchfutbol1", "matchfutbol2", "matchfutbol3",
    "setantasport", "setantasport1", "setantasport2",
    "sport1", "sport2", "sport3", "sport4", "sport5",
    "skysport1", "skysport2", "skysport3", "skysport4", "skysport5",
    "movistar", "dazn1", "dazn2", "dazn3", "dazn4",
    "canalsport", "canalplus", "canalplus1", "canalplus2",
    "nbatv", "nflnetwork", "nhl", "mlb", "ufc", "boxing", "fight",
    "golf", "tennis", "racing", "motorsport", "extreme",
    "redbulltv", "f1", "motoamerica", "supercross",
    "futbol", "football", "soccer", "calcio", "seriea", "premierleague",
    "la-liga", "ligue1", "bundesliga", "championsleague", "europaleague",
    "copa", "libertadores", "sudamericana", "concacaf", "afc", "uefa", "fifa"
]

DOKAGENTS_BASE = "http://dokagents.site/live"   # uso HTTP per compatibilità TV
DOKAGENTS_USER_AGENT = USER_AGENT

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})

_event_cache = {}

def http_get_json(url, referer=None):
    headers = {}
    if referer:
        headers["Referer"] = referer

    for attempt in range(3):
        try:
            r = session.get(url, headers=headers, timeout=30)
            if r.status_code in (502, 503):
                wait = 5 * (attempt + 1)
                print(f"⚠️ {r.status_code} su {url[:80]} → riprovo tra {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == 2:
                print(f"❌ Errore richiesta {url}: {e}")
                return None
            time.sleep(3)
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

            is_always_live = ev.get("always_live") == 1
            is_247_category = any(kw in category_name for kw in ["24/7", "channels"])

            if not is_always_live and not is_247_category:
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

def is_relevant_event(event, now_ts):
    start_raw = event.get("date")
    if not start_raw:
        return False
    if len(str(start_raw)) > 10:
        start_ts = int(str(start_raw)[:-3])
    else:
        start_ts = int(start_raw)
    return (now_ts - PAST_MINUTES * 60) <= start_ts <= (now_ts + UPCOMING_MINUTES * 60)

def build_sports_lines(seen_ids):
    print("📡 Recupero eventi sportivi live/imminenti da papi/matches/all-today...")
    data = http_get_json(API_MATCHES_TODAY, referer=f"{BASE_URL}/matches")
    if not data:
        print("❌ API non raggiungibile o dati non validi")
        return []

    if not isinstance(data, list):
        print("❌ Formato dati inaspettato")
        return []

    now_ts = int(time.time())
    lines = []
    event_count = 0

    for ev in data:
        if not isinstance(ev, dict):
            continue

        title = ev.get("title", "Sconosciuto")
        sport = ev.get("league", "")
        stream_id = ev.get("id", "")

        if not title or not stream_id:
            continue

        if stream_id.startswith("247") or sport.startswith("24/7"):
            continue
        if stream_id.lower().startswith("dl-"):
            continue

        if not is_relevant_event(ev, now_ts):
            continue

        event_count += 1
        print(f"⚽ Processo evento: {title}")

        m3u8_url = get_event_m3u8(stream_id)
        if not m3u8_url:
            m3u8_url = get_event_m3u8(stream_id, sd=True)

        if m3u8_url:
            seen_ids.add(stream_id)
            display = f"[{sport}] {title}"
            logo = ev.get("poster", "")
            if logo:
                lines.append(f'#EXTINF:-1 tvg-id="{stream_id}" tvg-logo="{logo}",{display}')
            else:
                lines.append(f'#EXTINF:-1 tvg-id="{stream_id}",{display}')
            lines.append(m3u8_url)

    print(f"✅ Eventi sportivi aggiunti: {len(lines)//2} (da {event_count} eventi)")
    return lines

def get_dokagents_channels():
    """Scopre canali disponibili su dokagents.site/live (HTTP) e restituisce lista di tuple (nome, url)."""
    print("📡 Ricerca canali su dokagents.site/live (HTTP)...")
    channels = []
    headers = {"User-Agent": DOKAGENTS_USER_AGENT}
    patterns = ["mono.m3u8", "index.m3u8"]

    for nome in DOKAGENTS_CANDIDATES:
        for pattern in patterns:
            url = f"{DOKAGENTS_BASE}/{nome}/{pattern}"
            try:
                r = requests.get(url, headers=headers, timeout=8, verify=False)
                if r.status_code == 200 and r.text.strip().startswith("#EXTM3U"):
                    channels.append((nome, url))
                    print(f"   ✅ {nome} -> {url}")
                    break
            except Exception:
                pass
            time.sleep(0.2)  # piccolo ritardo

    print(f"   Trovati {len(channels)} canali dokagents.")
    return channels

def main():
    seen_ids = set()
    lines = ["#EXTM3U"]

    # Invece di canali fissi, aggiungiamo i canali dokagents scoperti dinamicamente
    dokagents_channels = get_dokagents_channels()
    for name, url in dokagents_channels:
        lines.append(f'#EXTINF:-1 tvg-id="dok-{name}" group-title="DokAgents Sport",{name}')
        lines.append(url)

    lines.extend(get_24_7_channels(seen_ids))
    lines.extend(get_live_tv_channels(seen_ids))
    lines.extend(build_sports_lines(seen_ids))

    if len(lines) > 1:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"\n✅ Salvato {OUTPUT_FILE} con {len(lines)//2} voci totali")
    else:
        print("\n⚠️ Nessun canale trovato. Il file non è stato sovrascritto.")

if __name__ == "__main__":
    main()