"""
Живой GPS-слой TransGPS Chernivtsi.

Источник данных — открытый сайт перевозчика https://trans-gps.cv.ua
(авторизация не требуется, только GET):

    GET /map/routes/1   — справочник автобусных маршрутов
    GET /map/routes/2   — справочник троллейбусных маршрутов
    GET /map/tracker/?selectedRoutesStr=<ид_ид_ид> — поток ТС (сайт опрашивает
                          этот эндпоинт каждые 5 секунд)

Особенности источника (проверено на живых данных 2026-09-15):

    * ответ трекера — объект, ключ = IMEI, значение — словарь ТС;
    * поля speed и orientation приходят СТРОКАМИ ("000.0", "189.20"),
      поэтому их нужно приводить к float перед любой арифметикой;
    * gpstime — локальное время сервера в формате "YYYY-MM-DD HH:MM:SS";
    * флаг online у перевозчика практически всегда true, поэтому свежесть
      данных определяется ТОЛЬКО по возрасту gpstime
      (см. FRESH_MAX_AGE_SECONDS) — иначе «живыми» считаются машины,
      которые стоят на парковке со вчерашним треком;
    * часть ТС стоит в депо (inDepo=true) — они физически не на маршруте;
    * у троллейбусов routeName иногда не число (например "T"), поэтому
      человекочитаемую подпись маршрута берём из справочника по routeId;
    * цвет маршрута задан CSS-именем ("magenta", "maroon", ...), нативный
      клиент ожидает hex — конвертируем через CSS_COLOUR_HEX.

Модуль намеренно не знает ничего про остановки и расписание: его задача —
отдать нормализованный «живой слой» (кто, где, на каком маршруте, насколько
данные свежие), а маршрутизацию и ETA считает отдельный слой.
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from time_utils import APP_TIMEZONE, as_kyiv, format_kyiv, now_kyiv

import httpx

logger = logging.getLogger("transgps-live-layer")

# ---------------------------------------------------------------------------
# Константы источника и политики кэширования
# ---------------------------------------------------------------------------

LIVE_SOURCE_BASE = "https://trans-gps.cv.ua"
TRACKER_PATH = "/map/tracker/"
ROUTES_PATH = "/map/routes/{bus_type}"

# Сайт перевозчика сам опрашивает трекер раз в 5 с — придерживаемся того же
# темпа, чтобы данные были не свежее, чем на официальной карте.
POLL_INTERVAL_SECONDS = 5.0

# Порог «живости» данных: если GPS-трек старше 5 минут, машину показываем
# как «за розкладом», а не как «жива» (у перевозчика такие треки есть).
FRESH_MAX_AGE_SECONDS = 300.0

# HTTP-таймаут одного запроса: сеть перевозчика иногда отвечает вяло,
# но зависший поллер не должен блокировать остальные задачи.
HTTP_TIMEOUT_SECONDS = 8.0

# Справочник маршрутов меняется редко: обновляем его при смене поля version
# либо раз в 6 часов (version у перевозчика = ключ кэша).
ROUTES_TTL_SECONDS = 6 * 3600.0

# Типы ТС в терминах источника (поле idBusTypes).
BUS_TYPE_ID = 1
TROLLEY_TYPE_ID = 2

# CSS-имена цветов, которые реально встречаются в справочнике маршрутов.
# Нативному клиенту (RN) нужен hex, поэтому держим таблицу рядом.
CSS_COLOUR_HEX: Dict[str, str] = {
    "black": "#000000",
    "coral": "#ff7f50",
    "deeppink": "#ff1493",
    "green": "#008000",
    "grey": "#808080",
    "gray": "#808080",
    "magenta": "#ff00ff",
    "maroon": "#800000",
    "navy": "#000080",
    "olive": "#808000",
    "purple": "#800080",
    "red": "#ff0000",
    "teal": "#008080",
    "blue": "#0000ff",
}

# Заглушка на случай незнакомого имени цвета — чтобы клиент не получал None.
DEFAULT_COLOUR_HEX = "#666666"


# ---------------------------------------------------------------------------
# Низкоуровневые помощники нормализации
# ---------------------------------------------------------------------------

def to_float(value: Any, default: float = 0.0) -> float:
    """
    Приводит «число из источника» к float.

    Перевозчик отдаёт часть числовых полей строками ("000.0", "189.20"),
    а часть — настоящими числами (lat/lng). Пустая строка, None и мусор
    дают default, чтобы не ронять обработку всего пакета из-за одной машины.
    """
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return default


def colour_to_hex(colour_name: Optional[str]) -> str:
    """Переводит CSS-имя цвета маршрута ("magenta") в hex ("#ff00ff")."""
    if not colour_name:
        return DEFAULT_COLOUR_HEX
    name = str(colour_name).strip().lower()
    if name.startswith("#"):
        return name
    return CSS_COLOUR_HEX.get(name, DEFAULT_COLOUR_HEX)


def parse_gpstime(raw: Any) -> Optional[datetime]:
    """
    Разбирает gpstime ("2026-09-15 20:05:51") как aware datetime.

    Время в источнике — локальное (Europe/Kyiv), поэтому и сравниваем его
    с локальным временем приложения, независимо от timezone хоста/Docker.
    """
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).replace(tzinfo=APP_TIMEZONE)
        except ValueError:
            continue
    return None


def vehicle_type_from_id(bus_type_id: Any) -> str:
    """Переводит idBusTypes источника в человекочитаемый тип ТС."""
    return "trolley" if to_float(bus_type_id) == TROLLEY_TYPE_ID else "bus"


# ---------------------------------------------------------------------------
# Нормализация справочника маршрутов и отдельной машины
# ---------------------------------------------------------------------------

def normalize_route(route_id: Any, raw_route: Dict[str, Any], bus_type: str) -> Dict[str, Any]:
    """
    Приводит запись справочника маршрутов к единому виду.

    Пример сырой записи (GET /map/routes/1):
        {"id": 14, "code": "9az", "name": "9A", "colour": "magenta",
         "price": 20, "version": 10, "idBusTypes": 1, ...}
    """
    colour_name = str(raw_route.get("colour") or "").strip()
    return {
        "route_id": int(to_float(route_id)),
        "name": str(raw_route.get("name") or raw_route.get("code") or "").strip(),
        "code": str(raw_route.get("code") or "").strip(),
        "vehicle_type": bus_type,
        "colour_name": colour_name,
        "colour_hex": colour_to_hex(colour_name),
        "price": int(to_float(raw_route.get("price"))),
        "version": int(to_float(raw_route.get("version"))),
        "sort": int(to_float(raw_route.get("sort"))),
    }


def normalize_vehicle(
    raw: Dict[str, Any],
    route_meta: Optional[Dict[str, Any]],
    fresh_max_age_seconds: float = FRESH_MAX_AGE_SECONDS,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Приводит сырую запись трекера к нормализованному виду.

    Ключевое решение слоя: is_live = (трек свежий) AND (не в депо).
    Только такие машины могут использоваться для расчёта ETA и для фразы
    «виходи зараз». Остальные отдаются клиенту со статусом stale/depo,
    чтобы UI мог показать «⚪ за розкладом» вместо живой метки.
    """
    now = as_kyiv(now) if now is not None else now_kyiv()

    gps_dt = parse_gpstime(raw.get("gpstime"))
    age_seconds: Optional[float] = None
    if gps_dt is not None:
        age_seconds = max(0.0, (now - gps_dt).total_seconds())

    in_depo = bool(raw.get("inDepo"))
    is_fresh = age_seconds is not None and age_seconds <= fresh_max_age_seconds

    if in_depo:
        status = "depo"
    elif is_fresh:
        status = "live"
    elif age_seconds is None:
        status = "unknown"
    else:
        status = "stale"

    route_id = int(to_float(raw.get("routeId")))
    vehicle_type = vehicle_type_from_id(raw.get("idBusTypes"))
    route_name = str(raw.get("routeName") or "").strip()
    route_colour = str(raw.get("routeColour") or "").strip()

    return {
        "imei": str(raw.get("imei") or ""),
        "vehicle_id": int(to_float(raw.get("id"))),
        "board_number": str(raw.get("busNumber") or "").strip(),
        "vehicle_type": vehicle_type,
        "route_id": route_id,
        # routeName у троллейбусов бывает "T", поэтому подпись маршрута
        # берём из справочника, когда он доступен.
        "route_name": route_name,
        "route_label": (route_meta or {}).get("name") or route_name,
        "route_colour_name": route_colour,
        "route_colour_hex": colour_to_hex(route_colour or (route_meta or {}).get("colour_name", "")),
        "lat": to_float(raw.get("lat")),
        "lon": to_float(raw.get("lng")),
        "speed_kmh": to_float(raw.get("speed")),
        "heading_deg": to_float(raw.get("orientation")),
        "gpstime": gps_dt.strftime("%Y-%m-%d %H:%M:%S") if gps_dt else None,
        "age_seconds": None if age_seconds is None else round(age_seconds, 1),
        "in_depo": in_depo,
        "is_live": status == "live",
        "status": status,
        # Служебные поля: нужны для отладки и для поддержки перевозчика.
        "carrier": str(raw.get("perevName") or "").strip(),
        "remark": str(raw.get("remark") or "").strip(),
        "source_online_flag": bool(raw.get("online")),
    }


# ---------------------------------------------------------------------------
# Поллер живого слоя
# ---------------------------------------------------------------------------

class LiveTracker:
    """
    Асинхронный поллер живого GPS-слоя.

    Жизненный цикл: создаётся один раз при старте FastAPI (lifespan), внутри
    держит постоянный HTTP-клиент и фоновую задачу опроса. Наружу отдаёт
    данные только через snapshot(), который возвращает уже нормализованный
    и отфильтрованный срез — клиент не должен знать, что под капотом есть
    «сырые» треки и отдельный справочник маршрутов.
    """

    def __init__(
        self,
        base_url: str = LIVE_SOURCE_BASE,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
        fresh_max_age_seconds: float = FRESH_MAX_AGE_SECONDS,
        http_timeout_seconds: float = HTTP_TIMEOUT_SECONDS,
        routes_ttl_seconds: float = ROUTES_TTL_SECONDS,
    ):
        self.base_url = base_url.rstrip("/")
        self.poll_interval_seconds = poll_interval_seconds
        self.fresh_max_age_seconds = fresh_max_age_seconds
        self.http_timeout_seconds = http_timeout_seconds
        self.routes_ttl_seconds = routes_ttl_seconds

        # HTTP-клиент и фоновая задача создаются в start().
        self._client: Optional[httpx.AsyncClient] = None
        self._task: Optional[asyncio.Task] = None

        # Справочник маршрутов: ключ — routeId, значение — нормализованная
        # запись. version храним отдельно, чтобы понимать, когда перевозчик
        # реально поменял справочник.
        self._routes: Dict[int, Dict[str, Any]] = {}
        self._route_versions: Dict[int, int] = {}
        self._routes_fetched_at: float = 0.0

        # Последний удачный срез трекера (уже нормализованный).
        self._vehicles: List[Dict[str, Any]] = []
        self._last_success_at: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._poll_count: int = 0

    # -- Жизненный цикл --------------------------------------------------

    async def start(self) -> None:
        """Поднимает HTTP-клиент, прогревает справочник и запускает опрос."""
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.http_timeout_seconds),
            headers={"User-Agent": "TransGPS-Chernivtsi/1.0 (live-layer)"},
        )

        # Справочник прогреваем сразу, но падение источника на старте не
        # должно мешать серверу подняться: живой слой просто останется
        # без подписей маршрутов до первого успешного опроса.
        try:
            await self.refresh_routes(force=True)
        except Exception as exc:  # намеренно широкий перехват
            logger.warning("Живой слой: справочник маршрутов недоступен на старте: %s", exc)

        # Первый опрос делаем сразу и дожидаемся его: иначе запросы к /api/live
        # в первые секунды жизни сервера получали бы пустой срез — фоновая
        # задача ещё не успела сходить в сеть. Ошибка сети и тут не критична.
        try:
            await self.poll_once()
        except Exception as exc:  # источник внешний и нестабильный
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Живой слой: первый опрос трекера не удался: %s", self._last_error)

        self._task = asyncio.create_task(self._poll_loop(), name="transgps-live-poll")
        logger.info(
            "Живой слой запущен: %s, опрос каждые %.1f с, порог свежести %.0f с.",
            self.base_url, self.poll_interval_seconds, self.fresh_max_age_seconds,
        )

    async def stop(self) -> None:
        """Останавливает опрос и закрывает HTTP-клиент."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        if self._client is not None:
            await self._client.aclose()
            self._client = None

        logger.info("Живой слой остановлен (успешных опросов за сессию: %d).", self._poll_count)

    async def _poll_loop(self) -> None:
        """Бесконечный цикл опроса: ошибка одного тика не должна ломать цикл."""
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # источник внешний и нестабильный
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("Живой слой: ошибка опроса трекера: %s", self._last_error)

            await asyncio.sleep(self.poll_interval_seconds)

    # -- Работа с источником ---------------------------------------------

    async def _fetch_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """GET к источнику с проверкой статуса и разбором JSON."""
        if self._client is None:
            raise RuntimeError("LiveTracker не запущен: сначала вызовите start()")

        response = await self._client.get(f"{self.base_url}{path}", params=params)
        response.raise_for_status()
        return response.json()

    async def refresh_routes(self, force: bool = False) -> Dict[int, Dict[str, Any]]:
        """
        Обновляет справочник маршрутов (автобусы + троллейбусы).

        Справочник меняется редко, поэтому без force он перечитывается
        только когда истёк ROUTES_TTL_SECONDS — либо когда в потоке трекера
        встретился неизвестный routeId (см. poll_once).
        """
        fresh_enough = self._routes and (time.monotonic() - self._routes_fetched_at) < self.routes_ttl_seconds
        if not force and fresh_enough:
            return self._routes

        routes: Dict[int, Dict[str, Any]] = {}
        versions: Dict[int, int] = {}

        for bus_type_id, bus_type in ((BUS_TYPE_ID, "bus"), (TROLLEY_TYPE_ID, "trolley")):
            payload = await self._fetch_json(ROUTES_PATH.format(bus_type=bus_type_id))
            for route_id, raw_route in payload.items():
                if not isinstance(raw_route, dict):
                    continue
                meta = normalize_route(route_id, raw_route, bus_type)
                routes[meta["route_id"]] = meta
                versions[meta["route_id"]] = meta["version"]

        if versions != self._route_versions:
            logger.info(
                "Живой слой: справочник маршрутов обновлён (%d маршрутов: %d автобусных, %d троллейбусных).",
                len(routes),
                sum(1 for r in routes.values() if r["vehicle_type"] == "bus"),
                sum(1 for r in routes.values() if r["vehicle_type"] == "trolley"),
            )

        self._routes = routes
        self._route_versions = versions
        self._routes_fetched_at = time.monotonic()
        return routes

    async def poll_once(self) -> int:
        """Один тик опроса: тянет трекер и заменяет срез нормализованных ТС."""
        payload = await self._fetch_json(TRACKER_PATH, params={"selectedRoutesStr": ""})

        if not isinstance(payload, dict):
            raise ValueError(f"Неожиданный формат ответа трекера: {type(payload).__name__}")

        raw_vehicles = [raw for raw in payload.values() if isinstance(raw, dict)]

        # Если в потоке появился маршрут, которого нет в справочнике, —
        # перечитываем справочник, иначе подпись и цвет маршрута потеряются.
        known_route_ids = set(self._routes)
        seen_route_ids = {int(to_float(raw.get("routeId"))) for raw in raw_vehicles}
        if not known_route_ids or not seen_route_ids.issubset(known_route_ids):
            try:
                await self.refresh_routes(force=True)
            except Exception as exc:  # не критично для самого среза
                logger.warning("Живой слой: не удалось обновить справочник: %s", exc)

        now = now_kyiv()
        vehicles = [
            normalize_vehicle(
                raw,
                self._routes.get(int(to_float(raw.get("routeId")))),
                self.fresh_max_age_seconds,
                now,
            )
            for raw in raw_vehicles
        ]
        vehicles.sort(key=lambda v: (v["route_id"], v["board_number"]))

        self._vehicles = vehicles
        self._poll_count += 1
        self._last_success_at = now
        self._last_error = None
        return len(vehicles)

    # -- Публичное чтение данных -----------------------------------------

    def snapshot(
        self,
        only_fresh: bool = True,
        include_depo: bool = False,
        route_ids: Optional[List[int]] = None,
        vehicle_types: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Отдаёт готовый к отправке клиенту срез живого слоя.

        Параметры фильтрации:
            only_fresh     — оставить только машины со свежим треком
                             (age <= FRESH_MAX_AGE_SECONDS, inDepo=false);
            include_depo   — включать машины в депо (по умолчанию скрыты);
            route_ids      — ограничить конкретными routeId источника;
            vehicle_types  — "bus" / "trolley".

        counts всегда считаются по полному срезу — чтобы клиент и отладка
        видели, сколько машин отфильтровано и почему.
        """
        all_vehicles = self._vehicles

        counts = {
            "total": len(all_vehicles),
            "live": sum(1 for v in all_vehicles if v["status"] == "live"),
            "stale": sum(1 for v in all_vehicles if v["status"] == "stale"),
            "in_depo": sum(1 for v in all_vehicles if v["status"] == "depo"),
            "unknown_gpstime": sum(1 for v in all_vehicles if v["status"] == "unknown"),
        }

        selected = all_vehicles
        if route_ids:
            wanted_routes = {int(r) for r in route_ids}
            selected = [v for v in selected if v["route_id"] in wanted_routes]
        if vehicle_types:
            wanted_types = {str(t).strip().lower() for t in vehicle_types}
            selected = [v for v in selected if v["vehicle_type"] in wanted_types]
        if not include_depo:
            selected = [v for v in selected if not v["in_depo"]]
        if only_fresh:
            selected = [v for v in selected if v["is_live"]]

        return {
            "source": self.base_url,
            "generated_at": format_kyiv(),
            "last_success_at": (
                self._last_success_at.strftime("%Y-%m-%d %H:%M:%S") if self._last_success_at else None
            ),
            "last_error": self._last_error,
            "poll_interval_seconds": self.poll_interval_seconds,
            "fresh_max_age_seconds": self.fresh_max_age_seconds,
            "counts": counts,
            "returned": len(selected),
            "routes": {str(rid): meta for rid, meta in sorted(self._routes.items())},
            "vehicles": selected,
        }

    # -- Служебное --------------------------------------------------------

    @property
    def route_count(self) -> int:
        """Сколько маршрутов в справочнике (используется в health-check)."""
        return len(self._routes)

    @property
    def poll_count(self) -> int:
        """Сколько успешных опросов трекера сделано с момента старта."""
        return self._poll_count

