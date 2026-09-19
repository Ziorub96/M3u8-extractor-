# ================================================================
# CDN Live TV - Extractor v3 (Soccer only + anti rate-limit)
# Estrae canali TV (414) + eventi calcio dall'API cdnlivetv.is
# ================================================================

import requests
import re
import base64
import urllib3
import time
import random
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# === CONFIGURAZIONE ===
API_CHANNELS = "https://api.cdnlivetv.is/api/v1/channels/?user=cdnlivetv&plan=free"
API_EVENTS = "https://api.cdnlivetv.is/api/v1/events/sports/?user=cdnlivetv&plan=free"

CHANNELS_OUTPUT = "cdnlivetv_channels.m3u"
EVENTS_OUTPUT = "cdnlivetv_events.m3u"

# --- SOLO CALCIO ---
ONLY_SPORT = "Soccer"          # None = tutti gli sport

# --- ANTI RATE-LIMIT ---
MAX_WORKERS = 3
TIMEOUT = 20
RETRIES = 4
JITTER_MIN = 0.5
JITTER_MAX = 1.2
PAUSE_BETWEEN_PHASES = 45
BACKOFF_BASE = 3

MAX_CHANNELS = 0
EVENTS_ONLY_LIVE = False

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://cdnlivetv.tv/",
    "Origin": "https://cdnlivetv.tv",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

_resolve_cache: dict = {}


# ================================================================
# UTILITY
# ================================================================
def b64d(s: str) -> bytes:
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    try:
        return base64.b64decode(s)
    except Exception:
        return b""


def normalize_event_name(name: str) -> str:
    if not name:
        return ""
    name = re.sub(
        r"\s*-\s*[^-]*(?:HD|FHD|SD|UHD|4K|CA|US|UK|BR|PT|ES|IT|DE|FR|AR|MX|NL|PL)\s*$",
        "", name, flags=re.IGNORECASE,
    )
    name = re.sub(
        r"\s*-\s*(?:Premiere|TSN|Sportsnet|Sky Sport|DAZN|ESPN|Fox|beIN|Sport TV|"
        r"Polsat|Movistar|Canal|Nova|V Sport|Stan Sport|Fox Sports|Max Sport|"
        r"Cosmote|Cytavision|Digi Sport|Prima Sport|Sport 5|Sport)\s*\d*\s*"
        r"(?:HD|FHD|SD)?\s*$",
        "", name, flags=re.IGNORECASE,
    )
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", name).strip().lower()


# ================================================================
# RISOLUZIONE TOKEN
# ================================================================
def resolve_m3u8(player_url: str):
    if not player_url:
        return None, "no player url"
    if player_url in _resolve_cache:
        return _resolve_cache[player_url]

    time.sleep(random.uniform(JITTER_MIN, JITTER_MAX))
    last_err = ""

    for attempt in range(RETRIES):
        try:
            r = requests.get(player_url, headers=HEADERS,
                             timeout=TIMEOUT, verify=False)

            if r.status_code in (429, 502, 503):
                time.sleep((2 ** attempt) * BACKOFF_BASE + random.uniform(0, 2))
                last_err = f"HTTP {r.status_code}"
                continue

            if r.status_code != 200:
                result = (None, f"HTTP {r.status_code}")
                _resolve_cache[player_url] = result
                return result

            html = r.text
            join_match = re.search(
                r"[A-Za-z_$][\w$]*\s*=\s*("
                r"(?:[A-Za-z_$][\w$]*\([A-Za-z_$][\w$]*\)\s*\+\s*)+"
                r"[A-Za-z_$][\w$]*\([A-Za-z_$][\w$]*\))",
                html,
            )
            if not join_match:
                result = (None, "no concat")
                _resolve_cache[player_url] = result
                return result

            args = re.findall(
                r"[A-Za-z_$][\w$]*\(([A-Za-z_$][\w$]*)\)", join_match.group(1)
            )
            vars_dict = dict(re.findall(
                r"var\s+([A-Za-z_$][\w$]*)\s*=\s*'([^']*)'", html
            ))

            parts = [b64d(vars_dict[a]).decode("utf-8", errors="replace")
                     for a in args if a in vars_dict]
            url = "".join(parts)

            result = (url, None) if url.startswith("http") else (None, "url malformato")
            _resolve_cache[player_url] = result
            return result

        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:60]}"
            time.sleep(1 + attempt)

    result = (None, last_err or "unknown")
    _resolve_cache[player_url] = result
    return result


# ================================================================
# PROCESSORI
# ================================================================
def process_channel(ch: dict) -> dict:
    url, err = resolve_m3u8(ch.get("url", ""))
    return {
        "name": ch.get("name", "Unknown"),
        "code": ch.get("code", ""),
        "img": ch.get("image", ""),
        "url": url,
        "err": err,
    }


def process_event_item(event: dict, sport: str) -> list:
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
            "name": full_name, "title": title, "league": league,
            "country": country, "code": ch.get("channel_code", ""),
            "img": ch.get("image", ""), "url": url, "err": err, "sport": sport,
        })
    return out


# ================================================================
# FETCH API
# ================================================================
def fetch_api(url: str, label: str):
    for attempt in range(RETRIES):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=False)
            if r.status_code in (429, 502, 503):
                wait = (2 ** attempt) * BACKOFF_BASE + random.uniform(0, 2)
                print(f"  ⏸️ {label} HTTP {r.status_code}, attendo {wait:.1f}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"  ⚠️ {label} attempt {attempt+1}: {type(e).__name__}: {e}")
            time.sleep(2 + attempt)
    return None


# ================================================================
# MAIN
# ================================================================
def main():
    print("=" * 60)
    print(" CDN Live TV - Extractor v3 (Soccer only)")
    print(f" Workers: {MAX_WORKERS} | Jitter: {JITTER_MIN}-{JITTER_MAX}s")
    if ONLY_SPORT:
        print(f" Filtro sport: {ONLY_SPORT}")
    print("=" * 60)

    # ============ FASE 1: EVENTI ============
    print(f"\n[1/2] Scarico API eventi sport...")
    data_ev = fetch_api(API_EVENTS, "API eventi")

    results_ev_raw = []
    if data_ev:
        root = data_ev.get("cdn-live-tv", {})
        all_events = []
        for sport, evs in root.items():
            if not isinstance(evs, list):
                continue
            if ONLY_SPORT and sport != ONLY_SPORT:
                continue
            for ev in evs:
                all_events.append((sport, ev))

        print(f"✓ {len(all_events)} eventi" +
              (f" (solo {ONLY_SPORT})" if ONLY_SPORT else ""))

        if all_events:
            est_sec = len(all_events) * ((JITTER_MIN + JITTER_MAX) / 2) / MAX_WORKERS
            print(f"⏱️  Stima: ~{est_sec/60:.0f} min")

            t0 = time.time()
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                futures = [ex.submit(process_event_item, ev, sp)
                           for sp, ev in all_events]
                done = 0
                ok_total = 0
                last_log = 0
                for fut in as_completed(futures):
                    try:
                        chunk = fut.result()
                        results_ev_raw.extend(chunk)
                        ok_total += sum(1 for r in chunk if r.get("url"))
                    except Exception:
                        pass
                    done += 1
                    now = time.time()
                    if done % 100 == 0 or (now - last_log) > 30:
                        elapsed = now - t0
                        rate = done / elapsed if elapsed > 0 else 0
                        eta = (len(all_events) - done) / rate if rate > 0 else 0
                        print(f"  [{done}/{len(all_events)}] "
                              f"OK: {ok_total} | {elapsed:.0f}s | ETA {eta:.0f}s")
                        last_log = now

    # ============ PAUSA ============
    print(f"\n⏸️  Pausa {PAUSE_BETWEEN_PHASES}s...")
    time.sleep(PAUSE_BETWEEN_PHASES)

    # ============ FASE 2: CANALI ============
    print(f"\n[2/2] Scarico API canali TV...")
    data_ch = fetch_api(API_CHANNELS, "API canali")

    channels = []
    if data_ch:
        channels = data_ch.get("channels", [])
        print(f"✓ {data_ch.get('total_channels', len(channels))} canali ricevuti")

    if MAX_CHANNELS > 0:
        channels = channels[:MAX_CHANNELS]

    results_tv = []
    if channels:
        print(f"Elaboro {len(channels)} canali...")
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = [ex.submit(process_channel, ch) for ch in channels]
            done = 0
            last_log = 0
            for fut in as_completed(futures):
                results_tv.append(fut.result())
                done += 1
                now = time.time()
                if done % 50 == 0 or (now - last_log) > 30 or done == len(channels):
                    ok = sum(1 for r in results_tv if r["url"])
                    print(f"  [{done}/{len(channels)}] OK: {ok}  ({now-t0:.0f}s)")
                    last_log = now

    # ============ DEDUPLICA ============
    seen_titles = set()
    results_ev = []
    for r in results_ev_raw:
        if not r.get("url"):
            continue
        key = normalize_event_name(r["title"]) or r["title"].lower()
        if key in seen_titles:
            continue
        seen_titles.add(key)
        results_ev.append(r)

    # ============ OUTPUT ============
    tv_ok = [r for r in results_tv if r["url"]]
    with open(CHANNELS_OUTPUT, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for r in tv_ok:
            tvg_id = f' tvg-id="{r["code"]}"' if r["code"] else ""
            logo = f' tvg-logo="{r["img"]}"' if r["img"] else ""
            f.write(f'#EXTINF:-1{tvg_id}{logo} '
                    f'group-title="CDN Live TV Channels",{r["name"]}\n')
            f.write(f'{r["url"]}\n')

    with open(EVENTS_OUTPUT, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for r in results_ev:
            tvg_id = f' tvg-id="{r["code"]}"' if r.get("code") else ""
            logo = f' tvg-logo="{r["img"]}"' if r.get("img") else ""
            sport = r.get("sport", "Sport")
            f.write(f'#EXTINF:-1{tvg_id}{logo} '
                    f'group-title="CDN Live TV Events - {sport}",{r["name"]}\n')
            f.write(f'{r["url"]}\n')

    print(f"\n✅ Canali TV OK:    {len(tv_ok)}/{len(results_tv)}")
    print(f"✅ Eventi unici OK: {len(results_ev)}/{len(results_ev_raw)}")
    print(f"📄 {CHANNELS_OUTPUT} | {EVENTS_OUTPUT}")


if __name__ == "__main__":
    main()