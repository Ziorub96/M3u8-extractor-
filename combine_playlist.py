#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
combine_playlist.py — v2.7

Cambiamenti rispetto a v2.4:
- parse_m3u() IGNORA esplicitamente # CHECK: e altri commenti residui
- Preserva solo #EXTVLCOPT e #KODIPROP tra i commenti (utili per player)
- Nessun'altra modifica
"""
import re
from pathlib import Path
from urllib.parse import urlparse

EPG_URL = "https://raw.githubusercontent.com/Ziorub96/M3u8-extractor-/main/combined_epg.xml"

TVG_ID_MAP = {
    "Digi Sport 1": "DigiSport1.it", "DAZN 1 Italia": "DAZN1.it",
    "Sky Sport 24": "SkySport24.it", "ESPN": "ESPN.us", "ESPN2": "ESPN2.us",
}

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

EMBED_PATTERNS = ("getsugatensho", "sportsembed", "embed.st/embed/",
                  "pandecocogaming.sbs", "embedhd.", "embedme.st",
                  "taifood-blog.asia", "streamsgate.co")

LOCAL_SOURCES = [
    ("DAMITV", "damitv_events.m3u", 10),
    ("StreamCorner", "streamcorner_EVENTS_LIVE.m3u", 8),
    ("Daddylive", "daddylive_streams.m3u", 5),
    ("CDN Live TV Channels", "cdnlivetv_channels.m3u", 3),
    ("CDN Live TV Events", "cdnlivetv_events.m3u", 3),
]

REMOTE_SOURCES = [
    ("doms9", "https://s.id/d9M3U8", 1),
    ("iptv-org sports", "https://iptv-org.github.io/iptv/categories/sports.m3u", 1),
]

OUTPUT_FILE = "combined_events.m3u"


def host_of(u):
    return (urlparse(u).hostname or "").lower()


def is_embed(u):
    return any(p in u.lower() for p in EMBED_PATTERNS)


def is_api_endpoint(u):
    u = u.lower()
    return "workers.dev/?p=" in u or "workers.dev?p=" in u


def set_group_title(line, grp):
    if 'group-title="' in line:
        return re.sub(r'group-title="[^"]*"', f'group-title="{grp}"', line)
    if ',' in line:
        h, t = line.rsplit(',', 1)
        return f'{h} group-title="{grp}",{t}'
    return f'{line} group-title="{grp}"'


def parse_m3u(lines):
    """
    Parsing PULITO:
    - Riconosce #EXTINF, #EXTVLCOPT, #KODIPROP, URL
    - IGNORA # CHECK: e qualsiasi altro # sconosciuto
    """
    blocks, cur = [], []
    for l in lines:
        s = l.strip()
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
        # Qualsiasi altro # (incluso # CHECK:) viene ignorato
    if cur and cur[-1] and not cur[-1].startswith("#"):
        blocks.append(cur)
    return blocks


def process(blocks, name, prio, out, seen_urls, seen_titles):
    added = 0
    for b in blocks:
        url = b[-1]
        if any(d in url for d in ("youtube.com", "youtu.be", "googlevideo.com")):
            continue
        if is_embed(url) or is_api_endpoint(url):
            continue
        if host_of(url) in DEAD_HOSTS:
            continue
        if url in seen_urls:
            continue
        title = b[0].split(",")[-1].strip() if "," in b[0] else ""
        if title and title in seen_titles:
            continue
        seen_urls.add(url)
        if title:
            seen_titles.add(title)
        if b[0].startswith("#EXTINF"):
            b[0] = set_group_title(b[0], name)
        out.append((prio, b))
        added += 1
    return added


def main():
    import requests
    out_blocks = []
    seen_urls, seen_titles = set(), set()

    for name, path, prio in LOCAL_SOURCES:
        p = Path(path)
        if not p.exists():
            print(f"⚠️ {name}: file assente ({path})")
            continue
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        blocks = parse_m3u(lines)
        n = process(blocks, name, prio, out_blocks, seen_urls, seen_titles)
        print(f"📂 {name}: {n}/{len(blocks)} aggiunti (prio {prio})")

    for name, url, prio in REMOTE_SOURCES:
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            blocks = parse_m3u(r.text.splitlines())
            n = process(blocks, name, prio, out_blocks, seen_urls, seen_titles)
            print(f"📡 {name}: {n}/{len(blocks)} aggiunti (prio {prio})")
        except Exception as e:
            print(f"❌ {name}: {e}")

    out_blocks.sort(key=lambda x: -x[0])
    lines = [f'#EXTM3U url-tvg="{EPG_URL}"']
    for _, b in out_blocks:
        lines.extend(b)
    Path(OUTPUT_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n✅ {OUTPUT_FILE}: {len(out_blocks)} flussi unici")


if __name__ == "__main__":
    main()