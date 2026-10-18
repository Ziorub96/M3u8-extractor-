#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_playlist.py — v3.4

Cambiamenti chiave:
- SMART UA: legge #EXTVLCOPT:http-user-agent dal blocco. Se presente, usa
  quello. Se assente, prova iPhone prima (DAMITV), poi desktop.
- PRESERVA blocchi interi: #EXTVLCOPT e altri commenti restano nel file
  finale (necessari per il player TV).
- DROP severo: se il check fallisce con entrambi gli UA, lo stream viene
  scartato (niente più "keep" di comodo).
- NESSUN # CHECK: scritto nel file finale.
- Passa referer/origin dal blocco al check.
"""
import re, sys, time, random
from pathlib import Path
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from curl_cffi import requests as http
except ImportError:
    import requests as http

playlist = Path(sys.argv[1] if len(sys.argv) > 1 else "combined_events.m3u")
workers = 16
HTTP_TIMEOUT = 8

UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
UA_IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) "
             "Version/17.0 Mobile Safari/604.1")

DEAD_HOSTS = {
    "23.237.104.106", "cache0.wonvt.st",
    "edgestream1.pro", "edgestream2.pro", "edgestream3.pro",
    "edgestream4.pro", "edgestream5.pro", "edgestream6.pro", "edgestream7.pro",
    "193.47.62.41", "193.47.62.44", "193.47.62.50", "193.47.62.55", "193.47.62.59",
    "futbolx.ryzn.pro", "judiaslevels.embeds.gay", "chatgpt.hereisman.net",
    "karimianian.store", "instreams.xyz", "instreams.live", "instreams.pro",
    "rtmpcdn.com", "streamfree.top", "khala.futlivehd.com", "ftlly.com",
    "fubo18.com", "webcast-origin.b-cdn.net", "88.212.15.19", "89.33.29.115",
    "envivosenhd.beer", "cdn-5.nxplay.com.br", "stream.mcquack.net",
    "bozztv.com", "lbgo.bozztv.com", "live20.bozztv.com", "telekomsrbije.com",
    "31.148.48.15", "88.212.15.27", "162.19.255.233", "185.49.107.6",
    "190.83.2.182", "190.197.41.183", "195.178.110.110", "201.190.41.246",
    "45.4.1.201", "45.134.141.161", "45.167.2.101", "45.5.119.43",
    "46.32.176.50", "58.8.186.128", "74.91.26.218", "87.229.103.60",
    "91.228.35.249", "103.250.28.74", "103.151.60.162", "121.91.61.106",
    "143.14.134.107", "145.239.5.177", "158.101.222.193", "162.250.201.58",
    "168.228.44.241", "178.33.11.6", "180.188.254.253", "183.207.248.71",
    "192.162.64.99", "202.150.172.59", "xn.futbolx.ryzn.pro",
    "lb.iptvnetwork.net", "instream.click", "tvsen7.aynascope.net",
    "tvsen5.aynaott.com", "tvsen5.aynascope.net",
    "d2xeo83q8fcni6.cloudfront.net", "d12a2vxqkkh1bo.cloudfront.net",
    "live-stream.iblups.com", "soultv.com.br",
    "streamcorner.st", "streamcorner.cloud", "streamcorner.fyi",
    "streamcorner.vu", "streamcorner.foo", "streamcorner.info",
    "streamcorner.xyz", "streamcorner.to", "streamcorner.site",
}


def host_of(u):
    return (urlparse(u).hostname or "").lower()


def is_embed(u):
    return any(p in u.lower() for p in (
        "getsugatensho", "sportsembed", "embed.st/embed/", "pandecocogaming.sbs",
        "embedhd.", "embedme.st", "taifood-blog.asia", "streamsgate.co"))


def is_api_endpoint(u):
    u = u.lower()
    return "workers.dev/?p=" in u or "workers.dev?p=" in u


def extract_block_headers(block):
    """
    Legge #EXTVLCOPT:* dal blocco (tra #EXTINF e URL finale).
    Ritorna dict con keys User-Agent, Referer, Origin.
    """
    h = {}
    for line in block[1:-1]:
        s = line.strip()
        if s.startswith("#EXTVLCOPT:http-user-agent="):
            h["User-Agent"] = s.split("=", 1)[1]
        elif s.startswith("#EXTVLCOPT:http-referrer="):
            h["Referer"] = s.split("=", 1)[1]
        elif s.startswith("#EXTVLCOPT:http-origin="):
            h["Origin"] = s.split("=", 1)[1]
    return h


def check_once(url, ua, extra_headers=None):
    headers = {
        "User-Agent": ua,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if extra_headers:
        for k, v in extra_headers.items():
            if k.lower() not in ("user-agent",):  # UA gestito sopra
                headers[k] = v

    try:
        kw = {"impersonate": "chrome124"} if "curl_cffi" in str(http) else {}
        r = http.get(url, headers=headers, timeout=HTTP_TIMEOUT,
                     allow_redirects=True, verify=False, **kw)
        if r.status_code in (429, 502, 503):
            time.sleep(2)
            r = http.get(url, headers=headers, timeout=HTTP_TIMEOUT,
                         allow_redirects=True, verify=False, **kw)
        if r.status_code >= 400:
            return False, f"HTTP {r.status_code}"
        body = r.content[:8192]
        if not body.startswith(b"#EXTM3U") and not body.startswith(b"<MPD"):
            return False, "not_manifest"

        text = body.decode("utf-8", errors="ignore")
        segs = [l.strip() for l in text.splitlines()
                if l.strip() and not l.startswith("#")]
        if segs:
            seg = segs[0]
            if not seg.startswith("http"):
                seg = urljoin(url, seg)
            try:
                rs = http.get(seg, headers=headers, timeout=HTTP_TIMEOUT,
                              stream=True, verify=False, **kw)
                if rs.status_code >= 400:
                    return False, f"seg_{rs.status_code}"
                chunk = next(rs.iter_content(64), b"")
                if chunk[:1] == b"\x47" or b"ftyp" in chunk[:32]:
                    return True, "ok"
            except Exception:
                pass
        if "#EXT-X-STREAM-INF" in text or "#EXT-X-TARGETDURATION" in text:
            return True, "ok_manifest_only"
        return False, "seg_fail"
    except Exception as e:
        return False, f"err_{type(e).__name__}"


def check_block(block):
    """Smart: usa UA del blocco. Se assente, prova iPhone poi desktop."""
    url = block[-1]
    block_hdrs = extract_block_headers(block)

    if "User-Agent" in block_hdrs:
        # Il blocco ha già UA specifico → usa quello (DAMITV, DokAgents)
        ok, msg = check_once(url, block_hdrs["User-Agent"], block_hdrs)
        return ok, msg

    # Nessun UA specifico: prova iPhone prima (colpisce DAMITV)
    ok, msg = check_once(url, UA_IPHONE, block_hdrs)
    if ok:
        return True, "iphone"

    # Poi desktop
    time.sleep(0.3)
    ok, msg = check_once(url, UA_DESKTOP, block_hdrs)
    return ok, msg


def process_block(block):
    url = block[-1]
    if any(d in url for d in ("youtube.com", "youtu.be", "googlevideo.com")):
        return block, False, "youtube"
    if is_embed(url):
        return block, False, "embed"
    if is_api_endpoint(url):
        return block, False, "api"
    host = host_of(url)
    if host in DEAD_HOSTS or any(host.startswith(d) for d in DEAD_HOSTS):
        return block, False, "host_morto"
    time.sleep(random.uniform(0.1, 0.25))
    ok, msg = check_block(block)
    return block, ok, msg


def main():
    try:
        lines = playlist.read_text(encoding="utf-8", errors="ignore").splitlines()
    except FileNotFoundError:
        print(f"❌ {playlist} non trovato")
        sys.exit(1)

    # Parsing pulito: mantiene EXTVLCOPT, scarta # CHECK:
    blocks, cur = [], []
    for line in lines:
        s = line.strip()
        if s.startswith("#EXTINF"):
            if cur and cur[-1] and not cur[-1].startswith("#"):
                blocks.append(cur)
            cur = [s]
        elif s.startswith("#EXTVLCOPT") and cur:
            cur.append(s)
        elif s.startswith("#KODIPROP") and cur:
            cur.append(s)
        elif s and not s.startswith("#"):
            if cur:
                cur.append(s)
                blocks.append(cur)
                cur = []
        elif s.startswith("# CHECK:"):
            continue  # scarta commenti residui
    if cur and cur[-1] and not cur[-1].startswith("#"):
        blocks.append(cur)

    print(f"🔍 {len(blocks)} blocchi")

    seen, unique = set(), []
    for b in blocks:
        if b[-1] not in seen:
            seen.add(b[-1])
            unique.append(b)
    blocks = unique
    print(f"🔍 {len(blocks)} unici")

    funz, non_funz = [], []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(process_block, b) for b in blocks]
        for i, fut in enumerate(as_completed(futs), 1):
            block, ok, msg = fut.result()
            if ok:
                funz.append(block)  # ← blocco INTERO con EXTVLCOPT preservato
            else:
                non_funz.append((block, msg))
            if i % 25 == 0:
                print(f"[{i}/{len(blocks)}] OK={len(funz)} KO={len(non_funz)}")

    # Scrivi M3U: blocchi interi, ZERO # CHECK:
    out_lines = ["#EXTM3U"]
    for b in funz:
        out_lines.extend(b)
    Path("combined_events_checked.m3u").write_text(
        "\n".join(out_lines) + "\n", encoding="utf-8")

    with open("flussi_non_funzionanti.txt", "w", encoding="utf-8") as f:
        for b, m in non_funz:
            nome = b[0].split(",")[-1].strip() if "," in b[0] else "?"
            f.write(f"{nome} | {b[-1]} | {m}\n")

    print(f"\n✅ OK: {len(funz)}")
    print(f"❌ KO: {len(non_funz)}")
    print(f"📄 combined_events_checked.m3u (solo verificati, EXTVLCOPT preservati, no # CHECK:)")


if __name__ == "__main__":
    main()