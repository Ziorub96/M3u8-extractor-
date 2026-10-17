#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StreamCorner Extractor v13.0 — Playwright stealth su streamcorner.st.
- Bypassa Cloudflare con patchright (fallback: playwright).
- Header italiani, timezone Europe/Rome, UA desktop.
- Intercetta .m3u8 (HLS) e .mpd (DASH, log solo).
- Scarta host morti (embed.st, pandecocogaming, taifood, ecc.).
- Produce i 4 file attesi dal workflow: EVENTS_LIVE, VLC, KODI, COMPLETE.
- CLI compatibile col v10.4 (--output, --playwright-workers, ecc.).
"""
from __future__ import annotations
import argparse, json, os, re, sys, time
from pathlib import Path
from urllib.parse import urlparse

BASE = "https://streamcorner.st"
UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")
UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) "
             "Version/17.0 Mobile Safari/604.1")

SKIP_HOSTS = {
    "pandecocogaming.sbs", "taifood-blog.asia", "streamcorner.xyz",
    "embed.st", "getsugatensho", "sportsembed",
    "embedhd.click", "embedhd.pk", "embedme.st", "streamsgate.co",
}

DRM_SCHEMES = ("edef8ba9", "9a04f079", "1077efecc0b24d02",
               "urn:uuid:edef8ba9", "urn:uuid:9a04f079",
               "urn:uuid:1077efecc0b24d02")

CF_HINTS = ("challenges.cloudflare.com", "cf-chl",
            "/cdn-cgi/challenge", "turnstile", "challenge-platform")

CAPTURE_FILE = "sc_capture.json"

def log(*a): print(*a, flush=True)

def host_of(u): return (urlparse(u).hostname or "").lower()

def has_drm_hint(u):
    v = (u or "").lower()
    return any(s in v for s in DRM_SCHEMES)

def manifest_type(u):
    if not u: return None
    p = urlparse(u).path.lower()
    if p.endswith(".m3u8"): return "hls"
    if p.endswith(".mpd"):  return "dash"
    return None

def is_skipped(u):
    h = host_of(u)
    if h in SKIP_HOSTS or any(s in h for s in SKIP_HOSTS): return True
    if has_drm_hint(u): return True
    return False

# ------------------------------------------------------------ Playwright
def run_playwright(timeout_s=45, ua=UA_DESKTOP, headless=True):
    try:
        from patchright.sync_api import sync_playwright
        engine = "patchright"
    except ImportError:
        from playwright.sync_api import sync_playwright
        engine = "playwright"

    found = []      # list of dict(url, referer, title, source)
    cf_hit = {"cloudflare": False}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=[
            "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--use-gl=swiftshader", "--enable-unsafe-swiftshader",
            "--autoplay-policy=no-user-gesture-required",
            "--mute-audio",
            "--disable-features=IsolateOrigins,site-per-process",
        ])
        ctx = browser.new_context(
            user_agent=ua,
            viewport={"width": 1280, "height": 800},
            locale="it-IT",
            timezone_id="Europe/Rome",
            ignore_https_errors=True,
            extra_http_headers={
                "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
                "Referer": BASE + "/",
            },
        )
        # Stealth init script
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = {runtime: {}};
            Object.defineProperty(navigator, 'languages', {get: () => ['it-IT', 'it']});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        """)

        page = ctx.new_page()

        def on_response(resp):
            u = resp.url; lu = u.lower()
            if any(k in lu for k in CF_HINTS):
                cf_hit["cloudflare"] = True
            if ".m3u8" in lu:
                try:
                    t = resp.text()
                except Exception:
                    return
                if "#EXTM3U" in t:
                    found.append({"url": u, "referer": BASE + "/",
                                  "title": page.title() if page else "",
                                  "source": "network"})
                    log(f"    🎯 M3U8: {u[:120]}")
            elif ".mpd" in lu:
                try:
                    t = resp.text()
                except Exception:
                    return
                if "<MPD" in t:
                    found.append({"url": u, "referer": BASE + "/",
                                  "title": page.title() if page else "",
                                  "source": "network"})
                    log(f"    🎯 MPD: {u[:120]}")

        page.on("response", on_response)
        page.on("pageerror", lambda e: log(f"    JS ERROR: {str(e)[:120]}"))

        # 1. Homepage
        log(f"[*] Apro {BASE}")
        try:
            page.goto(BASE, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        except Exception as e:
            log(f"[!] goto home: {type(e).__name__}: {str(e)[:120]}")
        page.wait_for_timeout(15000)

        title = page.title()
        html_len = len(page.content())
        log(f"[*] Titolo: '{title[:80]}'  HTML: {html_len} byte")
        log(f"[*] Cloudflare hints: {cf_hit['cloudflare']}")

        # 2. Se "Avviso", reload e aspetta ancora
        if "avviso" in title.lower() or html_len < 2000:
            log(f"[!] Pagina 'Avviso' — reload + attesa 15s")
            try:
                page.reload(wait_until="domcontentloaded", timeout=timeout_s * 1000)
            except Exception as e:
                log(f"[!] reload: {type(e).__name__}")
            page.wait_for_timeout(15000)
            title = page.title()
            html_len = len(page.content())
            log(f"[*] Dopo reload — titolo: '{title[:80]}'  HTML: {html_len} byte")

        # 3. Se ancora "Avviso", salva HTML per debug e ritorna
        if "avviso" in title.lower() or html_len < 2000:
            try:
                Path("sc_homepage_blocked.html").write_text(
                    page.content(), encoding="utf-8")
                log(f"[!] Salvato sc_homepage_blocked.html ({html_len} byte) per debug")
            except Exception:
                pass
            # Chiudi browser e ritorna vuoto
            browser.close()
            return found, {"engine": engine, "title": title,
                           "html_len": html_len, "cloudflare": cf_hit["cloudflare"],
                           "blocked": True}

        # 4. Estrai link eventi
        selectors = [
            "a[href*='/match/']", "a[href*='/event/']", "a[href*='/live/']",
            "a[href*='/soccer/']", "a[href*='/serie-a']", "a[href*='/champions']",
            "a[href*='/fifa']", "a[href*='/world-cup']",
        ]
        links = []
        for sel in selectors:
            try:
                ls = page.eval_on_selector_all(
                    sel, "els => [...new Set(els.map(e => e.href))]")
                links.extend(ls)
            except Exception:
                continue
        # Fallback: qualsiasi link interno
        if not links:
            try:
                ls = page.eval_on_selector_all(
                    "a[href^='/']",
                    "els => [...new Set(els.map(e => e.href))].slice(0, 40)")
                links.extend(ls)
            except Exception:
                pass
        # Dedup + ordina
        links = list(dict.fromkeys(links))
        log(f"[*] {len(links)} link candidati")

        # 5. Visita ogni link (max 20 per non andare in timeout)
        for i, link in enumerate(links[:20], 1):
            if not link.startswith("http"):
                continue
            log(f"  [{i}/{min(len(links),20)}] {link[:100]}")
            try:
                page.goto(link, wait_until="domcontentloaded", timeout=25000)
            except Exception as e:
                log(f"    [!] goto: {type(e).__name__}")
                continue
            page.wait_for_timeout(8000)
            # click play se presente
            for sel in ("button[aria-label*='play' i]", ".vjs-big-play-button",
                        ".jw-icon-playback", ".bitmovinplayer-ui", "video"):
                try:
                    if page.locator(sel).count() > 0:
                        page.click(sel, timeout=2000)
                        page.wait_for_timeout(3000)
                        break
                except Exception:
                    continue
            page.wait_for_timeout(5000)

        browser.close()
        return found, {"engine": engine, "title": title,
                       "html_len": html_len, "cloudflare": cf_hit["cloudflare"],
                       "blocked": False, "links_examined": min(len(links), 20)}

# ------------------------------------------------------------ M3U
def entry_lines(url, title, group, referer, mtype):
    out = [f'#EXTINF:-1 group-title="{group}",{title}']
    if referer:
        out.append(f"#EXTVLCOPT:http-referrer={referer}")
    out.append(f"#EXTVLCOPT:http-user-agent={UA_DESKTOP}")
    out.append("#KODIPROP:inputstream=inputstream.adaptive")
    if mtype:
        out.append(f"#KODIPROP:inputstream.adaptive.manifest_type={mtype}")
    out.append(url)
    return out

def write_m3u(path, entries, group_override=None):
    """entries: list of dict {url, title, group, referer, mtype}"""
    seen = set(); n = 0
    with open(path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for e in entries:
            if not e.get("url") or e["url"] in seen: continue
            seen.add(e["url"])
            grp = group_override or e.get("group") or "StreamCorner"
            f.write("\n".join(entry_lines(e["url"], e.get("title") or "Evento",
                                          grp, e.get("referer") or (BASE + "/"),
                                          e.get("mtype") or manifest_type(e["url"]))) + "\n")
            n += 1
    return n

# ------------------------------------------------------------ Main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", "-o", default="streamcorner_COMPLETE.m3u")
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--proxy", default=os.environ.get("SC_PROXY"))
    ap.add_argument("--provider", default="admin")
    ap.add_argument("--all-events", action="store_true")
    ap.add_argument("--no-embeds", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--embed-workers", "--playwright-workers", type=int, default=4,
                    dest="embed_workers")
    ap.add_argument("--embed-limit", "--playwright-limit", type=int, default=0,
                    dest="embed_limit")
    ap.add_argument("--ua", choices=["desktop", "mobile"], default="desktop")
    ap.add_argument("--headful", action="store_true")
    args, unknown = ap.parse_known_args()
    if unknown:
        log(f"[*] Argomenti ignorati: {unknown}")

    if args.output_dir is None:
        args.output_dir = os.path.dirname(args.output) or "."
    os.makedirs(args.output_dir, exist_ok=True)
    ua = UA_DESKTOP if args.ua == "desktop" else UA_MOBILE

    log("=" * 66)
    log(f"📡 STREAMCORNER EXTRACTOR v13.0 (Playwright stealth, UA={args.ua})")
    log("=" * 66)

    found, meta = run_playwright(
        timeout_s=45, ua=ua, headless=not args.headful)

    log(f"[*] Risultato Playwright: {len(found)} manifest intercettati")
    log(f"[*] Meta: {meta}")

    # Filtra
    entries = []
    for f in found:
        if is_skipped(f["url"]):
            log(f"    ⏭️  SKIP {f['url'][:100]}")
            continue
        entries.append({
            "url": f["url"],
            "title": (f.get("title") or "StreamCorner")[:60],
            "group": (f.get("title") or "StreamCorner")[:60],
            "referer": f.get("referer") or (BASE + "/"),
            "mtype": manifest_type(f["url"]),
        })

    # Scrittura file (nomi invariati)
    n1 = write_m3u(os.path.join(args.output_dir, "streamcorner_EVENTS_LIVE.m3u"), entries)
    log(f"   💾 EVENTS_LIVE: {n1}")
    n2 = write_m3u(os.path.join(args.output_dir, "streamcorner_VLC.m3u"),
                   entries, group_override="StreamCorner")
    log(f"   💾 VLC: {n2}")
    n3 = write_m3u(os.path.join(args.output_dir, "streamcorner_KODI.m3u"), entries)
    log(f"   💾 KODI: {n3}")
    n4 = write_m3u(args.output, entries)
    log(f"   💾 {args.output}: {n4}")

    # Summary
    summary = {
        "site": BASE,
        "engine": meta.get("engine"),
        "title": meta.get("title"),
        "html_len": meta.get("html_len"),
        "cloudflare_detected": meta.get("cloudflare"),
        "blocked": meta.get("blocked"),
        "links_examined": meta.get("links_examined"),
        "manifests_found": len(found),
        "entries_final": len(entries),
        "extracted_at": int(time.time()),
    }
    try:
        Path(os.path.join(args.output_dir, CAPTURE_FILE)).write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        log(f"   💾 {CAPTURE_FILE} salvato")
    except Exception as e:
        log(f"   ⚠️ capture: {e}")

    log("=" * 66)
    log(f"✅ blocked={meta.get('blocked')}  entries={len(entries)}")
    log("=" * 66)

    # Exit 0 SEMPRE (graceful: il workflow non si rompe)
    return 0

if __name__ == "__main__":
    sys.exit(main())