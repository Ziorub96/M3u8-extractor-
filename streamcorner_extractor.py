#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StreamCorner / CornerStream Extractor — v9.2 (Playwright-as-oracle)
====================================================================
La nuova SPA cornerstream.tech decodifica il vault dentro un Web Worker
caricato da blob. Strategia: lasciare che la SPA faccia il lavoro e
intercettare le risposte JSON già decifrate.

Novità v9.2:
  * Hook su fetch, XHR, JSON.parse, Response.json, Worker
  * Navigazione multi-path + click su link per innescare il catalogo
  * Stampa del capture nei log (nessun artifact richiesto per debug)
  * Salvataggio opzionale sc_capture.json per artifact
"""

from __future__ import annotations
import argparse, base64, hashlib, json, os, random, re, struct, sys, threading, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# CONFIG
# ============================================================
UA_IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile Safari/604.1")

PRIORITY_SITES = [
    "https://cornerstream.tech/",
    "https://streamcorner.st/",
]

PROVIDERS_DEFAULT = "admin"
KEY_FIELDS = ("stream_id", "game_id", "channel_id", "id")
DIRECT_RE = re.compile(r"\.(?:m3u8|mpd)(?:$|\?)", re.I)

RUNTIME_CACHE = ".sc_runtime.json"
M3U8_CACHE = ".sc_m3u8_cache.json"
DRM_REPORT = "sc_drm_report.jsonl"
CAPTURE_FILE = "sc_capture.json"

LIVE_MARGIN_BEFORE = 1800
LIVE_MARGIN_AFTER = 3600


def log(*a):
    print(*a, flush=True)


# ============================================================
# SALSA20/12 (retrocompatibilità)
# ============================================================
M32 = 0xFFFFFFFF


def rotl32(v, c):
    return ((v << c) & M32) | (v >> (32 - c))


def x8(t, e):
    n = 7 & e
    return 255 & ((t << n) | (t >> (8 - n)))


def Y8(t, e):
    n = 7 & e
    return 255 & ((t >> n) | (t << (8 - n)))


def _qr(s, a, b, c, d):
    s[b] ^= rotl32((s[a] + s[d]) & M32, 7); s[c] ^= rotl32((s[b] + s[a]) & M32, 9)
    s[d] ^= rotl32((s[c] + s[b]) & M32, 13); s[a] ^= rotl32((s[d] + s[c]) & M32, 18)


def _blk(k32, n8, ctr):
    k = list(struct.unpack("<8I", k32)); n0, n1 = struct.unpack("<2I", n8)
    st = [0x61707865, k[0], k[1], k[2], k[3], 0x3320646e, n0, n1,
          ctr & M32, (ctr >> 32) & M32, 0x79622d32, k[4], k[5], k[6], k[7], 0x6b206574]
    x = st[:]
    for _ in range(6):
        _qr(x, 0, 4, 8, 12); _qr(x, 5, 9, 13, 1); _qr(x, 10, 14, 2, 6); _qr(x, 15, 3, 7, 11)
        _qr(x, 0, 1, 2, 3); _qr(x, 5, 6, 7, 4); _qr(x, 10, 11, 8, 9); _qr(x, 15, 12, 13, 14)
    return struct.pack("<16I", *[(x[i] + st[i]) & M32 for i in range(16)])


def salsa20_12(k, n8, d, ctr=0):
    out = bytearray(); i = 0
    while i < len(d):
        ks = _blk(k, n8, ctr + i // 64)
        out.extend(bytes(a ^ b for a, b in zip(d[i:i + 64], ks))); i += 64
    return bytes(out)


SBOX = [185, 243, 94, 251, 153, 162, 126, 77, 155, 41, 182, 62, 200, 173, 252, 244]


def dec_payload(p, k):
    out = bytearray(len(p)); c = 117; y = 84 ^ k[1]
    for a in range(len(p)):
        h = (k[15 & a] ^ SBOX[15 & a] ^ y ^ ((139 * a) & 0xFF)
             ^ ((1293491885 >> (8 * (3 & a))) & 0xFF) ^ c) & 0xFF
        out[a] = p[a] ^ h; y = p[a]; c = x8((c ^ p[a] ^ k[(a + 7) & 15]) & 0xFF, 1)
    return bytes(out)


def dec_spec(s):
    t0, t1, t2, t3, t4, t5 = s[:6]
    al = lambda v: v["v"] if isinstance(v, dict) else v
    t2, t3, t4, t5 = al(t2), al(t3), al(t4), al(t5)
    e = bytearray(len(t2)); n = t0
    for o in range(len(t2)):
        i = t5[o]; inner = x8(t2[o], 7 & (n ^ t1))
        m1 = (t3[i] + (n ^ ((13 * o) & 255))) & 255; m2 = (t1 + 17 * o) & 255
        r = Y8((inner ^ m1 ^ m2) & 255, 7 & (t4[o] ^ n)); e[o] = r
        n = (x8(n, 1) ^ ((r + t4[o] + o) & 255)) & 255
    return bytes(e)


def gc(v, i):
    consts = v.get("consts") or []
    c = consts[i]
    if isinstance(c, dict):
        t = c.get("t", "u8"); val = c.get("v")
        if t == "u8":  return bytes(val)
        if t == "u32": return b"".join(struct.pack("<I", x) for x in val)
        if t == "s":   return val.encode()
        if t == "a":   return b"".join(x.encode() for x in val)
        if t == "n":   return bytes([val])
    return b""


def b64d(s):
    s = s.strip().replace("-", "+").replace("_", "/")
    s += "=" * ((4 - len(s) % 4) % 4)
    return base64.b64decode(s)


# ============================================================
# HOOK SCRIPT (iniettato PRIMA del boot SPA)
# ============================================================
HOOK_SCRIPT = r"""
(() => {
    if (window.__sc_hook_installed) return;
    window.__sc_hook_installed = true;

    window.__sc_capture = {
        fetches: [], parsed: [], workers: [], globals: {},
        console: [], started: Date.now(),
    };

    // --- FETCH ---
    const orig_fetch = window.fetch;
    window.fetch = async function(...args) {
        const req = args[0];
        const url = typeof req === 'string' ? req : (req?.url || '');
        let method = 'GET';
        let body_sample = null;
        try {
            if (typeof req === 'object' && req.method) method = req.method;
            if (typeof req === 'object' && req.body && typeof req.body === 'string') {
                body_sample = req.body.slice(0, 4000);
            }
        } catch(e) {}
        const t0 = Date.now();
        const resp = await orig_fetch.apply(this, args);
        try {
            const clone = resp.clone();
            const text = await clone.text();
            window.__sc_capture.fetches.push({
                url: url.slice(0, 500),
                method: method,
                body_sample: body_sample,
                status: resp.status,
                resp_sample: text.slice(0, 50000),
                resp_len: text.length,
                ms: Date.now() - t0,
            });
        } catch(e) {}
        return resp;
    };

    // --- XHR ---
    const OrigXHR = window.XMLHttpRequest;
    function HXHR() {
        const x = new OrigXHR();
        const orig_open = x.open;
        const orig_send = x.send;
        let _method = 'GET', _url = '';
        x.open = function(m, u, ...rest) { _method = m; _url = u; return orig_open.call(x, m, u, ...rest); };
        x.send = function(b) {
            x.addEventListener('load', () => {
                try {
                    window.__sc_capture.fetches.push({
                        url: String(_url).slice(0, 500),
                        method: _method,
                        status: x.status,
                        resp_sample: String(x.responseText || '').slice(0, 50000),
                        resp_len: (x.responseText || '').length,
                    });
                } catch(e) {}
            });
            return orig_send.call(x, b);
        };
        return x;
    }
    HXHR.prototype = OrigXHR.prototype;
    window.XMLHttpRequest = HXHR;

    // --- JSON.parse (chiave: cattura risposte decifrate) ---
    const orig_parse = JSON.parse;
    JSON.parse = function(text, reviver) {
        const result = orig_parse.call(this, text, reviver);
        try {
            if (result && typeof result === 'object') {
                const s = JSON.stringify(result);
                if (s.length > 100) {
                    window.__sc_capture.parsed.push({
                        ts: Date.now(), size: s.length,
                        sample: s.slice(0, 50000),
                        source: 'JSON.parse',
                    });
                }
            }
        } catch(e) {}
        return result;
    };

    // --- Response.json (per fetch che usano .json() direttamente) ---
    try {
        const orig_rjson = Response.prototype.json;
        Response.prototype.json = async function() {
            const result = await orig_rjson.call(this);
            try {
                if (result && typeof result === 'object') {
                    const s = JSON.stringify(result);
                    if (s.length > 100) {
                        window.__sc_capture.parsed.push({
                            ts: Date.now(), size: s.length,
                            sample: s.slice(0, 50000),
                            source: 'Response.json',
                        });
                    }
                }
            } catch(e) {}
            return result;
        };
    } catch(e) {}

    // --- Worker ---
    const OrigWorker = window.Worker;
    window.Worker = function(url, opts) {
        try { window.__sc_capture.workers.push(String(url).slice(0, 500)); } catch(e) {}
        return new OrigWorker(url, opts);
    };
    window.Worker.prototype = OrigWorker.prototype;

    // --- Console ---
    ['log','warn','error'].forEach(k => {
        const orig = console[k];
        console[k] = function(...args) {
            try {
                window.__sc_capture.console.push({
                    level: k,
                    msg: args.map(a => typeof a === 'object'
                        ? JSON.stringify(a).slice(0,300)
                        : String(a).slice(0,300)).join(' ')
                });
            } catch(e) {}
            return orig.apply(console, args);
        };
    });

    // --- Snapshot globali (chiamato dall'esterno) ---
    window.__sc_snapshot_globals = function() {
        const keys = ['dr','Ar','vault','config','api','apiUrl','apiBase','corner',
                      'session','consts','specs','worker','streamcorner','sc'];
        const out = {};
        for (const k of keys) {
            try {
                const v = window[k];
                if (v !== undefined) {
                    if (typeof v === 'object' || typeof v === 'function') {
                        try { out[k] = JSON.stringify(v).slice(0, 3000); }
                        catch(e) { out[k] = '<unserializable ' + typeof v + '>'; }
                    } else {
                        out[k] = String(v).slice(0, 3000);
                    }
                }
            } catch(e) {}
        }
        return out;
    };
})();
"""


# ============================================================
# BROWSER CAPTURE
# ============================================================
def capture_from_browser(site, proxy=None, wait_boot=18, wait_nav=12):
    try:
        from patchright.sync_api import sync_playwright
        engine = "patchright"
    except ImportError:
        from playwright.sync_api import sync_playwright
        engine = "playwright"

    log(f"   🌐 Browser-as-oracle su {site} [{engine}]")
    out = {"site": site, "fetches": [], "parsed": [],
           "workers": [], "globals": {}, "console": [], "route_info": {}}

    try:
        with sync_playwright() as p:
            kw = {"headless": True,
                  "args": ["--no-sandbox", "--disable-dev-shm-usage",
                           "--use-gl=swiftshader", "--enable-unsafe-swiftshader",
                           "--autoplay-policy=no-user-gesture-required",
                           "--mute-audio"]}
            if proxy:
                kw["proxy"] = {"server": proxy}
            browser = p.chromium.launch(**kw)
            ctx = browser.new_context(
                user_agent=UA_IPHONE,
                viewport={"width": 1280, "height": 800},
                locale="it-IT", timezone_id="Europe/Rome",
                ignore_https_errors=True,
            )
            ctx.add_init_script(HOOK_SCRIPT)
            page = ctx.new_page()

            # 1. Boot home
            try:
                page.goto(site, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                log(f"      ⚠️ goto home: {str(e)[:80]}")
            page.wait_for_timeout(wait_boot * 1000)
            log(f"      ✓ home caricata ({page.url[:80]})")

            # 2. Snapshot route + link
            try:
                route_info = page.evaluate("""() => ({
                    url: location.href,
                    hash: location.hash,
                    pathname: location.pathname,
                    links: Array.from(document.querySelectorAll('a[href]'))
                            .map(a => ({text: (a.textContent||'').trim().slice(0,40),
                                        href: a.href}))
                            .filter(l => l.text.length > 0)
                            .slice(0, 50)
                })""")
                out["route_info"] = route_info
                log(f"      path={route_info.get('pathname')} hash={route_info.get('hash')}")
                log(f"      link trovati: {len(route_info.get('links', []))}")
                for l in route_info.get("links", [])[:20]:
                    log(f"         [{l['text'][:35]}] → {l['href'][:90]}")
            except Exception as e:
                log(f"      ⚠️ route info: {str(e)[:80]}")

            # 3. Naviga multi-path
            for path in ["live", "#/live", "events", "#/events",
                         "sport", "#/sport", "stream", "#/stream"]:
                try:
                    url_try = site.rstrip("/") + "/" + path
                    page.goto(url_try, wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(5000)
                    log(f"      → {path:10} URL={page.url[:70]}")
                except Exception as e:
                    log(f"      → {path:10} ⚠️ {str(e)[:60]}")

            # 4. Click link con testo chiave
            for kw in ["Live", "Sport", "Event", "24/7", "Channel", "Admin", "Home"]:
                for sel in [f"a:has-text('{kw}')", f"button:has-text('{kw}')"]:
                    try:
                        el = page.locator(sel).first
                        if el.count() > 0:
                            el.click(timeout=2000)
                            page.wait_for_timeout(4000)
                            log(f"      ✓ click '{kw}'")
                            break
                    except Exception:
                        continue

            # 5. Attesa finale
            page.wait_for_timeout(6000)

            # 6. Snapshot globals
            try:
                out["globals"] = page.evaluate("() => window.__sc_snapshot_globals()")
            except Exception:
                pass

            # 7. Raccogli capture
            try:
                cap = page.evaluate("() => window.__sc_capture")
                out["fetches"] = cap.get("fetches", [])
                out["parsed"]  = cap.get("parsed", [])
                out["workers"] = cap.get("workers", [])
                out["console"] = cap.get("console", [])
            except Exception as e:
                log(f"      ⚠️ capture read: {str(e)[:80]}")

            browser.close()
    except Exception as e:
        log(f"      ❌ browser: {str(e)[:120]}")
        return out

    # Dedup fetch
    seen = set()
    uniq = []
    for f in out["fetches"]:
        k = (f.get("url", "")[:200], f.get("method", ""), f.get("status", 0))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(f)
    out["fetches"] = uniq

    log(f"      TOTALE: fetch={len(out['fetches'])} parsed={len(out['parsed'])} "
        f"workers={len(out['workers'])} console={len(out['console'])}")
    return out


# ============================================================
# ESTRAZIONE CATALOGO
# ============================================================
def key_of(row):
    for f in KEY_FIELDS:
        v = row.get(f)
        if v:
            return str(v).strip()
    return ""


def extract_catalog_from_capture(cap):
    candidates = []
    for p in cap.get("parsed", []):
        s = p.get("sample", "")
        try:
            obj = json.loads(s)
        except Exception:
            continue
        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            if any(k in obj[0] for k in KEY_FIELDS):
                candidates.append(obj)
                continue
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    if any(kk in v[0] for kk in KEY_FIELDS):
                        candidates.append(v)
                        break
    return candidates


def print_capture_summary(cap):
    log("\n   === CAPTURE SUMMARY ===")
    log(f"   route: path={cap.get('route_info',{}).get('pathname')} "
        f"hash={cap.get('route_info',{}).get('hash')}")
    log(f"   link homepage: {len(cap.get('route_info',{}).get('links') or [])}")
    for l in (cap.get("route_info", {}).get("links") or [])[:15]:
        log(f"      [{l.get('text','')[:30]:30}] → {l.get('href','')[:90]}")

    log(f"\n   fetches: {len(cap.get('fetches') or [])}")
    for i, f in enumerate((cap.get("fetches") or [])[:15]):
        log(f"      [{i}] {f.get('method','?'):6} {f.get('url','?')[:120]} "
            f"→ {f.get('status','?')} ({f.get('resp_len', 0)}B)")
        rs = (f.get("resp_sample") or "")[:400]
        if rs:
            log(f"           resp: {rs}")
        bs = (f.get("body_sample") or "")[:200]
        if bs:
            log(f"           body: {bs}")

    log(f"\n   parsed: {len(cap.get('parsed') or [])}")
    for i, p in enumerate((cap.get("parsed") or [])[:8]):
        log(f"      [{i}] size={p.get('size','?'):>8}  src={p.get('source','?')}")
        sample = (p.get("sample") or "")[:1500]
        if sample:
            log(f"           {sample}")

    log(f"\n   workers: {len(cap.get('workers') or [])}")
    for w in (cap.get("workers") or [])[:5]:
        log(f"      {w}")

    log(f"\n   globals:")
    gl = cap.get("globals") or {}
    if not gl:
        log("      (nessuna)")
    for k, v in gl.items():
        log(f"      {k} = {str(v)[:300]}")

    log(f"\n   console (ultimi 10):")
    for c in (cap.get("console") or [])[-10:]:
        log(f"      [{c.get('level','?')}] {c.get('msg','')[:200]}")
    log("   === END CAPTURE ===\n")


# ============================================================
# RESOLVE EMBED
# ============================================================
LICENSE_HINTS = ("license", "widevine", "getlicense", "acquirelicense", "drm")
MANIFEST_HINTS = (".m3u8", ".mpd", "manifest", "playlist")
CF_HINTS = ("challenges.cloudflare.com", "cf-chl", "/cdn-cgi/challenge", "turnstile")


def resolve_embed(embed_url, origin, proxy=None, timeout=45, tag=""):
    try:
        from patchright.sync_api import sync_playwright
        engine = "patchright"
    except ImportError:
        from playwright.sync_api import sync_playwright
        engine = "playwright"

    rec = {"url": None, "m3u8": None, "mpd": None, "license_url": None,
           "cloudflare": False, "final_url": None, "title": None,
           "requests": [], "engine": engine, "referer": origin + "/"}

    def on_request(req):
        u = req.url; lu = u.lower()
        if any(k in lu for k in CF_HINTS):
            rec["cloudflare"] = True
        if any(k in lu for k in MANIFEST_HINTS + LICENSE_HINTS):
            if len(rec["requests"]) < 60:
                rec["requests"].append({"m": req.method, "u": u[:200]})
            if req.method == "POST" and any(k in lu for k in LICENSE_HINTS):
                rec["license_url"] = u

    def on_response(resp):
        u = resp.url; lu = u.lower()
        ct = (resp.headers.get("content-type") or "").lower()
        if ".m3u8" in lu or "mpegurl" in ct:
            try:
                txt = resp.text()
            except Exception:
                return
            if "#EXTM3U" in txt and rec["m3u8"] is None:
                rec["m3u8"] = u
        elif ".mpd" in lu or "dash+xml" in ct:
            try:
                txt = resp.text()
            except Exception:
                return
            if "<MPD" in txt and rec["mpd"] is None:
                rec["mpd"] = u

    try:
        with sync_playwright() as p:
            kw = {"headless": True,
                  "args": ["--no-sandbox", "--disable-dev-shm-usage",
                           "--use-gl=swiftshader", "--enable-unsafe-swiftshader",
                           "--autoplay-policy=no-user-gesture-required",
                           "--mute-audio"]}
            if proxy:
                kw["proxy"] = {"server": proxy}
            browser = p.chromium.launch(**kw)
            ctx = browser.new_context(
                user_agent=UA_IPHONE, viewport={"width": 1280, "height": 800},
                locale="it-IT", timezone_id="Europe/Rome",
                extra_http_headers={"Referer": origin + "/"},
                ignore_https_errors=True)
            if engine == "playwright":
                ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
            ctx.on("request", on_request)
            ctx.on("response", on_response)
            page = ctx.new_page()
            try:
                page.goto(embed_url, wait_until="domcontentloaded",
                          timeout=timeout * 1000)
            except Exception:
                pass
            page.wait_for_timeout(8000)
            for sel in ("button[aria-label*='play' i]", ".vjs-big-play-button",
                        ".jw-icon-playback", ".bitmovinplayer-ui",
                        ".clappr-play-button", "video"):
                try:
                    if page.locator(sel).count() > 0:
                        page.click(sel, timeout=2000)
                        page.wait_for_timeout(2500)
                        break
                except Exception:
                    continue
            page.wait_for_timeout(9000)
            browser.close()
    except Exception as e:
        log(f"      ❌ {tag}: {str(e)[:100]}")

    rec["url"] = rec["m3u8"] or rec["mpd"]
    return rec


# ============================================================
# M3U
# ============================================================
def entry_lines(rec, label, group, logo=""):
    lo = f' tvg-logo="{logo}"' if logo else ""
    out = [f'#EXTINF:-1{lo} group-title="{group}",{label}']
    if rec.get("referer"):
        out.append(f"#EXTVLCOPT:http-referrer={rec['referer']}")
    out.append(f"#EXTVLCOPT:http-user-agent={UA_IPHONE}")
    out.append("#KODIPROP:inputstream=inputstream.adaptive")
    out.append("#KODIPROP:inputstream.adaptive.manifest_type=hls")
    out.append(rec["url"])
    return out


def write_m3u(path, rows):
    seen, n = set(), 0
    with open(path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for label, logo, grp, rec in rows:
            if not rec.get("url") or rec["url"] in seen:
                continue
            seen.add(rec["url"])
            f.write("\n".join(entry_lines(rec, label, grp, logo)) + "\n")
            n += 1
    return n


def is_live(ev, now=None):
    if now is None:
        now = int(time.time())
    st = ev.get("start_time") or ev.get("timestamp") or 0
    et = ev.get("end_time") or 0
    if not st:
        return True
    if not et:
        et = st + 4 * 3600
    return (st - LIVE_MARGIN_BEFORE) <= now <= (et + LIVE_MARGIN_AFTER)


# ============================================================
# MAIN
# ============================================================
def main():
    ap = argparse.ArgumentParser(description="StreamCorner Extractor v9.2")
    ap.add_argument("--output-dir", "--output", default=".", dest="output_dir")
    ap.add_argument("--site", default=None)
    ap.add_argument("--proxy", default=os.environ.get("SC_PROXY"))
    ap.add_argument("--wait-boot", type=float, default=18)
    ap.add_argument("--wait-nav", type=float, default=12)
    ap.add_argument("--no-embeds", action="store_true")
    ap.add_argument("--all-events", action="store_true")
    ap.add_argument("--embed-workers", "--playwright-workers",
                    type=int, default=4, dest="embed_workers")
    ap.add_argument("--embed-limit", "--playwright-limit",
                    type=int, default=0, dest="embed_limit")
    args = ap.parse_args()

    combined_path = None
    if args.output_dir.lower().endswith((".m3u", ".m3u8")):
        combined_path = args.output_dir
        args.output_dir = os.path.dirname(combined_path) or "."

    os.makedirs(args.output_dir, exist_ok=True)
    drm_path = os.path.join(args.output_dir, DRM_REPORT)
    if os.path.exists(drm_path):
        os.remove(drm_path)

    log("=" * 66)
    log("📡 STREAMCORNER EXTRACTOR v9.2 (Playwright-as-oracle)")
    log("=" * 66)

    try:
        from patchright.sync_api import sync_playwright  # noqa
        log("   ✅ patchright attivo")
    except ImportError:
        log("   ⚠️ patchright assente — fallback playwright")

    # ---- 1. Capture ----
    site = args.site or PRIORITY_SITES[0]
    log(f"\n[1/5] Browser capture su {site}")
    cap = capture_from_browser(site, args.proxy,
                                wait_boot=int(args.wait_boot),
                                wait_nav=int(args.wait_nav))

    # Stampa capture nei log (debug senza artifact)
    print_capture_summary(cap)

    # Salva su file (per artifact)
    try:
        with open(os.path.join(args.output_dir, CAPTURE_FILE), "w",
                  encoding="utf-8") as f:
            json.dump(cap, f, ensure_ascii=False, indent=1, default=str)
        log(f"   💾 {CAPTURE_FILE} salvato (scaricalo via artifact)")
    except Exception as e:
        log(f"   ⚠️ salvataggio capture: {e}")

    # ---- 2. Estrazione catalogo ----
    log(f"\n[2/5] Estrazione catalogo dai JSON decifrati")
    catalogs = extract_catalog_from_capture(cap)
    if not catalogs:
        log("   ❌ nessun catalogo trovato nei JSON.parse")
        log("   → guarda CAPTURE SUMMARY sopra per capire cosa fa la SPA")
        for fn in ("streamcorner_VLC.m3u", "streamcorner_KODI.m3u",
                   "streamcorner_EVENTS_LIVE.m3u"):
            open(os.path.join(args.output_dir, fn), "w").write("#EXTM3U\n")
        if combined_path:
            open(combined_path, "w").write("#EXTM3U\n")
        return

    catalogs.sort(key=len, reverse=True)
    events = catalogs[0]
    log(f"   ✅ catalogo: {len(events)} item (da {len(catalogs)} candidati)")

    # Census
    n_src, n_url = 0, 0
    for ev in events:
        for s in (ev.get("streams") or []):
            if isinstance(s, dict):
                n_src += 1
                if s.get("stream_url"):
                    n_url += 1
    log(f"   census: sorgenti={n_src}  con stream_url={n_url}")

    # ---- 3. Filtro live ----
    log(f"\n[3/5] Filtro live")
    if args.all_events:
        live = events
    else:
        live = [e for e in events if is_live(e)]
        log(f"   🔴 {len(live)}/{len(events)} in onda adesso")

    # ---- 4. Sorgenti ----
    log(f"\n[4/5] Sorgenti")
    cache = {}
    if os.path.exists(M3U8_CACHE):
        try:
            cache = json.load(open(M3U8_CACHE, encoding="utf-8"))
        except Exception:
            cache = {}

    p = urlparse(site)
    origin = f"{p.scheme}://{p.netloc}"

    rows, embed_jobs = [], []
    for ev in live:
        name = ev.get("event_name") or ev.get("title") or "?"
        league = ev.get("league") or ""
        logo = ev.get("poster") or ev.get("hero_thumbnail") or ""
        base = f"{name} ({league})" if league else name

        for s in ev.get("streams") or []:
            if not isinstance(s, dict):
                continue
            u = str(s.get("stream_url") or "").strip()
            if u and DIRECT_RE.search(u.split("?")[0]):
                rows.append((f"{base} — {s.get('source_name') or '?'} [DIRETTA]",
                             logo, name, {"url": u, "referer": origin + "/"}))
                continue
            emb = s.get("embed_url")
            if not emb or args.no_embeds:
                continue
            sname = s.get("source_name") or "?"
            if cache.get(emb):
                rows.append((f"{base} — {sname}", logo, name,
                             {"url": cache[emb], "referer": origin + "/"}))
            else:
                embed_jobs.append((emb, f"{base} — {sname}", logo, name))

    log(f"   dirette: {len(rows)}   embed da risolvere: {len(embed_jobs)}")
    if args.embed_limit:
        embed_jobs = embed_jobs[:args.embed_limit]

    report = []
    if embed_jobs:
        done = 0

        def job(item):
            emb, label, logo, grp = item
            return emb, label, logo, grp, resolve_embed(
                emb, origin, args.proxy, tag=label[:28])

        with ThreadPoolExecutor(max_workers=args.embed_workers) as ex:
            for f in as_completed([ex.submit(job, i) for i in embed_jobs]):
                done += 1
                try:
                    emb, label, logo, grp, r = f.result()
                except Exception as e:
                    log(f"   ❌ {str(e)[:90]}"); continue
                r["embed"] = emb; r["label"] = label
                report.append(r)
                if r.get("url"):
                    cache[emb] = r["url"]
                    rows.append((label, logo, grp, r))
                    log(f"   🎯 [{done}/{len(embed_jobs)}] {label[:45]}")
                elif done % 5 == 0:
                    log(f"   · [{done}/{len(embed_jobs)}]")

        try:
            json.dump(cache, open(M3U8_CACHE, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
        except Exception:
            pass

    if report:
        with open(drm_path, "w", encoding="utf-8") as f:
            for r in report:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # ---- 5. Output ----
    log(f"\n[5/5] Output")
    n = write_m3u(os.path.join(args.output_dir, "streamcorner_EVENTS_LIVE.m3u"), rows)
    log(f"   💾 EVENTS_LIVE: {n}")
    n = write_m3u(os.path.join(args.output_dir, "streamcorner_VLC.m3u"),
                  [(l, lg, "StreamCorner", r) for l, lg, _, r in rows])
    log(f"   💾 VLC: {n}")
    n = write_m3u(os.path.join(args.output_dir, "streamcorner_KODI.m3u"), rows)
    log(f"   💾 KODI: {n}")
    if combined_path:
        n = write_m3u(combined_path, rows)
        log(f"   💾 COMBINED: {n}")

    log("\n" + "=" * 66)
    log(f"✅ eventi={len(live)} stream={len(rows)} report={len(report)}")
    log("=" * 66)


if __name__ == "__main__":
    main()