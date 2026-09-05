#!/usr/bin/env python3
"""
WatchFooty → watchfooty_events.m3u
API pubblica + Playwright (intercetta m3u8 dagli embed).
Pensato per girare su GitHub Actions.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import requests
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ====================== CONFIG ======================
API_URL = "https://api.watchfooty.st/api/v1/matches/all"
OUTPUT_FILE = "watchfooty_events.m3u"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
MAX_MATCHES = 20          # non alzare troppo su Actions (tempo + risorse)
TIMEOUT_PER_EMBED = 10    # secondi max per embed
HEADLESS = True
ONLY_LIVE = False         # True = solo status "in"
# ====================================================


def get_matches() -> list[dict]:
    print("📡 WatchFooty: scarico i match dall'API...")
    try:
        r = requests.get(API_URL, headers={"User-Agent": USER_AGENT}, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"❌ Errore API WatchFooty: {e}")
        return []

    matches: list[dict] = []
    for m in data:
        streams = m.get("streams") or []
        if not streams:
            continue
        status = (m.get("status") or "").lower()
        if ONLY_LIVE and status not in ("in", "live"):
            continue
        matches.append(
            {
                "id": str(m.get("matchId") or m.get("id") or ""),
                "title": m.get("title", "Sconosciuto"),
                "league": m.get("league", "WatchFooty"),
                "status": status,
                "embed": streams[0].get("url"),
                "quality": streams[0].get("quality") or "",
            }
        )

    print(f"✅ WatchFooty: {len(matches)} match con stream (processo max {MAX_MATCHES})")
    return matches[:MAX_MATCHES]


async def extract_m3u8(page, embed_url: str) -> str | None:
    captured: list[str] = []

    def on_request(request):
        url = request.url
        if ".m3u8" not in url.lower():
            return
        if url.startswith("blob:"):
            return
        low = url.lower()
        if any(x in low for x in ("ad.", "ads.", "tracker", "analytics", "doubleclick")):
            return
        captured.append(url)

    page.on("request", on_request)

    try:
        await page.goto(
            embed_url,
            wait_until="domcontentloaded",
            timeout=TIMEOUT_PER_EMBED * 1000,
        )
        for _ in range(TIMEOUT_PER_EMBED * 2):
            if captured:
                break
            await asyncio.sleep(0.5)
    except PlaywrightTimeout:
        pass
    except Exception as e:
        print(f"   ⚠️  Errore navigazione: {e}")
        return None
    finally:
        try:
            page.remove_listener("request", on_request)
        except Exception:
            pass

    if not captured:
        return None

    for url in captured:
        if re.search(r"(master|index|playlist|manifest)", url, re.I):
            return url
    return captured[0]


async def build_playlist() -> int:
    matches = get_matches()
    if not matches:
        # crea comunque un file vuoto valido così il combiner non crasha
        Path(OUTPUT_FILE).write_text("#EXTM3U\n", encoding="utf-8")
        print("⚠️  Nessun match WatchFooty, creato file vuoto")
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
                "--disable-gpu",
            ],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 720},
            extra_http_headers={
                "Referer": "https://www.watchfooty.st/",
                "Origin": "https://www.watchfooty.st",
            },
        )

        for i, m in enumerate(matches, 1):
            title = m["title"]
            embed = m["embed"]
            print(f"[{i}/{len(matches)}] {title}")

            if not embed:
                print("   ❌ Nessun embed")
                continue

            page = await context.new_page()
            try:
                m3u8 = await extract_m3u8(page, embed)
            finally:
                await page.close()

            if not m3u8:
                print("   ❌ Nessun m3u8")
                continue

            display = f"[{m['league']}] {title}"
            if m["quality"]:
                display += f" ({m['quality']})"

            lines.append(
                f'#EXTINF:-1 tvg-id="wf-{m["id"]}" group-title="WatchFooty",{display}'
            )
            lines.append(m3u8)
            success += 1
            print(f"   ✅ OK")

        await browser.close()

    Path(OUTPUT_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n✅ WatchFooty: salvato {OUTPUT_FILE} con {success} eventi")
    return success


def main() -> None:
    try:
        count = asyncio.run(build_playlist())
        # non fallire il workflow se 0 stream (il checker gestirà)
        if count == 0:
            print("Nessuno stream WatchFooty aggiunto (normale se non ci sono match live)")
    except Exception as e:
        print(f"❌ Errore fatale WatchFooty: {e}", file=sys.stderr)
        # crea file vuoto per non rompere i passi successivi
        Path(OUTPUT_FILE).write_text("#EXTM3U\n", encoding="utf-8")
        sys.exit(0)  # non bloccare tutto il workflow


if __name__ == "__main__":
    main()