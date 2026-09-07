import re
import subprocess
import sys
import time
import random
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from curl_cffi import requests as curl_requests

playlist = Path(sys.argv[1] if len(sys.argv) > 1 else "combined_events.m3u")
workers = 10
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

def http_check_m3u8(url: str) -> tuple[bool, str, str]:
    """
    Soft check: GET con fingerprint Chrome.
    OK se status < 400 e il body sembra una playlist HLS valida.
    Ritorna (ok, motivo, cookie_str).
    """
    headers = get_headers_dict(url)

    # Delay extra per domini sensibili al rate limiting
    sensitive_domains = ("cdnlivetv.tv", "bolaloca.my", "epiembeds.online", "cuttingfame.net")
    if any(domain in url for domain in sensitive_domains):
        time.sleep(random.uniform(0.4, 1.0))

    attempt = 0
    max_retries = 2
    while attempt < max_retries:
        try:
            session = curl_requests.Session(impersonate="chrome120")
            r = session.get(url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
            cookies = session.cookies.get_dict()
            cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())

            if r.status_code in (429, 502, 503):
                wait = 4 * (2 ** attempt) + random.uniform(0, 1)
                time.sleep(wait)
                attempt += 1
                continue

            if r.status_code >= 400:
                return False, f"HTTP {r.status_code}", cookie_str

            text = r.content[:4096].decode("utf-8", errors="ignore")
            text_l = text.lower()

            # 1) Blacklist parole chiave → scarto immediato
            blacklist = ("offline", "error", "denied", "invalid", "not found", "expired", "maintenance")
            if any(word in text_l for word in blacklist):
                return False, "Contenuto di errore nel manifest", cookie_str

            # 2) Lunghezza minima
            if len(text.strip()) < 200:
                return False, "Manifest troppo corto", cookie_str

            # 3) Controllo struttura HLS
            has_extm3u = "#extm3u" in text_l
            has_segment_ref = bool(re.search(r"#extinf:\s*[\d.]+.*?\.(ts|m4s|aac|mp3)", text_l, re.DOTALL))
            has_targetduration = "#ext-x-targetduration" in text_l
            has_streaminf = "#ext-x-stream-inf" in text_l

            if has_extm3u and (has_segment_ref or has_targetduration or has_streaminf):
                return True, "soft-ok", cookie_str

            if has_extm3u:
                return False, "Manifest HLS incompleto", cookie_str

            return False, "Body non sembra m3u8", cookie_str

        except Exception as e:
            if attempt == max_retries - 1:
                return False, f"HTTP error: {str(e)[:120]}", ""
            time.sleep(2)
            attempt += 1

    return False, "Max retries superati", ""

def ffprobe_check(url: str, cookie_str: str = "") -> tuple[bool, str]:
    headers_string = headers_for_ffprobe(url, cookie_str)
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_type",
        "-of", "default=noprint_wrappers=1:nokey=1",
        "-timeout", str(timeout * 1_000_000),
        "-analyzeduration", "1000000",
        "-probesize", "1000000",
        "-headers", headers_string,
        "-i", url
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
            return False, "Nessuna traccia audio/video"
        motivo = err.splitlines()[-1][:200] if err else f"ffprobe code {r.returncode}"
        return False, motivo
    except subprocess.TimeoutExpired:
        return False, "Timeout ffprobe"
    except Exception as e:
        return False, str(e)[:200]

def controlla_blocco(block):
    url = block[-1]

    if any(d in url for d in ("youtube.com", "youtu.be", "googlevideo.com")):
        return block, False, "YouTube escluso"

    time.sleep(random.uniform(0.1, 0.3))

    soft_ok, soft_msg, cookie_str = http_check_m3u8(url)

    if soft_ok:
        if soft_msg == "soft-ok":
            return block, True, "soft-ok"
        hard_ok, hard_msg = ffprobe_check(url, cookie_str)
        if hard_ok:
            return block, True, ""
        return block, True, f"soft-only ({hard_msg})"

    hard_ok, hard_msg = ffprobe_check(url, cookie_str)
    if hard_ok:
        return block, True, ""

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