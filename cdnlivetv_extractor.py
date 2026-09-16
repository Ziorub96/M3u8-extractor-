# ================================================================
# CDN Live TV - Extractor
# Estrae canali TV (414) + eventi sport dall'API cdnlivetv.is
# Decodifica token Base64 dal player HTML → URL m3u8 finale
# ================================================================

import requests
import re
import base64
import urllib3
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# === CONFIGURAZIONE ===
API_CHANNELS = "https://api.cdnlivetv.is/api/v1/channels/?user=cdnlivetv&plan=free"
API_EVENTS = "https://api.cdnlivetv.is/api/v1/events/sports/?user=cdnlivetv&plan=free"

CHANNELS_OUTPUT = "cdnlivetv_channels.m3u"
EVENTS_OUTPUT = "cdnlivetv_events.m3u"

MAX_WORKERS = 16
TIMEOUT = 15
RETRIES = 2

# Filtri opzionali (0 = nessun filtro)
MAX_CHANNELS = 0        # 0 = tutti i 414 canali
EVENTS_ONLY_LIVE = False  # True = solo eventi con status "in"/"live"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://cdnlivetv.tv/",
    "Origin": "https://cdnlivetv.tv",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# ================================================================
# UTILITY
# ================================================================
def b64d(s: str) -> bytes:
    """Decodifica Base64 URL-safe con padding automatico."""
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    try:
        return base64.b64decode(s)
    except Exception:
        return b""

def normalize_event_name(name: str) -> str:
    """
    Normalizza il nome evento per deduplica.
    Rimuove suffissi tipo ' - TSN 5 CA', ' - Premiere 3 HD BR', ' - HD', ecc.
    """
    if not name:
        return ""
    # Rimuovi suffisso " - Canale XYZ"
    name = re.sub(r"\s*-\s*[^-]*(?:HD|FHD|SD|UHD|4K|CA|US|UK|BR|PT|ES|IT|DE|FR|AR|MX|NL|PL)\s*$", "", name, flags=re.IGNORECASE)
    # Rimuovi suffisso " - Premiere 1".."7", "TSN 5", ecc.
    name = re.sub(r"\s*-\s*(?:Premiere|TSN|Sportsnet|Sky Sport|DAZN|ESPN|Fox|beIN|Sport TV|Polsat|Movistar|Canal|Nova|V Sport|Stan Sport|Fox Sports|Max Sport|Cosmote|Cytavision|Digi Sport|Prima Sport|Sport 5|Sport)\s*\d*\s*(?:HD|FHD|SD)?\s*$", "", name, flags=re.IGNORECASE)
    # Normalizza per confronto
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = re.sub(r"\s+", " ", name).strip().lower()
    return name

# ================================================================
# RISOLUZIONE TOKEN
# ================================================================
def resolve_m3u8(player_url: str):
    """Ritorna (m3u8_url, errore). Errore=None se OK."""
    if not player_url:
        return None, "no player url"

    last_err = ""
    for attempt in range(RETRIES):
        try:
            r = requests.get(player_url, headers=HEADERS, timeout=TIMEOUT, verify=False)
            if r.status_code in (429, 502, 503):
                time.sleep(2 + attempt)
                last_err = f"HTTP {r.status_code}"
                continue
            if r.status_code != 200:
                return None, f"HTTP {r.status_code}"
            html = r.text

            join_match = re.search(
                r"[A-Za-z_$][\w$]*\s*=\s*((?:[A-Za-z_$][\w$]*\([A-Za-z_$][\w$]*\)\s*\+\s*)+[A-Za-z_$][\w$]*\([A-Za-z_$][\w$]*\))",
                html,
            )
            if not join_match:
                return None, "no concat"

            args = re.findall(r"[A-Za-z_$][\w$]*\(([A-Za-z_$][\w$]*)\)", join_match.group(1))
            vars_dict = dict(re.findall(r"var\s+([A-Za-z_$][\w$]*)\s*=\s*'([^']*)'", html))

            parts = []
            for arg in args:
                if arg in vars_dict:
                    parts.append(b64d(vars_dict[arg]).decode("utf-8", errors="replace"))
            url = "".join(parts)

            if not url.startswith("http"):
                return None, "url malformato"
            return url, None
        except Exception as e:
            last_err = str(e)[:80]
            time.sleep(1)

    return None, last_err or "unknown"

# ================================================================
# PROCESSORI
# ================================================================
def process_channel(ch: dict) -> dict:
    name = ch.get("name", "Unknown")
    code = ch.get("code", "")
    img = ch.get("image", "")
    player_url = ch.get("url", "")
    url, err = resolve_m3u8(player_url)
    return {
        "name": name,
        "code": code,
        "img": img,
        "url": url,
        "err": err,
    }

def process_event_item(event: dict, sport: str) -> list:
    """Ritorna la lista di stream (uno per canale) dell'evento."""
    out = []
    title = event.get("event") or event.get("title") or "Evento"
    league = event.get("tournament") or event.get("league") or ""
    country = event.get("country") or ""
    status = (event.get("status") or "").lower()

    if EVENTS_ONLY_LIVE and status not in ("in", "live", "playing"):
        return out

    for ch in event.get("channels", []) or []:
        ch_name = ch.get("channel_name", "")
        ch_url = ch.get("url", "")
        if not ch_url:
            continue
        full_name = f"{title} - {ch_name}" if ch_name else title
        url, err = resolve_m3u8(ch_url)
        out.append({
            "name": full_name,
            "title": title,
            "league": league,
            "country": country,
            "code": ch.get("channel_code", ""),
            "img": ch.get("image", ""),
            "url": url,
            "err": err,
            "sport": sport,
        })
    return out

# ================================================================
# MAIN
# ================================================================
def main():
    print("=" * 60)
    print(" CDN Live TV - Extractor")
    print("=" * 60)

    # --- 1. Canali TV ---
    print(f"\n[1/2] Scarico API canali TV...")
    try:
        r = requests.get(API_CHANNELS, headers=HEADERS, timeout=TIMEOUT, verify=False)
        data = r.json()
        channels = data.get("channels", [])
        total = data.get("total_channels", len(channels))
        print(f"✓ {total} canali ricevuti")
    except Exception as e:
        print(f"✗ Errore API canali: {e}")
        channels = []

    if MAX_CHANNELS > 0:
        channels = channels[:MAX_CHANNELS]

    results_tv = []
    if channels:
        print(f"Elaboro {len(channels)} canali con {MAX_WORKERS} worker...")
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = [ex.submit(process_channel, ch) for ch in channels]
            done = 0
            for fut in as_completed(futures):
                results_tv.append(fut.result())
                done += 1
                if done % 50 == 0 or done == len(channels):
                    ok = sum(1 for r in results_tv if r["url"])
                    print(f"  [{done}/{len(channels)}] OK: {ok}  ({time.time()-t0:.0f}s)")

    # --- 2. Eventi sport ---
    print(f"\n[2/2] Scarico API eventi sport...")
    results_ev_raw = []
    try:
        r = requests.get(API_EVENTS, headers=HEADERS, timeout=TIMEOUT, verify=False)
        data = r.json()
        root = data.get("cdn-live-tv", {})
        all_events = []
        for sport, evs in root.items():
            if isinstance(evs, list):
                for ev in evs:
                    all_events.append((sport, ev))
        print(f"✓ {len(all_events)} eventi totali")

        if all_events:
            print(f"Elaboro {len(all_events)} eventi con {MAX_WORKERS} worker...")
            t0 = time.time()
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                futures = [ex.submit(process_event_item, ev, sp) for sp, ev in all_events]
                done = 0
                for fut in as_completed(futures):
                    try:
                        results_ev_raw.extend(fut.result())
                    except Exception:
                        pass
                    done += 1
                    if done % 100 == 0 or done == len(all_events):
                        ok = sum(1 for r in results_ev_raw if r.get("url"))
                        print(f"  [{done}/{len(all_events)}] stream OK: {ok}  ({time.time()-t0:.0f}s)")
    except Exception as e:
        print(f"✗ Errore API eventi: {e}")

    # --- 3. Deduplica eventi per titolo ---
    seen_titles = set()
    results_ev = []
    for r in results_ev_raw:
        if not r.get("url"):
            continue
        key = normalize_event_name(r["title"])
        if not key:
            key = r["title"].lower()
        if key in seen_titles:
            continue
        seen_titles.add(key)
        results_ev.append(r)

    # --- 4. Output M3U: Canali TV ---
    tv_ok = [r for r in results_tv if r["url"]]
    with open(CHANNELS_OUTPUT, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for r in tv_ok:
            tvg_id = f' tvg-id="{r["code"]}"' if r["code"] else ""
            logo = f' tvg-logo="{r["img"]}"' if r["img"] else ""
            f.write(f'#EXTINF:-1{tvg_id}{logo} group-title="CDN Live TV Channels",{r["name"]}\n')
            f.write(f'{r["url"]}\n')

    # --- 5. Output M3U: Eventi ---
    with open(EVENTS_OUTPUT, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for r in results_ev:
            tvg_id = f' tvg-id="{r["code"]}"' if r.get("code") else ""
            logo = f' tvg-logo="{r["img"]}"' if r.get("img") else ""
            sport = r.get("sport", "Sport")
            f.write(f'#EXTINF:-1{tvg_id}{logo} group-title="CDN Live TV Events - {sport}",{r["name"]}\n')
            f.write(f'{r["url"]}\n')

    print(f"\n✅ Canali TV OK:    {len(tv_ok)}/{len(results_tv)}")
    print(f"✅ Eventi unici OK: {len(results_ev)}/{len(results_ev_raw)}")
    print(f"📄 {CHANNELS_OUTPUT} | {EVENTS_OUTPUT}")

if __name__ == "__main__":
    main()