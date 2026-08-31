import sys
import os

sys.path.append(os.path.dirname(__file__))

from damitv_extractor import build_sports_lines
from anime_unity import get_anime_episodes

OUTPUT_FILE = "damitv_events.m3u"

def main():
    print("Generazione playlist completa...")
    sports_lines = build_sports_lines()
    anime_lines = get_anime_episodes()

    final_lines = ["#EXTM3U"]
    final_lines.extend(sports_lines)
    if anime_lines:
        final_lines.append("# ===== ANIMEUNITY - DRAGON BALL SUPER =====")
        final_lines.extend(anime_lines)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(final_lines))

    print(f"\n💾 Playlist salvata in {OUTPUT_FILE}")
    print(f"   Sportive: {len(sports_lines)//5} voci")
    print(f"   Anime: {len(anime_lines)//2} episodi")

if __name__ == "__main__":
    main()