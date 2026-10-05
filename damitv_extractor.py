# ================================================================
# DAMITV Extractor v2 (parallelizzato)
# - DokAgents: timeout ridotto + ThreadPoolExecutor(8)
# - 24/7 / Live TV / Eventi: ThreadPoolExecutor(5)
# - Timeout DAMITV: 15s (era 30s)
# - DokAgents candidati ridotti ai funzionanti noti
# ================================================================

import json
import time
import requests
import urllib3
from concurrent.futures import ThreadPoolExecutor

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://ondemand.st"
API_STREAMS = f"{BASE_URL}/papi/api/streams"
API_MATCHES_TODAY = f"{BASE_URL}/papi/matches/all-today"
API_EXTRACT = f"{BASE_URL}/papi/extract-url/"
API_TV_RESOLVE = f"{BASE_URL}/papi/tv/resolve/"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0.0.0 Safari/537.36")
OUTPUT_FILE = "damitv_events.m3u"

PAST_MINUTES = 30
UPCOMING_MINUTES = 180

# --- PARALLELISMO ---
WORKERS_DAMITV = 5          # API DAMITV (extract-url, tv/resolve)
WORKERS_DOKAGENTS = 8       # test DokAgents
TIMEOUT_DAMITV = 15         # era 30s
TIMEOUT_DOKAGENTS = 4       # era 8s

# ==================== CANALI McQUACK (statici) ====================
MCQUACK_SPORT_CHANNELS = [
    ("Eurosport 1", "http://stream.mcquack.net/176/index.m3u8"),
    ("Eurosport 2", "http://stream.mcquack.net/192/index.m3u8"),
    ("Match! Football 1", "http://stream.mcquack.net/130/index.m3u8"),
    ("Match! Football 2", "http://stream.mcquack.net/142/index.m3u8"),
    ("Match! Football 3", "http://stream.mcquack.net/185/index.m3u8"),
    ("Match! Igra", "http://stream.mcquack.net/188/index.m3u8"),
    ("Match! Strana", "http://stream.mcquack.net/143/index.m3u8"),
    ("Match! Arena", "http://stream.mcquack.net/163/index.m3u8"),
    ("Match TV", "http://stream.mcquack.net/169/index.m3u8"),
    ("Match! Boets", "http://stream.mcquack.net/112/index.m3u8"),
    ("Match Premier", "http://stream.mcquack.net/193/index.m3u8"),
    ("Setanta Sports 1", "http://stream.mcquack.net/234/index.m3u8"),
    ("Setanta Sports 2", "http://stream.mcquack.net/235/index.m3u8"),
    ("Setanta Sports UA", "http://stream.mcquack.net/313/index.m3u8"),
    ("Setanta Sports+", "http://stream.mcquack.net/390/index.m3u8"),
    ("Sport 1 Baltic", "http://stream.mcquack.net/461/index.m3u8"),
    ("Sport 1", "http://stream.mcquack.net/391/index.m3u8"),
    ("Sport 2", "http://stream.mcquack.net/392/index.m3u8"),
    ("Sport 3", "http://stream.mcquack.net/393/index.m3u8"),
    ("Sport 4", "http://stream.mcquack.net/394/index.m3u8"),
    ("Sport 5", "http://stream.mcquack.net/212/index.m3u8"),
    ("Dynamo Kyiv TV", "http://stream.mcquack.net/396/index.m3u8"),
    ("Trace Sports", "http://stream.mcquack.net/256/index.m3u8"),
    ("Suspilne Sport", "http://stream.mcquack.net/444/index.m3u8"),
    ("DiVi Sport", "http://stream.mcquack.net/457/index.m3u8"),
    ("Maincast Cybersport", "http://stream.mcquack.net/487/index.m3u8"),
    ("Maincast Sport", "http://stream.mcquack.net/490/index.m3u8"),
    ("KHL TV", "http://stream.mcquack.net/166/index.m3u8"),
    ("KHL Prime", "http://stream.mcquack.net/178/index.m3u8"),
    ("Viju+ Sport", "http://stream.mcquack.net/333/index.m3u8"),
    ("TV3 Sport LT", "http://stream.mcquack.net/416/index.m3u8"),
    ("Box TV", "http://stream.mcquack.net/154/index.m3u8"),
    ("Belarus 5", "http://stream.mcquack.net/33/index.m3u8"),
    ("Extreme Sports", "http://stream.mcquack.net/148/index.m3u8"),
    ("UDAR", "http://stream.mcquack.net/279/index.m3u8"),
    ("Qazsport", "http://stream.mcquack.net/72/index.m3u8"),
    ("Equalympic", "http://stream.mcquack.net/485/index.m3u8"),
    ("OKKO Football", "http://stream.mcquack.net/491/index.m3u8"),
    ("OKKO Sport", "http://stream.mcquack.net/492/index.m3u8"),
    ("OKKO Prime Sport", "http://stream.mcquack.net/493/index.m3u8"),
]

# ==================== DOKAGENTS (ridotti ai funzionanti) ====================
DOKAGENTS_BASE = "http://dokagents.site/live"
DOKAGENTS_CANDIDATES = [
    # Funzionanti confermati dai log precedenti
    "digisport1", "digisport2", "digisport3", "digisport4",
    "eurosport", "eurosport2",
    # Pochi altri probabili
    "digisport5", "digisportplus",
    "eurosport1",
    "sportklub1", "sportklub2", "sportklub3",
    "arenasport1", "arenasport2",
    "maxsport1", "maxsport2",
]

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})

_event_cache = {}


# ================================================================
# HTTP HELPER
# ================================================================
def http_get_json(url, referer=None):
    headers = {}
    if referer:
        headers["Referer"] = referer

    for attempt in range(3):
        try:
            r = session.get(url, headers=headers,
                            timeout=TIMEOUT_DAMITV, verify=False)
            if r.status_code in (502, 503):
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == 2:
                return None
            time.sleep(3)
    return None


# ================================================================
# RESOLVER (con cache)
# ================================================================
def get_event_m3u8(event_id, sd=False):
    key = (event_id, sd)
    if key in _event_cache:
        return _event_cache[key]

    url = API_EXTRACT + event_id
    if sd:
        url += "?sd=1"

    data = http_get_json(url, referer=f"{BASE_URL}/embed/?id={event_id}")
    result = None
    if data and data.get("success"):
        result = data.get("hlsUrl") or data.get("sdUrl")
    _event_cache[key] = result
    return result


def get_channel_m3u8(ch_id):
    key = (ch_id, "channel")
    if key in _event_cache:
        return _event_cache[key]

    data = http_get_json(API_TV_RESOLVE + ch_id,
                         referer=f"{BASE_URL}/embed/?id={ch_id}")
    result = None
    if data and (data.get("stream") or data.get("url")):
        result = data.get("stream") or data.get("url")
    _event_cache[key] = result
    return result


# ================================================================
# FASE 3: 24/7 (parallelizzata)
# ================================================================
def get_24_7_channels(seen_ids):
    print("📡 Recupero canali 24/7...")
    data = http_get_json(API_STREAMS, referer=BASE_URL)
    if not data or not data.get("success"):
        print("❌ API non raggiungibile")
        return []

    todo = []
    for category in data.get("streams", []):
        if not isinstance(category, dict):
            continue
        cat_name = category.get("category", "").lower()
        for ev in category.get("streams", []):
            if not isinstance(ev, dict):
                continue
            ev_id = ev.get("id", "")
            title = ev.get("name", "Sconosciuto")
            logo = ev.get("poster", "")
            is_always_live = ev.get("always_live") == 1
            is_247 = any(kw in cat_name for kw in ["24/7", "channels"])
            if not is_always_live and not is_247:
                continue
            if ev_id in seen_ids:
                continue
            todo.append((ev_id, title, logo))

    print(f"   {len(todo)} canali 24/7 da risolvere (parallel {WORKERS_DAMITV})...")
    lines = []

    def resolve(item):
        ev_id, title, logo = item
        return (ev_id, title, logo, get_event_m3u8(ev_id))

    with ThreadPoolExecutor(max_workers=WORKERS_DAMITV) as ex:
        for ev_id, title, logo, m3u8 in ex.map(resolve, todo):
            if m3u8:
                seen_ids.add(ev_id)
                lines.append(f'#EXTINF:-1 tvg-id="{ev_id}" tvg-logo="{logo}",{title}')
                lines.append(m3u8)

    print(f"✅ Canali 24/7 aggiunti: {len(lines)//2}")
    return lines


# ================================================================
# FASE 4: Live TV (parallelizzata)
# ================================================================
def get_live_tv_channels(seen_ids):
    ts_url = f"{BASE_URL}/data/ts-channels.json"
    print("📡 Scarico ts-channels.json...")
    data = http_get_json(ts_url, referer=f"{BASE_URL}/livetv")
    if not data or not isinstance(data, dict) or "channels" not in data:
        print("❌ Errore nel recupero")
        return []

    todo = []
    for ch in data["channels"]:
        if not isinstance(ch, dict):
            continue
        daddy_id = ch.get("daddyId")
        name = ch.get("name", "Sconosciuto")
        logo = ch.get("image", "")
        if not daddy_id or daddy_id in seen_ids:
            continue
        todo.append((daddy_id, name, logo))

    print(f"   {len(todo)} canali Live TV da risolvere (parallel {WORKERS_DAMITV})...")
    lines = []

    def resolve(item):
        daddy_id, name, logo = item
        return (daddy_id, name, logo, get_channel_m3u8(daddy_id))

    with ThreadPoolExecutor(max_workers=WORKERS_DAMITV) as ex:
        for daddy_id, name, logo, m3u8 in ex.map(resolve, todo):
            if m3u8:
                seen_ids.add(daddy_id)
                lines.append(f'#EXTINF:-1 tvg-id="{daddy_id}" tvg-logo="{logo}",{name}')
                lines.append(m3u8)

    print(f"✅ Canali Live TV aggiunti: {len(lines)//2}")
    return lines


# ================================================================
# FASE 5: Eventi sportivi (parallelizzata)
# ================================================================
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
    print("📡 Recupero eventi sportivi...")
    data = http_get_json(API_MATCHES_TODAY, referer=f"{BASE_URL}/matches")
    if not data or not isinstance(data, list):
        print("❌ API non raggiungibile")
        return []

    now_ts = int(time.time())
    todo = []
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
        todo.append((stream_id, title, sport, ev.get("poster", "")))

    print(f"   {len(todo)} eventi da risolvere (parallel {WORKERS_DAMITV})...")
    lines = []

    def resolve(item):
        stream_id, title, sport, logo = item
        m3u8 = get_event_m3u8(stream_id)
        if not m3u8:
            m3u8 = get_event_m3u8(stream_id, sd=True)
        return (stream_id, title, sport, logo, m3u8)

    with ThreadPoolExecutor(max_workers=WORKERS_DAMITV) as ex:
        for stream_id, title, sport, logo, m3u8 in ex.map(resolve, todo):
            if m3u8:
                seen_ids.add(stream_id)
                display = f"[{sport}] {title}"
                if logo:
                    lines.append(f'#EXTINF:-1 tvg-id="{stream_id}" tvg-logo="{logo}",{display}')
                else:
                    lines.append(f'#EXTINF:-1 tvg-id="{stream_id}",{display}')
                lines.append(m3u8)

    print(f"✅ Eventi aggiunti: {len(lines)//2}")
    return lines


# ================================================================
# FASE 2: DokAgents (parallelizzata)
# ================================================================
def get_dokagents_channels():
    print(f"📡 Test DokAgents (parallel {WORKERS_DOKAGENTS}, timeout {TIMEOUT_DOKAGENTS}s)...")
    channels = []

    def check(nome):
        url = f"{DOKAGENTS_BASE}/{nome}/mono.m3u8"
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT},
                             timeout=TIMEOUT_DOKAGENTS, verify=False)
            if r.status_code == 200 and r.text.strip().startswith("#EXTM3U"):
                return (nome, url)
        except Exception:
            pass
        return None

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS_DOKAGENTS) as ex:
        for res in ex.map(check, DOKAGENTS_CANDIDATES):
            if res:
                channels.append(res)

    print(f"   Trovati {len(channels)} canali in {time.time()-t0:.1f}s.")
    for nome, url in channels:
        print(f"   ✅ {nome}")
    return channels


# ================================================================
# MAIN
# ================================================================
def main():
    t_start = time.time()
    seen_ids = set()
    lines = ["#EXTM3U"]

    # 1) McQuack Sport (statici)
    for name, url in MCQUACK_SPORT_CHANNELS:
        lines.append(f'#EXTINF:-1 tvg-id="mcq-{name}" group-title="McQuack Sport",{name}')
        lines.append(url)
    print(f"[1/5] McQuack Sport: {len(MCQUACK_SPORT_CHANNELS)} canali (statici)")

    # 2) DokAgents
    print(f"\n[2/5] DokAgents...")
    for name, url in get_dokagents_channels():
        lines.append(f'#EXTINF:-1 tvg-id="dok-{name}" group-title="DokAgents Sport",{name}')
        lines.append(url)

    # 3) DAMITV 24/7
    print(f"\n[3/5] DAMITV 24/7...")
    lines.extend(get_24_7_channels(seen_ids))

    # 4) DAMITV Live TV
    print(f"\n[4/5] DAMITV Live TV...")
    lines.extend(get_live_tv_channels(seen_ids))

    # 5) Eventi sportivi
    print(f"\n[5/5] Eventi sportivi...")
    lines.extend(build_sports_lines(seen_ids))

    if len(lines) > 1:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"\n✅ Salvato {OUTPUT_FILE} con {len(lines)//2} voci totali")
        print(f"⏱️  Tempo totale: {time.time()-t_start:.1f}s")
    else:
        print("\n⚠️ Nessun canale trovato. Il file non è stato sovrascritto.")


if __name__ == "__main__":
    main()