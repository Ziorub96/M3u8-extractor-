#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import requests
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

API_URL = "https://api.watchfooty.st/api/v1/matches/all"
OUTPUT_FILE = "watchfooty_events.m3u"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
MAX_MATCHES = 20
TIMEOUT_PER_EMBED = 20
ONLY_LIVE = True

def get_matches() -> list[dict]:
    print("📡 WatchFooty: scarico i match dall'API...")
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.watchfooty.st/",
        "Origin": "https://www.watchfooty.st",
    }
    try:
        r = requests.get(API_URL, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"❌ Errore API WatchFooty: {e}")
        return []

    matches = []
    for m in data:
        streams = m.get("streams") or []
        if not streams:
            continue
        status = (m.get("status") or "").lower()
        if ONLY_LIVE and status not in ("in", "live"):
            continue
        matches.append({
            "id": str(m.get("matchId") or m.get("id") or ""),
            "title": m.get("title", "Sconosciuto"),
            "league": m.get("league", "WatchFooty"),
            "status": status,
            "streams": streams[:3],
        })

    print(f"✅ WatchFooty: {len(matches)} match live (max {MAX_MATCHES})")
    return matches[:MAX_MATCHES]

async def extract_m3u8_from_page(context, embed_url: str) -> str | None:
    captured: list[str] = []
    page = await context.new_page()

    def on_request(req):
        url = req.url
        low = url.lower()
        if ".m3u8" in low and not url.startswith("blob:"):
            if not any(x in low for x in ("ad.", "ads.", "tracker", "analytics", "doubleclick")):
                if url not in captured:
                    captured.append(url)

    page.on("request", on_request)

    try:
        # Naviga verso l'embed principale di sportsembed.su
        await page.goto(embed_url, wait_until="domcontentloaded", timeout=TIMEOUT_PER_EMBED * 1000)
        await asyncio.sleep(2)

        # 1. Cerca eventuale iframe dentro sportsembed.su e ricava il suo SRC
        iframe_element = await page.query_selector("iframe")
        if iframe_element:
            iframe_src = await iframe_element.get_attribute("src")
            if iframe_src and iframe_src.startswith("http"):
                # Naviga direttamente dentro l'URL del player finale
                try:
                    await page.goto(iframe_src, wait_until="domcontentloaded", timeout=10000)
                    await asyncio.sleep(2)
                except Exception:
                    pass

        # 2. Clicca al centro dello schermo/video per sbloccare l'overlay dei player (Clappr/JWPlayer/HlsJS)
        try:
            await page.mouse.click(640, 360)
            await asyncio.sleep(1)
        except Exception:
            pass

        # 3. Attesa breve per intercettare l'm3u8
        for _ in range(5):
            if captured:
                break
            await asyncio.sleep(1)

    except PlaywrightTimeout:
        pass
    except Exception as e:
        pass
    finally:
        await page.close()

    if not captured:
        return None

    # Priorità a playlist/master/index
    for url in captured:
        if re.search(r"(master|index|playlist|manifest)", url, re.I):
            return url
    return captured[0]

async def build_playlist() -> int:
    matches = get_matches()
    if not matches:
        Path(OUTPUT_FILE).write_text("#EXTM3U\n", encoding="utf-8")
        print("⚠️ Nessun match live, file vuoto creato")
        return 0

    lines = ["#EXTM3U"]
    success = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-web-security",  # Bypass per la cattura cross-origin negli iframe
                "--disable-features=IsolateOrigins,site-per-process"
            ]
        )
        
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 720},
            bypass_csp=True, # Permette l'ispezione di script ed iframe protetti
            extra_http_headers={
                "Referer": "https://sportsembed.su/",
                "Origin": "https://sportsembed.su"
            }
        )

        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        for i, match in enumerate(matches, 1):
            print(f"\n[{i}/{len(matches)}] {match['title']}")
            for j, stream in enumerate(match["streams"], 1):
                embed_url = stream.get("url")
                quality = stream.get("quality", "")
                if not embed_url:
                    continue
                print(f"   ⏳ Stream {j} ({quality}): {embed_url[:80]}...")

                m3u8 = await extract_m3u8_from_page(context, embed_url)

                if m3u8:
                    display = f"[{match['league']}] {match['title']} ({quality})"
                    lines.append(f'#EXTINF:-1 tvg-id="wf-{match["id"]}-{j}" group-title="WatchFooty",{display}')
                    lines.append(f'#EXTVLCOPT:http-referrer=https://sportsembed.su/')
                    lines.append(m3u8)
                    success += 1
                    print("   ✅ OK")
                    break
                else:
                    print("   ❌ Nessun m3u8")

        await browser.close()

    Path(OUTPUT_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n✅ WatchFooty: salvato {OUTPUT_FILE} con {success} eventi")
    return success

def main() -> None:
    try:
        count = asyncio.run(build_playlist())
        if count == 0:
            print("Nessuno stream WatchFooty aggiunto")
    except Exception as e:
        print(f"❌ Errore fatale WatchFooty: {e}", file=sys.stderr)
        Path(OUTPUT_FILE).write_text("#EXTM3U\n", encoding="utf-8")
        sys.exit(1)

if __name__ == "__main__":
    main()
