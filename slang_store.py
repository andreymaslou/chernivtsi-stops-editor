"""
Сленговые псевдонимы и переименование остановок — «редактор сленга».

Зачем: люди говорят «Тралка», «Соборка», «Універ», а в данных лежит
«пл. Театральна», «пл. Соборна», «Держуніверситет». Плюс отдельная беда —
безымянные остановки «на вимогу», которые надо назвать по ближайшей улице.

Файл data/slang_overrides.json устроен так:
    {
      "version": 1,
      "updated": "2026-09-15 23:40:00",
      "stops": {
        "65": {"aliases": ["соборка", "соборна"], "name": "Соборна площа",
               "generic": false, "comment": "так кажуть місцеві"}
      }
    }

Правки применяются к списку остановок, с которым работает Locator:
    * aliases   — добавляются к существующим (ничего не затираем);
    * name      — если задано, заменяет название для поиска и показа;
    * generic   — true выключает остановку из текстового поиска (человек её
                  так не назовёт), но в графе маршрутов она остаётся.

Остановки с названием «на вимогу» выключаются из поиска автоматически, даже
без правок: таких остановок в городе десятки, и запрос «на вимогу» иначе
даёт случайную остановку на другом конце города.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from time_utils import now_kyiv

from graph_layer import is_generic_stop_name
from storage import SLANG_PATH, read_json, write_json_atomic

logger = logging.getLogger("transgps-slang")

OVERRIDES_VERSION = 1


def load_overrides() -> Dict[str, Any]:
    """Читает файл правок (или пустую структуру, если его ещё нет)."""
    data = read_json(SLANG_PATH, {})
    if not isinstance(data, dict):
        data = {}
    stops = data.get("stops")
    if not isinstance(stops, dict):
        stops = {}
    return {
        "version": data.get("version", OVERRIDES_VERSION),
        "updated": data.get("updated"),
        "stops": stops,
    }


def save_overrides(data: Dict[str, Any]) -> Dict[str, Any]:
    """Сохраняет файл правок, проставляя версию и время изменения."""
    payload = {
        "version": OVERRIDES_VERSION,
        "updated": now_kyiv().strftime("%Y-%m-%d %H:%M:%S"),
        "stops": data.get("stops") or {},
    }
    write_json_atomic(SLANG_PATH, payload)
    logger.info("Сленг: правки сохранены (%d остановок)", len(payload["stops"]))
    return payload


def upsert_stop(
    stop_id: Any,
    aliases: Optional[Sequence[str]] = None,
    name: Optional[str] = None,
    generic: Optional[bool] = None,
    comment: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Добавляет или обновляет правку одной остановки.

    None означает «не трогать это поле»: так из админки можно поменять только
    псевдонимы, не задев переименование (и наоборот).
    """
    data = load_overrides()
    key = str(stop_id).strip()
    if not key:
        raise ValueError("stop_id не задан")

    entry = dict(data["stops"].get(key) or {})

    if aliases is not None:
        cleaned: List[str] = []
        for alias in aliases:
            alias = str(alias).strip()
            if alias and alias.lower() not in {a.lower() for a in cleaned}:
                cleaned.append(alias)
        entry["aliases"] = cleaned

    if name is not None:
        entry["name"] = str(name).strip()

    if generic is not None:
        entry["generic"] = bool(generic)

    if comment is not None:
        entry["comment"] = str(comment).strip()

    entry["updated"] = now_kyiv().strftime("%Y-%m-%d %H:%M:%S")
    data["stops"][key] = entry
    save_overrides(data)
    return entry


def delete_stop(stop_id: Any) -> bool:
    """Убирает правку остановки (возвращает False, если её и не было)."""
    data = load_overrides()
    key = str(stop_id).strip()
    if key not in data["stops"]:
        return False
    data["stops"].pop(key)
    save_overrides(data)
    return True


def apply_overrides(stops: Sequence[Dict[str, Any]], keep_generic: bool = False) -> List[Dict[str, Any]]:
    """
    Применяет правки к списку остановок для Locator.

    keep_generic=True оставляет безымянные остановки в списке (нужно, если
    понадобится искать их напрямую, например для отладки).
    """
    overrides = load_overrides()["stops"]

    merged_stops: List[Dict[str, Any]] = []
    dropped_generic = 0
    renamed = 0

    for stop in stops:
        override = overrides.get(str(stop.get("id"))) or {}

        name = str(override.get("name") or stop.get("name") or "").strip()
        if override.get("name") and str(override["name"]).strip() != str(stop.get("name") or "").strip():
            renamed += 1

        aliases = [str(alias) for alias in (stop.get("aliases") or []) if str(alias).strip()]
        known = {alias.lower() for alias in aliases}
        for alias in override.get("aliases") or []:
            alias = str(alias).strip()
            if alias and alias.lower() not in known:
                aliases.append(alias)
                known.add(alias.lower())

        generic = bool(override["generic"]) if "generic" in override else is_generic_stop_name(name)

        if generic and not keep_generic:
            dropped_generic += 1
            continue

        merged_stops.append({
            **stop,
            "name": name,
            "aliases": aliases,
            "generic": generic,
        })

    logger.info(
        "Сленг: применено правок %d, переименовано %d, скрыто безымянных %d (осталось %d)",
        len(overrides), renamed, dropped_generic, len(merged_stops),
    )
    return merged_stops


def overrides_stats() -> Dict[str, Any]:
    """Сводка правок для админки."""
    data = load_overrides()
    entries = list(data["stops"].values())
    return {
        "stops": len(entries),
        "with_name": sum(1 for entry in entries if entry.get("name")),
        "with_aliases": sum(1 for entry in entries if entry.get("aliases")),
        "marked_generic": sum(1 for entry in entries if entry.get("generic")),
        "updated": data.get("updated"),
        "path": str(SLANG_PATH),
    }
