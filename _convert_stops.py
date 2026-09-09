"""
Одноразовый скрипт-конвертер: превращает существующий global_stops.json
(формат {"id": [lat*1e6, lon*1e6, name, routes]}) в целевой формат
stops.json, требуемый для API голосового помощника:
    [{"id": int, "name": str, "lat": float, "lon": float, "aliases": [...]}]

Также добавляет народные названия (aliases) для известных объектов,
упомянутых в AI_SERVER_PLAN.md ("Калинка", "Соборка", "Універ", "Гравітон").

Запускается один раз вручную, в состав API-сервера (main.py) не входит.
"""

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SRC_PATH = BASE_DIR / "global_stops.json"
DST_PATH = BASE_DIR / "stops.json"

# Известные народные названия -> подстрока в официальном названии остановки.
KNOWN_ALIASES = {
    "Калинка": "калинівський ринок",
    "Соборка": "соборна",
    "Універ": "університет",
    "Гравітон": "гравітон",
    "Ринок": "ринок",
}


def build_aliases(name: str) -> list:
    lowered = name.lower()
    aliases = []
    for alias, substring in KNOWN_ALIASES.items():
        if substring in lowered and alias.lower() not in lowered:
            aliases.append(alias)
    return aliases


def main() -> None:
    with SRC_PATH.open(encoding="utf-8") as f:
        raw = json.load(f)

    stops = []
    for stop_id_str, (lat_int, lon_int, name, _routes) in raw.items():
        stops.append(
            {
                "id": int(stop_id_str),
                "name": name,
                "lat": lat_int / 1_000_000,
                "lon": lon_int / 1_000_000,
                "aliases": build_aliases(name),
            }
        )

    with DST_PATH.open("w", encoding="utf-8") as f:
        json.dump(stops, f, ensure_ascii=False, indent=2)

    print(f"Сконвертировано {len(stops)} остановок -> {DST_PATH}")


if __name__ == "__main__":
    main()
