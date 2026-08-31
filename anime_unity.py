import requests
import re
import time

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
ANIMEUNITY_REFERER = "https://www.animeunity.so/"

# Mappa episodio: (episode_id, scws_id)
# I dati sono stati estratti dalla pagina https://www.animeunity.so/anime/390-dragon-ball-super-ita/9262
EPISODES = {
    1: (9198, 71552), 2: (9199, 71556), 3: (9200, 71553), 4: (9201, 71555),
    5: (9202, 71554), 6: (9203, 71551), 7: (9204, 71557), 8: (9205, 71561),
    9: (9206, 71559), 10: (9207, 71560), 11: (9208, 71564), 12: (9209, 71565),
    13: (9210, 71567), 14: (9211, 71570), 15: (9212, 71566), 16: (9213, 71558),
    17: (9214, 71597), 18: (9215, 71563), 19: (9216, 71562), 20: (9217, 69086),
    21: (9218, 71572), 22: (9219, 71571), 23: (9220, 71573), 24: (9221, 71574),
    25: (9222, 71575), 26: (9223, 69087), 27: (9224, 71603), 28: (9225, 71588),
    29: (9226, 71589), 30: (9227, 71591), 31: (9228, 71590), 32: (9229, 71592),
    33: (9230, 71594), 34: (9231, 71593), 35: (9232, 71595), 36: (9233, 71607),
    37: (9234, 71596), 38: (9235, 71608), 39: (9236, 71609), 40: (9237, 71678),
    41: (9238, 71626), 42: (9239, 71598), 43: (9240, 71599), 44: (9241, 71600),
    45: (9242, 71615), 46: (9243, 71604), 47: (9244, 71602), 48: (9246, 71601),
    49: (9247, 71605), 50: (9248, 71606), 51: (9249, 71627), 52: (9250, 71610),
    53: (9251, 71612), 54: (9252, 71611), 55: (9253, 71614), 56: (9254, 71617),
    57: (9255, 71619), 58: (9256, 71618), 59: (9257, 71620), 60: (9258, 71621),
    61: (9259, 71623), 62: (9260, 71613), 63: (9261, 71622), 64: (9262, 71624),
    65: (9263, 71616), 66: (9264, 71625), 67: (9265, 71630), 68: (9266, 71629),
    69: (9267, 71642), 70: (9268, 71631), 71: (9269, 71634), 72: (9270, 71632),
    73: (9271, 71647), 74: (9272, 71633), 75: (9273, 71635), 76: (9274, 71637),
    77: (9275, 71636), 78: (9276, 71638), 79: (9277, 71639), 80: (9278, 71640),
    81: (9279, 71648), 82: (9280, 71679), 83: (12232, 71643), 84: (12233, 71645),
    85: (12234, 71681), 86: (12235, 71646), 87: (12236, 71649), 88: (12237, 71650),
    89: (12238, 71651), 90: (12239, 71654), 91: (12240, 71652), 92: (12241, 71682),
    93: (12242, 71657), 94: (12243, 71653), 95: (12280, 71658), 96: (12281, 71656),
    97: (12282, 71660), 98: (12688, 71664), 99: (12689, 71663), 100: (12690, 71665),
    101: (13195, 71668), 102: (13196, 71666), 103: (13197, 71667), 104: (14614, 71659),
    105: (14615, 71680), 106: (14616, 71669), 107: (14617, 71670), 108: (15013, 71671),
    109: (15014, 71672), 110: (15015, 71662), 111: (15403, 71673), 112: (15404, 71661),
    113: (15405, 71674), 114: (15894, 71675), 115: (15895, 71676), 116: (15896, 71677),
    117: (29358, 71683), 118: (29457, 71684), 119: (29458, 71689), 120: (29535, 71685)
}

def get_embed_url(episode_id):
    """Scarica la pagina dell'episodio ed estrae l'embed_url fresco."""
    url = f"https://www.animeunity.so/anime/390-dragon-ball-super-ita/{episode_id}"
    headers = {"User-Agent": USER_AGENT, "Referer": ANIMEUNITY_REFERER}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"Errore pagina episodio {episode_id}: {e}")
        return None

    m = re.search(r'embed_url="([^"]+)"', html)
    if m:
        return m.group(1)
    else:
        print(f"embed_url non trovato per episode_id {episode_id}")
        return None

def get_vixcloud_m3u8(embed_url):
    """Dato un embed_url VixCloud, restituisce il link master m3u8."""
    headers = {"User-Agent": USER_AGENT, "Referer": ANIMEUNITY_REFERER}
    try:
        r = requests.get(embed_url, headers=headers, timeout=15)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"Errore recupero embed VixCloud: {e}")
        return None

    # Estrai window.masterPlaylist
    m = re.search(
        r"window\.masterPlaylist\s*=\s*\{.*?params:\s*\{[^}]*?'token':\s*'([^']+)'[^}]*?'expires':\s*'([^']+)'[^}]*?\}.*?url:\s*'([^']+)'",
        html,
        re.DOTALL
    )
    if not m:
        print("masterPlaylist non trovato")
        return None

    token, expires, base_url = m.group(1), m.group(2), m.group(3)
    can_fhd = "window.canPlayFHD = true" in html

    m3u8 = f"{base_url}?token={token}&expires={expires}"
    if can_fhd:
        m3u8 += "&h=1"
    return m3u8

def get_anime_episodes():
    """Genera le voci M3U per tutti gli episodi di Dragon Ball Super."""
    lines = []
    for ep, (ep_id, scws_id) in EPISODES.items():
        print(f"Processando episodio {ep}...")
        embed_url = get_embed_url(ep_id)
        if not embed_url:
            continue
        m3u8 = get_vixcloud_m3u8(embed_url)
        if m3u8:
            lines.append(f'#EXTINF:-1 group-title="AnimeUnity - Dragon Ball Super",Dragon Ball Super Ep {ep:03d}')
            lines.append(m3u8)
            print(f"  ok")
        else:
            print(f"  m3u8 non ottenuto")
    return lines

if __name__ == "__main__":
    lines = get_anime_episodes()
    print(f"Totale episodi estratti: {len(lines)//2}")