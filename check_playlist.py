import subprocess
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

playlist = Path(sys.argv[1] if len(sys.argv) > 1 else "combined_events.m3u")
workers = 15
timeout = 5

# 1. Leggi e parsa la playlist con gestione del file mancante
try:
    righe = playlist.read_text(encoding="utf-8", errors="ignore").splitlines()
except FileNotFoundError:
    print(f"❌ Errore: File della playlist '{playlist}' non trovato.")
    sys.exit(1)

voci = []

for i, riga in enumerate(righe):
    riga = riga.strip()
    if riga and not riga.startswith("#"):
        nome = "Senza nome"
        for precedente in reversed(righe[:i]):
            if precedente.startswith("#EXTINF"):
                nome = precedente.split(",", 1)[-1].strip()
                break
        voci.append((nome, riga))

# 2. Deduplicazione per URL
url_visti = set()
voci_uniche = []
for nome, url in voci:
    if url not in url_visti:
        url_visti.add(url)
        voci_uniche.append((nome, url))
voci = voci_uniche

print(f"🔍 Flussi unici da testare: {len(voci)}")

def controlla(voce):
    nome, url = voce
    comando = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_type",
        "-of", "default=noprint_wrappers=1:nokey=1",
        "-timeout", str(timeout * 1_000_000),   # Corretto per HTTP/HTTPS
        "-analyzeduration", "2000000",          # Limita analisi a 2 secondi
        "-probesize", "2000000",                # Limita buffer di probe
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
            return (voce, True, "")
        else:
            err = r.stderr.strip().splitlines()
            motivo = err[-1][:200] if err else f"Errore sconosciuto (codice {r.returncode})"
            return (voce, False, motivo)
    except subprocess.TimeoutExpired:
        return (voce, False, "Timeout scaduto")
    except Exception as e:
        return (voce, False, str(e)[:200])

funzionanti = []
non_funzionanti = []

with ThreadPoolExecutor(max_workers=workers) as executor:
    futures = [executor.submit(controlla, voce) for voce in voci]
    for i, future in enumerate(as_completed(futures), 1):
        (nome, url), ok, motivo = future.result()
        stato = "OK" if ok else "KO"
        print(f"[{i}/{len(voci)}] {stato} | {nome}")
        if ok:
            funzionanti.append((nome, url))
        else:
            non_funzionanti.append((nome, url, motivo))

# 3. Salva playlist funzionante
with open("combined_events_checked.m3u", "w", encoding="utf-8") as f:
    f.write("#EXTM3U\n")
    for nome, url in funzionanti:
        f.write(f"#EXTINF:-1,{nome}\n{url}\n")

# 4. Salva file con i motivi degli errori
with open("flussi_non_funzionanti.txt", "w", encoding="utf-8") as f:
    for nome, url, motivo in non_funzionanti:
        f.write(f"{nome} | {url} | {motivo}\n")

print(f"\n✅ Funzionanti: {len(funzionanti)}")
print(f"❌ Non funzionanti: {len(non_funzionanti)}")
print("📄 Dettagli errori in flussi_non_funzionanti.txt")