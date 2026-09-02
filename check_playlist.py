import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from playwright.sync_api import sync_playwright

# --- Configurazione ---
playlist = Path(sys.argv[1] if len(sys.argv) > 1 else "combined_events.m3u")
workers = 5  # Ridotto leggermente per ottimizzare le risorse del browser headless
timeout = 10

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
REFERER_DAMITV = "https://ondemand.st/"
ORIGIN_DAMITV = "https://ondemand.st"

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

def controlla_con_playwright(browser, block):
    url = block[-1]
    context = browser.new_context(
        user_agent=USER_AGENT,
        extra_http_headers={
            "Referer": REFERER_DAMITV,
            "Origin": ORIGIN_DAMITV
        }
    )
    successo = False
    motivo = ""

    try:
        # Sfruttiamo il contesto di Playwright per gestire cookie e richieste HTTP protette
        response = context.request.get(url, timeout=timeout * 1000)
        if response.status == 200:
            body = response.text()
            if "#EXTM3U" in body or "#EXT-X-STREAM-INF" in body:
                successo = True
            else:
                motivo = "Contenuto non valido (manifest HLS assente)"
        else:
            motivo = f"Errore HTTP {response.status}"
    except Exception as e:
        motivo = str(e)[:200]
    finally:
        context.close()

    return (block, successo, motivo)

funzionanti = []
non_funzionanti = []

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(controlla_con_playwright, browser, block) for block in blocks]
        for i, future in enumerate(as_completed(futures), 1):
            block, ok, motivo = future.result()
            nome = block[0].split(",")[-1].strip() if "," in block[0] else "Senza nome"
            stato = "OK" if ok else "KO"
            print(f"[{i}/{len(blocks)}] {stato} | {nome}")

            if ok:
                funzionanti.append(block)
            else:
                non_funzionanti.append((block, motivo))
    browser.close()

# Salva playlist pulita
with open("combined_events_checked.m3u", "w", encoding="utf-8") as f:
    f.write("#EXTM3U\n")
    for block in funzionanti:
        f.write("\n".join(block) + "\n")

# Salva errori
with open("flussi_non_funzionanti.txt", "w", encoding="utf-8") as f:
    for block, motivo in non_funzionanti:
        nome = block[0].split(",")[-1].strip()
        url = block[-1]
        f.write(f"{nome} | {url} | {motivo}\n")

print(f"\n✅ Funzionanti: {len(funzionanti)}")
print(f"❌ Non funzionanti: {len(non_funzionanti)}")
print("📄 Dettagli errori in flussi_non_funzionanti.txt")
