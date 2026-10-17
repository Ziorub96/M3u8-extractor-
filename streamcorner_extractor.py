#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StreamCorner Extractor v13.0 — Playwright stealth (GitHub Actions).
- Usa patchright per bypassare Cloudflare.
- Header italiani, timezone Europe/Rome.
- Intercetta .m3u8 e scarta DASH/host morti.
- Da eseguire su GitHub Actions, non su Carnets.
"""
import json, os, re, sys, time
from pathlib import Path
from urllib.parse import urlparse

BASE = "https://streamcorner.st"
OUT = "streamcorner.m3u"
SKIP_HOSTS = {"pandecocogaming.sbs", "taifood-blog.asia", "streamcorner.xyz",
              "embed.st", "getsugatensho", "sportsembed"}

def log(*a): print(*a, flush=True)

def host_of(u): return (urlparse(u).hostname or "").lower()

def is_bad(u):
    u = u.lower()
    if any(h in u for h in SKIP_HOSTS): return True
    if u.split("?")[0].endswith(".mpd"): return True
    return False

def run():
    try:
        from patchright.sync_api import sync_playwright
        log("[*] Uso patchright (stealth)")
    except ImportError:
        from playwright.sync_api import sync_playwright
        log("[*] Uso playwright standard")

    m3u8_urls = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-web-security",
            "--disable-features=IsolateOrigins,site-per-process",
        ])
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 800},
            locale="it-IT",
            timezone_id="Europe/Rome",
            extra_http_headers={
                "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
                "Referer": "https://www.google.it/",
            },
        )
        # Patch per nascondere webdriver
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = {runtime: {}};
            Object.defineProperty(navigator, 'languages', {get: () => ['it-IT', 'it']});
        """)

        page = ctx.new_page()

        def on_response(resp):
            u = resp.url
            if ".m3u8" in u.lower() and u not in m3u8_urls:
                try:
                    body = resp.text()
                    if "#EXTM3U" in body:
                        m3u8_urls.append(u)
                        log(f"  [M3U8] {u[:100]}")
                except Exception:
                    pass
        page.on("response", on_response)

        log(f"[*] Apro {BASE}")
        try:
            page.goto(BASE, wait_until="domcontentloaded", timeout=40000)
        except Exception as e:
            log(f"[!] goto fallito: {e}")
            browser.close()
            return []

        # Attesa challenge Cloudflare
        page.wait_for_timeout(15000)
        title = page.title()
        log(f"[*] Titolo: '{title}'")
        if "avviso" in title.lower():
            log("[!] Ancora bloccato. Provo a ricaricare...")
            page.reload(wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(10000)
            title = page.title()
            log(f"[*] Titolo dopo reload: '{title}'")

        # Cerca link eventi
        links = page.eval_on_selector_all(
            "a[href*='/match/'], a[href*='/event/'], a[href*='/live/'], "
            "a[href*='/soccer/'], a[href*='/serie-a'], a[href*='/fifa']",
            "els => [...new Set(els.map(e => e.href))]"
        )
        log(f"[*] {len(links)} link eventi")
        if not links:
            # Fallback: cerca qualsiasi link interno
            links = page.eval_on_selector_all(
                "a[href^='/']",
                "els => [...new Set(els.map(e => e.href))].slice(0, 30)"
            )
            log(f"[*] Fallback: {len(links)} link interni")

        for i, link in enumerate(links[:15], 1):
            try:
                log(f"  [{i}/{min(len(links),15)}] {link[:80]}")
                page.goto(link, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(8000)
                # Prova click play
                for sel in ("button[aria-label*='play' i]", ".vjs-big-play-button",
                            ".jw-icon-playback", "video"):
                    try:
                        if page.locator(sel).count() > 0:
                            page.click(sel, timeout=2000)
                            page.wait_for_timeout(3000)
                            break
                    except Exception:
                        continue
                page.wait_for_timeout(5000)
            except Exception as e:
                log(f"    [!] {e}")

        browser.close()

    return m3u8_urls

def main():
    log("=" * 60)
    log("📡 STREAMCORNER EXTRACTOR v13.0 (Playwright stealth)")
    log("=" * 60)
    urls = run()
    lines = ["#EXTM3U"]
    count = 0
    for u in urls:
        if is_bad(u):
            log(f"  [SKIP] {u[:100]}")
            continue
        lines.append(f"#EXTINF:-1,StreamCorner")
        lines.append(u)
        count += 1
    Path(OUT).write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"[+] {OUT}: {count} stream")

if __name__ == "__main__":
    main()