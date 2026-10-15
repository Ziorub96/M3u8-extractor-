#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StreamCorner Extractor — EVENTS LIVE ONLY
==========================================
Estrae SOLO gli eventi attualmente in onda dal provider "admin" di
StreamCorner e risolve gli m3u8 via Playwright.

Compatibile con il vecchio CLI (accetta --playwright-limit, --workers,
--providers, ecc.) e con i file che il workflow/combine si aspettano
(streamcorner_VLC.m3u, streamcorner_COMPLETE.m3u).

Output prodotti:
    streamcorner_VLC.m3u          → m3u8 eventi live (usato dal combine)
    streamcorner_EVENTS_LIVE.m3u  → eventi live, raggruppati per partita
    streamcorner_COMPLETE.m3u     → mixed (embed + m3u8), per debug
    streamcorner_events.json      → dati grezzi per debug

Uso:
    python streamcorner_extractor.py                     # default
    python streamcorner_extractor.py --playwright-limit 400
    python streamcorner_extractor.py --no-playwright
"""

import os, re, json, time, struct, hashlib, argparse, base64
import requests, urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# CONFIG
# ============================================================
WORKER_HOST = "data.senbon003.workers.dev"
WORKER_URL  = f"https://{WORKER_HOST}/corner"
WORKER_ASSETS = [
    "https://streamcorner.st/assets/provider-data.worker-DzCsTezu.js",
    "https://streamcorner.st/assets/provider_worker.js",
]
VAULT_CACHE = ".streamcorner_vault.json"
M3U8_CACHE  = ".streamcorner_events_m3u8_cache.json"

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile Safari/604.1")

PROVIDER   = "admin"
KEY_FIELD  = "stream_id"

# Finestra "live adesso" (margine pre/post partita in secondi)
LIVE_MARGIN_BEFORE = 1800   # 30 min prima
LIVE_MARGIN_AFTER  = 3600   # 60 min dopo

# ============================================================
# SALSA20/12
# ============================================================
M32 = 0xffffffff
def rotl32(v,c): return ((v<<c)&M32)|(v>>(32-c))
def x8(t,e):
    n=7&e; return 255&((t<<n)|(t>>(8-n)))
def Y8(t,e):
    n=7&e; return 255&((t>>n)|(t<<(8-n)))
def _qr(s,a,b,c,d):
    s[b]^=rotl32((s[a]+s[d])&M32,7); s[c]^=rotl32((s[b]+s[a])&M32,9)
    s[d]^=rotl32((s[c]+s[b])&M32,13); s[a]^=rotl32((s[d]+s[c])&M32,18)
def _blk(k32,n8,ctr):
    k=list(struct.unpack('<8I',k32)); n0,n1=struct.unpack('<2I',n8)
    st=[0x61707865,k[0],k[1],k[2],k[3],0x3320646e,n0,n1,
        ctr&M32,(ctr>>32)&M32,0x79622d32,k[4],k[5],k[6],k[7],0x6b206574]
    x=st[:]
    for _ in range(6):
        _qr(x,0,4,8,12);_qr(x,5,9,13,1);_qr(x,10,14,2,6);_qr(x,15,3,7,11)
        _qr(x,0,1,2,3);_qr(x,5,6,7,4);_qr(x,10,11,8,9);_qr(x,15,12,13,14)
    return struct.pack('<16I',*[(x[i]+st[i])&M32 for i in range(16)])
def salsa20_12(k,n8,d,ctr=0):
    out=bytearray(); i=0
    while i<len(d):
        ks=_blk(k,n8,ctr+i//64)
        out.extend(bytes(a^b for a,b in zip(d[i:i+64],ks))); i+=64
    return bytes(out)

# ============================================================
# VAULT
# ============================================================
SBOX=[185,243,94,251,153,162,126,77,155,41,182,62,200,173,252,244]
def dec_payload(p,k):
    out=bytearray(len(p)); c=117; y=84^k[1]
    for a in range(len(p)):
        h=(k[15&a]^SBOX[15&a]^y^((139*a)&0xFF)
           ^((1293491885>>(8*(3&a)))&0xFF)^c)&0xFF
        out[a]=p[a]^h; y=p[a]; c=x8((c^p[a]^k[(a+7)&15])&0xFF,1)
    return bytes(out)
def dec_spec(s):
    t0,t1,t2,t3,t4,t5=s[:6]
    al=lambda v: v['v'] if isinstance(v,dict) else v
    t2,t3,t4,t5=al(t2),al(t3),al(t4),al(t5)
    e=bytearray(len(t2)); n=t0
    for o in range(len(t2)):
        i=t5[o]; inner=x8(t2[o],7&(n^t1))
        m1=(t3[i]+(n^((13*o)&255)))&255; m2=(t1+17*o)&255
        r=Y8((inner^m1^m2)&255,7&(t4[o]^n)); e[o]=r
        n=(x8(n,1)^((r+t4[o]+o)&255))&255
    return bytes(e)
def gc(v,i):
    c=v['consts'][i]
    if isinstance(c,dict):
        t=c.get('t','u8'); val=c.get('v')
        if t=='u8': return bytes(val)
        if t=='u32': return b''.join(struct.pack('<I',x) for x in val)
        if t=='s': return val.encode()
        if t=='a': return b''.join(x.encode() for x in val)
        if t=='n': return bytes([val])
    return b''
def _b64(s):
    s=s.strip().replace("-","+").replace("_","/")
    s+="="*((4-len(s)%4)%4); return base64.b64decode(s)
def _arr(js,n):
    m=re.search(rf'\b{re.escape(n)}\s*=\s*\[',js)
    if not m: return None
    st=m.end()-1; d=1; i=st+1
    while i<len(js) and d>0:
        c=js[i]
        if c=='[': d+=1
        elif c==']': d-=1
        elif c in ('"',"'"):
            q=c; i+=1
            while i<len(js) and js[i]!=q:
                if js[i]=='\\': i+=1
                i+=1
        i+=1
    return re.findall(r'["\']([^"\']+)["\']', js[st:i])

def load_vault(refresh=False):
    if not refresh and os.path.exists(VAULT_CACHE):
        try:
            v = json.load(open(VAULT_CACHE))
            print(f"   📂 vault da cache ({len(v.get('consts',[]))} consts)")
            return v
        except Exception: pass
    print("   🔄 rigenerazione vault...")
    s=requests.Session()
    s.headers.update({"User-Agent":UA,"Referer":"https://streamcorner.st/"})
    for u in WORKER_ASSETS:
        try:
            r=s.get(u,timeout=15,verify=False)
            if r.status_code==200 and len(r.text)>10000:
                dr=_arr(r.text,"dr"); Ar=_arr(r.text,"Ar")
                if not dr or not Ar: continue
                v=json.loads(dec_payload(_b64("".join(Ar)),
                                          _b64("".join(dr))).decode())
                json.dump(v,open(VAULT_CACHE,"w"),ensure_ascii=False)
                print(f"   ✅ vault OK ({u.split('/')[-1]})")
                return v
        except Exception: continue
    raise RuntimeError("worker non scaricabile")

# ============================================================
# CLIENT
# ============================================================
class SC:
    def __init__(self,v):
        self.t=dec_spec(v['specs'][0]); self.e=dec_spec(v['specs'][1])
        self.n=dec_spec(v['specs'][3])
        self.tr=gc(v,16); self.nr=gc(v,20); self.Br=gc(v,25)
        self.rk=hashlib.sha256(self.t+self.e+self.tr).digest()
        self.ns=self.n.decode('utf-8','replace')
        self.s=requests.Session()
        self.s.headers.update({"User-Agent":UA,"Origin":"https://streamcorner.st",
            "Referer":"https://streamcorner.st/","Content-Type":"text/plain",
            "Accept":"application/octet-stream"})
    def call(self,p,extra=None,timeout=15):
        n=os.urandom(8); u=int(time.time()//300)
        msg=json.dumps([self.ns,p,u],separators=(",",":"))
        ct=salsa20_12(self.rk,n,msg.encode())
        tag=hashlib.sha256(self.rk+n+ct+self.nr).digest()[:12]
        body=bytes([0x0A])+n+tag+ct
        url=f"{WORKER_URL}?p={p}"
        if extra:
            for k,v in extra.items(): url+=f"&{k}={v}"
        try:
            r=self.s.post(url,data=body,timeout=timeout,verify=False)
        except Exception: return None
        if r.status_code!=200 or not r.content or r.content[0]!=0x04: return None
        rn=r.content[1:9]; rc=r.content[21:]
        R=hashlib.sha256(self.t+self.e+self.ns.encode()+n+p.encode()
                         +struct.pack(">I",u)+self.Br).digest()
        try: return json.loads(salsa20_12(R,rn,rc).decode())
        except Exception: return None

# ============================================================
# FETCH EVENTI
# ============================================================
def fetch_events(client):
    catalog = client.call(PROVIDER)
    if not isinstance(catalog, list):
        print("   ❌ admin non risponde")
        return []
    print(f"   📋 {len(catalog)} eventi nel catalogo")

    def enrich(ev):
        sid = ev.get(KEY_FIELD)
        if not sid: return None
        d = client.call(PROVIDER, extra={"id": sid})
        return d if isinstance(d, dict) else None

    out = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for f in as_completed([ex.submit(enrich, ev) for ev in catalog]):
            try:
                d = f.result()
                if d: out.append(d)
            except Exception: pass
    print(f"   ✅ {len(out)} eventi arricchiti")
    return out

# ============================================================
# FILTRO LIVE
# ============================================================
def is_live_now(ev, now=None):
    """True se l'evento è in onda adesso (con margine pre/post)."""
    if now is None: now = int(time.time())
    st = ev.get("start_time") or ev.get("timestamp") or 0
    et = ev.get("end_time") or 0
    if not st:
        # evento senza orari (es. 24/7 come Family Guy) → sempre live
        return True
    if not et:
        # solo start_time: consideralo live per 4 ore
        et = st + 4*3600
    return (st - LIVE_MARGIN_BEFORE) <= now <= (et + LIVE_MARGIN_AFTER)

def filter_live(events):
    now = int(time.time())
    live = []
    for ev in events:
        if is_live_now(ev, now):
            live.append(ev)
        else:
            name = ev.get("event_name","?")[:40]
            t = ev.get("time_utc","?")
            print(f"   ⏭️  skip {name} (inizia {t})")
    print(f"   🔴 {len(live)}/{len(events)} eventi in onda adesso")
    return live

# ============================================================
# PLAYWRIGHT
# ============================================================
def resolve_with_playwright(embed_url, timeout=15):
    from playwright.sync_api import sync_playwright
    found = {"m3u8": None}

    def on_response(response):
        u = response.url
        if ".m3u8" in u and found["m3u8"] is None:
            try:
                if "#EXTM3U" in response.text()[:500]:
                    found["m3u8"] = u
            except Exception: pass

    BLOCK = {"image","font","stylesheet"}
    def route(r):
        if r.request.resource_type in BLOCK: return r.abort()
        return r.continue_()

    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, args=[
                "--no-sandbox","--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage","--disable-gpu","--disable-images"])
            ctx = b.new_context(user_agent=UA,
                viewport={"width":1280,"height":720}, locale="en-US")
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
            ctx.route("**/*", route)
            page = ctx.new_page()
            page.on("response", on_response)
            try:
                page.goto(embed_url, wait_until="domcontentloaded",
                          timeout=timeout*1000)
            except Exception: pass
            for _ in range(6):
                if found["m3u8"]: break
                try: page.wait_for_timeout(500)
                except Exception: break
            if not found["m3u8"]:
                for sel in ["button[aria-label*='play' i]",
                            ".vjs-big-play-button",".jw-icon-playback",
                            ".play-button",".clappr-play-button"]:
                    try: page.click(sel, timeout=800); break
                    except Exception: continue
                for x,y in [(640,360),(400,300),(800,400)]:
                    try:
                        page.mouse.click(x,y); page.wait_for_timeout(500)
                        if found["m3u8"]: break
                    except Exception: continue
                for _ in range(8):
                    if found["m3u8"]: break
                    try: page.wait_for_timeout(500)
                    except Exception: break
            b.close()
    except Exception:
        return None
    return found["m3u8"]

# ============================================================
# RESOLVE
# ============================================================
def resolve_all(events, workers=4, limit=0):
    cache = {}
    if os.path.exists(M3U8_CACHE):
        try: cache = json.load(open(M3U8_CACHE, encoding="utf-8"))
        except Exception: cache = {}
    print(f"   📂 cache m3u8: {len(cache)} voci")

    jobs = []
    already = 0
    for ev in events:
        for i, s in enumerate(ev.get("streams") or []):
            emb = s.get("embed_url")
            if not emb: continue
            if emb in cache:
                s["m3u8"] = cache[emb]
                already += 1
                continue
            jobs.append((ev, i, emb))

    print(f"   🔄 da risolvere: {len(jobs)} (già in cache: {already})")

    if limit > 0 and limit < len(jobs):
        jobs = jobs[:limit]
        print(f"   ⚠️  limite: {limit}")

    if not jobs: return events

    resolved = 0
    done = 0

    def worker(ev, idx, emb):
        return ev, idx, emb, resolve_with_playwright(emb, timeout=15)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for f in as_completed([ex.submit(worker, e, i, u) for e, i, u in jobs]):
            done += 1
            try:
                ev, idx, emb, m = f.result()
                if m:
                    ev["streams"][idx]["m3u8"] = m
                    cache[emb] = m
                    resolved += 1
                    ev_name = (ev.get("event_name") or "?")[:35]
                    src = (ev["streams"][idx].get("source_name") or "?")[:22]
                    print(f"   🎯 [{done}/{len(jobs)}] {ev_name} → {src}")
                    print(f"      {m[:110]}")
                elif done % 5 == 0:
                    print(f"   ·  [{done}/{len(jobs)}] risolti: {resolved}")
            except Exception: pass

    try:
        json.dump(cache, open(M3U8_CACHE,"w",encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    except Exception: pass
    print(f"   ✅ risolti: {resolved}/{len(jobs)}")
    return events

# ============================================================
# M3U WRITERS
# ============================================================
def write_events_grouped(events, filename, only_live=False):
    """M3U con group-title = event_name (una entry per fonte)."""
    now = int(time.time())
    seen = set(); groups = {}
    with open(filename, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for ev in events:
            if only_live and not is_live_now(ev, now):
                continue
            ev_name = ev.get("event_name") or "?"
            league  = ev.get("league") or ""
            logo    = ev.get("poster") or ev.get("hero_thumbnail") or ""
            for s in ev.get("streams") or []:
                url = s.get("m3u8")
                if not url or url in seen: continue
                seen.add(url)
                src = s.get("source_name") or "?"
                label = f"{ev_name} — {src}"
                if league: label = f"{label} ({league})"
                if logo:
                    f.write(f'#EXTINF:-1 tvg-logo="{logo}" '
                            f'group-title="{ev_name}",{label}\n')
                else:
                    f.write(f'#EXTINF:-1 group-title="{ev_name}",{label}\n')
                f.write(url + "\n")
                groups[ev_name] = groups.get(ev_name, 0) + 1
    return len(seen), len(groups)

def write_vlc_flat(events, filename, only_live=True):
    """M3U flat (group-title unico), compatibile col combine esistente."""
    now = int(time.time())
    seen = set()
    with open(filename, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for ev in events:
            if only_live and not is_live_now(ev, now):
                continue
            ev_name = ev.get("event_name") or "?"
            league  = ev.get("league") or ""
            logo    = ev.get("poster") or ev.get("hero_thumbnail") or ""
            for s in ev.get("streams") or []:
                url = s.get("m3u8")
                if not url or url in seen: continue
                seen.add(url)
                src = s.get("source_name") or "?"
                label = f"{ev_name} — {src}"
                if league: label = f"{label} ({league})"
                if logo:
                    f.write(f'#EXTINF:-1 tvg-logo="{logo}" '
                            f'group-title="StreamCorner",{label}\n')
                else:
                    f.write(f'#EXTINF:-1 group-title="StreamCorner",{label}\n')
                f.write(url + "\n")
    return len(seen)

def write_complete(events, filename, only_live=True):
    """Mixed: m3u8 se presente, altrimenti embed_url (debug)."""
    now = int(time.time())
    seen = set()
    with open(filename, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for ev in events:
            if only_live and not is_live_now(ev, now):
                continue
            ev_name = ev.get("event_name") or "?"
            for s in ev.get("streams") or []:
                url = s.get("m3u8") or s.get("embed_url")
                if not url or url in seen: continue
                seen.add(url)
                src = s.get("source_name") or "?"
                tag = "🎯" if s.get("m3u8") else "🔗"
                f.write(f'#EXTINF:-1 group-title="StreamCorner",'
                        f'{tag} {ev_name} — {src}\n')
                f.write(url + "\n")
    return len(seen)

# ============================================================
# MAIN
# ============================================================
def main():
    ap = argparse.ArgumentParser(description="StreamCorner Events Extractor")
    # Argomenti compatibili col vecchio CLI
    ap.add_argument("--output", default="streamcorner_COMPLETE.m3u")
    ap.add_argument("--output-dir", default=".")
    ap.add_argument("--providers", default="all")   # ignorato
    ap.add_argument("--refresh-vault", action="store_true")
    ap.add_argument("--workers", type=int, default=15)  # ignorato
    ap.add_argument("--split", action="store_true")     # ignorato
    ap.add_argument("--no-playwright", action="store_true")
    ap.add_argument("--no-quick-probe", action="store_true")  # ignorato
    ap.add_argument("--playwright-workers", type=int, default=4)
    ap.add_argument("--playwright-limit", type=int, default=0)
    ap.add_argument("--playwright-priority", default="")  # ignorato
    args = ap.parse_args()

    print("="*60)
    print("📡 STREAMCORNER EXTRACTOR — EVENTS LIVE ONLY")
    print("="*60)

    os.makedirs(args.output_dir, exist_ok=True)

    # 1. Vault
    print("\n[1/4] Vault")
    vault = load_vault(args.refresh_vault)
    client = SC(vault)

    # 2. Eventi
    print("\n[2/4] Fetch eventi (provider admin)")
    events = fetch_events(client)
    if not events:
        print("❌ nessun evento — scrivo M3U vuoti")
        for fn in ["streamcorner_VLC.m3u", "streamcorner_EVENTS_LIVE.m3u",
                   args.output]:
            open(os.path.join(args.output_dir, fn), "w").write("#EXTM3U\n")
        return

    # 3. Filtro live adesso
    print("\n[3/4] Filtro live adesso")
    live_events = filter_live(events)

    # 4. Risoluzione m3u8
    print("\n[4/4] Risoluzione m3u8 (Playwright)")
    if args.no_playwright:
        print("   ⏭️  saltata (--no-playwright)")
    else:
        live_events = resolve_all(
            live_events,
            workers=args.playwright_workers,
            limit=args.playwright_limit,
        )

    # 5. Output
    print("\n[5/5] Scrittura output")
    live_path = os.path.join(args.output_dir, "streamcorner_EVENTS_LIVE.m3u")
    n_live, g_live = write_events_grouped(live_events, live_path, only_live=True)
    print(f"   💾 streamcorner_EVENTS_LIVE.m3u: {n_live} stream in {g_live} gruppi")

    vlc_path = os.path.join(args.output_dir, "streamcorner_VLC.m3u")
    n_vlc = write_vlc_flat(live_events, vlc_path, only_live=True)
    print(f"   💾 streamcorner_VLC.m3u: {n_vlc} stream (flat, gruppo StreamCorner)")

    mixed_path = os.path.join(args.output_dir, args.output)
    n_mix = write_complete(live_events, mixed_path, only_live=True)
    print(f"   💾 {args.output}: {n_mix} stream (mixed, debug)")

    json_path = os.path.join(args.output_dir, "streamcorner_events.json")
    json.dump(live_events, open(json_path,"w",encoding="utf-8"),
              indent=2, ensure_ascii=False, default=str)
    print(f"   💾 streamcorner_events.json")

    # Riepilogo
    total = sum(len(ev.get("streams") or []) for ev in live_events)
    with_url = sum(1 for ev in live_events for s in ev.get("streams") or []
                   if s.get("embed_url"))
    with_m3u8 = sum(1 for ev in live_events for s in ev.get("streams") or []
                    if s.get("m3u8"))
    print("\n" + "="*60)
    print(f"✅ FATTO")
    print(f"   Eventi live adesso:    {len(live_events)}")
    print(f"   Stream totali:         {total}")
    print(f"   Con embed_url:         {with_url}")
    print(f"   Con m3u8 risolto:      {with_m3u8}")
    print("="*60)

if __name__ == "__main__":
    main()