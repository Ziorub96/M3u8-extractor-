#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PirloTV / Rojadirecta / Liveru Extractor v1.0

Scansiona i domini attivi della famiglia "tR" (PirloTV + Rojadirecta + Liveru):
  1. pirlotv1.live          (343KB, il più ricco)
  2. pirlo-tv.su            (163KB)
  3. pirlotvv.org           (91KB)
  4. liveru.me              (39KB, motore misto)
  5. rojadirectaoficial.de  (25KB)

Strategie implementate:
  A) Link /live-match/?r=<BASE64>  → decodifica base64 → player URL
  B) Player HTML con array tR      → decodifica carattere-per-carattere
  C) Iframe diretto                → segue l'iframe (liveru e simili)
  D) master.m3u8                   → sceglie variante a 1080p

Produce:
  pirlotv_EVENTS_LIVE.m3u   (raggruppati per campionato)
  pirlotv_VLC.m3u           (gruppo unico "PirloTV")
  pirlotv_COMPLETE.m3u      (tutto)
  pirlotv_capture.json      (metadata per debug)

Note:
- IP-locked: il token m3u8 è legato all'IP che ha generato il player.
  Funziona da GitHub Actions perché il job ha IP fisso per tutta la durata.
- Scadenza: il token dura ~5-10 minuti. Va consumato subito.
- Exit 0 sempre (graceful).
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

# Domini della famiglia PirloTV/Rojadirecta/Liveru
# (ordinati per "ricchezza" del catalogo)
SITES = [
    {"name": "pirlotv1", "base": "https://pirlotv1.live", "engine": "tR"},
    {"name": "pirlo-tv", "base": "https://pirlo-tv.su", "engine": "tR"},
    {"name": "pirlotvv", "base": "https://pirlotvv.org", "engine": "tR"},
    {"name": "liveru", "base": "https://liveru.me", "engine": "auto"},
    {"name": "rojadirectaoficial", "base": "https://rojadirectaoficial.de", "engine": "tR"},
]

# Link che assomigliano a partite (candidati)
MATCH_LINK_PATTERNS = [
    r'/live-match/',
    r'/match/',
    r'/evento?/',
    r'/live/[^"\']+',
    r'/calcio/',
    r'/stream/',
    r'\?r=[A-Za-z0-9+/=]{20,}',       # PirloTV: r= base64
    r'\?id=\d+',                       # id numerico
    r'\?ch(?:annel)?=\w+',
]

# Domini di embed da seguire (iframe interni ai player)
IFRAME_PATTERNS = [
    r'/embed/', r'/global\d*\.php', r'/player/', r'/watch/',
]

# Host da scartare (ad-farming, morti noti)
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
    """GET con fallback su requests, urllib. Ritorna (status, testo) o (None, '')."""
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
    # fallback urllib
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
    """Estrae href che assomigliano a partite."""
    links = []
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html):
        href = m.group(1).strip()
        if not href or href.startswith(("javascript:", "#", "mailto:")):
            continue
        if any(re.search(p, href) for p in MATCH_LINK_PATTERNS):
            full = href if href.startswith("http") else urljoin(base, href)
            if full not in links:
                links.append(full)
    return links


def decode_r_param(match_url):
    """Se l'URL contiene ?r=<base64>, decodifica e ritorna l'URL del player."""
    try:
        qs = parse_qs(_urlparse(match_url).query)
        if "r" not in qs:
            return None
        b64 = qs["r"][0]
        # padding se mancante
        pad = "=" * (-len(b64) % 4)
        decoded = base64.b64decode(b64 + pad).decode("utf-8", "replace")
        if decoded.startswith("http"):
            return decoded
    except Exception:
        pass
    return None


def decode_tR(html):
    """
    Decodifica l'array tR:
      tR = [[idx, "base64"], ...];
      k = const1() + const2();  (o somma di tutte le cost. rilevate)
      per ogni [idx, b64] ordinato per idx:
        chars.append(chr(int(digits_only(atob(b64))) - k))
      ritorna m3u8_url
    """
    # Array tR
    m = re.search(r'tR\s*=\s*\[(.*?)\]\s*;', html, re.S)
    if not m:
        return None
    pairs = re.findall(r'\[\s*(\d+)\s*,\s*"([^"]+)"\s*\]', m.group(1))
    if not pairs:
        return None
    # Costanti: funzioni anonime senza argomenti che ritornano un numero
    # (zzbCl, AErSQ, ecc.). Somma tutte quelle trovate (di solito 2).
    consts = re.findall(
        r'function\s+\w*\s*\(\s*\)\s*\{\s*return\s+(\d+)\s*;?\s*\}', html)
    if not consts:
        # fallback: cerca "var k=<num>+<num>" o "return <num>;}" ripetuti
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
    """Estrae src da <iframe>."""
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
    """
    Scarica master.m3u8 e ritorna l'URL della variante con RESOLUTION+BANDWIDTH
    più alti. Se il master non è un master playlist (solo segmenti), ritorna
    master_url così com'è.
    """
    st, txt = http_get(master_url, referer=referer)
    if not st or st != 200 or not txt:
        return master_url
    if "#EXT-X-STREAM-INF" not in txt:
        return master_url  # è già la variante diretta
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


def process_player(player_url, referer=None, depth=0):
    """
    Scarica il player HTML e cerca di estrarre l'm3u8.
    Ritorna dict {url, mtype, title, referer} o None.
    """
    if depth > 2:
        return None
    if any(h in (urlparse(player_url).hostname or "") for h in SKIP_HOSTS):
        log(f"      ⏭️ skip host {player_url[:80]}")
        return None
    st, html = http_get(player_url, referer=referer)
    if not st or st != 200:
        log(f"      ⚠️ player HTTP {st} ({player_url[:80]})")
        return None
    # 1) Se è già un .m3u8
    if player_url.endswith(".m3u8") or "#EXTM3U" in html[:200]:
        return {"url": player_url, "mtype": "hls", "referer": referer}
    # 2) tR decoder
    m3u8 = decode_tR(html)
    if m3u8:
        log(f"      🎯 tR → {m3u8[:100]}")
        best = pick_best_variant(m3u8, referer=player_url)
        mtype = "dash" if ".mpd" in best else "hls"
        return {"url": best, "mtype": mtype, "referer": player_url}
    # 3) regex diretta su m3u8/mpd
    for pat in (r'https?://[^"\'\s]+\.m3u8[^"\'\s]*',
                r'https?://[^"\'\s]+\.mpd[^"\'\s]*'):
        for m in re.finditer(pat, html):
            u = m.group(0).replace("\\/", "/")
            if "//" in u[7:]:
                continue  # doppio slash interno
            if any(h in u for h in DRM_HINTS):
                continue
            log(f"      🎯 regex → {u[:100]}")
            best = pick_best_variant(u, referer=player_url)
            mtype = "dash" if ".mpd" in best else "hls"
            return {"url": best, "mtype": mtype, "referer": player_url}
    # 4) iframe interni
    ifs = find_iframes(html, player_url)
    for if_u in ifs:
        if any(h in (urlparse(if_u).hostname or "") for h in SKIP_HOSTS):
            continue
        log(f"      🖼️ segue iframe: {if_u[:100]}")
        sub = process_player(if_u, referer=player_url, depth=depth + 1)
        if sub:
            return sub
    return None


def extract_from_site(site, max_matches=25, timeout_total=600):
    """Estrae streams da un singolo dominio."""
    base = site["base"]
    log(f"\n🌐 [{site['name']}] {base}")
    t0 = time.time()
    st, html = http_get(base, referer=base)
    if not st or st != 200:
        log(f"  ❌ homepage HTTP {st}")
        return []
    log(f"  ✓ homepage {len(html)} byte")
    # Trova link partita
    links = find_match_links(html, base)
    log(f"  📋 {len(links)} link partita trovati")
    if not links:
        return []
    # Esplora fino a max_matches
    streams = []
    for i, link in enumerate(links[:max_matches], 1):
        if time.time() - t0 > timeout_total:
            log(f"  ⏰ budget esaurito ({timeout_total}s)")
            break
        log(f"  [{i}/{min(len(links), max_matches)}] {link[:100]}")
        # Estrai titolo dal link: se contiene ?r=base64, il titolo è nel <a>
        # Per semplicità, titolo generico
        title = f"PirloTV {i}"
        # Salta se il link è già un m3u8
        if ".m3u8" in link:
            streams.append({"url": link, "mtype": "hls", "title": title,
                            "referer": base, "site": site["name"]})
            continue
        # PirloTV family: prova decodifica r=
        player_url = decode_r_param(link)
        if player_url:
            log(f"      🔓 r= → {player_url[:100]}")
        else:
            # liveru: potrebbe essere iframe diretto o pagina da seguire
            player_url = link
        # Segui e decodifica
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
    ap.add_argument("--max-per-site", type=int, default=20,
                    help="Max partite da esplorare per dominio")
    ap.add_argument("--timeout-per-site", type=int, default=300,
                    help="Timeout in secondi per dominio")
    args = ap.parse_args()

    if args.output_dir is None:
        args.output_dir = os.path.dirname(args.output) or "."
    os.makedirs(args.output_dir, exist_ok=True)

    log("=" * 66)
    log("📡 PIRLOTV EXTRACTOR v1.0 (multi-dominio)")
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

    # Dedup per URL
    seen = set()
    uniq = []
    for s in all_streams:
        if s["url"] not in seen:
            seen.add(s["url"])
            uniq.append(s)

    log(f"\n📊 TOTALE: {len(uniq)} stream unici su {len(all_streams)} trovati")

    # Scrittura file
    n1 = write_m3u(os.path.join(args.output_dir, "pirlotv_EVENTS_LIVE.m3u"), uniq)
    n2 = write_m3u(os.path.join(args.output_dir, "pirlotv_VLC.m3u"),
                   uniq, group_override="PirloTV")
    n3 = write_m3u(args.output, uniq)
    log(f"  💾 pirlotv_EVENTS_LIVE.m3u: {n1}")
    log(f"  💾 pirlotv_VLC.m3u: {n2}")
    log(f"  💾 {args.output}: {n3}")

    # Capture JSON
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