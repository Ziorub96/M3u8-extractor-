#!/usr/bin/env python3
"""
smtk_extractor.py – Scarica TV.m3u da JekaLich/smtk, filtra i canali sportivi russi desiderati
e salva smtk_sport.m3u.
"""

import requests
import sys
from pathlib import Path

URL_RAW = "https://raw.githubusercontent.com/JekaLich/smtk/main/TV.m3u"
OUTPUT_FILE = "smtk_sport.m3u"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# Nomi dei canali da estrarre (esattamente come compaiono nel campo dopo la virgola)
WANTED_NAMES = {
    "Матч Премьер",
    "Матч Премьер HD",
    "Футбол",
    "Футбол HD",
    "МАТЧ! Футбол 1",
    "МАТЧ! Футбол 1 HD",
    "МАТЧ! Футбол 2",
    "МАТЧ! Футбол 2 HD",
    "МАТЧ! Футбол 3",
    "МАТЧ! Футбол 3 HD",
    "Матч ТВ",
    "Матч ТВ HD",
    "Матч! Игра",
    "Матч! Игра HD",
    "Матч! Арена",
    "Матч! Арена HD",
    "Матч! Страна",
    "Матч! Страна HD",
    "Старт HD",
    "KHL",
    "KHL Prime HD",
    "Хоккейный HD",
    "Футбольный HD",
    "Спортивный HD",
    "Viju+ sport",
    "Viju+ sport HD",
    "Eurosport 1",
    "Eurosport 1 HD",
    "Eurosport 2",
    "Eurosport 2 HD",
    "Setanta Sports 1 HD",
    "Setanta Sports 2 HD",
    "Setanta Sports UA HD",
    "Setanta Sports + UA HD",
    "Setanta Qazaqstan HD",
    "QSport HD",
    "Спорт 1",
    "Спорт 2",
    "MMA-TV",
    "MMA-TV HD",
    "UDAR HD",
    "МАТЧ! Боец",
    "Матч! Боец HD",
    "Точка Отрыва",
    "Русский Экстрим HD",
    "Extreme Sports",
    "Конный Мир HD",
    "Авто Плюс",
    "Авто Плюс HD",
    "Авто 24 HD",
    "Драйв"
}

def extract_name(extinf_line: str) -> str:
    """Estrae il nome del canale dalla riga #EXTINF (campo dopo l'ultima virgola)."""
    if ',' in extinf_line:
        return extinf_line.split(',')[-1].strip()
    return ""

def main():
    print("📡 Scarico TV.m3u da smtk...")
    try:
        r = requests.get(URL_RAW, headers={"User-Agent": USER_AGENT}, timeout=30)
        r.raise_for_status()
        content = r.text
    except Exception as e:
        print(f"❌ Errore scaricando TV.m3u: {e}")
        Path(OUTPUT_FILE).write_text("#EXTM3U\n", encoding="utf-8")
        sys.exit(0)

    lines = content.splitlines()
    output_lines = ["#EXTM3U"]
    count = 0
    i = 0

    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXTINF") and 'group-title="Спорт"' in line:
            name = extract_name(line)
            if name in WANTED_NAMES:
                output_lines.append(line)
                i += 1
                # Aggiunge eventuali opzioni VLC (#EXTVLCOPT)
                while i < len(lines) and lines[i].startswith("#EXTVLCOPT"):
                    output_lines.append(lines[i])
                    i += 1
                # Aggiunge URL
                if i < len(lines) and (lines[i].startswith("http://") or lines[i].startswith("https://")):
                    output_lines.append(lines[i])
                    count += 1
                continue
        i += 1

    Path(OUTPUT_FILE).write_text("\n".join(output_lines) + "\n", encoding="utf-8")
    print(f"✅ Salvato {OUTPUT_FILE} con {count} canali sportivi russi")

if __name__ == "__main__":
    main()