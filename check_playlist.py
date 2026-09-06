import subprocess
import sys
import time
import random
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from curl_cffi import requests as curl_requests

playlist = Path(sys.argv[1] if len(sys.argv) > 1 else "combined_events.m3u")
workers = 8                # Bilanciato tra velocità e anti-bot
timeout = 8

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
DAMITV_REFERER = "https://ondemand.st/"
DAMITV_ORIGIN = "https://ondemand.st"

DAMITV_DOMAINS = [
    "ondemand.st",
    "messi.damitv.st",
    "embedindia.st",
    "damitv.st",
]

PROBLEMATIC_DOMAINS = {
    "dokagents.site": {
        "Referer": "https://dokagents.site/",
        "Origin": "https://dokagents.site",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    },
    "xameleon.phantemlis.top": {
        "Referer": "https://xameleon.phantemlis.top/",
        "Origin": "https://xameleon.phantemlis.top",
        "Accept": "*/*",
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    },
    "p13.usnlive.com": {
        "Referer": "https://p13.usnlive.com/",
        "Origin": "https://p13.usnlive.com",
        "Accept": "*/*",
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    },
}

def parse_m3u(lines):
    blocks = []
    current_block = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#EXTINF"):
            if current_block:
                blocks.append(current_block)
            current_block = [stripped]
        elif stripped and not stripped.startswith("#"):
            current_block.append(stripped)
            blocks.append(current_block)
            current_block = []
        elif stripped.startswith("#") and current_block:
            current_block.append(stripped)
    if current_block:
        blocks.append(current_block)
    return blocks

try:
    righe = playlist.read_text(encoding="utf-8", errors="ignore").splitlines()
except FileNotFoundError:
    print(f"❌ Errore: File della playlist '{playlist}' non trovato.")
    sys.exit(1)

blocks = parse_m3u(righe)
print(f"🔍 Flussi totali da testare: {len(blocks)}")

visti = set()
blocchi_unici = []
for block in blocks:
    url = block[-1]
    if url not in visti:
        visti.add(url)
        blocchi_unici.append(block)
blocks = blocchi_unici
print(f"🔍 Flussi unici da testare: {len(blocks)}")

def get_headers_dict_for_url(url):
    """Ritorna un dizionario di header per curl_cffi e per ffprobe."""
    headers = {"User-Agent": USER_AGENT}

    for domain, extra_headers in PROBLEMATIC_DOMAINS.items():
        if domain in url:
            headers.update(extra_headers)
            return headers

    if any(domain in url for domain in DAMITV_DOMAINS):
        headers["Referer"] = DAMITV_REFERER
        headers["Origin"] = DAMITV_ORIGIN

    return headers

def get_headers_string_for_ffprobe(url, extra_cookies=None):
    """Converte il dizionario header in stringa per ffprobe (-headers)."""
    headers = get_headers_dict_for_url(url)
    if extra_cookies:
        headers["Cookie"] = extra_cookies
    lines = [f"{k}: {v}" for k, v in headers.items()]
    return "\r\n".join(lines) + "\r\n"

def warm_up_with_curl(url):
    """
    Effettua una richiesta preliminare con curl_cffi (fingerprint Chrome)
    solo per domini critici per superare anti-bot e ottenere cookie.
    """
    headers = get_headers_dict_for_url(url)
    try:
        session = curl_requests.Session(impersonate="chrome120")
        r = session.get(url, headers=headers, timeout=8, allow_redirects=True)
        if r.status_code < 400:
            content = r.content[:1024]
            if content and b"<html" in content.lower() and b"404" in content.lower():
                return False, ""
            cookies = session.cookies.get_dict()
            cookie_str = "; ".join([f"{k}={v}" for k, v in cookies.items()])
            return True, cookie_str
        return False, ""
    except Exception:
        return False, ""

def controlla_blocco(block):
    url = block[-1]

    if any(domain in url for domain in ["youtube.com", "youtu.be", "googlevideo.com"]):
        return (block, False, "YouTube escluso")

    # Controlla se il flusso richiede effettivamente il warm-up
    needs_warmup = any(domain in url for domain in PROBLEMATIC_DOMAINS) or any(domain in url for domain in DAMITV_DOMAINS)

    cookie_str = ""
    if needs_warmup:
        # Delay applicato solo ai domini che richiedono protezione
        time.sleep(random.uniform(0.5, 1.5))
        ok_warmup, cookie_str = warm_up_with_curl(url)
        if not ok_warmup:
            return (block, False, "Warm-up fallito")

    # Header per ffprobe, inclusi eventuali cookie
    headers_string = get_headers_string_for_ffprobe(url, cookie_str)

    comando = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_type",
        "-of", "default=noprint_wrappers=1:nokey=1",
        "-timeout", str(timeout * 1_000_000),
        "-analyzeduration", "2000000",
        "-probesize", "2000000",
        "-headers", headers_string,
        "-i", url
    ]

    try:
        r = subprocess.run(
            comando,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout + 3,
            check=False
        )
        if r.returncode == 0 and r.stdout.strip():
            if "audio" not in r.stdout:
                return (block, False, "Nessuna traccia audio")
            # Ritorna il blocco originale con URL pulito
            return (block, True, "")
        else:
            err = r.stderr.strip().splitlines()
            motivo = err[-1][:200] if err else f"Errore sconosciuto (codice {r.returncode})"
            return (block, False, motivo)
    except subprocess.TimeoutExpired:
        return (block, False, "Timeout scaduto")
    except Exception as e:
        return (block, False, str(e)[:200])

funzionanti = []
non_funzionanti = []

with ThreadPoolExecutor(max_workers=workers) as executor:
    futures = [executor.submit(controlla_blocco, block) for block in blocks]
    for i, future in enumerate(as_completed(futures), 1):
        block, ok, motivo = future.result()
        nome = block[0].split(",")[-1].strip() if "," in block[0] else "Senza nome"
        stato = "OK" if ok else "KO"
        if i % 10 == 0 or not ok:
            print(f"[{i}/{len(blocks)}] {stato} | {nome}")
        if ok:
            funzionanti.append(block)
        else:
            non_funzionanti.append((block, motivo))

with open("combined_events_checked.m3u", "w", encoding="utf-8") as f:
    f.write("#EXTM3U\n")
    for block in funzionanti:
        f.write("\n".join(block) + "\n")

with open("flussi_non_funzionanti.txt", "w", encoding="utf-8") as f:
    for block, motivo in non_funzionanti:
        nome = block[0].split(",")[-1].strip()
        url = block[-1]
        f.write(f"{nome} | {url} | {motivo}\n")

print(f"\n✅ Funzionanti: {len(funzionanti)}")
print(f"❌ Non funzionanti: {len(non_funzionanti)}")
print("📄 Dettagli errori in flussi_non_funzionanti.txt")