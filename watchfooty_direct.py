#!/usr/bin/env python3
"""
WatchFooty → watchfooty_events.m3u
API pubblica + Playwright (intercetta m3u8 dagli embed).
Versione rafforzata: solo live, multi-source, iframe embedindia, stealth.
Pensato per GitHub Actions.
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

MAX_MATCHES = 12          # live only → 12 è sensato su Actions
MAX_SOURCES_PER_MATCH = 3 # prova più embed per lo stesso match
TIMEOUT_GOTO = 25         # secondi
WAIT_AFTER_LOAD = 12      # attesa dopo load / click
ONLY_LIVE = True          # obbligatorio: i "post" quasi mai danno m3u8
HEADLESS = True
# ====================================================

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
"""


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

        # deduplica URL embed, tieni i migliori prima (deluxe/hd/sigma)
        seen = set()
        ordered = []
        priority = {"deluxe": 0, "hd": 1, "sigma": 2, "prime": 3, "delta": 4}
        for s in sorted(streams, key=lambda x: priority.get((x.get("source") or "").lower(), 99)):
            url = s.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            ordered.append(s)

        if not ordered:
            continue

        matches.append(
            {
                "id": str(m.get("matchId") or m.get("id") or ""),
                "title": m.get("title", "Sconosciuto"),
                "league": m.get("league", "WatchFooty"),
                "status": status,
                "streams": ordered[:MAX_SOURCES_PER_MATCH],
            }
        )

    print(f"✅ WatchFooty: {len(matches)} match live con stream (max {MAX_MATCHES})")
    return matches[:MAX_MATCHES]


def is_good_m3u8(url: str) -> bool:
    low = url.lower()
    if not url.startswith("http"):
        return False
    if url.startswith("blob:"):
        return False
    if any(x in low for x in ("ad.", "ads.", "tracker", "analytics", "doubleclick", "pagead")):
        return False
    return (".m3u8" in low) or ("playlist" in low) or ("manifest" in low) or ("/hls/" in low)


async def extract_m3u8_from_page(page, embed_url: str) -> str | None:
    captured: list[str] = []

    def on_request(request):
        url = request.url
        if is_good_m3u8(url):
            captured.append(url)

    def on_response(response):
        url = response.url
        ct = (response.headers.get("content-type") or "").lower()
        if is_good_m3u8(url) or "mpegurl" in ct or "application/vnd.apple.mpegurl" in ct:
            captured.append(url)

    page.on("request", on_request)
    page.on("response", on_response)

    try:
        await page.goto(embed_url, wait_until="domcontentloaded", timeout=TIMEOUT_GOTO * 1000)
    except PlaywrightTimeout:
        pass
    except Exception as e:
        print(f"   ⚠️  goto: {e}")
        return None

    # attesa iniziale + click sul player
    await page.wait_for_timeout(3000)
    try:
        await page.mouse.click(640, 360)
    except Exception:
        pass
    await page.wait_for_timeout(WAIT_AFTER_LOAD * 500)

    # se c'è iframe (embedindia), entra lì
    try:
        iframe_src = await page.evaluate(
            "() => document.querySelector('iframe')?.src || ''"
        )
    except Exception:
        iframe_src = ""

    if iframe_src and iframe_src.startswith("http"):
        try:
            await page.goto(iframe_src, wait_until="domcontentloaded", timeout=TIMEOUT_GOTO * 1000)
            await page.wait_for_timeout(3000)
            try:
                await page.mouse.click(640, 360)
            except Exception:
                pass
            await page.wait_for_timeout(WAIT_AFTER_LOAD * 500)
            try:
                await page.evaluate(
                    """() => {
                        const v = document.querySelector('video');
                        if (v) { v.muted = true; v.play().catch(()=>{}); }
                    }"""
                )
            except Exception:
                pass
            await page.wait_for_timeout(4000)
        except Exception as e:
            print(f"   ⚠️  iframe: {e}")

    # cleanup listener
    try:
        page.remove_listener("request", on_request)
        page.remove_listener("response", on_response)
    except Exception:
        pass

    if not captured:
        return None

    # preferisci master/index/playlist
    for url in captured:
        if re.search(r"(master|index|playlist|manifest|mono)", url, re.I):
            return url
    return captured[0]


async def build_playlist() -> int:
    matches = get_matches()
    if not matches:
        Path(OUTPUT_FILE).write_text("#EXTM3U\n", encoding="utf-8")
        print("⚠️  Nessun match live WatchFooty, creato file vuoto")
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
                "--disable-web-security",
            ],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 720},
            locale="en-US",
            timezone_id="America/New_York",
            ignore_https_errors=True,
            extra_http_headers={
                "Referer": "https://www.watchfooty.st/",
                "Origin": "https://www.watchfooty.st",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        await context.add_init_script(STEALTH_JS)

        for i, m in enumerate(matches, 1):
            title = m["title"]
            print(f"[{i}/{len(matches)}] {title}")

            m3u8 = None
            for j, stream in enumerate(m["streams"], 1):
                embed = stream.get("url")
                if not embed:
                    continue
                src = stream.get("source") or "?"
                quality = stream.get("quality") or ""
                print(f"   → try {j}/{len(m['streams'])} [{src} {quality}]")

                page = await context.new_page()
                try:
                    m3u8 = await extract_m3u8_from_page(page, embed)
                finally:
                    await page.close()

                if m3u8:
                    print(f"   ✅ OK ({src})")
                    break
                else:
                    print(f"   ❌ no m3u8 ({src})")

            if not m3u8:
                continue

            display = f"[{m['league']}] {title}"
            lines.append(
                f'#EXTINF:-1 tvg-id="wf-{m["id"]}" group-title="WatchFooty",{display}'
            )
            lines.append(m3u8)
            success += 1

        await browser.close()

    Path(OUTPUT_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n✅ WatchFooty: salvato {OUTPUT_FILE} con {success} eventi")
    return success


def main() -> None:
    try:
        count = asyncio.run(build_playlist())
        if count == 0:
            print(
                "Nessuno stream WatchFooty estratto. "
                "Normale se non ci sono live o se l'embed blocca headless."
            )
    except Exception as e:
        print(f"❌ Errore fatale WatchFooty: {e}", file=sys.stderr)
        Path(OUTPUT_FILE).write_text("#EXTM3U\n", encoding="utf-8")
        sys.exit(0)  # non bloccare il workflow


if __name__ == "__main__":
    main()