#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StreamCorner Extractor v13.6 — Playwright stealth + SPA nav + source click

Changelog v13.5 → v13.6:
- FIX #4 (BLOCCANTE): il player StreamCorner NON auto-playa. Il componente
  PlayerIframe viene montato solo dopo click su `.stream-source-btn`
  (o sul bottone "Play"). Senza click: nessun iframe caricato, nessuna
  richiesta m3u8. Ecco perché v13.5 otteneva 0 stream nonostante la SPA
  nav funzionasse. Ora:
    • wait_for_selector('.stream-source-btn', timeout=12s) dopo la SPA nav
    • click sul primo `.stream-source-btn` (scroll_into_view preventivo)
    • attesa 3.5s per il load dell'iframe
    • fallback click su "Play"/"Guarda"/video se mancano source buttons
- FIX #5: filtro chat iframe (`streamcorner-chat.pages.dev`) da log frame
  e da cattura URL — non è rumore utile e non contiene stream.
- FIX #6: `wait_for_timeout(12000)` (blind) sostituito da
  `wait_for_selector('.stream-source-btn')` — la pagina è cachata dopo il
  primo match, il blind wait era tempo sprecato. Per-match: ~6-8s vs 12s.
- FIX #7: contatore `consecutive_no_sources`. Se 5 match di fila non hanno
  source buttons, interrompe (probabile layout rotto o geo-block).
- MINOR: categoria nav wait 5000 → 3000ms (categories raramente aggiungono
  match oltre a quelli della homepage).

NOTE WORKFLOW:
- Con questa fix il run StreamCorner dura ~6-8 min per 25 match. Il
  `timeout-minutes: 3` corrente nel workflow TAGLIA la run a metà. Alzare
  a `timeout-minutes: 10` (o 12) nello step "Extract StreamCorner streams".

Invariato da v13.5:
- FIX #1: navigate_client_side senza reload BASE (SPA nav React Router)
- FIX #2: contatore consecutive_fails su navigazione fallita
- FIX #3: probe mirror streamcorner.{st,foo,cloud,fun}
- Salvataggio TUTTI i bundle /assets/*.js in sc_assets/
- Playwright stealth (patchright con fallback playwright)
- Produzione 4 file M3U, exit 0 sempre
"""
from __future__ import annotations
import argparse, json, os, re, sys, time
from pathlib import Path
from urllib.parse import urlparse, urljoin

BASE_DEFAULT = "https://streamcorner.st"
MIRRORS = [
    "https://streamcorner.st",
    "https://streamcorner.foo",
    "https://streamcorner.cloud",
    "https://streamcorner.fun",
]
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
    "streamxhd.com", "streamtp3.com",
}

# v13.6: filtro chat iframe (non contiene stream, solo rumore)
CHAT_HOST_FILTER = "streamcorner-chat.pages.dev"

DRM_SCHEMES = ("edef8ba9", "9a04f079", "1077efecc0b24d02",
               "urn:uuid:edef8ba9", "urn:uuid:9a04f079",
               "urn:uuid:1077efecc0b24d02")

CF_HINTS = ("challenges.cloudflare.com", "cf-chl",
            "/cdn-cgi/challenge", "turnstile", "challenge-platform")

CAPTURE_FILE = "sc_capture.json"
NETWORK_FILE = "sc_network.json"
HOMEPAGE_FILE = "sc_homepage.html"
ASSETS_DIR = "sc_assets"

CATEGORY_PATTERNS = [
    r'/fifa', r'/world-cup', r'/serie-a', r'/champions',
    r'/premier', r'/liga', r'/soccer', r'/football',
    r'/nba', r'/nfl', r'/mlb', r'/ufc', r'/sport',
    r'/live-tv', r'/channels',
]
MATCH_PATTERNS = [
    r'/match/', r'/event/', r'/live/', r'/watch/',
    r'/game/', r'/play/', r'/stream/',
    r'-vs-', r'\?match=', r'\?id=', r'\?event=',
    r'\?channel=', r'\?stream=',
]

INTERESTING_REQ_KW = (
    ".m3u8", ".mpd", "/api/", "/corner", "workers.dev",
    "embed", "/stream", "/channel", "/match", "/event",
    "playlist", ".json", "manifest", "worker",
)

JSON_STREAM_KEYS = ("stream_url", "play_url", "embed_url", "m3u8",
                    "manifest_url", "hls_url", "dash_url", "src",
                    "url", "link", "source", "video_url")

MAX_CONSECUTIVE_NAV_FAILS = 3
MAX_CONSECUTIVE_NO_SOURCES = 5


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


def find_links(html, base, patterns):
    found = []
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html):
        href = m.group(1)
        if any(re.search(p, href) for p in patterns):
            full = href if href.startswith("http") else urljoin(base, href)
            found.append(full)
    return list(dict.fromkeys(found))


def scan_json_for_streams(obj, base_url, results, depth=0):
    if depth > 8: return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and k.lower() in JSON_STREAM_KEYS:
                if v.startswith("http") or v.startswith("/"):
                    full = v if v.startswith("http") else urljoin(base_url, v)
                    if ".m3u8" in full or ".mpd" in full:
                        results.append({"url": full, "key": k, "base": base_url})
                    elif "/stream" in full or "/embed" in full:
                        results.append({"url": full, "key": k, "base": base_url})
            elif isinstance(v, (dict, list)):
                scan_json_for_streams(v, base_url, results, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            scan_json_for_streams(item, base_url, results, depth + 1)


def _http_probe(url, timeout=8):
    try:
        import requests
        r = requests.get(url, timeout=timeout, verify=False,
                         headers={"User-Agent": UA_DESKTOP})
        return r.status_code, r.text
    except ImportError:
        pass
    except Exception:
        return None, ""
    try:
        import urllib.request, ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={"User-Agent": UA_DESKTOP})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception:
        return None, ""


def _probe_mirrors():
    log("[*] Probe mirror StreamCorner:")
    alive = []
    for m in MIRRORS:
        try:
            st, body = _http_probe(m, timeout=10)
            size = len(body) if body else 0
            if body and "Sito Illegale" in body[:3000]:
                tag = "AGCOM"
            elif body and "challenges.cloudflare.com" in body:
                tag = "CF-challenge"
            elif st and st < 400 and size > 20000:
                tag = "OK"
                alive.append(m)
            else:
                tag = f"HTTP {st}"
            log(f"    {m:35s} → {tag:12s} ({size} byte)")
        except Exception as e:
            log(f"    {m:35s} → err {type(e).__name__}")
    return alive


def run_playwright(timeout_s=45, ua=UA_DESKTOP, headless=True,
                   max_categories=10, max_matches=25, debug_dir=".",
                   max_total_time_s=900, base_url=BASE_DEFAULT):
    try:
        from patchright.sync_api import sync_playwright
        engine = "patchright"
    except ImportError:
        from playwright.sync_api import sync_playwright
        engine = "playwright"

    BASE = base_url
    found = []
    network_map = {}
    cf_hit = {"cloudflare": False}
    start_time = time.time()
    cat_links, match_links = [], []
    assets_dir = os.path.join(debug_dir, ASSETS_DIR)
    try:
        os.makedirs(assets_dir, exist_ok=True)
    except Exception:
        pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=[
            "--no-sandbox", "--disable-dev-shm-usage",
            "--use-gl=swiftshader", "--enable-unsafe-swiftshader",
            "--autoplay-policy=no-user-gesture-required",
            "--mute-audio",
            "--disable-web-security",
        ])
        ctx = browser.new_context(
            user_agent=ua,
            viewport={"width": 1280, "height": 800},
            locale="it-IT",
            timezone_id="Europe/Rome",
            ignore_https_errors=True,
            extra_http_headers={
                "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
            },
        )
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = {runtime: {}};
            Object.defineProperty(navigator, 'languages', {get: () => ['it-IT', 'it']});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        """)

        page = ctx.new_page()

        def on_request(req):
            try:
                u = req.url
                lu = u.lower()
                if CHAT_HOST_FILTER in lu:
                    return
                if not any(k in lu for k in INTERESTING_REQ_KW):
                    return
                if u in network_map:
                    return
                network_map[u] = {
                    "method": req.method,
                    "url": u[:300],
                    "type": req.resource_type,
                    "status": None,
                    "mime": "",
                }
            except Exception:
                pass

        def on_response(resp):
            try:
                u = resp.url
                lu = u.lower()

                # v13.6: skip chat iframe (non contiene stream)
                if CHAT_HOST_FILTER in lu:
                    return

                if "/assets/" in lu and lu.endswith(".js"):
                    try:
                        body = resp.text()
                        if body and len(body) > 500:
                            safe = re.sub(r'[^a-zA-Z0-9_.-]', '_',
                                          u.split("/")[-1])[:80]
                            fpath = os.path.join(assets_dir, safe)
                            with open(fpath, "w", encoding="utf-8") as fh:
                                fh.write(body)
                            log(f"    💾 {ASSETS_DIR}/{safe} ({len(body)} byte)")
                    except Exception as e:
                        log(f"    ⚠️ save asset: {type(e).__name__}")

                if u in network_map:
                    network_map[u]["status"] = resp.status
                    network_map[u]["mime"] = (resp.headers.get("content-type") or "")[:80]

                if any(k in lu for k in CF_HINTS):
                    cf_hit["cloudflare"] = True

                if ".m3u8" in lu:
                    verified = False
                    try:
                        t = resp.text()
                        verified = "#EXTM3U" in t
                    except Exception:
                        pass
                    if verified or resp.status == 200:
                        found.append({"url": u,
                                      "referer": page.url or (BASE + "/"),
                                      "title": page.title() if page else "",
                                      "source": "network",
                                      "verified": verified})
                        log(f"    🎯 M3U8 {'✓' if verified else '(unverified)'}: {u[:120]}")

                elif ".mpd" in lu:
                    verified = False
                    try:
                        t = resp.text()
                        verified = "<MPD" in t
                    except Exception:
                        pass
                    if verified or resp.status == 200:
                        found.append({"url": u,
                                      "referer": page.url or (BASE + "/"),
                                      "title": page.title() if page else "",
                                      "source": "network",
                                      "verified": verified})
                        log(f"    🎯 MPD {'✓' if verified else '(unverified)'}: {u[:120]}")

                elif "json" in (resp.headers.get("content-type") or "").lower():
                    cl = resp.headers.get("content-length")
                    try:
                        if cl and int(cl) > 500_000:
                            return
                    except Exception:
                        pass
                    try:
                        body = resp.text()
                        if len(body) > 500_000:
                            return
                        j = json.loads(body)
                        hits = []
                        scan_json_for_streams(j, u, hits)
                        for h in hits:
                            found.append({"url": h["url"],
                                          "referer": page.url or (BASE + "/"),
                                          "title": page.title() if page else "",
                                          "source": f"json:{h['key']}"})
                            log(f"    🎯 JSON[{h['key']}]: {h['url'][:120]}")
                    except Exception:
                        pass
            except Exception:
                pass

        def on_requestfailed(req):
            try:
                u = req.url
                err = req.failure or "unknown"
                if "brunhild.challenges.cloudflare.com" in u:
                    network_map[u] = {
                        "method": req.method, "url": u[:300],
                        "type": req.resource_type, "status": "FAILED",
                        "mime": "", "error": "brunhild (known)",
                    }
                    return
                log(f"    ❌ FAILED: {req.method} {u[:110]} ({err})")
                network_map[u] = {
                    "method": req.method,
                    "url": u[:300],
                    "type": req.resource_type,
                    "status": "FAILED",
                    "mime": "",
                    "error": str(err)[:200],
                }
            except Exception:
                pass

        def on_frame_navigated(frame):
            try:
                u = frame.url
                if u and u != "about:blank" and "chrome-error" not in u:
                    # v13.6: skip chat iframe dal log
                    if CHAT_HOST_FILTER in u:
                        return
                    log(f"    🖼️ FRAME NAV: {u[:120]}")
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("requestfailed", on_requestfailed)
        page.on("framenavigated", on_frame_navigated)
        page.on("pageerror", lambda e: log(f"    JS ERROR: {str(e)[:120]}"))

        # ---------------- SPA navigation (invariato v13.5) ----------------
        def navigate_client_side(relative_path):
            if "streamcorner" not in (page.url or ""):
                try:
                    page.goto(BASE, wait_until="domcontentloaded",
                              timeout=timeout_s * 1000)
                    page.wait_for_timeout(4000)
                except Exception:
                    return False

            try:
                sel = f'a[href="{relative_path}"]'
                loc = page.locator(sel).first
                if loc.count() > 0:
                    try:
                        loc.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass
                    try:
                        loc.click(timeout=5000, no_wait_after=True)
                    except TypeError:
                        loc.click(timeout=5000)
                    page.wait_for_timeout(800)
                    if "__cf_chl" not in (page.url or ""):
                        log(f"    ✅ click SPA su {relative_path}")
                        return True
                    log(f"    ⚠️ click ha portato a CF challenge, provo pushState")
            except Exception as e:
                log(f"    ⚠️ click failed: {type(e).__name__}")

            try:
                page.evaluate(f"""() => {{
                    history.pushState({{}}, '', '{relative_path}');
                    window.dispatchEvent(new PopStateEvent('popstate'));
                }}""")
                page.wait_for_timeout(800)
                if "__cf_chl" not in (page.url or ""):
                    log(f"    ✅ pushState SPA su {relative_path}")
                    return True
                log(f"    ❌ pushState ha portato a CF challenge")
            except Exception as e:
                log(f"    ❌ pushState failed: {type(e).__name__}")

            return False

        # ===== Homepage =====
        log(f"[*] Apro {BASE}")
        try:
            page.goto(BASE, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        except Exception as e:
            log(f"[!] goto home: {type(e).__name__}: {str(e)[:120]}")
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            log(f"[*] networkidle timeout, proseguo")
        page.wait_for_timeout(8000)

        title = page.title()
        html = page.content()
        html_len = len(html)
        log(f"[*] Titolo: '{title[:80]}'  HTML: {html_len} byte")
        log(f"[*] Cloudflare hints: {cf_hit['cloudflare']}")

        try:
            Path(os.path.join(debug_dir, HOMEPAGE_FILE)).write_text(html, encoding="utf-8")
            log(f"   💾 {HOMEPAGE_FILE} ({html_len} byte)")
        except Exception as e:
            log(f"   ⚠️ salvataggio homepage: {e}")

        if "avviso" in title.lower() or html_len < 2000:
            log(f"[!] Pagina 'Avviso' — reload + attesa 15s")
            try:
                page.reload(wait_until="domcontentloaded", timeout=timeout_s * 1000)
                page.wait_for_timeout(15000)
            except Exception:
                pass
            title = page.title()
            html = page.content()
            html_len = len(html)
            log(f"[*] Dopo reload — titolo: '{title[:80]}'  HTML: {html_len} byte")
            if "avviso" in title.lower() or html_len < 2000:
                try:
                    Path(os.path.join(debug_dir, "sc_homepage_blocked.html")).write_text(
                        html, encoding="utf-8")
                except Exception:
                    pass
                browser.close()
                return found, network_map, {"engine": engine, "title": title,
                                            "html_len": html_len,
                                            "cloudflare": cf_hit["cloudflare"],
                                            "blocked": True,
                                            "base_url": BASE}

        # ===== Link extraction =====
        cat_links = find_links(html, BASE, CATEGORY_PATTERNS)[:max_categories]
        match_links = find_links(html, BASE, MATCH_PATTERNS)[:max_matches]
        log(f"[*] {len(cat_links)} categorie, {len(match_links)} partite dirette")
        for c in cat_links:  log(f"    📂 {c[:100]}")
        for m in match_links: log(f"    🎬 {m[:100]}")

        # ===== Categorie (v13.6: wait 3000ms) =====
        for cat_url in cat_links:
            if time.time() - start_time > max_total_time_s:
                log(f"[!] Budget tempo esaurito")
                break
            log(f"  📂 categoria (client-side): {cat_url[:100]}")
            try:
                path = urlparse(cat_url).path
                if not navigate_client_side(path):
                    log(f"      ⚠️ skip (navigazione fallita)")
                    continue
                page.wait_for_timeout(3000)
                cat_html = page.content()
                inner = find_links(cat_html, cat_url, MATCH_PATTERNS)
                log(f"      → {len(inner)} partite dentro")
                for il in inner[:8]:
                    if il not in match_links:
                        match_links.append(il)
            except Exception as e:
                log(f"      ⚠️ {type(e).__name__}")

        match_links = list(dict.fromkeys(match_links))[:max_matches]
        log(f"[*] Totale partite da visitare: {len(match_links)}")

        # ===== Visita partite (v13.6: click source button) =====
        consecutive_fails = 0
        consecutive_no_sources = 0

        for i, link in enumerate(match_links, 1):
            if time.time() - start_time > max_total_time_s:
                log(f"[!] Budget tempo esaurito, interrompo")
                break
            if consecutive_fails >= MAX_CONSECUTIVE_NAV_FAILS:
                log(f"[!] {consecutive_fails} navigazioni fallite di fila, interrompo")
                log(f"    (probabile Cloudflare challenge persistente)")
                break
            if consecutive_no_sources >= MAX_CONSECUTIVE_NO_SOURCES:
                log(f"[!] {consecutive_no_sources} match di fila senza source-btn, interrompo")
                log(f"    (probabile layout rotto o geo-block)")
                break

            log(f"  [{i}/{len(match_links)}] (client-side) {link[:100]}")
            try:
                path = urlparse(link).path
                if not navigate_client_side(path):
                    log(f"      ⚠️ skip (navigazione fallita)")
                    consecutive_fails += 1
                    continue
                consecutive_fails = 0  # reset su successo navigazione

                # ---- v13.6 FIX #4: click source button ----
                source_buttons_found = False
                try:
                    page.wait_for_selector('.stream-source-btn', timeout=12000)
                    source_count = page.locator('.stream-source-btn').count()
                    source_buttons_found = source_count > 0
                    if source_buttons_found:
                        log(f"      🎬 {source_count} source(s) trovati")
                        try:
                            loc = page.locator('.stream-source-btn').first
                            try:
                                loc.scroll_into_view_if_needed(timeout=3000)
                            except Exception:
                                pass
                            try:
                                loc.click(timeout=5000, no_wait_after=True)
                            except TypeError:
                                loc.click(timeout=5000)
                            log(f"      ✅ click source")
                            page.wait_for_timeout(3500)  # iframe load
                        except Exception as e:
                            log(f"      ⚠️ click source failed: {type(e).__name__}")
                            page.wait_for_timeout(2000)
                except Exception:
                    log(f"      ⚠️ no source-btn in 12s")

                # Fallback: se non c'erano source button, prova Play/video
                if not source_buttons_found:
                    for sel in ("button:has-text('Play')",
                                "button:has-text('Guarda')",
                                "button[aria-label*='play' i]",
                                ".vjs-big-play-button",
                                ".jw-icon-playback"):
                        try:
                            if page.locator(sel).count() > 0:
                                try:
                                    page.locator(sel).first.click(timeout=2000, no_wait_after=True)
                                except TypeError:
                                    page.locator(sel).first.click(timeout=2000)
                                log(f"      🎬 fallback click: {sel}")
                                page.wait_for_timeout(2500)
                                break
                        except Exception:
                            continue

                # Track "no sources" (non è un nav fail, ma se 5 di fila stop)
                if source_buttons_found:
                    consecutive_no_sources = 0
                else:
                    consecutive_no_sources += 1

                # Debug save post-click
                try:
                    slug = re.sub(r'[^a-z0-9]', '_', path.split("/")[-1])[:40]
                    dbg = os.path.join(debug_dir, f"sc_stream_{slug}.html")
                    Path(dbg).write_text(page.content(), encoding="utf-8")
                except Exception:
                    pass

                # Log frames (skip chat)
                for f in page.frames:
                    furl = f.url or ""
                    if (furl and furl != "about:blank"
                            and CHAT_HOST_FILTER not in furl):
                        log(f"      🖼️ frame: {furl[:100]}")

                # Check frame URLs per m3u8
                for frame in page.frames:
                    try:
                        furl = frame.url
                        if furl and (".m3u8" in furl or ".mpd" in furl):
                            found.append({"url": furl, "referer": link,
                                          "title": page.title() or "StreamCorner",
                                          "source": "frame-url"})
                            log(f"    🎯 FRAME: {furl[:120]}")
                    except Exception:
                        pass
            except Exception as e:
                log(f"      ⚠️ {type(e).__name__}")
                consecutive_fails += 1

        browser.close()

    return found, network_map, {"engine": engine, "title": title,
                                "html_len": html_len,
                                "cloudflare": cf_hit["cloudflare"],
                                "blocked": False,
                                "categories": len(cat_links),
                                "matches": len(match_links),
                                "base_url": BASE}


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
    seen = set(); n = 0
    with open(path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for e in entries:
            if not e.get("url") or e["url"] in seen: continue
            seen.add(e["url"])
            grp = group_override or e.get("group") or "StreamCorner"
            f.write("\n".join(entry_lines(e["url"], e.get("title") or "Evento",
                                          grp, e.get("referer") or (BASE_DEFAULT + "/"),
                                          e.get("mtype") or manifest_type(e["url"]))) + "\n")
            n += 1
    return n


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
    ap.add_argument("--base", default=BASE_DEFAULT,
                    help="Base URL (default streamcorner.st). "
                         "Usare un mirror .foo/.cloud/.fun se .st è bloccato.")
    ap.add_argument("--no-probe", action="store_true",
                    help="Salta il probe iniziale dei mirror.")
    args, unknown = ap.parse_known_args()
    if unknown:
        log(f"[*] Argomenti ignorati: {unknown}")

    if args.output_dir is None:
        args.output_dir = os.path.dirname(args.output) or "."
    os.makedirs(args.output_dir, exist_ok=True)
    ua = UA_DESKTOP if args.ua == "desktop" else UA_MOBILE

    log("=" * 66)
    log(f"📡 STREAMCORNER EXTRACTOR v13.6 (source-click fix)")
    log("=" * 66)

    if not args.no_probe:
        _probe_mirrors()
        log("")

    log(f"[*] Base URL: {args.base}")

    found, network_map, meta = run_playwright(
        timeout_s=45, ua=ua, headless=not args.headful,
        debug_dir=args.output_dir, max_total_time_s=900,
        base_url=args.base)

    log(f"[*] Risultato Playwright: {len(found)} manifest intercettati")
    log(f"[*] Meta: {meta}")

    try:
        net_list = list(network_map.values())
        net_path = os.path.join(args.output_dir, NETWORK_FILE)
        Path(net_path).write_text(
            json.dumps({"total": len(net_list), "requests": net_list},
                       indent=2, ensure_ascii=False), encoding="utf-8")
        log(f"   💾 {NETWORK_FILE} ({len(net_list)} richieste)")
    except Exception as e:
        log(f"   ⚠️ salvataggio network: {e}")

    seen = set()
    entries = []
    for f in found:
        u = f["url"]
        if is_skipped(u):
            log(f"    ⏭️  SKIP {u[:100]}")
            continue
        if u in seen:
            continue
        seen.add(u)
        entries.append({
            "url": u,
            "title": (f.get("title") or "StreamCorner")[:60],
            "group": (f.get("title") or "StreamCorner")[:60],
            "referer": f.get("referer") or (args.base + "/"),
            "mtype": manifest_type(u),
        })

    n1 = write_m3u(os.path.join(args.output_dir, "streamcorner_EVENTS_LIVE.m3u"), entries)
    log(f"   💾 EVENTS_LIVE: {n1}")
    n2 = write_m3u(os.path.join(args.output_dir, "streamcorner_VLC.m3u"),
                   entries, group_override="StreamCorner")
    log(f"   💾 VLC: {n2}")
    n3 = write_m3u(os.path.join(args.output_dir, "streamcorner_KODI.m3u"), entries)
    log(f"   💾 KODI: {n3} (non committato)")
    n4 = write_m3u(args.output, entries)
    log(f"   💾 {args.output}: {n4}")

    summary = {
        "version": "13.6",
        "site": args.base,
        "engine": meta.get("engine"),
        "title": meta.get("title"),
        "html_len": meta.get("html_len"),
        "cloudflare_detected": meta.get("cloudflare"),
        "blocked": meta.get("blocked"),
        "categories_found": meta.get("categories"),
        "matches_found": meta.get("matches"),
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

    try:
        assets_path = os.path.join(args.output_dir, ASSETS_DIR)
        if os.path.isdir(assets_path):
            files = [f for f in os.listdir(assets_path) if f.endswith(".js")]
            total_size = sum(os.path.getsize(os.path.join(assets_path, f))
                             for f in files)
            log(f"   📦 Bundle JS salvati: {len(files)} file ({total_size} byte)")
    except Exception:
        pass

    log("=" * 66)
    log(f"✅ blocked={meta.get('blocked')}  entries={len(entries)}")
    log("=" * 66)

    return 0


if __name__ == "__main__":
    sys.exit(main())