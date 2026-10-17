#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
streamed_extractor.py — Streamed.pk via resolver WASM esterno
==============================================================
Consuma un resolver Streamed.pk (Node.js + lock.wasm) via HTTP.
NON reimplementa il cipher: chiama /api/matches, /api/stream, /api/hls.

Uso:
    # 1) avvia il resolver (una volta sola, in un altro terminale):
    #    cd streamed-pk-hls-stream-resolver && PORT=3000 npm start
    # 2) esegui:
    python streamed_extractor.py --resolver http://127.0.0.1:3000

Opzioni utili:
    --sports football,basketball,tennis   # default: football
    --verify                               # verifica un flusso campione
    --fallback-embed                       # se il resolver è giù, usa embed URL (Android WebView)
    --output streamed.m3u
"""

from __future__ import annotations
import argparse, os, sys, time
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    print("[!] manca requests: pip install requests", file=sys.stderr)
    sys.exit(2)

DEFAULT_RESOLVER = os.environ.get("RESOLVER_URL", "http://127.0.0.1:3000")
DEFAULT_SPORTS = os.environ.get("SPORTS", "football")
DEFAULT_OUTPUT = "streamed.m3u"


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- resolver
def wait_resolver(resolver: str, timeout: int) -> bool:
    """Aspetta che /api/health risponda. Ritorna True se pronto."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = requests.get(f"{resolver}/api/health", timeout=3)
            if r.status_code < 500:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def list_sports(resolver: str) -> list[str] | None:
    """Opzionale: il resolver espone /api/sports?"""
    try:
        r = requests.get(f"{resolver}/api/sports", timeout=5)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                return [str(x.get("id") if isinstance(x, dict) else x) for x in data]
    except Exception:
        pass
    return None


def fetch_matches(resolver: str, sport: str) -> list[dict]:
    r = requests.get(f"{resolver}/api/matches/{sport}", timeout=20)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else data.get("matches", [])


def fetch_streams(resolver: str, source: str, match_id: str) -> list[dict]:
    r = requests.get(f"{resolver}/api/stream/{source}/{match_id}", timeout=15)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def slot_url(resolver: str, source: str, match_id: str,
             stream_no: int, title: str) -> str:
    """Entry slot-based: l'unlock avviene al tune-in, mai token stantii."""
    q = urlencode({"source": source, "id": match_id,
                   "stream": stream_no, "title": title})
    return f"{resolver}/api/hls?{q}"


# ---------------------------------------------------------------- verify
def verify_one(resolver: str, url: str) -> bool:
    """GET sul primo URL della playlist; verifica che sia HLS valido."""
    try:
        r = requests.get(url, timeout=15, stream=True)
        if r.status_code != 200:
            return False
        chunk = next(r.iter_content(chunk_size=2048), b"")
        return b"#EXTM3U" in chunk
    except Exception:
        return False


# ---------------------------------------------------------------- self-test
def selftest(resolver: str) -> bool:
    """Verifica che il resolver risponda alle 3 API fondamentali."""
    log("[*] Self-test resolver...")
    if not wait_resolver(resolver, timeout=10):
        log(f"[!] /api/health non risponde su {resolver}")
        return False
    log("    ✅ /api/health")
    try:
        sports = list_sports(resolver)
        if sports:
            log(f"    ✅ /api/sports ({len(sports)} sport)")
    except Exception:
        pass
    try:
        matches = fetch_matches(resolver, "football")
        log(f"    ✅ /api/matches/football ({len(matches)} match)")
        if not matches:
            return True
        m = matches[0]
        srcs = m.get("sources", [])
        if not srcs:
            log("    ⚠️ primo match senza sources, non posso testare /api/stream")
            return True
        s = srcs[0]
        streams = fetch_streams(resolver, s["source"], s["id"])
        log(f"    ✅ /api/stream/{s['source']}/{s['id']} ({len(streams)} stream)")
        return True
    except Exception as e:
        log(f"[!] self-test fallito: {type(e).__name__}: {e}")
        return False


# ---------------------------------------------------------------- build M3U
def build_m3u(resolver: str, matches: list[dict], use_embed: bool) -> tuple[list[str], int, int]:
    """
    Ritorna (righe_m3u, totale_entry, match_saltati).
    Se use_embed=True e resolver assente: usa embedUrl come fallback (WebView Android).
    """
    lines = ["#EXTM3U"]
    total = 0
    skipped = 0

    for m in matches:
        mid = m.get("id")
        title = m.get("title") or "Match"
        sources = m.get("sources") or []
        if not sources:
            skipped += 1
            continue

        for src in sources:
            source = src.get("source")
            sid = src.get("id")
            if not (source and sid):
                skipped += 1
                continue

            try:
                streams = fetch_streams(resolver, source, sid)
            except Exception as ex:
                log(f"    [!] {source}/{sid}: {type(ex).__name__}")
                skipped += 1
                continue

            if not streams:
                skipped += 1
                continue

            for st in streams:
                n = st.get("streamNo")
                if not n:
                    continue
                lang = st.get("language") or "?"
                hd = "HD" if st.get("hd") else "SD"
                label = f"{title} [{source}/{lang}/{hd}]"

                if use_embed and st.get("embedUrl"):
                    # Fallback: URL embed diretto (funziona solo su WebView Android)
                    url = st["embedUrl"]
                else:
                    url = slot_url(resolver, source, sid, n, f"{title} {lang}")

                lines.append(f"#EXTINF:-1,{label}")
                lines.append(url)
                total += 1

    return lines, total, skipped


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="Streamed.pk extractor (via resolver WASM)")
    ap.add_argument("--resolver", default=DEFAULT_RESOLVER,
                    help=f"URL resolver (default {DEFAULT_RESOLVER})")
    ap.add_argument("--sports", default=DEFAULT_SPORTS,
                    help="lista sport separata da virgola (default football)")
    ap.add_argument("--output", "-o", default=DEFAULT_OUTPUT)
    ap.add_argument("--wait", type=int, default=60,
                    help="attesa max per il resolver (default 60s)")
    ap.add_argument("--verify", action="store_true",
                    help="verifica un flusso campione prima di scrivere la M3U")
    ap.add_argument("--fallback-embed", action="store_true",
                    help="se resolver giù, usa embedUrl (solo WebView Android)")
    ap.add_argument("--selftest", action="store_true",
                    help="solo self-test resolver, non scrive la M3U")
    args = ap.parse_args()

    resolver = args.resolver.rstrip("/")

    log("=" * 66)
    log(f"📡 STREAMED.PK EXTRACTOR  (resolver: {resolver})")
    log("=" * 66)

    # --selftest: solo verifica
    if args.selftest:
        ok = selftest(resolver)
        return 0 if ok else 1

    # health-check
    if not wait_resolver(resolver, args.wait):
        log(f"[!] resolver non raggiungibile su {resolver} dopo {args.wait}s")
        if args.fallback_embed:
            log("    → fallback --fallback-embed attivo: uso embed URL (WebView Android)")
            # produci M3U con embedUrl statici (senza resolver)
            # ma non possiamo listare i match senza resolver: usiamo API pubblica diretta
            try:
                r = requests.get("https://streamed.pk/api/matches/football",
                                 timeout=20, verify=False)
                r.raise_for_status()
                matches = r.json()
                lines = ["#EXTM3U"]
                total = 0
                for m in matches:
                    title = m.get("title") or "Match"
                    for src in m.get("sources") or []:
                        source, sid = src.get("source"), src.get("id")
                        if not (source and sid):
                            continue
                        # embed URL noto: https://embed.st/embed/{source}/{id}/{n}
                        lines.append(f"#EXTINF:-1,{title} [{source}/?]")
                        lines.append(f"https://embed.st/embed/{source}/{sid}/1")
                        total += 1
                with open(args.output, "w") as f:
                    f.write("\n".join(lines) + "\n")
                log(f"[+] {args.output} scritto in modalità embed ({total} entry, WebView solo)")
                return 0
            except Exception as ex:
                log(f"[!] fallback anche fallito: {ex}")
        # graceful: scrivi M3U vuota
        with open(args.output, "w") as f:
            f.write("#EXTM3U\n")
        log(f"[!] {args.output} scritto vuoto (graceful)")
        return 0

    # self-test API
    if not selftest(resolver):
        log("[!] self-test fallito, ma provo comunque")

    # raccolta match per sport
    all_matches: list[dict] = []
    for sport in [s.strip() for s in args.sports.split(",") if s.strip()]:
        try:
            matches = fetch_matches(resolver, sport)
            log(f"    ✅ {sport}: {len(matches)} match")
            all_matches.extend(matches)
        except Exception as e:
            log(f"    ❌ {sport}: {type(e).__name__}: {e}")

    # dedup per id
    seen_ids = set()
    deduped = []
    for m in all_matches:
        mid = m.get("id")
        if mid and mid not in seen_ids:
            seen_ids.add(mid)
            deduped.append(m)
    log(f"[*] {len(deduped)} match unici")

    # build
    lines, total, skipped = build_m3u(resolver, deduped,
                                       use_embed=args.fallback_embed)

    # verify opzionale
    if args.verify and total > 0:
        sample = lines[-1]  # ultimo URL (probabilmente il primo o l'ultimo)
        log(f"[*] verify su un campione: {sample[:80]}...")
        if verify_one(resolver, sample):
            log("    ✅ flusso valido")
        else:
            log("    ⚠️ verify fallito (ma il flusso potrebbe richiedere header player)")

    # write
    with open(args.output, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log(f"[+] {args.output}: {total} entry ({skipped} match/sorgenti saltate)")
    return 0


if __name__ == "__main__":
    sys.exit(main())