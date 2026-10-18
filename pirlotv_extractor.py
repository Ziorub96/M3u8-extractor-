#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PirloTV / Rojadirecta / Liveru Extractor v1.5

Changelog v1.4 → v1.5:
- FIX #1: nuova funzione extract_playback_url() che cattura
  `var playbackURL = "https://..."` (streamtp99a.sbs e simili).
  Priorità 1.5 in process_player: prima dell'offuscamento.
- FIX #2: _extract_iframe_with_fid() ora gestisce stringhe JS con
  quote interne al valore src. Il bug `[^"']+` è risolto con `[^"]+`.
  Strip automatico dei pattern `+variabile+` dal suffix/prefix
  (es. `.php?width=+width+` → `.php?width=`).
  Rojadirecta: fid → reproductor/{fid}.php → playbackURL → m3u8.
- FIX #3: nuova funzione is_library_script() con LIBRARY_HOSTS
  (clappr, hls.js, dash.js, jwplayer, jsdelivr, gstatic, ecc.).
  MAX_EMBED_BYTES = 80_000 (embed reali ~578 B).
  applicato in find_embed_scripts() e process_embed_script().
  Skip clappr.min.js 630 KB (risparmio ~10s/run).
- Log IP-lock riconosciuto (token con `ip=` nel log, non nel M3U).

Invariato da v1.4:
- resolve_js_redirect con fix pathname in var_def
- _collapse_strings per stringhe spezzate
- decode_obfuscated_url generico (qG, eQ, tR, Lg, …)
- Catalogo streamtp99a.sbs come fallback
- Retry UA iPhone su 403
"""
from __future__ import annotations
import argparse, base64, json, os, re, sys, time
from pathlib import Path
from urllib.parse import urlparse, urljoin, parse_qs

UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")
UA_IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) "
             "Version/17.0 Mobile Safari/604.1")

SITES = [
    {"name": "pirlotv1", "base": "https://pirlotv1.live", "engine": "tR"},
    {"name": "pirlo-tv", "base": "https://pirlo-tv.su", "engine": "tR"},
    {"name": "pirlotvv", "base": "https://pirlotvv.org", "engine": "tR"},
    {"name": "liveru", "base": "https://liveru.me", "engine": "auto"},
    {"name": "rojadirectaoficial", "base": "https://rojadirectaoficial.de", "engine": "tR"},
]

MATCH_LINK_PATTERNS = [
    r'/live-match/',
    r'/match/',
    r'/evento?/',
    r'/partita/',
    r'/ver/',
    r'/live/[^"\']+',
    r'/calcio/',
    r'/stream/',
    r'/canal-',
    r'\?r=[A-Za-z0-9+/=]{20,}',
    r'\?id=\d+',
    r'\?ch(?:annel)?=\w+',
    r'\?stream=',
]

ASSET_SKIP = (
    "/css/", "/js/", "/img/", "/images/", "/fonts/", "/assets/",
    ".css", ".js", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".woff",
    ".woff2", ".ttf", ".gif", ".webp",
    "/tag/", "/category/", "/feed", "/about", "/contact",
    "/privacy", "/terms", "/dmca", "/faq", "/login", "/register", "/search",
)

SKIP_HOSTS = {
    "taifood-blog.asia", "pandecocogaming.sbs",
    "doubleclick.net", "googlesyndication.com", "googletagmanager.com",
    "google-analytics.com", "facebook.net", "facebook.com",
    "cloudflareinsights.com", "cdn4ads.com", "adsco.re",
    "yandex.ru", "yandex.com", "mc.yandex.ru", "histats.com",
    "s10.histats.com", "waust.at", "platform-api.sharethis.com",
    "llvpn.one",
}

DRM_HINTS = ("edef8ba9", "9a04f079", "1077efecc0b24d02")

EMBED_SCRIPT_PATTERNS = [
    r'src=["\']([^"\']*embed[^"\']*\.js[^"\']*)["\']',
    r'src=["\']([^"\']*player[^"\']*\.js[^"\']*)["\']',
    r'src=["\']([^"\']*stream[^"\']*\.js[^"\']*)["\']',
]

CATALOG_HOSTS = {"streamtp99a.sbs", "streamtp99b.sbs", "streamtpday1.xyz"}

# v1.5: skip librerie JS (non sono embed, sono codice)
LIBRARY_HOSTS = (
    "clappr", "hls.js", "hls.min", "p2p-engine", "swarmcloud",
    "dash.js", "shaka", "video.js", "jwplayer", "jquery",
    "bootstrap", "fontawesome", "googleapis", "gstatic", "jsdelivr",
    "cloudflareinsights", "cdnjs", "unpkg",
)
MAX_EMBED_BYTES = 80_000

# v1.5: pattern per estrarre playbackURL / source / file
PLAYBACK_URL_PATTERNS = [
    r'var\s+playbackURL\s*=\s*["\']([^"\']+)["\']',
    r'playbackURL\s*=\s*["\']([^"\']+)["\']',
    r'"playbackURL"\s*:\s*["\']([^"\']+)["\']',
    r'source\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
    r'source\s*=\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
    r'file\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
    r'hlsUrl\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
    r'"url"\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
]


def log(*a): print(*a, flush=True)


def http_get(url, referer=None, ua=UA_DESKTOP, timeout=20, session=None):
    headers = {
        "User-Agent": ua,
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    try:
        import requests
        sess = session or requests.Session()
        r = sess.get(url, headers=headers, timeout=timeout, verify=False)
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
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception:
        return None, ""


def find_match_links(html, base):
    links = []
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html):
        href = m.group(1).strip()
        if not href or href.startswith(("javascript:", "#", "mailto:", "tel:")):
            continue
        if any(x in href.lower() for x in ASSET_SKIP):
            continue
        if any(re.search(p, href) for p in MATCH_LINK_PATTERNS):
            full = href if href.startswith("http") else urljoin(base, href)
            if full not in links:
                links.append(full)
    if not links:
        for m in re.finditer(r'href=["\'](/([a-z0-9][a-z0-9-]{3,})/?)["\']', html, re.I):
            href = m.group(1)
            if any(x in href.lower() for x in ASSET_SKIP):
                continue
            full = urljoin(base, href)
            if full not in links:
                links.append(full)
    return links


def decode_r_param(match_url):
    try:
        qs = parse_qs(urlparse(match_url).query)
        if "r" not in qs:
            return None
        b64 = qs["r"][0]
        pad = "=" * (-len(b64) % 4)
        decoded = base64.b64decode(b64 + pad).decode("utf-8", "replace")
        if decoded.startswith("http"):
            return decoded
    except Exception:
        pass
    return None


# -------------------------------------------------------------------
# v1.5 FIX #1: estrazione playbackURL / source / file / hlsUrl
# -------------------------------------------------------------------
def extract_playback_url(html: str):
    """
    Cerca l'URL m3u8 in chiaro nell'HTML.
    Casi coperti:
      - var playbackURL = "https://...m3u8?..."  (streamtp99a.sbs, streamtp*)
      - source: "https://...m3u8"                 (Clappr config diretto)
      - source = "https://...m3u8"
      - file: "https://...m3u8"                   (JWPlayer)
      - hlsUrl: "https://...m3u8"
      - "url": "https://...m3u8"
      - fallback: qualsiasi URL .m3u8 inline
    """
    for pat in PLAYBACK_URL_PATTERNS:
        m = re.search(pat, html)
        if m:
            url = m.group(1).replace("\\/", "/").replace("\\u002F", "/")
            if not url:
                continue
            if url.startswith("//"):
                url = "https:" + url
            if url.startswith(("http://", "https://")):
                return url
    # Fallback: qualsiasi m3u8 inline (esclude data:/blob:)
    for m in re.finditer(r'(https?://[^"\'\s\\]+\.m3u8[^"\'\s\\]*)', html):
        url = m.group(1).replace("\\/", "/")
        return url
    return None


# -------------------------------------------------------------------
# v1.5 FIX #3: skip librerie JS
# -------------------------------------------------------------------
def is_library_script(url: str, content_len: int = 0) -> bool:
    """
    Ritorna True se l'URL punta a una libreria JS nota o il contenuto
    è troppo grande per essere un embed script.
    """
    ul = url.lower()
    if any(h in ul for h in LIBRARY_HOSTS):
        return True
    if content_len > MAX_EMBED_BYTES:
        return True
    return False


# -------------------------------------------------------------------
# Decodifica offuscamento generico (Lg=, tR=, qG=, eQ=, …)
# -------------------------------------------------------------------
def _find_k_from_functions(html):
    m = re.search(r'var\s+k\s*=\s*(\w+)\s*\(\s*\)\s*([+\-*])\s*(\w+)\s*\(\s*\)', html)
    if m:
        f1, op, f2 = m.groups()
        r1 = re.search(r'function\s+' + re.escape(f1) + r'\s*\(\s*\)\s*\{\s*return\s+(\d+)\s*;?\s*\}', html)
        r2 = re.search(r'function\s+' + re.escape(f2) + r'\s*\(\s*\)\s*\{\s*return\s+(\d+)\s*;?\s*\}', html)
        if r1 and r2:
            n1, n2 = int(r1.group(1)), int(r2.group(1))
            if op == "+": return n1 + n2
            if op == "-": return n1 - n2
            if op == "*": return n1 * n2
    m = re.search(r'var\s+k\s*=\s*(\d+)\s*([+\-*])\s*(\d+)', html)
    if m:
        n1, op, n2 = int(m.group(1)), m.group(2), int(m.group(3))
        if op == "+": return n1 + n2
        if op == "-": return n1 - n2
        if op == "*": return n1 * n2
    consts = re.findall(r'function\s+\w+\s*\(\s*\)\s*\{\s*return\s+(\d+)\s*;?\s*\}', html)
    if len(consts) >= 2:
        return sum(int(c) for c in consts[:2])
    return None


def decode_obfuscated_url(html):
    array_matches = re.findall(
        r'(\w+)\s*=\s*\[((?:\[\s*\d+\s*,\s*"[^"]+"\s*\](?:\s*,\s*)?)+)\]\s*;',
        html
    )
    if not array_matches:
        return None

    for var_name, content in array_matches:
        pairs = re.findall(r'\[\s*(\d+)\s*,\s*"([^"]+)"\s*\]', content)
        if len(pairs) < 10:
            continue

        k = _find_k_from_functions(html)
        if k is None:
            continue

        log(f"      🔓 {var_name}: {len(pairs)} coppie, k={k}")

        try:
            chars = []
            for idx_str, b64 in sorted((int(i), b) for i, b in pairs):
                decoded = base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8", "replace")
                digits = re.sub(r"\D", "", decoded)
                if not digits:
                    continue
                c = int(digits) - k
                if 0 < c < 0x110000:
                    chars.append(chr(c))
            url = "".join(chars)
            log(f"         → decodificato: {url[:60]!r}")
            if url.startswith("http"):
                return url
        except Exception as e:
            log(f"      ⚠️ {var_name} decode err: {type(e).__name__}")
            continue

    return None


# -------------------------------------------------------------------
# v1.4: resolve_js_redirect con fix pathname in var_def
# -------------------------------------------------------------------
def resolve_js_redirect(html, base_url):
    m = re.search(
        r'(?:const|let|var)\s+(\w+)\s*=\s*["\'](https?://[^"\']+)["\']\s*;'
        r'[\s\S]{0,300}?window\.location\.(?:replace|assign|href\s*=)\s*\(\s*'
        + r'\1' + r'\s*\+\s*([\w\.\(\)\s]+?)\)',
        html
    )
    if m:
        nuevo_dominio = m.group(2)
        suffix_expr = m.group(3).strip()

        var_name_match = re.match(r'^(\w+)$', suffix_expr)
        var_def = ""
        if var_name_match:
            var_name = var_name_match.group(1)
            var_def_match = re.search(
                r'(?:const|let|var)\s+' + re.escape(var_name) + r'\s*=\s*([^;]+);',
                html
            )
            if var_def_match:
                var_def = var_def_match.group(1)

        has_path = ("pathname" in var_def or "search" in var_def or
                    "pathname" in suffix_expr or "location" in suffix_expr)

        if has_path:
            base = urlparse(base_url)
            path = base.path + ("?" + base.query if base.query else "")
            full = nuevo_dominio + path
            log(f"      ↪️ redirect JS (dominio+path): {full[:100]}")
            return full
        log(f"      ↪️ redirect JS (solo dominio): {nuevo_dominio[:100]}")
        return nuevo_dominio

    m = re.search(
        r'window\.location\.(?:replace|assign|href\s*=)\s*\(?\s*["\']([^"\']+)["\']',
        html
    )
    if m:
        target = m.group(1)
        if target.startswith("http"):
            log(f"      ↪️ redirect JS diretto: {target[:100]}")
            return target
        else:
            full = urljoin(base_url, target)
            log(f"      ↪️ redirect JS relativo: {full[:100]}")
            return full

    m = re.search(
        r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\']\d+\s*;\s*url=([^"\']+)["\']',
        html, re.I
    )
    if m:
        target = m.group(1)
        full = target if target.startswith("http") else urljoin(base_url, target)
        log(f"      ↪️ redirect meta: {full[:100]}")
        return full

    return None


# -------------------------------------------------------------------
# v1.4: _collapse_strings per stringhe spezzate
# -------------------------------------------------------------------
def _collapse_strings(js):
    prev = None
    s = js
    max_iter = 20
    while s != prev and max_iter > 0:
        prev = s
        s = re.sub(r"'([^']*)'\s*\+\s*'([^']*)'", r"'\1\2'", s)
        s = re.sub(r'"([^"]*)"\s*\+\s*"([^"]*)"', r'"\1\2"', s)
        max_iter -= 1
    return s


def _extract_iframe_with_fid(js):
    """
    v1.5 FIX #2: gestisce stringhe spezzate ('<ifr'+'ame') e
    valori src con quote interne.
    Es: document.write('<iframe src="host/path/'+ fid +'.php?...">')
    Ritorna (prefix, suffix) o (None, None).
    """
    js_collapsed = _collapse_strings(js)

    def _clean_and_split(src_val):
        # Sostituisci '+ fid +' con {fid}
        cleaned = re.sub(r"['\"]?\s*\+\s*fid\s*\+\s*['\"]?", "{fid}", src_val)
        # Rimuovi quote residue
        cleaned = cleaned.replace("'", "").replace('"', "")
        parts = cleaned.split("{fid}")
        if len(parts) != 2:
            return None
        prefix, suffix = parts
        # Strip pattern +variabile+ da prefix e suffix
        prefix = re.sub(r'\+[a-zA-Z_]\w*\+', '', prefix)
        suffix = re.sub(r'\+[a-zA-Z_]\w*\+', '', suffix)
        return (prefix, suffix)

    # src="..." (double quotes) — ammette singole quote dentro il valore
    for m in re.finditer(r'src\s*=\s*"([^"]+)"', js_collapsed):
        src_val = m.group(1)
        if "fid" not in src_val:
            continue
        result = _clean_and_split(src_val)
        if result:
            return result

    # src='...' (single quotes) — ammette doppie quote dentro il valore
    for m in re.finditer(r"src\s*=\s*'([^']+)'", js_collapsed):
        src_val = m.group(1)
        if "fid" not in src_val:
            continue
        result = _clean_and_split(src_val)
        if result:
            return result

    return (None, None)


def process_embed_script(script_url, referer):
    """Scarica e analizza un JS embed (es. embedt1.js). v1.5: skip librerie."""
    # v1.5 FIX #3: pre-check senza scaricare
    if is_library_script(script_url):
        log(f"      ⏭️ skip library: {script_url[:80]}")
        return None

    st, js = http_get(script_url, referer=referer)
    if not st or st != 200 or not js:
        log(f"      ⚠️ embed script HTTP {st}")
        return None

    # v1.5 FIX #3: post-check sulla dimensione
    if is_library_script(script_url, len(js)):
        log(f"      ⏭️ skip library/oversize ({len(js)} byte): {script_url[:80]}")
        return None

    try:
        safe_host = re.sub(r'[^a-z0-9]', '_', urlparse(script_url).hostname or "")[:30]
        safe_name = re.sub(r'[^a-z0-9]', '_', script_url.split("/")[-1])[:30]
        Path("pirlotv_debug").mkdir(exist_ok=True)
        Path(f"pirlotv_debug/script_{safe_host}__{safe_name}.js").write_text(js, encoding="utf-8")
        log(f"      💾 script salvato ({len(js)} byte)")
    except Exception:
        pass

    # Pattern iframe + fid
    prefix, suffix = _extract_iframe_with_fid(js)
    if prefix and suffix:
        log(f"      📜 embed script → NEED_FID: {prefix[:60]!r} + fid + {suffix[:40]!r}")
        return ("NEED_FID", prefix, suffix)

    # Fallback: offuscamento
    decoded = decode_obfuscated_url(js)
    if decoded:
        return decoded

    # Fallback: m3u8/mpd diretto
    for pat in (r'https?://[^"\'\\\s]+\.m3u8[^"\'\\\s]*',
                r'https?://[^"\'\\\s]+\.mpd[^"\'\\\s]*'):
        for m in re.finditer(pat, js):
            u = m.group(0).replace("\\/", "/")
            if any(h in u for h in DRM_HINTS):
                continue
            log(f"      🎯 m3u8 in embed script: {u[:100]}")
            return u

    return None


def find_iframes(html, base):
    iframes = []
    for m in re.finditer(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.I):
        src = m.group(1).strip()
        if src.startswith(("about:", "javascript:", "data:")):
            continue
        full = src if src.startswith("http") else urljoin(base, src)
        if full not in iframes:
            iframes.append(full)
    return iframes


def pick_best_variant(master_url, referer=None):
    st, txt = http_get(master_url, referer=referer)
    if not st or st != 200 or not txt:
        return master_url
    if "#EXT-X-STREAM-INF" not in txt:
        return master_url
    variants = []
    lines = txt.splitlines()
    for i, l in enumerate(lines):
        if l.startswith("#EXT-X-STREAM-INF"):
            res = re.search(r"RESOLUTION=(\d+x\d+)", l)
            bw = re.search(r"BANDWIDTH=(\d+)", l)
            if i + 1 < len(lines):
                uri = lines[i + 1].strip()
                if uri and not uri.startswith("#"):
                    bw_n = int(bw.group(1)) if bw else 0
                    res_s = res.group(1) if res else "?"
                    variants.append((bw_n, res_s, uri))
    if not variants:
        return master_url
    variants.sort(reverse=True)
    best = variants[0][2]
    if not best.startswith("http"):
        best = urljoin(master_url, best)
    return best


def _debug_save(html, host, player_url):
    try:
        safe_host = re.sub(r'[^a-z0-9]', '_', host)[:30]
        safe_path = re.sub(r'[^a-z0-9]', '_',
                           player_url.split("?")[0].split("/")[-1])[:30] or "root"
        fname = f"player_{safe_host}__{safe_path}.html"
        Path("pirlotv_debug").mkdir(exist_ok=True)
        Path(f"pirlotv_debug/{fname}").write_text(html, encoding="utf-8")
        log(f"      💾 {fname} ({len(html)} byte)")
    except Exception:
        pass


def _diagnose(html):
    has_array = bool(re.search(r'\w+\s*=\s*\[\[\s*\d+\s*,\s*"', html))
    has_atob = "atob(" in html
    has_source = bool(re.search(r'source["\']?\s*[:=]', html, re.I))
    has_file = bool(re.search(r'file["\']?\s*[:=]', html, re.I))
    has_clappr = "clappr" in html.lower()
    has_jw = "jwplayer" in html.lower()
    has_bitmovin = "bitmovin" in html.lower()
    has_hls_js = "hls.js" in html.lower() or "hlsjs" in html.lower()
    has_redirect = "window.location" in html
    has_catalog = "const channels" in html or "const channels=" in html
    has_playback_url = bool(re.search(r'playbackURL\s*=\s*["\']https?://', html))
    log(f"      🔬 arr={has_array} atob={has_atob} src={has_source} file={has_file} "
        f"clappr={has_clappr} jw={has_jw} bitmovin={has_bitmovin} hls={has_hls_js} "
        f"redir={has_redirect} catalog={has_catalog} pbURL={has_playback_url}")


def _extract_catalog_channels(html):
    channels = {}
    m = re.search(r'const\s+channels\s*=\s*\{([^}]+)\}', html, re.DOTALL)
    if not m:
        return channels
    body = m.group(1)
    for cm in re.finditer(r'["\']([^"\']+)["\']\s*:\s*["\']([^"\']+)["\']', body):
        name = cm.group(1)
        url = cm.group(2)
        if url.startswith("http"):
            channels[name] = url
    return channels


def process_player(player_url, referer=None, depth=0, max_catalog_channels=10):
    if depth > 3:
        return None
    host = urlparse(player_url).hostname or ""
    if host in SKIP_HOSTS:
        log(f"      ⏭️ skip host {player_url[:80]}")
        return None

    st, html = http_get(player_url, referer=referer)
    if not st or st != 200:
        log(f"      ⚠️ player HTTP {st} ({player_url[:80]})")
        if st == 403:
            st2, html2 = http_get(player_url, referer=referer, ua=UA_IPHONE)
            if st2 == 200:
                log(f"      ✓ retry mobile OK ({len(html2)} byte)")
                st, html = st2, html2
            else:
                log(f"      ✗ retry mobile HTTP {st2}")
                return None
        else:
            return None

    _debug_save(html, host, player_url)
    _diagnose(html)

    # 1) Già un manifest
    if player_url.endswith(".m3u8") or "#EXTM3U" in html[:200]:
        return {"url": player_url, "mtype": "hls", "referer": referer}

    # 1.5) v1.5 FIX #1: playbackURL (streamtp99a.sbs e simili)
    pb_url = extract_playback_url(html)
    if pb_url:
        log(f"      🎯 playbackURL → {pb_url[:100]}")
        best = pick_best_variant(pb_url, referer=player_url)
        mtype = "dash" if ".mpd" in best else "hls"
        return {"url": best, "mtype": mtype, "referer": player_url}

    # 2) Offuscamento generico (Lg, tR, qG, eQ, …)
    m3u8 = decode_obfuscated_url(html)
    if m3u8:
        log(f"      🎯 offuscato → {m3u8[:100]}")
        best = pick_best_variant(m3u8, referer=player_url)
        mtype = "dash" if ".mpd" in best else "hls"
        return {"url": best, "mtype": mtype, "referer": player_url}

    # 3) Regex m3u8/mpd diretta (fallback)
    for pat in (
        r'https?://[^"\'\\\s]+\.m3u8[^"\'\\\s]*',
        r'https?://[^"\'\\\s]+\.mpd[^"\'\\\s]*',
        r'["\']([^"\']*\.m3u8[^"\']*)["\']',
        r'"url"\s*:\s*"([^"]+\.m3u8[^"]*)"',
    ):
        for m in re.finditer(pat, html, re.I):
            u = m.group(1) if m.lastindex else m.group(0)
            u = u.replace("\\/", "/").replace("\\u002F", "/")
            if not u.startswith("http"):
                if u.startswith("//"):
                    u = "https:" + u
                else:
                    continue
            if any(h in u for h in DRM_HINTS):
                continue
            st_v, txt_v = http_get(u, referer=player_url)
            if st_v == 200 and ("#EXTM3U" in txt_v or "<MPD" in txt_v):
                log(f"      🎯 regex → {u[:100]}")
                best = pick_best_variant(u, referer=player_url)
                mtype = "dash" if ".mpd" in best else "hls"
                return {"url": best, "mtype": mtype, "referer": player_url}

    # 4) atob automatico
    for m in re.finditer(r'atob\(["\']([A-Za-z0-9+/=]{30,})["\']\)', html):
        try:
            decoded = base64.b64decode(m.group(1) + "=" * (-len(m.group(1)) % 4)).decode(
                "utf-8", "replace")
            if ".m3u8" in decoded or ".mpd" in decoded:
                for u in re.findall(r'https?://[^"\'\s]+\.(?:m3u8|mpd)[^"\'\s]*', decoded):
                    log(f"      🎯 atob → {u[:100]}")
                    best = pick_best_variant(u, referer=player_url)
                    mtype = "dash" if ".mpd" in best else "hls"
                    return {"url": best, "mtype": mtype, "referer": player_url}
        except Exception:
            pass

    # 5) Redirect JavaScript
    redirect_url = resolve_js_redirect(html, player_url)
    if redirect_url:
        log(f"      🔀 seguo redirect JS → {redirect_url[:100]}")
        sub = process_player(redirect_url, referer=player_url, depth=depth + 1,
                             max_catalog_channels=max_catalog_channels)
        if sub:
            return sub

    # 6) Iframe annidati
    ifs = find_iframes(html, player_url)
    for if_u in ifs:
        if (urlparse(if_u).hostname or "") in SKIP_HOSTS:
            continue
        log(f"      🖼️ segue iframe: {if_u[:100]}")
        sub = process_player(if_u, referer=player_url, depth=depth + 1,
                             max_catalog_channels=max_catalog_channels)
        if sub:
            return sub

    # 7) Script embed esterni (embedt1.js) — v1.5: skip librerie
    scripts = find_embed_scripts(html, player_url)
    for s_url in scripts:
        log(f"      📜 script embed: {s_url[:100]}")
        decoded = process_embed_script(s_url, referer=player_url)
        if decoded:
            if isinstance(decoded, tuple) and decoded[0] == "NEED_FID":
                _, prefix, suffix = decoded
                fid_m = re.search(r'fid\s*=\s*["\']([^"\']+)["\']', html)
                if fid_m:
                    fid = fid_m.group(1)
                    full_url = f"{prefix}{fid}{suffix}"
                    log(f"      🔀 embed URL con fid: {full_url[:100]}")
                    sub = process_player(full_url, referer=player_url, depth=depth + 1,
                                         max_catalog_channels=max_catalog_channels)
                    if sub:
                        return sub
                else:
                    log(f"      ⚠️ NEED_FID ma nessun fid nell'HTML")
                continue
            log(f"      🎯 script embed → {decoded[:100]}")
            best = pick_best_variant(decoded, referer=player_url)
            mtype = "dash" if ".mpd" in best else "hls"
            return {"url": best, "mtype": mtype, "referer": player_url}

    # 8) JSON con campo url
    try:
        j = json.loads(html)
        if isinstance(j, dict):
            for k in ("url", "stream", "hls", "src", "file", "source"):
                if k in j and isinstance(j[k], str) and ".m3u8" in j[k]:
                    log(f"      🎯 JSON[{k}] → {j[k][:100]}")
                    best = pick_best_variant(j[k], referer=player_url)
                    return {"url": best, "mtype": "hls", "referer": player_url}
    except Exception:
        pass

    # 9) Catalogo STREAMTP (fallback)
    if host in CATALOG_HOSTS:
        catalog = _extract_catalog_channels(html)
        if catalog:
            log(f"      📚 catalogo {host}: {len(catalog)} canali")
            for i, (name, ch_url) in enumerate(catalog.items()):
                if i >= max_catalog_channels:
                    break
                log(f"        [{i+1}/{min(len(catalog), max_catalog_channels)}] {name}")
                sub = process_player(ch_url, referer=player_url, depth=depth + 1,
                                     max_catalog_channels=0)
                if sub:
                    return sub

    return None


def find_embed_scripts(html, base):
    """v1.5 FIX #3: skip librerie JS."""
    scripts = []
    for pat in EMBED_SCRIPT_PATTERNS:
        for m in re.finditer(pat, html, re.I):
            src = m.group(1).strip()
            if src.startswith(("http", "//")):
                full = "https:" + src if src.startswith("//") else src
                host = urlparse(full).hostname or ""
                if host in SKIP_HOSTS:
                    continue
                if is_library_script(full):
                    log(f"      ⏭️ skip library: {full[:80]}")
                    continue
                if full not in scripts:
                    scripts.append(full)
    return scripts


def extract_from_site(site, max_matches=25, timeout_total=600):
    base = site["base"]
    log(f"\n🌐 [{site['name']}] {base}")
    t0 = time.time()
    st, html = http_get(base, referer=base)
    if not st or st != 200:
        log(f"  ❌ homepage HTTP {st}")
        return []
    log(f"  ✓ homepage {len(html)} byte")
    links = find_match_links(html, base)
    log(f"  📋 {len(links)} link partita trovati")
    if not links:
        return []
    streams = []
    for i, link in enumerate(links[:max_matches], 1):
        if time.time() - t0 > timeout_total:
            log(f"  ⏰ budget esaurito ({timeout_total}s)")
            break
        log(f"  [{i}/{min(len(links), max_matches)}] {link[:100]}")
        title = f"PirloTV {i}"
        if ".m3u8" in link:
            streams.append({"url": link, "mtype": "hls", "title": title,
                            "referer": base, "site": site["name"]})
            continue
        player_url = decode_r_param(link)
        if player_url:
            log(f"      🔓 r= → {player_url[:100]}")
        else:
            player_url = link
        result = process_player(player_url, referer=link)
        if result:
            result["title"] = title
            result["site"] = site["name"]
            streams.append(result)
            log(f"      ✅ {result['url'][:100]}")
    return streams


def write_m3u(path, entries, group_override=None):
    seen = set()
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for e in entries:
            u = e.get("url")
            if not u or u in seen:
                continue
            seen.add(u)
            grp = group_override or e.get("site") or "PirloTV"
            title = (e.get("title") or "Evento")[:60]
            ref = e.get("referer") or ""
            f.write(f'\n#EXTINF:-1 group-title="{grp}",{title}\n')
            if ref:
                f.write(f"#EXTVLCOPT:http-referrer={ref}\n")
            f.write(f"#EXTVLCOPT:http-user-agent={UA_DESKTOP}\n")
            f.write("#KODIPROP:inputstream=inputstream.adaptive\n")
            if e.get("mtype"):
                f.write(f"#KODIPROP:inputstream.adaptive.manifest_type={e['mtype']}\n")
            f.write(f"{u}\n")
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", "-o", default="pirlotv_COMPLETE.m3u")
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--max-per-site", type=int, default=20)
    ap.add_argument("--timeout-per-site", type=int, default=300)
    args = ap.parse_args()

    if args.output_dir is None:
        args.output_dir = os.path.dirname(args.output) or "."
    os.makedirs(args.output_dir, exist_ok=True)

    log("=" * 66)
    log("📡 PIRLOTV EXTRACTOR v1.5 (playbackURL + iframe fid + skip libraries)")
    log("=" * 66)

    all_streams = []
    for site in SITES:
        try:
            streams = extract_from_site(
                site, max_matches=args.max_per_site,
                timeout_total=args.timeout_per_site)
            all_streams.extend(streams)
            log(f"  ✅ [{site['name']}] {len(streams)} stream")
        except Exception as e:
            log(f"  ⚠️ [{site['name']}] errore: {type(e).__name__}: {e}")

    seen = set()
    uniq = []
    for s in all_streams:
        if s["url"] not in seen:
            seen.add(s["url"])
            uniq.append(s)

    log(f"\n📊 TOTALE: {len(uniq)} stream unici su {len(all_streams)} trovati")

    n1 = write_m3u(os.path.join(args.output_dir, "pirlotv_EVENTS_LIVE.m3u"), uniq)
    n2 = write_m3u(os.path.join(args.output_dir, "pirlotv_VLC.m3u"),
                   uniq, group_override="PirloTV")
    n3 = write_m3u(args.output, uniq)
    log(f"  💾 pirlotv_EVENTS_LIVE.m3u: {n1}")
    log(f"  💾 pirlotv_VLC.m3u: {n2}")
    log(f"  💾 {args.output}: {n3}")

    summary = {
        "sites": [s["name"] for s in SITES],
        "total_found": len(all_streams),
        "total_unique": len(uniq),
        "streams": [{"title": s.get("title"), "url": s["url"][:200],
                     "site": s.get("site"), "mtype": s.get("mtype")}
                    for s in uniq],
        "extracted_at": int(time.time()),
    }
    with open(os.path.join(args.output_dir, "pirlotv_capture.json"), "w",
              encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    log(f"  💾 pirlotv_capture.json")

    log("=" * 66)
    log(f"✅ FATTO — {len(uniq)} stream")
    log("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())