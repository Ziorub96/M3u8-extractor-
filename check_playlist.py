import subprocess
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

playlist = Path(sys.argv[1] if len(sys.argv) > 1 else "combined_events.m3u")
workers = 15
timeout = 5

# 1. Leggi e parsifica a blocchi (come in combine_playlist.py)
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
            # URL finale
            current_block.append(stripped)
            blocks.append(current_block)
            current_block = []
        elif stripped.startswith("#") and current_block:
            # Tag aggiuntivo (es. #EXTVLCOPT)
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

# 2. Deduplicazione per URL (opzionale, ma utile)
visti = set()
blocchi_unici = []
for block in blocks:
    url = block[-1]
    if url not in visti:
        visti.add(url)
        blocchi_unici.append(block)
blocks = blocchi_unici
print(f"🔍 Flussi unici da testare: {len(blocks)}")

def controlla_blocco(block):
    url = block[-1]
    comando = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_type",
        "-of", "default=noprint_wrappers=1:nokey=1",
        "-timeout", str(timeout * 1_000_000),
        "-analyzeduration", "2000000",
        "-probesize", "2000000",
        "-i", url
    ]
    try:
        r = subprocess.run(
            comando,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout + 3
        )
        if r.returncode == 0 and r.stdout.strip():
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
        url = block[-1]
        nome = block[0]  # #EXTINF...
        stato = "OK" if ok else "KO"
        print(f"[{i}/{len(blocks)}] {stato} | {nome.split(',')[-1].strip()}")

        if ok:
            funzionanti.append(block)
        else:
            non_funzionanti.append((block, motivo))

# 3. Salva playlist pulita con la stessa struttura
with open("combined_events_checked.m3u", "w", encoding="utf-8") as f:
    f.write("#EXTM3U\n")
    for block in funzionanti:
        f.write("\n".join(block) + "\n")

# 4. Salva errori con motivazioni
with open("flussi_non_funzionanti.txt", "w", encoding="utf-8") as f:
    for block, motivo in non_funzionanti:
        url = block[-1]
        nome = block[0].split(",")[-1].strip()
        f.write(f"{nome} | {url} | {motivo}\n")

print(f"\n✅ Funzionanti: {len(funzionanti)}")
print(f"❌ Non funzionanti: {len(non_funzionanti)}")
print("📄 Dettagli errori in flussi_non_funzionanti.txt")