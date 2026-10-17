#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_playlist.py — v2.0
Checker IPTV consapevole di token scaduti e falsi negativi.
- Non scarta DAMITV/Streamed senza test con header corretti.
- Distingue "host morto" (da scartare) da "token scaduto" (da mantenere).
- Usa curl_cffi per fingerprint Chrome.
"""
import re, sys, time, random, subprocess
from pathlib import Path
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from curl_cffi import requests as http
    IMP = {"impersonate": "chrome124"}
except ImportError:
    import requests as http
    IMP = {}

playlist = Path(sys.argv[1] if len(sys.argv) > 1 else "combined_events.m3u")
workers = 16
timeout = 5
HTTP_TIMEOUT = 8

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Host definitivamente morti: scartati SENZA test
DEAD_HOSTS = {
    "23.237.104.106", "cache0.wonvt.st", "edgestream1.pro", "edgestream2.pro",
    "edgestream3.pro", "edgestream4.pro", "edgestream5.pro", "edgestream6.pro",
    "edgestream7.pro", "193.47.62.41", "193.47.62.44", "193.47.62.50",
    "193.47.62.55", "193.47.62.59", "futbolx.ryzn.pro", "judiaslevels.embeds.gay",
    "chatgpt.hereisman.net", "karimianian.store", "instreams.xyz",
    "instreams.live", "instreams.pro", "rtmpcdn.com", "streamfree.top",
    "khala.futlivehd.com", "ftlly.com", "fubo18.com", "webcast-origin.b-cdn.net",
    "88.212.15.19", "89.33.29.115", "envivosenhd.beer", "cdn-5.nxplay.com.br",
    "stream.mcquack.net", "bozztv.com", "lbgo.bozztv.com", "live20.bozztv.com",
    "telekomsrbije.com", "31.148.48.15", "88.212.15.27", "162.19.255.233",
    "185.49.107.6", "190.83.2.182", "190.197.41.183", "195.178.110.110",
    "201.190.41.246", "45.4.1.201", "45.134.141.161", "45.167.2.101",
    "45.5.119.43", "46.32.176.50", "58.8.186.128", "74.91.26.218",
    "87.229.103.60", "91.228.35.249", "103.250.28.74", "103.151.60.162",
    "121.91.61.106", "143.14.134.107", "145.239.5.177", "158.101.222.193",
    "162.250.201.58", "168.228.44.241", "178.33.11.6", "180.188.254.253",
    "183.207.248.71", "192.162.64.99", "202.150.172.59", "xn.futbolx.ryzn.pro",
    "lb.iptvnetwork.net", "instream.click", "tvsen7.aynascope.net",
    "tvsen5.aynaott.com", "tvsen5.aynascope.net", "d2xeo83q8fcni6.cloudfront.net",
    "d12a2vxqkkh1bo.cloudfront.net", "live-stream.iblups.com", "soultv.com.br",
}

# Header specifici per host che li richiedono (NON morti, solo esigenti)
HOST_HEADERS = {
    "messi.damitv.st": {"Referer": "https://ondemand.st/", "Origin": "https://ondemand.st"},
    "damitv.st": {"Referer": "https://ondemand.st/", "Origin": "https://ondemand.st"},
    "ondemand.st": {"Referer": "https://ondemand.st/", "Origin": "https://ondemand.st"},
    "embedindia.st": {"Referer": "https://ondemand.st/", "Origin": "https://ondemand.st"},
    "cdnlivetv.tv": {"Referer": "https://cdnlivetv.tv/", "Origin": "https://cdnlivetv.tv"},
    "daddylive.app": {"Referer": "https://daddylive.app/", "Origin": "https://daddylive.app"},
    "dlhd.so": {"Referer": "https://dlhd.so/", "Origin": "https://dlhd.so"},
    "dokagents.site": {"Referer": "https://dokagents.site/", "Origin": "https://dokagents.site"},
    "xameleon.phantemlis.top": {"Referer": "https://xameleon.phantemlis.top/", "Origin": "https://xameleon.phantemlis.top"},
    "bolaloca.my": {"Referer": "https://bolaloca.my/", "Origin": "https://bolaloca.my"},
    "cuttingfame.net": {"Referer": "https://cuttingfame.net/", "Origin": "https://cuttingfame.net"},
    "p13.usnlive.com": {"Referer": "https://p13.usnlive.com/", "Origin": "https://p13.usnlive.com"},
}

def host_of(url):
    return (urlparse(url).hostname or "").lower()

def is_embed(u):
    u = u.lower()
    return any(p in u for p in ("getsugatensho", "sportsembed", "embed.st/embed/",
                                "pandecocogaming.sbs", "embedhd.", "embedme.st",
                                "taifood-blog.asia", "streamsgate.co", "instream."))

def is_token_url(url):
    u = url.lower()
    return any(k in u for k in ("token=", "st=", "tk=", "?e=", "&e="))

def get_headers(url):
    h = {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
    host = host_of(url)
    for dom, extra in HOST_HEADERS.items():
        if dom in host or dom in url:
            h.update(extra)
            return h
    if host.endswith(".damitv.st") or "damitv" in host:
        h.update({"Referer": "https://ondemand.st/", "Origin": "https://ondemand.st"})
    return h

def http_check(url):
    """Ritorna (ok, motivo, cookie_str)."""
    headers = get_headers(url)
    if any(h in url for h in ("cdnlivetv.tv", "bolaloca.my", "cuttingfame.net")):
        time.sleep(random.uniform(0.3, 0.8))
    try:
        s = http.Session(impersonate="chrome124")
        r = s.get(url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
        cookies = "; ".join(f"{k}={v}" for k, v in s.cookies.get_dict().items())
        if r.status_code in (429, 502, 503):
            time.sleep(2)
            r = s.get(url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
        if r.status_code >= 400:
            if is_token_url(url) and r.status_code in (401, 403, 404):
                return False, "token_scaduto", cookies
            return False, f"HTTP {r.status_code}", cookies
        text = r.content[:8192].decode("utf-8", errors="ignore").lower()
        if any(w in text for w in ("offline", "error", "denied", "invalid", "expired", "not found")):
            return False, "contenuto_errore", cookies
        if len(text.strip()) < 50:
            return False, "manifest_troppo_corto", cookies
        if "#extm3u" not in text:
            return False, "body_non_m3u8", cookies
        # Verifica primo segmento
        seg_ok, seg_msg = check_first_segment(text, url)
        if seg_ok:
            return True, f"ok ({seg_msg})", cookies
        if "#ext-x-stream-inf" in text or "#ext-x-targetduration" in text:
            return True, "ok (manifest-only)", cookies
        return False, f"segment_fail: {seg_msg}", cookies
    except Exception as e:
        return False, f"err_{type(e).__name__}", ""

def check_first_segment(manifest, base_url):
    try:
        lines = [l.strip() for l in manifest.splitlines() if l.strip() and not l.startswith("#")]
        if not lines:
            return False, "no_segments"
        seg = lines[0]
        if not seg.startswith("http"):
            seg = urljoin(base_url, seg)
        r = http.get(seg, headers=get_headers(seg), timeout=HTTP_TIMEOUT, stream=True)
        if r.status_code >= 400:
            return False, f"seg_http_{r.status_code}"
        chunk = next(r.iter_content(512), b"")
        if chunk[:1] == b"\x47":
            return True, "ts"
        if b"ftyp" in chunk[:64]:
            return True, "mp4"
        if chunk[:4] == b"RIFF" and b"WEBP" in chunk[:16]:
            return True, "webp"
        return False, f"unknown_{chunk[:4].hex()}"
    except Exception as e:
        return False, f"seg_err_{type(e).__name__}"

def ffprobe_check(url, cookies=""):
    h = get_headers(url)
    if cookies:
        h["Cookie"] = cookies
    hs = "\r\n".join(f"{k}: {v}" for k, v in h.items()) + "\r\n"
    cmd = ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
           "-of", "default=noprint_wrappers=1:nokey=1", "-timeout", str(timeout * 1_000_000),
           "-analyzeduration", "1000000", "-probesize", "1000000",
           "-headers", hs, "-i", url]
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, timeout=timeout + 2, check=False)
        out = (r.stdout or "").strip().lower()
        if r.returncode == 0 and out and ("audio" in out or "video" in out):
            return True, "ffprobe_ok"
        return False, (r.stderr or "").strip().splitlines()[-1][:120] if r.stderr else "ffprobe_fail"
    except Exception as e:
        return False, str(e)[:120]

def process_block(block):
    url = block[-1]
    if any(d in url for d in ("youtube.com", "youtu.be", "googlevideo.com")):
        return block, False, "youtube_escluso"
    if is_embed(url):
        return block, False, "embed_html"
    host = host_of(url)
    if host in DEAD_HOSTS or any(host.startswith(d) for d in DEAD_HOSTS):
        return block, False, "host_morto"
    time.sleep(random.uniform(0.1, 0.3))
    ok, msg, cookies = http_check(url)
    if ok:
        if msg.startswith("ok (ts") or msg.startswith("ok (mp4") or msg.startswith("ok (webp"):
            return block, True, msg
        hard_ok, hard_msg = ffprobe_check(url, cookies)
        if hard_ok:
            return block, True, msg
        return block, True, f"soft_only ({hard_msg})"
    if msg == "token_scaduto":
        return block, None, "token_scaduto (mantenuto)"  # None = mantenuto con avviso
    hard_ok, hard_msg = ffprobe_check(url, cookies)
    if hard_ok:
        return block, True, "ffprobe_ok"
    return block, False, msg[:200]

def main():
    try:
        lines = playlist.read_text(encoding="utf-8", errors="ignore").splitlines()
    except FileNotFoundError:
        print(f"❌ {playlist} non trovato")
        sys.exit(1)
    # Parse blocchi
    blocks, cur = [], []
    for line in lines:
        s = line.strip()
        if s.startswith("#EXTINF"):
            if cur: blocks.append(cur)
            cur = [s]
        elif s and not s.startswith("#"):
            cur.append(s); blocks.append(cur); cur = []
        elif s.startswith("#") and cur:
            cur.append(s)
    if cur: blocks.append(cur)
    print(f"🔍 {len(blocks)} flussi")
    seen, unique = set(), []
    for b in blocks:
        if b[-1] not in seen:
            seen.add(b[-1]); unique.append(b)
    blocks = unique
    print(f"🔍 {len(blocks)} unici")

    funz, non_funz, token_keep, dead, embed = [], [], 0, 0, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(process_block, b) for b in blocks]
        for i, fut in enumerate(as_completed(futs), 1):
            block, ok, msg = fut.result()
            nome = block[0].split(",")[-1].strip() if "," in block[0] else "?"
            if ok is True:
                funz.append(block)
            elif ok is None:
                token_keep += 1
                funz.append(block)  # mantenuto
                funz.append([f"# CHECK: {msg}"])
            else:
                non_funz.append((block, msg))
                if msg == "host_morto": dead += 1
                if msg == "embed_html": embed += 1
            if i % 25 == 0:
                print(f"[{i}/{len(blocks)}] OK={len(funz)} KO={len(non_funz)}")

    Path("combined_events_checked.m3u").write_text(
        "#EXTM3U\n" + "\n".join("\n".join(b) for b in funz) + "\n", encoding="utf-8")
    with open("flussi_non_funzionanti.txt", "w", encoding="utf-8") as f:
        for b, m in non_funz:
            nome = b[0].split(",")[-1].strip()
            f.write(f"{nome} | {b[-1]} | {m}\n")
    print(f"\n✅ OK: {len(funz)} (token mantenuti: {token_keep})")
    print(f"❌ KO: {len(non_funz)} (host morti: {dead}, embed: {embed})")
    print("📄 combined_events_checked.m3u | flussi_non_funzionanti.txt")

if __name__ == "__main__":
    main()