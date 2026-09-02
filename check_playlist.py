import subprocess
import sys
import urllib.request
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- Configurazione ---
playlist = Path(sys.argv[1] if len(sys.argv) > 1 else "combined_events.m3u")
workers = 10
timeout = 5

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
REFERER_DAMITV = "https://ondemand.st/"
ORIGIN_DAMITV = "https://ondemand.st"

def ottieni_proxy_italiani():
    """Scarica una lista di proxy italiani gratuiti da fonti pubbliche."""
    proxy_disponibili = []
    # Fonti pubbliche di proxy a rotazione (es. PubProxy / ProxyScrape API)
    url_api = "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=IT&ssl=all&anonymity=all"
    try:
        req = urllib.request.Request(url_api, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = response.read().decode("utf-8")
            linee = data.splitlines()
            for p in linee:
                p = p.strip()
                if p:
                    proxy_disponibili.append(f"http://{p}")
    except Exception as e:
        print(f"⚠️ Impossibile scaricare la lista proxy automatica: {e}")
    
    return proxy_disponibili

print("🔍 Recupero proxy italiani gratuiti per il bypass cloud...")
lista_proxy = ottieni_proxy_italiani()
print(f"🌐 Trovati {len(lista_proxy)} proxy italiani potenziali.")

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

# Deduplicazione per URL
visti = set()
blocchi_unici = []
for block in blocks:
    url = block[-1]
    if url not in visti:
        visti.add(url)
        blocchi_unici.append(block)
blocks = blocchi_unici
print(f"🔍 Flussi unici da testare: {len(blocks)}")

def controlla_blocco(block, proxy_corrente=None):
    url = block[-1]
    comando = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_type",
        "-of", "default=noprint_wrappers=1:nokey=1",
        "-timeout", str(timeout * 1_000_000),
        "-analyzeduration", "2000000",
        "-probesize", "2000000",
        "-user_agent", USER_AGENT,
        "-headers", f"Referer: {REFERER_DAMITV}\r\nOrigin: {ORIGIN_DAMITV}\r\n"
    ]
    
    if proxy_corrente:
        comando.extend(["-http_proxy", proxy_corrente])
        
    comando.extend(["-i", url])

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

def testa_con_fallback(block):
    # Primo tentativo diretto (senza proxy o con connessione standard)
    block_res, ok, motivo = controlla_blocco(block, proxy_corrente=None)
    if ok:
        return block_res, True, ""

    # Se fallisce (probabile blocco 403 / datacenter), prova ciclicamente i proxy italiani disponibili
    if lista_proxy and ("403" in motivo or "Forbidden" in motivo or "Timeout" in motivo):
        for px in lista_proxy[:5]: # prova al massimo i primi 5 proxy per non rallentare troppo
            block_res, ok, motivo_px = controlla_blocco(block, proxy_corrente=px)
            if ok:
                return block_res, True, ""

    return block, False, motivo

funzionanti = []
non_funzionanti = []

with ThreadPoolExecutor(max_workers=workers) as executor:
    futures = [executor.submit(testa_con_fallback, block) for block in blocks]
    for i, future in enumerate(as_completed(futures), 1):
        block, ok, motivo = future.result()
        nome = block[0].split(",")[-1].strip() if "," in block[0] else "Senza nome"
        stato = "OK" if ok else "KO"
        print(f"[{i}/{len(blocks)}] {stato} | {nome}")

        if ok:
            funzionanti.append(block)
        else:
            non_funzionanti.append((block, motivo))

# Salva playlist pulita
with open("combined_events_checked.m3u", "w", encoding="utf-8") as f:
    f.write("#EXTM3U\n")
    for block in funzionanti:
        f.write("\n".join(block) + "\n")

# Salva errori con motivo
with open("flussi_non_funzionanti.txt", "w", encoding="utf-8") as f:
    for block, motivo in non_funzionanti:
        nome = block[0].split(",")[-1].strip()
        url = block[-1]
        f.write(f"{nome} | {url} | {motivo}\n")

print(f"\n✅ Funzionanti: {len(funzionanti)}")
print(f"❌ Non funzionanti: {len(non_funzionanti)}")
print("📄 Dettagli errori in flussi_non_funzionanti.txt")
