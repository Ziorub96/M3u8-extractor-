#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
combine_playlist.py — v2.0
Unisce sorgenti con priorità, deduplica per URL e per titolo.
Filtra host morti, embed HTML e endpoint API cifrati.
"""
import re
from pathlib import Path
from urllib.parse import urlparse

EPG_URL = "https://raw.githubusercontent.com/Ziorub96/M3u8-extractor-/main/combined_epg.xml"

TVG_ID_MAP = {
    "Digi Sport 1": "DigiSport1.it", "Digi Sport 2 HD": "DigiSport2.it",
    "Digi Sport 3": "DigiSport3.it", "Digi Sport 4": "DigiSport4.it",
    "DAZN 1 Italia": "DAZN1.it", "Sky Sport 24": "SkySport24.it",
    "Sky Sport Calcio": "SkySportCalcio.it", "Sky Sport UNO": "SkySportUno.it",
    "Sky Sports Main Event": "SkySportsMainEvent.uk",
    "Sky Sports Premier League": "SkySportsPremierLeague.uk",
    "TNT Sports 1": "TNTSports1.uk", "TNT Sports 2": "TNTSports2.uk",
    "ESPN": "ESPN.us", "ESPN2": "ESPN2.us",
    "FOX Sports 1": "FoxSports1.us", "beIN Sports 1": "beINSports1.fr",
}

# Host morti (scartati sempre)
DEAD_HOSTS = {
    "23.237.104.106", "cache0.wonvt.st", "edgestream1.pro", "edgestream2.pro",
    "edgestream3.pro", "edgestream4.pro", "edgestream5.pro", "edgestream6.pro",
    "edgestream7.pro", "193.47.62.41", "193.47.62.44", "193.47.62.50",
    "193.47.62.55", "193.47.62.59", "futbolx.ryzn.pro", "judiaslevels.embeds.gay",
    "chatgpt.hereisman.net", "karimianian.store", "instreams.xyz",
    "instreams.live", "instreams.pro", "rtmpcdn.com", "streamfree.top",
    "khala.futlivehd.com", "ftlly.com", "fubo18.com", "webcast-origin.b-cdn.net",
}

EMBED_PATTERNS = (
    "getsugatensho", "sportsembed", "embed.st/embed/", "pandecocogaming.sbs",
    "embedhd.", "embedme.st", "taifood-blog.asia", "streamsgate.co",
)

# (nome, file, priorità) — numero più alto = più prioritario
LOCAL_SOURCES = [
    ("DAMITV", "damitv_events.m3u", 10),
    ("Streamed", "streamed.m3u", 10),
    ("Daddylive", "daddylive_streams.m3u", 5),
    ("CDN Live TV Channels", "cdnlivetv_channels.m3u", 3),
    ("CDN Live TV Events", "cdnlivetv_events.m3u", 3),
    ("StreamCorner", "streamcorner_EVENTS_LIVE.m3u", 2),
]

REMOTE_SOURCES = [
    ("doms9", "https://s.id/d9M3U8", 1),
    ("iptv-org sports", "https://iptv-org.github.io/iptv/categories/sports.m3u", 1),
]

OUTPUT_FILE = "combined_events.m3u"

def host_of(u):
    return (urlparse(u).hostname or "").lower()

def is_embed(u):
    u = u.lower()
    return any(p in u for p in EMBED_PATTERNS)

def is_api_endpoint(u):
    """Scarta endpoint API cifrati (workers.dev/?p=...)."""
    u = u.lower()
    return "workers.dev/?p=" in u or "workers.dev?p=" in u

def is_localhost(u):
    return any(b in u.lower() for b in ("://127.0.0.1", "://localhost", "://0.0.0.0"))

def set_group_title(line, grp):
    if 'group-title="' in line:
        return re.sub(r'group-title="[^"]*"', f'group-title="{grp}"', line)
    if ',' in line:
        h, t = line.rsplit(',', 1)
        return f'{h} group-title="{grp}",{t}'
    return f'{line} group-title="{grp}"'

def set_tvg_id(line, name):
    if 'tvg-id="' in line:
        return line
    tid = TVG_ID_MAP.get(name)
    if not tid:
        return line
    if 'group-title="' in line:
        return line.replace('group-title="', f'tvg-id="{tid}" group-title="')
    if ',' in line:
        h, t = line.rsplit(',', 1)
        return f'{h} tvg-id="{tid}",{t}'
    return line

def parse_m3u(lines):
    blocks, cur = [], []
    for l in lines:
        s = l.strip()
        if s.startswith("#EXTINF"):
            if cur: blocks.append(cur)
            cur = [s]
        elif s and not s.startswith("#"):
            cur.append(s); blocks.append(cur); cur = []
        elif s.startswith("#") and cur:
            cur.append(s)
    if cur: blocks.append(cur)
    return blocks

def process(blocks, name, prio, out, seen_urls, seen_titles):
    added = 0
    for b in blocks:
        url = b[-1]
        if any(d in url for d in ("youtube.com", "youtu.be", "googlevideo.com")):
            continue
        if is_embed(url) or is_api_endpoint(url) or is_localhost(url):
            continue
        if host_of(url) in DEAD_HOSTS:
            continue
        if url in seen_urls:
            continue
        title = b[0].split(",")[-1].strip() if "," in b[0] else ""
        if title and title in seen_titles:
            continue
        seen_urls.add(url)
        if title: seen_titles.add(title)
        if b[0].startswith("#EXTINF"):
            b[0] = set_group_title(b[0], name)
            b[0] = set_tvg_id(b[0], title)
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
            print(f"⚠️ {name}: file assente")
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

    # Ordina per priorità decrescente
    out_blocks.sort(key=lambda x: -x[0])
    lines = [f'#EXTM3U url-tvg="{EPG_URL}"']
    for _, b in out_blocks:
        lines.extend(b)
    Path(OUTPUT_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n✅ {OUTPUT_FILE}: {len(out_blocks)} flussi unici")

if __name__ == "__main__":
    main()