import subprocess
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

playlist = Path(sys.argv[1] if len(sys.argv) > 1 else "combined_events.m3u")
workers = 12
timeout = 8

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
DAMITV_REFERER = "https://ondemand.st/"
DAMITV_ORIGIN = "https://ondemand.st"

DAMITV_DOMAINS = [
    "ondemand.st",
    "messi.damitv.st",
    "embedindia.st",
    "dokagents.site",
    "damitv.st",
]

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

def get_headers_for_url(url):
    if any(domain in url for domain in DAMITV_DOMAINS):
        return f"User-Agent: {USER_AGENT}\r\nReferer: {DAMITV_REFERER}\r\nOrigin: {DAMITV_ORIGIN}\r\n"
    else:
        return f"User-Agent: {USER_AGENT}\r\n"

def controlla_blocco(block):
    url = block[-1]

    # Domini da saltare (Daddylive, WatchFooty) → considerati validi senza test
    skip_domains = [
        "daddylive", "streamtp-", "domhsd.com",
        "sportsembed.su", "watchfooty.st"
    ]
    if any(domain in url for domain in skip_domains):
        return (block, True, "")

    headers_string = get_headers_for_url(url)

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
            return (block, True, "")
        else:
            err = r.stderr.strip().splitlines()
            motivo = err[-1][:200] if err else f"Errore sconosciuto (codice {r.returncode})"

            if any(domain in url for domain in DAMITV_DOMAINS) and "403" in motivo:
                return (block, True, "")

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