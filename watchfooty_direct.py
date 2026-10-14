#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WatchFooty Extractor — CARNETS-style (Playwright)
=================================================
Estrae eventi live da watchfooty.st e risolve gli embed
sportsembed.su → m3u8 diretto.

Uso:
    python watchfooty_extractor.py                # live only (default)
    python watchfooty_extractor.py --all          # anche non-live
    python watchfooty_extractor.py --max 5        # limita a 5 match
"""
from __future__ import annotations

import argparse
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
MAX_MATCHES_DEFAULT = 20
TIMEOUT_PER_EMBED = 25
ONLY_LIVE = True


def get_matches(max_matches: int, only_live: bool = True) -> list[dict]:
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
        if only_live and status not in ("in", "live"):
            continue
        matches.append({
            "id": str(m.get("matchId") or m.get("id") or ""),
            "title": m.get("title", "Sconosciuto"),
            "league": m.get("league", "WatchFooty"),
            "status": status,
            "streams": streams[:3],
        })

    print(f"✅ WatchFooty: {len(matches)} match "
          f"{'live' if only_live else 'totali'} (max {max_matches})")
    return matches[:max_matches]


async def _try_navigate_iframe(page, timeout_ms: int = 10000) -> bool:
    """
    Cerca un iframe nella pagina e naviga dentro di esso.
    Ritorna True se ha navigato, False altrimenti.
    """
    try:
        iframe_element = await page.query_selector("iframe")
        if not iframe_element:
            return False
        iframe_src = await iframe_element.get_attribute("src")
        if not iframe_src:
            return False
        if iframe_src.startswith("//"):
            iframe_src = "https:" + iframe_src
        if not iframe_src.startswith("http"):
            return False
        await page.goto(iframe_src, wait_until="domcontentloaded",
                        timeout=timeout_ms)
        await asyncio.sleep(2)
        return True
    except Exception:
        return False


async def extract_m3u8_from_page(context, embed_url: str) -> str | None:
    captured: list[str] = []
    page = await context.new_page()

    def on_request(req):
        url = req.url
        low = url.lower()
        if ".m3u8" in low and not url.startswith("blob:"):
            if not any(x in low for x in
                       ("ad.", "ads.", "tracker", "analytics", "doubleclick")):
                if url not in captured:
                    captured.append(url)

    page.on("request", on_request)

    try:
        # 1. Naviga verso l'embed principale
        await page.goto(embed_url, wait_until="domcontentloaded",
                        timeout=TIMEOUT_PER_EMBED * 1000)
        await asyncio.sleep(2)

        # 2. Primo iframe annidato
        if await _try_navigate_iframe(page):
            # 3. Secondo iframe (annidato, alcuni player ne hanno 2)
            if not captured:
                await _try_navigate_iframe(page, timeout_ms=8000)

        # 4. Click su più punti per sbloccare overlay/popup (Clappr/JWPlayer/HLS.js)
        if not captured:
            for x, y in [(640, 360), (400, 300), (800, 400), (640, 500)]:
                try:
                    await page.mouse.click(x, y)
                    await asyncio.sleep(0.5)
                    if captured:
                        break
                except Exception:
                    continue

        # 5. Attesa estesa (player lenti)
        for _ in range(15):
            if captured:
                break
            await asyncio.sleep(1)

    except PlaywrightTimeout:
        pass
    except Exception:
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


async def build_playlist(max_matches: int, only_live: bool) -> int:
    matches = get_matches(max_matches, only_live)
    if not matches:
        Path(OUTPUT_FILE).write_text("#EXTM3U\n", encoding="utf-8")
        print("⚠️  Nessun match, file vuoto creato")
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
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
            ]
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 720},
            bypass_csp=True,
            extra_http_headers={
                "Referer": "https://sportsembed.su/",
                "Origin": "https://sportsembed.su",
            }
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

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
                    lines.append(
                        f'#EXTINF:-1 tvg-id="wf-{match["id"]}-{j}" '
                        f'group-title="WatchFooty",{display}'
                    )
                    lines.append("#EXTVLCOPT:http-referrer=https://sportsembed.su/")
                    lines.append(m3u8)
                    success += 1
                    print(f"   ✅ {m3u8[:100]}")
                    break
                else:
                    print("   ❌ Nessun m3u8")

        await browser.close()

    Path(OUTPUT_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n✅ WatchFooty: salvato {OUTPUT_FILE} con {success} eventi")
    return success


def main() -> None:
    parser = argparse.ArgumentParser(
        description="WatchFooty Extractor — Playwright"
    )
    parser.add_argument("--max", type=int, default=MAX_MATCHES_DEFAULT,
                        help=f"Max match da processare (default: {MAX_MATCHES_DEFAULT})")
    parser.add_argument("--all", action="store_true",
                        help="Includi anche match non-live")
    parser.add_argument("--output", default=OUTPUT_FILE,
                        help=f"File output (default: {OUTPUT_FILE})")
    args = parser.parse_args()

    # Applica output custom
    global OUTPUT_FILE
    OUTPUT_FILE = args.output

    try:
        count = asyncio.run(build_playlist(args.max, not args.all))
        if count == 0:
            print("⚠️  Nessuno stream WatchFooty aggiunto")
    except Exception as e:
        print(f"❌ Errore fatale WatchFooty: {e}", file=sys.stderr)
        Path(OUTPUT_FILE).write_text("#EXTM3U\n", encoding="utf-8")
        sys.exit(1)


if __name__ == "__main__":
    main()