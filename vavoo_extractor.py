#!/usr/bin/env python3
# vavoo_extractor.py - Ottimizzato per Samsung TV IPTV

import time
import uuid
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

OUTPUT_FILE = "vavoo_streams.m3u"
GROUPS = ["Italy", "France", "Germany", "Spain", "United Kingdom"]
MAX_WORKERS = 8

class VavooResolver:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "MediaHubMX/2"})

    def get_auth_signature(self):
        unique_id = str(uuid.uuid4())
        now = int(time.time() * 1000)
        data = {
            "token": "",
            "reason": "app-focus",
            "locale": "de",
            "theme": "dark",
            "metadata": {
                "device": {"type": "desktop", "uniqueId": unique_id},
                "os": {"name": "win32", "version": "Windows 10 Pro"},
                "app": {"platform": "electron"},
                "version": {"package": "tv.vavoo.app", "binary": "1.3.1", "js": "1.3.1"}
            },
            "package": "tv.vavoo.app",
            "version": "1.3.1",
            "process": "app",
            "firstAppStart": now - 86400000,
            "lastAppStart": now,
        }
        try:
            r = self.session.post("https://www.vavoo.tv/api/app/ping", json=data, timeout=10)
            return r.json().get("addonSig")
        except:
            return None

    def get_channels(self, group="Italy"):
        signature = self.get_auth_signature()
        if not signature:
            return []
        headers = {
            "user-agent": "okhttp/4.11.0",
            "accept": "application/json",
            "content-type": "application/json; charset=utf-8",
            "mediahubmx-signature": signature
        }
        channels = []
        cursor = 0
        while True:
            data = {
                "language": "de",
                "region": "AT",
                "catalogId": "iptv",
                "id": "iptv",
                "adult": False,
                "search": "",
                "sort": "name",
                "filter": {"group": group},
                "cursor": cursor,
                "clientVersion": "3.0.2"
            }
            try:
                r = self.session.post("https://vavoo.to/mediahubmx-catalog.json", json=data, headers=headers, timeout=12)
                j = r.json()
                channels.extend(j.get("items", []))
                cursor = j.get("nextCursor")
                if not cursor:
                    break
            except:
                break
        return channels

    def resolve_link(self, link):
        signature = self.get_auth_signature()
        if not signature:
            return None
        headers = {
            "user-agent": "okhttp/4.11.0",
            "accept": "application/json",
            "content-type": "application/json; charset=utf-8",
            "mediahubmx-signature": signature
        }
        data = {
            "language": "de",
            "region": "AT",
            "url": link,
            "clientVersion": "3.0.2"
        }
        try:
            r = self.session.post("https://vavoo.to/mediahubmx-resolve.json", json=data, headers=headers, timeout=10)
            result = r.json()
            if isinstance(result, list) and result and result[0].get("url"):
                return result[0]["url"]
            if isinstance(result, dict) and result.get("url"):
                return result["url"]
        except:
            pass
        return None

def clean_name(name: str) -> str:
    return name.replace('"', '').replace(',', ' ').strip()

def main():
    resolver = VavooResolver()
    all_results = []

    for group in GROUPS:
        print(f"📡 Gruppo: {group}")
        channels = resolver.get_channels(group)
        print(f"   Trovati {len(channels)} canali")

        def resolve_one(ch):
            name = ch.get("name", "Canale")
            url = ch.get("url")
            if not url:
                return None
            resolved = resolver.resolve_link(url)
            if resolved:
                return (clean_name(name), resolved, group)
            return None

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = [executor.submit(resolve_one, ch) for ch in channels]
            for fut in as_completed(futures):
                res = fut.result()
                if res:
                    all_results.append(res)
                    print(f"   ✅ {res[0]}")

    # Deduplica
    seen = set()
    unique = []
    for name, url, group in all_results:
        if url not in seen:
            seen.add(url)
            unique.append((name, url, group))

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for name, url, group in unique:
            f.write(f'#EXTINF:-1 tvg-name="{name}" group-title="Vavoo {group}",{name}\n')
            f.write(f"{url}\n")

    print(f"\n✅ Salvato {len(unique)} canali → {OUTPUT_FILE}")

if __name__ == "__main__":
    main()