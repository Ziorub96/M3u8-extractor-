#!/usr/bin/env python3
"""
xyzstreams_proxy.py – Estrae i flussi m3u8 dai canali live di xyzstreams
tramite il server Node pubblico (Render/Railway).
"""

import os
import requests
import sys
from pathlib import Path

# ====================== CONFIG ======================
PROXY_BASE_URL = os.environ.get("XYZPROXY_URL", "https://xyzproxy.onrender.com")
OUTPUT_FILE = "xyzstreams_events.m3u"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Canali da estrarre
CHANNELS = [
    "fox",
    "fox4k",
    "bbc",
    "tsn",
    "tsn4k",
    "bein",
    "bein4k",
    "beinfr",
    "telemundo",
    "telemundo4k",
    "fussball4k",
    "hindi"
]
# ====================================================

def get_stream_from_proxy(channel: str) -> str | None:
    """Chiama /api/proxy/stream/{channel} e restituisce l'URL m3u8 completo."""
    url = f"{PROXY_BASE_URL}/api/proxy/stream/{channel}"
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        if r.status_code == 200:
            content_type = r.headers.get("Content-Type", "")
            if "mpegurl" in content_type or r.text.strip().startswith("#EXTM3U"):
                return url
            else:
                print(f"⚠️  {channel}: risposta 200 ma non m3u8")
                return None
        else:
            print(f"❌ {channel}: HTTP {r.status_code}")
            return None
    except Exception as e:
        print(f"❌ {channel}: errore di connessione al proxy: {e}")
        return None

def main():
    lines = ["#EXTM3U"]
    success = 0

    print(f"📡 Estraggo canali live da xyzstreams (proxy: {PROXY_BASE_URL})...")
    for channel in CHANNELS:
        print(f"⏳ {channel}")
        stream_url = get_stream_from_proxy(channel)
        if stream_url:
            display_name = f"{channel.upper()} (XYZStreams)"
            lines.append(f'#EXTINF:-1 group-title="XYZStreams",{display_name}')
            lines.append(stream_url)
            success += 1
            print(f"   ✅ OK")
        else:
            print(f"   ❌ Non disponibile")

    Path(OUTPUT_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n✅ Salvato {OUTPUT_FILE} con {success} canali")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ Errore fatale: {e}", file=sys.stderr)
        Path(OUTPUT_FILE).write_text("#EXTM3U\n", encoding="utf-8")
        sys.exit(0)