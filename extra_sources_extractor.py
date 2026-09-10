#!/usr/bin/env python3
# extra_sources_extractor.py – Scarica la playlist sportiva da srhady/bingstream
# Deduplica per URL e per nome canale, salva in extra_sources.m3u

import re
import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

SOURCES = [
    ("bingstream", "https://raw.githubusercontent.com/srhady/bingstream/main/playlist.m3u"),
]

OUTPUT_FILE = "extra_sources.m3u"
TIMEOUT = 30

def fetch_playlist(name, url):
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        if r.status_code != 200:
            print(f"  ❌ {name}: HTTP {r.status_code}")
            return name, []
        lines = r.text.splitlines()
        print(f"  ✓ {name}: {len(lines)} righe")
        return name, lines
    except Exception as e:
        print(f"  ❌ {name}: {e}")
        return name, []

def parse_m3u(lines):
    blocks = []
    current = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#EXTINF"):
            if current:
                blocks.append(current)
            current = [stripped]
        elif stripped.startswith("#"):
            if current:
                current.append(stripped)
        else:
            if current:
                current.append(stripped)
                blocks.append(current)
                current = []
    return blocks

def extract_channel_name(extinf_line):
    if "," in extinf_line:
        return extinf_line.rsplit(",", 1)[-1].strip().lower()
    return ""

def normalize_name(name):
    n = name.strip().lower()
    n = re.sub(r'\s+', ' ', n)
    n = re.sub(r'\b(hd|fhd|uhd|4k|8k|sd|h265|hevc|backup|alt|alternative)\b', '', n)
    n = re.sub(r'[\(\)\[\]]', '', n)
    n = re.sub(r'\s+', ' ', n).strip()
    return n

def set_group_title(extinf, group):
    if 'group-title="' in extinf:
        return re.sub(r'group-title="[^"]*"', f'group-title="{group}"', extinf)
    if ',' in extinf:
        head, tail = extinf.rsplit(',', 1)
        return f'{head} group-title="{group}",{tail}'
    return f'{extinf} group-title="{group}"'

def main():
    all_blocks = []
    seen_urls = set()
    seen_names = set()

    print(f"📡 Scarico {len(SOURCES)} fonte...\n")

    for name, url in SOURCES:
        _, lines = fetch_playlist(name, url)
        if not lines:
            continue

        blocks = parse_m3u(lines)
        added = 0
        dup_url = 0
        dup_name = 0

        for block in blocks:
            stream_url = block[-1]
            if not stream_url.startswith("http"):
                continue

            if stream_url in seen_urls:
                dup_url += 1
                continue

            ch_name_raw = extract_channel_name(block[0])
            ch_name_norm = normalize_name(ch_name_raw)
            if not ch_name_norm:
                continue
            if ch_name_norm in seen_names:
                dup_name += 1
                continue

            seen_urls.add(stream_url)
            seen_names.add(ch_name_norm)

            block[0] = set_group_title(block[0], f"Extra - {name}")
            all_blocks.append(block)
            added += 1

        print(f"  → {name}: {added} canali unici | {dup_url} duplicati URL | {dup_name} duplicati nome")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for block in all_blocks:
            f.write("\n".join(block) + "\n")

    print(f"\n✅ Salvato {OUTPUT_FILE} con {len(all_blocks)} canali unici totali")

if __name__ == "__main__":
    main()