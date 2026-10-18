#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StreamCorner Extractor v13.4 — Playwright stealth + salvataggio bundle JS.

Changelog v13.3 → v13.4:
- Nel listener on_response, salva TUTTI i bundle `/assets/*.js` in `sc_assets/`
  * Necessario per analizzare `provider-data.worker-*.js` offline
  * Il worker contiene il protocollo crypto (Salsa20/12) e il vault con le chiavi
  * Da Carnets il worker è irraggiungibile (Cloudflare 403), ma da GH Actions sì
- Log di ogni file salvato con dimensione
- Nessun'altra modifica: la pipeline (homepage → click → match) resta identica

Invariato da v13.3:
- Playwright stealth (patchright con fallback playwright)
- Click client-side per evitare Cloudflare su /stream/admin/*
- Diagnostica on_requestfailed + on_frame_navigated
- Salvataggio sc_homepage.html, sc_network.json, sc_capture.json
- Produzione dei 4 file M3U
- Exit 0 sempre (graceful)
"""
from __future__ import annotations
import argparse, json, os, re, sys, time
from pathlib import Path
from urllib.parse import urlparse, urljoin

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

# ------------------------------------------------------------ Playwright
def run_playwright(timeout_s=45, ua=UA_DESKTOP, headless=True,
                   max_categories=10, max_matches=25, debug_dir=".",
                   max_total_time_s=900):
    try:
        from patchright.sync_api import sync_playwright
        engine = "patchright"
    except ImportError:
        from playwright.sync_api import sync_playwright
        engine = "playwright"

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

                # v13.4: salva TUTTI i bundle JS in /assets/ per analisi offline
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
                    try:
                        t = resp.text()
                    except Exception:
                        return
                    if "#EXTM3U" in t:
                        found.append({"url": u,
                                      "referer": page.url or (BASE + "/"),
                                      "title": page.title() if page else "",
                                      "source": "network"})
                        log(f"    🎯 M3U8: {u[:120]}")

                elif ".mpd" in lu:
                    try:
                        t = resp.text()
                    except Exception:
                        return
                    if "<MPD" in t:
                        found.append({"url": u,
                                      "referer": page.url or (BASE + "/"),
                                      "title": page.title() if page else "",
                                      "source": "network"})
                        log(f"    🎯 MPD: {u[:120]}")

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
                    log(f"    🖼️ FRAME NAV: {u[:120]}")
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("requestfailed", on_requestfailed)
        page.on("framenavigated", on_frame_navigated)
        page.on("pageerror", lambda e: log(f"    JS ERROR: {str(e)[:120]}"))

        def navigate_client_side(relative_path):
            if page.url.rstrip("/") != BASE:
                try:
                    page.goto(BASE, wait_until="domcontentloaded", timeout=timeout_s * 1000)
                    page.wait_for_timeout(3000)
                except Exception:
                    return False
            try:
                sel = f'a[href="{relative_path}"]'
                loc = page.locator(sel).first
                if loc.count() > 0:
                    try:
                        loc.scroll_into_view_if_needed(timeout=5000)
                    except Exception:
                        pass
                    loc.click(timeout=5000)
                    log(f"    ✅ click client-side su {relative_path}")
                    return True
            except Exception as e:
                log(f"    ⚠️ click failed: {type(e).__name__}")
            try:
                page.evaluate(f"""() => {{
                    history.pushState({{}}, '', '{relative_path}');
                    window.dispatchEvent(new PopStateEvent('popstate'));
                }}""")
                log(f"    ⚠️ pushState fallback su {relative_path}")
                return True
            except Exception as e:
                log(f"    ❌ pushState failed: {type(e).__name__}")
            return False

        # ===== Livello 1: homepage =====
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
                                            "blocked": True}

        # ===== Livello 2: estrai link =====
        cat_links = find_links(html, BASE, CATEGORY_PATTERNS)[:max_categories]
        match_links = find_links(html, BASE, MATCH_PATTERNS)[:max_matches]
        log(f"[*] {len(cat_links)} categorie, {len(match_links)} partite dirette")
        for c in cat_links:  log(f"    📂 {c[:100]}")
        for m in match_links: log(f"    🎬 {m[:100]}")

        # ===== Livello 3a: esplora categorie =====
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
                page.wait_for_timeout(5000)
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

        # ===== Livello 3b: visita partite =====
        for i, link in enumerate(match_links, 1):
            if time.time() - start_time > max_total_time_s:
                log(f"[!] Budget tempo esaurito, interrompo")
                break
            log(f"  [{i}/{len(match_links)}] (client-side) {link[:100]}")
            try:
                path = urlparse(link).path
                if not navigate_client_side(path):
                    log(f"      ⚠️ skip")
                    continue

                page.wait_for_timeout(12000)

                try:
                    slug = re.sub(r'[^a-z0-9]', '_', path.split("/")[-1])[:40]
                    dbg = os.path.join(debug_dir, f"sc_stream_{slug}.html")
                    Path(dbg).write_text(page.content(), encoding="utf-8")
                except Exception:
                    pass

                for f in page.frames:
                    if f.url and f.url != "about:blank":
                        log(f"      🖼️ frame: {f.url[:100]}")

                for sel in ("button[aria-label*='play' i]",
                            ".vjs-big-play-button", ".jw-icon-playback",
                            ".bitmovinplayer-ui",
                            "button:has-text('Play')",
                            "button:has-text('Guarda')",
                            "[class*='play']", "[id*='play']",
                            "video"):
                    try:
                        if page.locator(sel).count() > 0:
                            page.click(sel, timeout=2000)
                            page.wait_for_timeout(1500)
                    except Exception:
                        continue

                page.wait_for_timeout(5000)

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

        browser.close()

    return found, network_map, {"engine": engine, "title": title,
                                "html_len": html_len,
                                "cloudflare": cf_hit["cloudflare"],
                                "blocked": False,
                                "categories": len(cat_links),
                                "matches": len(match_links)}

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
    log(f"📡 STREAMCORNER EXTRACTOR v13.4 (Playwright stealth + save assets)")
    log("=" * 66)

    found, network_map, meta = run_playwright(
        timeout_s=45, ua=ua, headless=not args.headful,
        debug_dir=args.output_dir, max_total_time_s=900)

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
            "referer": f.get("referer") or (BASE + "/"),
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
        "site": BASE,
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

    # Conta bundle JS salvati
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