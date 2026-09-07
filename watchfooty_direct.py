#!/usr/bin/env python3
"""
WatchFooty → watchfooty_events.m3u
API pubblica + Playwright (intercetta m3u8 dagli embed).
Versione avanzata con click automatici e gestione iframe annidati.
"""

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
TIMEOUT_PER_EMBED = 25
HEADLESS = True
ONLY_LIVE = True

def get_matches() -> list[dict]:
    print("📡 WatchFooty: scarico i match dall'API...")
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
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
        quality_order = ["deluxe", "hd", "sigma", "sd", "auto"]
        streams_sorted = sorted(
            streams,
            key=lambda s: quality_order.index(s.get("quality", "auto").lower())
                if s.get("quality", "auto").lower() in quality_order else len(quality_order)
        )
        matches.append({
            "id": str(m.get("matchId") or m.get("id") or ""),
            "title": m.get("title", "Sconosciuto"),
            "league": m.get("league", "WatchFooty"),
            "status": status,
            "streams": streams_sorted[:3],
        })

    print(f"✅ WatchFooty: {len(matches)} match live (max {MAX_MATCHES})")
    return matches[:MAX_MATCHES]

async def capture_m3u8_from_frame(frame, captured: list[str], wait_seconds=5):
    """Cattura richieste .m3u8 da un frame specifico."""
    def on_request(request):
        url = request.url
        low = url.lower()
        if ".m3u8" not in low or url.startswith("blob:"):
            return
        if any(x in low for x in ("ad.", "ads.", "tracker", "analytics", "doubleclick", "telemetry")):
            return
        if url not in captured:
            captured.append(url)

    frame.on("request", on_request)
    try:
        for selector in ['.play-button', '#play', 'button[class*="play"]', 'video', '.jwplayer', '.vjs-big-play-button']:
            try:
                locator = frame.locator(selector).first
                if await locator.count() > 0 and await locator.is_visible():
                    await locator.click(timeout=1000)
                    break
            except Exception:
                pass
        await asyncio.sleep(wait_seconds)
    except Exception:
        pass
    finally:
        try:
            frame.remove_listener("request", on_request)
        except Exception:
            pass

async def extract_m3u8(page, embed_url: str) -> str | None:
    captured: list[str] = []

    # Handler definito con nome per consentire una corretta deregistrazione
    def page_request_handler(req):
        url = req.url
        low = url.lower()
        if ".m3u8" in low and not url.startswith("blob:"):
            if not any(x in low for x in ("ad.", "ads.", "tracker", "analytics")):
                if url not in captured:
                    captured.append(url)

    page.on("request", page_request_handler)

    try:
        await page.goto(embed_url, wait_until="domcontentloaded", timeout=TIMEOUT_PER_EMBED * 1000)
        await asyncio.sleep(2)

        # Scansione iframe
        iframes = await page.locator("iframe").all()
        if iframes:
            for iframe in iframes:
                try:
                    frame = iframe.content_frame()
                    if frame:
                        await capture_m3u8_from_frame(frame, captured, wait_seconds=3)
                except Exception:
                    pass

        # Attendi l'intercettazione con breve polling
        for _ in range(8):
            if captured:
                break
            await asyncio.sleep(1)

    except PlaywrightTimeout:
        print(f"   ⚠️  Timeout su {embed_url}")
    except Exception as e:
        print(f"   ⚠️  Errore: {e}")
        return None
    finally:
        try:
            # ✅ Rimuove correttamente il listener passando lo stesso riferimento
            page.remove_listener("request", page_request_handler)
        except Exception:
            pass

    if not captured:
        return None

    # Priorità a playlist/master/manifest
    for url in captured:
        if re.search(r"(master|index|playlist|manifest)", url, re.I):
            return url
    return captured[0]

async def build_playlist() -> int:
    matches = get_matches()
    if not matches:
        Path(OUTPUT_FILE).write_text("#EXTM3U\n", encoding="utf-8")
        print("⚠️  Nessun match live, file vuoto creato")
        return 0

    lines = ["#EXTM3U"]
    success = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu"
            ]
        )
        
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 720},
            extra_http_headers={
                "Referer": "https://www.watchfooty.st/",
                "Origin": "https://www.watchfooty.st"
            }
        )

        # Nasconde la proprietà navigator.webdriver per bypassare controlli bot
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        for i, match in enumerate(matches, 1):
            print(f"\n[{i}/{len(matches)}] {match['title']}")
            for j, stream in enumerate(match["streams"], 1):
                embed_url = stream.get("url")
                quality = stream.get("quality", "")
                if not embed_url:
                    continue
                print(f"   ⏳ Stream {j} ({quality}): {embed_url[:80]}...")
                page = await context.new_page()
                try:
                    m3u8 = await extract_m3u8(page, embed_url)
                finally:
                    await page.close()

                if m3u8:
                    display = f"[{match['league']}] {match['title']} ({quality})"
                    lines.append(f'#EXTINF:-1 tvg-id="wf-{match["id"]}-{j}" group-title="WatchFooty",{display}')
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
