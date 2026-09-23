"""
Емулятор GPS для тестов и демо: виртуальный парк ТС на графе маршрутов.

Реальный трекер trans-gps.cv.ua показывает мало машин (в пике ~20% от
теоретического парка, ночью — ноль), поэтому «перший потрібний ТС» и карта
эмулятора в такие часы пустые. Для настройки и проверки логики удобнее
иметь детерминированный «эмулятор GPS»: те же поездки по тем же маршрутам,
но расписание генерирует машины на линиях в любой момент.

Как работает:

  * для каждого направления маршрута (из graph.json) читаем ланцюжок узлов
    и времена сегментов (prefix-суммы);
  * расписание (routes_schedule.json) задаёт первый/последний рейс и средний
    интервал; рейс = машина, вышедшая из терминала в момент dep;
  * в момент времени t активны рейсы, у которых dep <= t <= dep + время_рейса;
    позиция машины — интерполяция вдоль ланцюжка по истёкшему времени;
  * assume_in_service=True «зажимает» время в рабочий интервал (в тестах
    ночью парк выглядит как в середине дня);
  * контракт наружу 1-в-1 с live_layer.snapshot(): те же ключи машин, чтобы
    роутер и эмулятор не знали, реальный GPS или симуляция.

Детерминированность: никаких источников случайности — same now -> same fleet.
"""

import math
import zlib
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from time_utils import as_kyiv, now_kyiv

# Интервалы по умолчанию, если маршрута нет в routes_schedule.json.
DEFAULT_FIRST = "06:00"
DEFAULT_LAST = "22:00"
DEFAULT_HEADWAY_MIN = 20.0

# Детерминированная палитра цветов маршрутов для симуляций (по имени).
SIM_COLOURS = [
    "#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231", "#911eb4",
    "#46f0f0", "#f032e6", "#bcf60c", "#fabebe", "#008080", "#e6beff",
    "#9a6324", "#fffac8", "#800000", "#aaffc3", "#808000", "#ffd8b1",
    "#000075", "#808080",
]


def _minutes_of_day(value: Any) -> Optional[float]:
    if not value:
        return None
    try:
        hours, minutes = str(value).strip().split(":")
        return int(hours) * 60 + int(minutes)
    except (ValueError, TypeError):
        return None


class SimLayer:
    """Віртуальний флот Черновців (той самий контракт, що в live_layer)."""

    def __init__(
        self,
        graph: Dict[str, Any],
        schedule: Optional[Dict[str, Any]] = None,
        assume_in_service: bool = False,
    ):
        self.assume_in_service = assume_in_service
        self.schedule = schedule or {}

        self.directions: List[Dict[str, Any]] = []
        nodes: Dict[int, Dict[str, Any]] = {
            int(nid): node for nid, node in graph["nodes"].items()
        }

        for index, (key, route) in enumerate(graph["routes"].items()):
            stop_ids = [int(n) for n in route["stops"]]
            coords = [(nodes[n]["lat"], nodes[n]["lon"]) for n in stop_ids]
            prefix = [0.0]
            for segment in route.get("segments", []):
                prefix.append(prefix[-1] + float(segment.get("minutes", 0.0)))
            total_min = max(0.01, prefix[-1])

            label = route.get("live_route_name") or route.get("route_name") or "?"
            service = self._service_for(route.get("vehicle_type"), route.get("route_name"))
            # crc32, а не вбудований hash(): hash() рядків рандомізується на
            # кожен процес (PYTHONHASHSEED), тому кольори маршрутів «стрибали»
            # б при кожному рестарті сервера і не збігалися з палітрою в UI.
            colour = SIM_COLOURS[zlib.crc32(label.encode("utf-8")) % len(SIM_COLOURS)]

            length_km = float(route.get("length_km") or 0.0)
            self.directions.append({
                "key": key,
                "vehicle_type": route.get("vehicle_type", "bus"),
                "route_name": route.get("route_name", "?"),
                "label": str(label),
                "coords": coords,
                "prefix": prefix,
                "total_min": total_min,
                "speed_kmh": (length_km / total_min * 60.0) if length_km > 0 else 20.0,
                "first_min": service[0],
                "last_min": service[1],
                "headway_min": service[2],
                "colour": colour,
                "route_id": 200000 + index,
            })

    # ------------------------------------------------------------------
    # Расписание
    # ------------------------------------------------------------------

    def _service_for(self, vehicle_type: str, route_name: str) -> Tuple[float, float, float]:
        route = (self.schedule.get(vehicle_type) or {}).get(route_name) or {}
        first = _minutes_of_day(route.get("first")) or _minutes_of_day(DEFAULT_FIRST)
        last = _minutes_of_day(route.get("last")) or _minutes_of_day(DEFAULT_LAST)
        interval = route.get("interval") or {}
        lo, hi = interval.get("min"), interval.get("max")
        if lo and hi:
            headway = (float(lo) + float(hi)) / 2.0
        else:
            headway = DEFAULT_HEADWAY_MIN
        return float(first), float(last), float(headway)

    # ------------------------------------------------------------------
    # Моделирование парка в момент времени
    # ------------------------------------------------------------------

    def _virtual_minutes(self, now: datetime) -> float:
        """Хвилини доби. У тестовому режимі ніч «переграється» серединою дня."""
        minutes = now.hour * 60 + now.minute + now.second / 60.0
        if not self.assume_in_service:
            return minutes
        firsts = [d["first_min"] for d in self.directions]
        lasts = [d["last_min"] for d in self.directions]
        day_start = min(firsts) if firsts else 360.0
        day_end = max(lasts) if lasts else 1320.0
        if minutes < day_start or minutes > day_end:
            return day_start + (day_end - day_start) * 0.5
        return minutes

    @staticmethod
    def _position_at(direction: Dict[str, Any], elapsed_min: float) -> Tuple[float, float, float, float]:
        """(lat, lon, speed_kmh, heading_deg) — машина, яка проїхала elapsed_min."""
        coords = direction["coords"]
        prefix = direction["prefix"]
        idx = 0
        for i, node_min in enumerate(prefix[:-1]):
            if elapsed_min >= node_min and elapsed_min < prefix[i + 1]:
                idx = i
                break
        else:
            idx = len(coords) - 2

        seg_minutes = max(0.001, prefix[idx + 1] - prefix[idx])
        fraction = min(1.0, (elapsed_min - prefix[idx]) / seg_minutes)
        lat1, lon1 = coords[idx]
        lat2, lon2 = coords[idx + 1]
        lat = lat1 + (lat2 - lat1) * fraction
        lon = lon1 + (lon2 - lon1) * fraction

        mid_lat = math.radians((lat1 + lat2) / 2.0)
        dlat = lat2 - lat1
        dlon = (lon2 - lon1) * math.cos(mid_lat)
        heading = 0.0 if (dlat == 0 and dlon == 0) else math.degrees(math.atan2(dlon, dlat)) % 360.0
        return lat, lon, direction["speed_kmh"], heading

    def _compute_vehicles(self, now: datetime) -> List[Dict[str, Any]]:
        t = self._virtual_minutes(now)
        vehicles: List[Dict[str, Any]] = []

        for direction in self.directions:
            first, last, headway = direction["first_min"], direction["last_min"], direction["headway_min"]
            total = direction["total_min"]

            start_k = max(0, int(math.floor((t - total - first) / headway)))
            end_k = int(math.floor((t - first) / headway))
            if end_k < 0:
                continue

            for k in range(start_k, end_k + 1):
                dep = first + k * headway
                if dep > last:
                    break
                elapsed = t - dep
                if elapsed < 0.0 or elapsed > total:
                    continue

                lat, lon, speed, heading = self._position_at(direction, elapsed)
                label = direction["label"]
                dir_offset = 500 if direction["key"].endswith(":B") else 0
                board = f"{direction['route_name']}-{k + 1 + dir_offset:03d}"
                vehicles.append({
                    "imei": f"SIM-{direction['key']}-{k}",
                    "vehicle_id": direction["route_id"] * 1000 + k,
                    "board_number": board,
                    "vehicle_type": direction["vehicle_type"],
                    "route_id": direction["route_id"],
                    "route_name": direction["route_name"],
                    "route_label": label,
                    "route_colour_name": None,
                    "route_colour_hex": direction["colour"],
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                    "speed_kmh": round(speed, 1),
                    "heading_deg": round(heading, 1),
                    "gpstime": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "age_seconds": 0.0,
                    "in_depo": False,
                    "is_live": True,
                    "status": "live",
                    "carrier": "Симуляція",
                    "remark": "емулятор GPS",
                    "direction": direction["key"].split(":")[-1],
                    "progress": round(min(1.0, elapsed / total), 3),
                })

        vehicles.sort(key=lambda v: (v["route_label"], v["board_number"]))
        return vehicles

    def snapshot(
        self,
        now: Optional[datetime] = None,
        only_fresh: bool = True,
        include_depo: bool = False,
        route_ids: Optional[Sequence[int]] = None,
        vehicle_types: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """
        Срез виртуального парка. Формат повторює live_layer.snapshot():
        counts, vehicles (той самий набір полів), routes. only_fresh /
        include_depo приймаються для сумісності — у симуляції всі машини
        живі і не в депо.
        """
        now = as_kyiv(now) if now is not None else now_kyiv()
        all_vehicles = self._compute_vehicles(now)

        counts = {
            "total": len(all_vehicles),
            "live": len(all_vehicles),
            "stale": 0,
            "in_depo": 0,
            "unknown_gpstime": 0,
        }

        selected = all_vehicles
        if route_ids:
            wanted = {int(rid) for rid in route_ids}
            selected = [v for v in selected if v["route_id"] in wanted]
        if vehicle_types:
            wanted_types = {str(t).strip().lower() for t in vehicle_types}
            selected = [v for v in selected if v["vehicle_type"] in wanted_types]
        if not include_depo:
            selected = [v for v in selected if not v["in_depo"]]
        if only_fresh:
            selected = [v for v in selected if v["is_live"]]

        return {
            "source": "sim",
            "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "last_success_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "last_error": None,
            "poll_interval_seconds": 0.0,
            "fresh_max_age_seconds": 0.0,
            "counts": counts,
            "returned": len(selected),
            "routes": {
                str(d["route_id"]): {
                    "route_id": d["route_id"],
                    "name": d["label"],
                    "code": d["route_name"],
                    "vehicle_type": d["vehicle_type"],
                    "colour_hex": d["colour"],
                }
                for d in self.directions
            },
            "vehicles": selected,
        }

    # -- Служебное ----------------------------------------------------

    @property
    def route_count(self) -> int:
        return len(self.directions)

    @property
    def poll_count(self) -> int:
        return 1