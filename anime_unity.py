def get_anime_episodes():
    """Genera le voci M3U per tutti gli episodi di Dragon Ball Super."""
    lines = []
    for ep in range(1, 132):  # 131 episodi
        ep_str = f"{ep:03d}"
        if ep == 20:
            url = "https://www.forbiddenlol.cloud/DDL/ANIME/DragonBallSuper_Ep_020_ITA.mp4"
        elif ep == 26:
            url = "https://www.forbiddenlol.cloud/DDL/ANIME/DragonBallSuper_Ep_026_ITA.mp4"
        else:
            url = f"https://www.nisekoi-anime.it/DLL/ANIME/DBSuperITA/DragonBallSuper_Ep_{ep_str}_ITA.mp4"
        lines.append(f'#EXTINF:-1 group-title="AnimeUnity - Dragon Ball Super",Dragon Ball Super Ep {ep_str}')
        lines.append(url)
    return lines

if __name__ == "__main__":
    # Se eseguito direttamente, stampa solo il conteggio
    lines = get_anime_episodes()
    print(f"Totale episodi: {len(lines)//2}")