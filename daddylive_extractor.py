# daddylive_extractor.py
# Estrae flussi m3u8 da player2 + player5 + player6 + player14
# Pure Python + requests → funziona su Carnets e GitHub Actions
# Output: daddylive_streams.m3u (compatibile con combine_playlist.py)

import re
import json
import base64
import math
import time
import random
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ===================== CONFIG =====================

BASE_CANDIDATES = [
    "https://daddylive.app",
    "https://dlhd.so",
    "https://daddylive.sx",
    "https://daddylive.li",
]

PLAYER_FILES = [
    "player2.json",
    "player5.json",
    "player6.json",
    "player14.json",
]

ONLY_SPORT = True
MAX_WORKERS = 5
REQUEST_DELAY = (0.4, 1.0)
OUTPUT_FILE = "daddylive_streams.m3u"

USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
]

SPORT_KEYWORDS = [
    "sport", "espn", "sky sports", "premier league", "nfl", "nba", "nhl", "mlb",
    "ufc", "boxing", "football", "calcio", "soccer", "tennis", "golf", "rugby",
    "cricket", "f1", "motogp", "bundesliga", "serie a", "la liga", "champions",
    "europa league", "dazn", "bein", "movistar", "canale sport", "sport tv",
    "fox sports", "eleven", "premier", "nba tv", "nfl network", "wwe", "aew",
    "tnt sports", "rai sport", "ziggo sport", "polsat sport",
    "sky sport", "canal sport", "rmc sport", "v sport", "match football",
]

# ===================== UTILS =====================

def b64d(s: str) -> bytes:
    s = s.replace('-', '+').replace('_', '/')
    s += '=' * (-len(s) % 4)
    return base64.b64decode(s)

def is_sport(name: str) -> bool:
    if not ONLY_SPORT:
        return True
    n = name.lower()
    for kw in SPORT_KEYWORDS:
        if len(kw) <= 4:
            if re.search(rf'\b{re.escape(kw)}\b', n):
                return True
        elif kw in n:
            return True
    return False

def get_headers(referer: str | None = None) -> dict:
    h = {"User-Agent": random.choice(USER_AGENTS)}
    if referer:
        h["Referer"] = referer
    return h

def find_working_base() -> str | None:
    for base in BASE_CANDIDATES:
        try:
            r = requests.get(
                f"{base}/player/player5.json",
                headers=get_headers(),
                timeout=12,
            )
            if r.status_code == 200 and r.text.strip().startswith('['):
                print(f"✅ Dominio attivo: {base}")
                return base
        except Exception:
            continue
    print("❌ Nessun dominio base funzionante trovato")
    return None

# ===================== EXTRACTORS =====================

def extract_player2(html: str) -> str | None:
    """streamtp / global1.php → var playbackURL"""
    m = re.search(r'var\s+playbackURL\s*=\s*"([^"]+)"', html)
    if m:
        return m.group(1).replace('\\/', '/')
    return None

def extract_player5(html: str) -> str | None:
    """cdnlivetv.tv → pezzi Base64 con nomi variabili random"""
    join_match = re.search(
        r'[A-Za-z_$][\w$]*\s*=\s*((?:[A-Za-z_$][\w$]*\([A-Za-z_$][\w$]*\)\s*\+\s*)+[A-Za-z_$][\w$]*\([A-Za-z_$][\w$]*\))',
        html,
    )
    if not join_match:
        return None
    args = re.findall(r'[A-Za-z_$][\w$]*\(([A-Za-z_$][\w$]*)\)', join_match.group(1))
    vars_dict = dict(re.findall(r"var\s+([A-Za-z_$][\w$]*)\s*=\s*'([^']*)'", html))
    parts = []
    for arg in args:
        if arg not in vars_dict:
            return None
        try:
            parts.append(b64d(vars_dict[arg]).decode('utf-8', errors='replace'))
        except Exception:
            return None
    return ''.join(parts) if parts else None

def extract_player6(html: str, page_url: str, session: requests.Session) -> str | None:
    """bolaloca.my → iframe cuttingfame → _econfig"""
    iframe_m = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.I)
    if not iframe_m:
        return None

    iframe_url = iframe_m.group(1)
    if iframe_url.startswith('//'):
        iframe_url = 'https:' + iframe_url
    elif iframe_url.startswith('/'):
        iframe_url = urljoin(page_url, iframe_url)

    time.sleep(random.uniform(*REQUEST_DELAY))
    try:
        r = session.get(iframe_url, headers=get_headers(page_url), timeout=15)
        if r.status_code != 200:
            return None
    except Exception:
        return None

    econfig_m = re.search(r"window\._econfig='([^']+)'", r.text)
    if not econfig_m:
        return None

    try:
        decoded = b64d(econfig_m.group(1)).decode('utf-8', errors='replace')
        length = len(decoded)
        chunk = math.ceil(length / 4)
        parts = []
        pos = 0
        for _ in range(4):
            part = decoded[pos:pos + chunk]
            pos += chunk
            parts.append(part[:3] + part[4:])
        ordered = [parts[i] for i in [1, 3, 0, 2]]
        joined = ''.join(ordered)
        d2 = b64d(joined).decode('utf-8', errors='replace')
        d3 = b64d(d2).decode('utf-8', errors='replace')

        m = re.search(r'"stream_url(?:_nop2p)?"\s*:\s*"([^"]+)"', d3)
        if m:
            return m.group(1).replace('\\/', '/')
    except Exception:
        return None
    return None

def extract_player14(html: str) -> str | None:
    """epiembeds.online → array interi + XOR (chiavi dinamiche)"""
    arr_m = re.search(r'var\s+_qb8\s*=\s*\[([^\]]+)\]', html)
    if not arr_m:
        return None

    nums = []
    for x in arr_m.group(1).split(','):
        x = x.strip()
        if x.lstrip('-').isdigit():
            nums.append(int(x))

    sx_m = re.search(r'_sx8\s*=\s*(\d+)', html)
    vq_m = re.search(r'_vq9\s*=\s*(\d+)', html)
    sx8 = int(sx_m.group(1)) if sx_m else 61
    vq9 = int(vq_m.group(1)) if vq_m else 9

    try:
        decoded = ''.join(chr(((n ^ sx8) - vq9 + 256) & 255) for n in nums)
    except Exception:
        return None

    m = re.search(r'https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*', decoded)
    return m.group(0) if m else None

# ===================== RESOLVER =====================

def resolve_stream(name: str, url: str, base_url: str) -> tuple | None:
    """Sessione locale → thread-safe."""
    session = requests.Session()
    time.sleep(random.uniform(*REQUEST_DELAY))

    try:
        r = session.get(url, headers=get_headers(base_url), timeout=15)
        if r.status_code != 200:
            return None
        html = r.text
    except Exception:
        return None

    stream = None

    if "cdnlivetv.tv" in url:
        stream = extract_player5(html)
    elif "streamtp-golden" in url or "global1.php" in url:
        stream = extract_player2(html)
    elif "bolaloca.my" in url:
        stream = extract_player6(html, url, session)
    elif "epiembeds.online" in url:
        stream = extract_player14(html)
    else:
        stream = (
            extract_player5(html)
            or extract_player2(html)
            or extract_player14(html)
            or extract_player6(html, url, session)
        )

    if stream and stream.startswith("http"):
        return (name, stream)
    return None

# ===================== MAIN =====================

def main():
    print("🔍 Cerco dominio attivo...")
    base_url = find_working_base()
    if not base_url:
        return

    all_candidates = []

    print("\n📡 Scarico le liste player...")
    for pfile in PLAYER_FILES:
        try:
            r = requests.get(
                f"{base_url}/player/{pfile}",
                headers=get_headers(),
                timeout=20,
            )
            entries = r.json()
            count_sport = 0
            for e in entries:
                if not isinstance(e, dict):
                    continue
                name = e.get("name") or e.get("title") or "Senza nome"
                if not is_sport(name):
                    continue
                php_url = next(
                    (
                        e.get(k)
                        for k in ("url", "url1", "url2", "url3")
                        if e.get(k) and str(e.get(k)).startswith("http")
                    ),
                    None,
                )
                if php_url:
                    all_candidates.append((name, php_url))
                    count_sport += 1
            print(f"   {pfile}: {len(entries)} totali → {count_sport} sport")
        except Exception as ex:
            print(f"   ❌ {pfile}: {ex}")

    print(f"\n🎯 Canali da risolvere: {len(all_candidates)}")
    print(f"🚀 Avvio con {MAX_WORKERS} worker...\n")

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(resolve_stream, name, url, base_url): name
            for name, url in all_candidates
        }
        done = 0
        total = len(futures)
        for fut in as_completed(futures):
            done += 1
            name = futures[fut]
            try:
                res = fut.result()
                if res:
                    results.append(res)
                    print(f"[{done}/{total}] ✅ {res[0]}")
                else:
                    print(f"[{done}/{total}] ❌ {name}")
            except Exception:
                print(f"[{done}/{total}] ❌ {name}")

    # Deduplica
    seen = set()
    unique = []
    for name, url in results:
        if url not in seen:
            seen.add(url)
            unique.append((name, url))

    print(f"\n🔗 Flussi unici trovati: {len(unique)}")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for name, url in unique:
            clean = name.replace('"', "").replace(",", " -").replace("\n", "")
            f.write(f'#EXTINF:-1 group-title="Sport",{clean}\n')
            f.write(url + "\n")

    print(f"✅ Salvato → {OUTPUT_FILE}")

if __name__ == "__main__":
    main()