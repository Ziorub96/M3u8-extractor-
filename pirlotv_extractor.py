#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PirloTV / Rojadirecta / Liveru Extractor v1.1

Changelog v1.0 → v1.1:
- MATCH_LINK_PATTERNS molto più larghi (canal-, partita, ver, stream=, ecc.)
- find_match_links con fallback generico (qualsiasi /slug/ che non sia asset)
- process_player:
  * salva SEMPRE l'HTML del player in pirlotv_debug/ (debug)
  * diagnostica: tR / atob / source / file / clappr / jw / bitmovin / hls.js
  * retry con UA iPhone se il player risponde 403
  * regex più tolleranti (source:, file:, hlsUrl:, JSON url)
  * decodifica automatica atob("lungo") se contiene .m3u8
  * riconosce risposte JSON con campo url/stream/hls/src/file/source
  * segue iframe anche dentro player annidati
- Le funzioni find_iframes, decode_tR, decode_r_param, pick_best_variant
  sono identiche a v1.0 (già funzionanti)
"""
from __future__ import annotations
import argparse, base64, json, os, re, sys, time
from pathlib import Path
from urllib.parse import urlparse, urljoin, parse_qs, urlparse as _urlparse

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

# Pattern per i link "partita" (v1.1: molto più larghi)
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

# Asset da NON considerare link partita (fallback generico)
ASSET_SKIP = (
    "/css/", "/js/", "/img/", "/images/", "/fonts/", "/assets/",
    ".css", ".js", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".woff",
    ".woff2", ".ttf", ".gif", ".webp",
    "/tag/", "/category/", "/feed", "/about", "/contact",
    "/privacy", "/terms", "/dmca", "/faq", "/login", "/register", "/search",
)

IFRAME_PATTERNS = [
    r'/embed/', r'/global\d*\.php', r'/player/', r'/watch/',
]

SKIP_HOSTS = {
    "taifood-blog.asia", "pandecocogaming.sbs",
    "doubleclick.net", "googlesyndication.com", "googletagmanager.com",
    "google-analytics.com", "facebook.net", "facebook.com",
    "cloudflareinsights.com", "cdn4ads.com", "adsco.re",
    "yandex.ru", "yandex.com", "mc.yandex.ru",
}

DRM_HINTS = ("edef8ba9", "9a04f079", "1077efecc0b24d02")


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
    """v1.1: pattern più larghi + fallback generico su /slug/."""
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
    # Fallback: se 0 trovati, prendi qualsiasi link /slug/ non-asset
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
        qs = parse_qs(_urlparse(match_url).query)
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


def decode_tR(html):
    m = re.search(r'tR\s*=\s*\[(.*?)\]\s*;', html, re.S)
    if not m:
        return None
    pairs = re.findall(r'\[\s*(\d+)\s*,\s*"([^"]+)"\s*\]', m.group(1))
    if not pairs:
        return None
    consts = re.findall(
        r'function\s+\w*\s*\(\s*\)\s*\{\s*return\s+(\d+)\s*;?\s*\}', html)
    if not consts:
        consts = re.findall(r'return\s+(\d+)\s*;}', html)
    if len(consts) < 2:
        return None
    k = sum(int(c) for c in consts)
    log(f"      🔓 tR: {len(pairs)} coppie, {len(consts)} costanti, k={k}")
    try:
        chars = []
        for idx_str, b64 in sorted((int(i), b) for i, b in pairs):
            decoded = base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode(
                "utf-8", "replace")
            digits = re.sub(r"\D", "", decoded)
            if not digits:
                continue
            c = int(digits) - k
            if 0 < c < 0x110000:
                chars.append(chr(c))
        url = "".join(chars)
        if url.startswith("http") and (".m3u8" in url or ".mpd" in url or "/global/" in url):
            return url
        return url if url.startswith("http") else None
    except Exception as e:
        log(f"      ⚠️ tR decode err: {type(e).__name__}")
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
    """Salva HTML del player in pirlotv_debug/ con nome stabile."""
    try:
        safe_host = re.sub(r'[^a-z0-9]', '_', host)[:30]
        safe_path = re.sub(r'[^a-z0-9]', '_',
                           player_url.split("?")[0].split("/")[-1])[:30] or "root"
        fname = f"player_{safe_host}__{safe_path}.html"
        Path("pirlotv_debug").mkdir(exist_ok=True)
        Path(f"pirlotv_debug/{fname}").write_text(html, encoding="utf-8")
        log(f"      💾 salvato pirlotv_debug/{fname} ({len(html)} byte)")
    except Exception:
        pass


def _diagnose(html):
    has_tR = "tR=" in html or "tR =" in html
    has_atob = "atob(" in html
    has_source = bool(re.search(r'source["\']?\s*[:=]', html, re.I))
    has_file = bool(re.search(r'file["\']?\s*[:=]', html, re.I))
    has_clappr = "clappr" in html.lower()
    has_jw = "jwplayer" in html.lower()
    has_bitmovin = "bitmovin" in html.lower()
    has_hls_js = "hls.js" in html.lower() or "hlsjs" in html.lower()
    log(f"      🔬 tR={has_tR} atob={has_atob} src={has_source} file={has_file} "
        f"clappr={has_clappr} jw={has_jw} bitmovin={has_bitmovin} hls={has_hls_js}")


def process_player(player_url, referer=None, depth=0):
    if depth > 2:
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

    # 2) tR decoder (motore classico)
    m3u8 = decode_tR(html)
    if m3u8:
        log(f"      🎯 tR → {m3u8[:100]}")
        best = pick_best_variant(m3u8, referer=player_url)
        mtype = "dash" if ".mpd" in best else "hls"
        return {"url": best, "mtype": mtype, "referer": player_url}

    # 3) Regex dirette (più tolleranti)
    for pat in (
        r'https?://[^"\'\\\s]+\.m3u8[^"\'\\\s]*',
        r'https?://[^"\'\\\s]+\.mpd[^"\'\\\s]*',
        r'["\']([^"\']*\.m3u8[^"\']*)["\']',
        r'source\s*[:=]\s*["\']([^"\']+)["\']',
        r'file\s*[:=]\s*["\']([^"\']+)["\']',
        r'hlsUrl\s*[:=]\s*["\']([^"\']+)["\']',
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

    # 5) Iframe annidati
    ifs = find_iframes(html, player_url)
    for if_u in ifs:
        if (urlparse(if_u).hostname or "") in SKIP_HOSTS:
            continue
        log(f"      🖼️ segue iframe: {if_u[:100]}")
        sub = process_player(if_u, referer=player_url, depth=depth + 1)
        if sub:
            return sub

    # 6) JSON con campo url
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

    return None


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
    log("📡 PIRLOTV EXTRACTOR v1.1 (multi-dominio, debug HTML)")
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