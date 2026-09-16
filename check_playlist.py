import re
import subprocess
import sys
import time
import random
from pathlib import Path
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed

from curl_cffi import requests as curl_requests

playlist = Path(sys.argv[1] if len(sys.argv) > 1 else "combined_events.m3u")
workers = 16
timeout = 5
HTTP_TIMEOUT = 8

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

DOMAIN_HEADERS = {
    "ondemand.st": {"Referer": "https://ondemand.st/", "Origin": "https://ondemand.st"},
    "messi.damitv.st": {"Referer": "https://ondemand.st/", "Origin": "https://ondemand.st"},
    "damitv.st": {"Referer": "https://ondemand.st/", "Origin": "https://ondemand.st"},
    "embedindia.st": {"Referer": "https://ondemand.st/", "Origin": "https://ondemand.st"},
    "dokagents.site": {
        "Referer": "https://dokagents.site/",
        "Origin": "https://dokagents.site",
        "Accept": "*/*",
    },
    "xameleon.phantemlis.top": {
        "Referer": "https://xameleon.phantemlis.top/",
        "Origin": "https://xameleon.phantemlis.top",
        "Accept": "*/*",
    },
    "p13.usnlive.com": {
        "Referer": "https://p13.usnlive.com/",
        "Origin": "https://p13.usnlive.com",
        "Accept": "*/*",
    },
    "cdnlivetv.tv": {
        "Referer": "https://cdnlivetv.tv/",
        "Origin": "https://cdnlivetv.tv",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    },
    "daddylive.app": {
        "Referer": "https://daddylive.app/",
        "Origin": "https://daddylive.app",
        "Accept": "*/*",
    },
    "dlhd.so": {
        "Referer": "https://dlhd.so/",
        "Origin": "https://dlhd.so",
    },
    "cuttingfame.net": {
        "Referer": "https://cuttingfame.net/",
        "Origin": "https://cuttingfame.net",
        "Accept": "*/*",
    },
    "bolaloca.my": {
        "Referer": "https://bolaloca.my/",
        "Origin": "https://bolaloca.my",
        "Accept": "*/*",
    },
}


def is_daddylive_cdn(url: str) -> bool:
    u = url.lower()
    return (
        ":8443" in u
        or "/hls/" in u
        or "stream_url" in u
        or bool(re.search(r"https?://[a-z0-9.-]+\.[a-z0-9.-]+/(hls|live)/", u))
    )


def parse_m3u(lines):
    blocks = []
    current = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#EXTINF"):
            if current:
                blocks.append(current)
            current = [stripped]
        elif stripped and not stripped.startswith("#"):
            current.append(stripped)
            blocks.append(current)
            current = []
        elif stripped.startswith("#") and current:
            current.append(stripped)
    if current:
        blocks.append(current)
    return blocks


def get_headers_dict(url: str) -> dict:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    for domain, extra in DOMAIN_HEADERS.items():
        if domain in url:
            headers.update(extra)
            return headers

    if is_daddylive_cdn(url):
        headers["Referer"] = "https://daddylive.app/"
        headers["Origin"] = "https://daddylive.app"
    return headers


def headers_for_ffprobe(url: str, cookie_str: str = "") -> str:
    h = get_headers_dict(url)
    if cookie_str:
        h["Cookie"] = cookie_str
    return "\r\n".join(f"{k}: {v}" for k, v in h.items()) + "\r\n"


def check_first_segment(manifest_text: str, manifest_url: str) -> tuple:
    """
    Scarica i primi 512 byte del primo segmento del manifest.
    Ritorna (ok, motivo).
    Gestisce URL relativi (es. cdnlivetv.tv usa /stream-segment/...).
    Riconosce: MPEG-TS (0x47), MP4/fMP4 (ftyp), WebP+EXIF (RIFF/WEBP).
    """
    try:
        seg_lines = [
            ln.strip()
            for ln in manifest_text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        if not seg_lines:
            return False, "no segments"

        seg_url = seg_lines[0]
        if not seg_url.startswith("http"):
            seg_url = urljoin(manifest_url, seg_url)

        seg_headers = get_headers_dict(seg_url)
        session = curl_requests.Session(impersonate="chrome120")
        r = session.get(
            seg_url,
            headers=seg_headers,
            timeout=HTTP_TIMEOUT,
            allow_redirects=True,
            stream=True,
        )

        if r.status_code >= 400:
            r.close()
            return False, f"segment HTTP {r.status_code}"

        chunk = next(r.iter_content(512), b"")
        r.close()

        if not chunk:
            return False, "segment empty"

        # MPEG-TS: sync byte 0x47
        if chunk[0:1] == b"\x47":
            return True, "ts-ok"

        # MP4 / fMP4: contiene 'ftyp'
        if b"ftyp" in chunk[:64]:
            return True, "mp4-ok"

        # WebP con EXIF (DAMITV)
        if chunk[:4] == b"RIFF" and b"WEBP" in chunk[:16]:
            return True, "webp-exif"

        return False, f"unknown fmt {chunk[:8].hex()}"
    except Exception as e:
        return False, f"segment error: {str(e)[:80]}"


def http_check_m3u8(url: str) -> tuple:
    """
    Soft-check HTTP con fingerprint Chrome.
    Ritorna (ok, motivo, cookie_str).
    """
    headers = get_headers_dict(url)

    sensitive_domains = (
        "cdnlivetv.tv",
        "bolaloca.my",
        "epiembeds.online",
        "cuttingfame.net",
    )
    if any(domain in url for domain in sensitive_domains):
        time.sleep(random.uniform(0.4, 1.0))

    attempt = 0
    max_retries = 2
    while attempt < max_retries:
        try:
            session = curl_requests.Session(impersonate="chrome120")
            r = session.get(
                url,
                headers=headers,
                timeout=HTTP_TIMEOUT,
                allow_redirects=True,
            )
            cookies = session.cookies.get_dict()
            cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())

            if r.status_code in (429, 502, 503):
                wait = 4 * (2 ** attempt) + random.uniform(0, 1)
                time.sleep(wait)
                attempt += 1
                continue

            if r.status_code >= 400:
                return False, f"HTTP {r.status_code}", cookie_str

            text = r.content[:8192].decode("utf-8", errors="ignore")
            text_l = text.lower()

            # Blacklist
            blacklist = (
                "offline",
                "error",
                "denied",
                "invalid",
                "not found",
                "expired",
                "maintenance",
            )
            if any(word in text_l for word in blacklist):
                return False, "contenuto di errore", cookie_str

            if len(text.strip()) < 50:
                return False, "manifest troppo corto", cookie_str

            if "#extm3u" not in text_l:
                return False, "body non m3u8", cookie_str

            # Verifica primo segmento (gestisce URL relativi)
            seg_ok, seg_msg = check_first_segment(text, url)
            if seg_ok:
                return True, f"soft-ok ({seg_msg})", cookie_str

            # Se ha tag tipici HLS ma il segmento non è verificabile, considera valido
            if "#ext-x-stream-inf" in text_l or "#ext-x-targetduration" in text_l:
                return True, "soft-ok (manifest-only)", cookie_str

            return False, f"segment check failed: {seg_msg}", cookie_str

        except Exception as e:
            if attempt == max_retries - 1:
                return False, f"http error: {str(e)[:120]}", ""
            time.sleep(2)
            attempt += 1

    return False, "max retries", ""


def ffprobe_check(url: str, cookie_str: str = "") -> tuple:
    headers_string = headers_for_ffprobe(url, cookie_str)
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "stream=codec_type",
        "-of", "default=noprint_wrappers=1:nokey=1",
        "-timeout", str(timeout * 1_000_000),
        "-analyzeduration", "1000000",
        "-probesize", "1000000",
        "-headers", headers_string,
        "-i", url,
    ]
    try:
        r = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout + 2,
            check=False,
        )
        out = (r.stdout or "").strip().lower()
        err = (r.stderr or "").strip()

        if r.returncode == 0 and out:
            if "audio" in out or "video" in out:
                return True, "ffprobe-ok"
            return False, "nessuna traccia audio/video"
        motivo = err.splitlines()[-1][:200] if err else f"ffprobe code {r.returncode}"
        return False, motivo
    except subprocess.TimeoutExpired:
        return False, "timeout ffprobe"
    except Exception as e:
        return False, str(e)[:200]


def controlla_blocco(block):
    url = block[-1]

    if any(d in url for d in ("youtube.com", "youtu.be", "googlevideo.com")):
        return block, False, "youtube escluso"

    time.sleep(random.uniform(0.1, 0.3))

    soft_ok, soft_msg, cookie_str = http_check_m3u8(url)

    if soft_ok:
        # Se il soft-check ha già verificato il segmento, salta ffprobe
        if any(tag in soft_msg for tag in ("ts-ok", "mp4-ok", "webp-exif")):
            return block, True, soft_msg
        # Altrimenti conferma con ffprobe
        hard_ok, hard_msg = ffprobe_check(url, cookie_str)
        if hard_ok:
            return block, True, soft_msg
        return block, True, f"soft-only ({hard_msg})"

    # Soft-check fallito → prova ffprobe come ultima chance
    hard_ok, hard_msg = ffprobe_check(url, cookie_str)
    if hard_ok:
        return block, True, "ffprobe-ok"

    motivo = soft_msg if soft_msg else hard_msg
    if soft_msg and hard_msg and soft_msg != hard_msg:
        motivo = f"{soft_msg} | {hard_msg}"
    return block, False, motivo[:250]


def main():
    try:
        lines = playlist.read_text(encoding="utf-8", errors="ignore").splitlines()
    except FileNotFoundError:
        print(f"❌ Playlist non trovata: {playlist}")
        sys.exit(1)

    blocks = parse_m3u(lines)
    print(f"🔍 Flussi totali: {len(blocks)}")

    seen = set()
    unique = []
    for b in blocks:
        u = b[-1]
        if u not in seen:
            seen.add(u)
            unique.append(b)
    blocks = unique
    print(f"🔍 Flussi unici: {len(blocks)}")

    funzionanti = []
    non_funzionanti = []
    soft_only = 0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(controlla_blocco, b) for b in blocks]
        for i, fut in enumerate(as_completed(futs), 1):
            block, ok, motivo = fut.result()
            nome = block[0].split(",")[-1].strip() if "," in block[0] else "?"
            if ok:
                funzionanti.append(block)
                if motivo.startswith("soft-"):
                    soft_only += 1
                if i % 20 == 0:
                    print(f"[{i}/{len(blocks)}] OK | {nome}")
            else:
                non_funzionanti.append((block, motivo))
                print(f"[{i}/{len(blocks)}] KO | {nome} | {motivo}")

    out_ok = Path("combined_events_checked.m3u")
    with out_ok.open("w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for block in funzionanti:
            f.write("\n".join(block) + "\n")

    out_ko = Path("flussi_non_funzionanti.txt")
    with out_ko.open("w", encoding="utf-8") as f:
        for block, motivo in non_funzionanti:
            nome = block[0].split(",")[-1].strip()
            f.write(f"{nome} | {block[-1]} | {motivo}\n")

    print(f"\n✅ Funzionanti: {len(funzionanti)} (di cui soft-only: {soft_only})")
    print(f"❌ Non funzionanti: {len(non_funzionanti)}")
    print(f"📄 {out_ok} | {out_ko}")


if __name__ == "__main__":
    main()