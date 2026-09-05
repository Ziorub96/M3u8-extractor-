# watchfooty_direct_extractor.py – estrae URL diretti m3u8 da WatchFooty
import json
import time
import requests
from urllib.parse import urljoin, urlencode
import re

API_URL = "https://api.watchfooty.st"
BASE_URL = "https://www.watchfooty.st"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
OUTPUT_FILE = "watchfooty_events.m3u"

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})

def build_api_url(live: bool, event_id: str | None = None) -> str:
    if live:
        endpoint = "_internal/trpc/sports.getSportsLiveMatchesCount,sports.getPopularMatches,sports.getPopularLiveMatches"
    else:
        endpoint = "_internal/trpc/sports.getSportsLiveMatchesCount,sports.getMatchById"

    url = urljoin(API_URL, endpoint)

    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    end = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + 86400))

    if live:
        input_data = {
            "0": {"json": {"start": now, "end": end}},
            "1": {"json": None, "meta": {"values": ["undefined"]}},
            "2": {"json": None, "meta": {"values": ["undefined"]}},
        }
    else:
        input_data = {
            "0": {"json": {"start": now, "end": end}},
            "1": {"json": {"id": event_id, "withoutAdditionalInfo": True, "withoutLinks": False}},
        }

    params = {
        "batch": "1",
        "input": json.dumps(input_data, separators=(",", ":")),
    }

    return f"{url}?{urlencode(params)}"

def get_live_matches():
    url = build_api_url(live=True)
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
        api_data = None
        if isinstance(data, list) and data:
            for item in reversed(data):
                result = item.get("result", {}).get("data", {}).get("json")
                if result is not None:
                    api_data = result
                    break
        if not api_data:
            print("❌ Nessun dato live trovato")
            return []
        if isinstance(api_data, list):
            return api_data
        for key in ("matches", "events", "items"):
            if key in api_data:
                return api_data[key]
        return []
    except Exception as e:
        print(f"❌ Errore API live: {e}")
        return []

def get_embed_url(event_id: str) -> str | None:
    url = build_api_url(live=False, event_id=event_id)
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
        api_data = None
        if isinstance(data, list) and data:
            for item in reversed(data):
                result = item.get("result", {}).get("data", {}).get("json")
                if result is not None:
                    api_data = result
                    break
        if not api_data:
            return None
        links = api_data.get("fixtureData", {}).get("links", [])
        if not links:
            return None
        quality_links = [link for link in links if link.get("wld") and "e" not in link["wld"]]
        if not quality_links:
            return None
        quality_links.sort(key=lambda x: x.get("viewerCount") or -1, reverse=True)
        best = quality_links[0]
        parts = [
            best["gi"],
            best["t"],
            best["wld"]["cn"],
            best["wld"]["sn"],
        ]
        embed_path = "/".join(parts)
        return f"https://sportsembed.su/embed/{embed_path}?player=clappr&autoplay=true"
    except Exception as e:
        print(f"⚠️ Errore dettaglio evento {event_id}: {e}")
        return None

def resolve_embed_to_m3u8(embed_url: str) -> str | None:
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": BASE_URL,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        r = session.get(embed_url, headers=headers, timeout=15)
        if r.status_code != 200:
            return None
        match = re.search(r'var playbackURL\s*=\s*["\']([^"\']+)["\']', r.text)
        if match:
            return match.group(1).replace('\\/', '/')
        m3u8_urls = re.findall(r'https?://[^\s"\']+\.m3u8[^\s"\']*', r.text)
        if m3u8_urls:
            return m3u8_urls[0]
        return None
    except Exception as e:
        print(f"⚠️ Errore risolvendo embed {embed_url}: {e}")
        return None

def main():
    print("📡 Recupero match live da WatchFooty...")
    live_matches = get_live_matches()
    if not live_matches:
        print("⚠️ Nessun evento live trovato")
        return

    lines = ["#EXTM3U"]
    count = 0
    for match in live_matches:
        if not isinstance(match, dict):
            continue
        event_id = match.get("id")
        title = match.get("title", "Sconosciuto")
        league = match.get("league", "WatchFooty")
        if not event_id:
            continue

        embed_url = get_embed_url(event_id)
        if not embed_url:
            print(f"❌ Nessun embed per {title}")
            continue

        m3u8_url = resolve_embed_to_m3u8(embed_url)
        if not m3u8_url:
            print(f"❌ Nessun m3u8 per {title}")
            continue

        display = f"[{league}] {title}"
        lines.append(f'#EXTINF:-1 tvg-id="{event_id}" group-title="WatchFooty",{display}')
        lines.append(m3u8_url)
        count += 1
        print(f"✅ Aggiunto: {title}")

    if count > 0:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"\n✅ Salvato {OUTPUT_FILE} con {count} eventi")
    else:
        print("⚠️ Nessun evento aggiunto alla playlist")

if __name__ == "__main__":
    main()