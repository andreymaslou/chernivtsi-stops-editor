"""
Граф маршрутов Черновцов — основа для роутера и симуляции ТС.

Модуль читает уже собранные данные проекта и строит из них единый граф:

    scraped_data/route_bus_<маршрут>_<направление>.json
    scraped_data/route_trolley_<маршрут>_<направление>.json
        → последовательность остановок маршрута (order, name, lat, lon)

    stops.json
        → канонический список остановок с ID, который использует Locator
          (именно эти ID возвращает POST /api/route)

Что получается на выходе (см. build_graph):

    nodes      — канонические узлы: одна физическая остановка = один узел.
                 Узлы, стоящие вплотную (<= NODE_MERGE_METERS), сливаются:
                 посадка/высадка по разные стороны дороги — это одна точка
                 для пассажира, ради неё и сливаем.
    routes     — маршрут + направление: порядок узлов, сегменты с длиной
                 и временем хода, итоговая длина и время «туда».
                 shape сегмента — ПРЯМАЯ между остановками (геометрия дорог
                 не запрашивается: см. «Важные решения» ниже).
    transfers  — узлы, между которыми можно перейти пешком
                 (<= TRANSFER_MAX_METERS), с временем перехода.
    index      — обратные ссылки: какие маршруты проходят через узел.

Важные решения:
    * Остановки «на вимогу» НЕ выбрасываются, а помечаются generic=true:
      их нельзя отдавать как ответ текстового поиска (человек не назовёт
      такую остановку), но они нужны в графе, иначе развалится цепочка
      сегментов и потеряется связность маршрута.
    * Имена маршрутов у нас официальные («19», «6A»), а у trans-gps своё
      id-пространство и свои подписи («19», «6/6a»). Связываем по имени
      функцией match_live_route_name(), чтобы «первый нужный ТС» можно
      было найти среди живых/симулированных машин.
    * Модель времени намеренно простая и калибруемая: длина сегмента
      (гаверсинус × DETOUR_FACTOR) / COMMERCIAL_SPEED_KMH + выдержка
      на остановке DWELL_SECONDS. Коэффициенты вынесены в параметры,
      чтобы позже подставить измеренные по GPS значения.
    * Геометрия сегмента — прямая между остановками (две точки). Внешняя
      служба (OSRM) не нужна: сборка графа не ходит в сеть, результат
      детерминирован, а петли/чужие улицы невозможны по построению.
      Длину и время это не меняет — они и раньше считались по прямой
      с DETOUR_FACTOR, а форма дороги использовалась только для рисования.
"""

import json
import logging
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from time_utils import now_kyiv

logger = logging.getLogger("transgps-graph")

# ---------------------------------------------------------------------------
# Параметры модели (калибруются по реальным GPS-трейсам)
# ---------------------------------------------------------------------------

# Коэффициент извилистости: расстояние «по прямой между остановками» короче
# реального пути по улицам.
DETOUR_FACTOR = 1.25

# Средняя эксплуатационная скорость по городу, км/ч.
COMMERCIAL_SPEED_KMH = 20.0

# Выдержка на остановке, секунд.
DWELL_SECONDS = 25.0

# Узлы ближе этого расстояния считаются одной физической остановкой.
# 20м: покрывает GPS-погрешность между остановками направлений А/Б (~7-13м),
# но не сливает физически разные остановки (было 80м — слишком агрессивно).
NODE_MERGE_METERS = 20.0

# Максимальное расстояние пешего перехода между разными остановками.
# 250 м (было 150): при 150 м в графе не было половины реальных связок —
# например «Училище №15» ↔ «Поліклініка» (узлы 202↔144, 204 м), из-за чего
# прямой автобус 20 не попадал в выдачу вообще (docs/BRIEF-plan-variants.md §1.7).
# При 250 м пар становится на 124 больше (47 из них — соседние остановки одной
# ветки), поэтому порог обязан применяться ВМЕСТЕ с фильтрами в build_transfers.
# Замеры и решения: §12.1, §12.4 брифа.
TRANSFER_MAX_METERS = 250.0

# Порог, при котором граф собирался ДО расширения. Рёбра внутри него остаются
# как есть (фильтры к ним не применяем): они часть уже принятого поведения, и их
# удаление ломает реальные планы — замер: «Онколікарня → вул. Гетьмана Дорошенка»
# был 75 мин / 20 грн / 0 пересадок, стал 112 мин / 60 грн / 2 пересадки.
# Фильтры нужны только для новой полосы 150–250 м (там 54 пары из 124 —
# «соседние остановки одной ветки» или дубли групп).
TRANSFER_LEGACY_MAX_METERS = 150.0

# Скорость пешехода при пересадке, км/ч.
WALK_SPEED_KMH = 4.5

# Радиус привязки узла графа к канонической остановке из stops.json.
STOP_ID_MATCH_METERS = 60.0

# Геометрия сегмента — ПРЯМАЯ между остановками (координаты EasyWay из
# scraped_data/, ровно как их рисует редактор маршрутов app.js). OSRM-форму
# дорог убрали: петли и «левые улицы» не стоят усложнения, а прямые линии
# пользователя полностью устраивают (см. docs/STATUS.md, п. 19).

# Шаг пространственной сетки: 0.002° ≈ 220 м по широте — при обходе 3×3
# клеток это ~440 м, с запасом покрывает и слияние, и пересадки.
GRID_CELL_DEGREES = 0.002

# Слова, по которым остановка считается «неназываемой» (generic).
GENERIC_MARKERS = ("на вимогу", "на вимогу", "за запитом", "на замовлення")

# Радиус остановочной группы: узлы с одинаковым (нормализованным) названием
# в этих пределах — одна точка посадки. В Черновцах, например, «пл. Соборна»
# это ДВА разных узла: от одного идут маршруты 1, 3, 5-троллейбус, а от
# другого — 9A и 9 на Гравітон. Пассажир на площади просто переходит дорогу,
# поэтому роутер обязан рассматривать всю группу, а не один узел.
GROUP_NAME_MAX_METERS = 400.0

# Служебные слова в названиях: не несут смысла для сопоставления,
# поэтому «пл. Соборна» и «Соборна площа» дают один и тот же ключ.
GROUP_NAME_NOISE = (
    "пл", "площа", "вул", "вулиця", "просп", "проспект", "пров", "провулок",
    "ст", "станція", "м", "н", "мікрорайон", "набережна",
)


def normalize_stop_name(name: Optional[str]) -> str:
    """
    Приводит название остановки к ключу для группировки.

    Убирает пунктуацию, номера маршрутов в скобках («вул. Садова (35)»)
    и служебные слова («пл.», «вул.»), чтобы «пл. Соборна» и «Соборна площа»
    склеились в один ключ «соборна».
    """
    text = str(name or "").lower()
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"[^0-9a-zа-яіїєґ\s]", " ", text)
    tokens = [token for token in text.split() if token and token not in GROUP_NAME_NOISE]
    return " ".join(tokens)



def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние по большому кругу в метрах (для дистанций города — ок)."""
    earth_radius = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * earth_radius * math.asin(math.sqrt(a))


def is_generic_stop_name(name: Optional[str]) -> bool:
    """True, если остановка не имеет собственного названия («на вимогу»)."""
    if not name:
        return True
    lowered = name.strip().lower()
    if not lowered:
        return True
    return any(marker in lowered for marker in GENERIC_MARKERS)


# ---------------------------------------------------------------------------
# Пространственный индекс и сопоставление имён маршрутов
# ---------------------------------------------------------------------------

class _SpatialGrid:
    """
    Сетка «клетка → индексы точек»: ищем соседей без честного O(n²).

    При масштабе города (тысячи остановок) полный перебор расстояний дал бы
    миллионы операций на каждую сборку графа — сетка сокращает это до
    нескольких сравнений на точку.
    """

    def __init__(self, cell_degrees: float = GRID_CELL_DEGREES):
        self.cell = cell_degrees
        self.cells: Dict[Tuple[int, int], List[int]] = {}

    def add(self, index: int, lat: float, lon: float) -> None:
        self.cells.setdefault(self._key(lat, lon), []).append(index)

    def candidates(self, lat: float, lon: float) -> List[int]:
        """Индексы точек в своей клетке и в восьми соседних."""
        cell_i, cell_j = self._key(lat, lon)
        found: List[int] = []
        for delta_i in (-1, 0, 1):
            for delta_j in (-1, 0, 1):
                found.extend(self.cells.get((cell_i + delta_i, cell_j + delta_j), ()))
        return found

    def _key(self, lat: float, lon: float) -> Tuple[int, int]:
        return int(math.floor(lat / self.cell)), int(math.floor(lon / self.cell))


# Официальные подписи маршрутов иногда содержат кириллицу («8А», «15К»),
# а живой источник отдаёт латиницу — приводим к общему виду.
_TRANSLIT = {
    "а": "a", "в": "b", "с": "c", "е": "e", "к": "k", "м": "m",
    "н": "h", "о": "o", "р": "p", "т": "t", "х": "x", "у": "y", "і": "i",
}


def route_tokens(value: Optional[str]) -> List[str]:
    """
    Разбивает подпись маршрута на нормализованные токены.

    Примеры: "6A" → ["6a"]; "6/6a" → ["6", "6a"]; "15К" → ["15k"].
    """
    if not value:
        return []
    text = "".join(_TRANSLIT.get(ch, ch) for ch in str(value).strip().lower())
    return [token for token in re.split(r"[^0-9a-z]+", text) if token]


def _token_score(
    ours: str,
    live: str,
    first_position: bool,
    allow_letter_suffix: bool,
) -> Optional[int]:
    """
    Насколько токен живого источника соответствует нашему: меньше — лучше.

    Точное совпадение — идеально. Совпадение «по цифрам» допускается только
    для троллейбусов (allow_letter_suffix): у них источник дописывает тип
    транспорта («1» → «1T», «8» → «8T»). Для автобусов буква меняет маршрут
    по существу: «9» и «9A» — это два разных маршрута, их путать нельзя.
    """
    if ours == live:
        base = 0
    elif allow_letter_suffix and _digits_prefix(ours) and _digits_prefix(ours) == _digits_prefix(live):
        base = 1
    else:
        return None
    return base + (0 if first_position else 1)


def _digits_prefix(token: str) -> str:
    """Ведущие цифры токена: «6a» → «6», «9a» → «9», «a» → «»."""
    match = re.match(r"^\d+", token or "")
    return match.group(0) if match else ""


def match_live_route_name(
    graph_name: str,
    live_route_names: Iterable[str],
    vehicle_type: str = "bus",
) -> Optional[str]:
    """
    Находит подпись маршрута у живого источника по нашей официальной.

    У trans-gps своё id-пространство и свои подписи («6/6a» вместо «6A»),
    поэтому «первый нужный ТС» ищем не по id, а по нормализованному имени.

    vehicle_type важен: для троллейбусов источник дописывает тип («1T», «6/6a»),
    и совпадение «по цифрам» допустимо; для автобусов буквенный суффикс —
    это отдельный маршрут, поэтому требуется точное совпадение.

    Возвращает подпись источника или None, если такого маршрута там нет —
    это нормальная ситуация: GPS стоит не на всех маршрутах, и такие
    маршруты обслуживает только симулятор.
    """
    ours_tokens = route_tokens(graph_name)
    if not ours_tokens:
        return None

    allow_letter_suffix = str(vehicle_type).lower() == "trolley"

    best: Optional[Tuple[int, str]] = None
    for live_name in live_route_names or ():
        live_tokens = route_tokens(live_name)
        if not live_tokens:
            continue

        total = 0
        matched = True
        for position, our_token in enumerate(ours_tokens):
            best_for_token: Optional[int] = None
            for live_token in live_tokens:
                score = _token_score(our_token, live_token, position == 0, allow_letter_suffix)
                if score is not None and (best_for_token is None or score < best_for_token):
                    best_for_token = score
            if best_for_token is None:
                matched = False
                break
            total += best_for_token

        if not matched:
            continue
        # Штраф за «лишние» токены источника: «6/6a» точнее для «6», чем что-то длиннее.
        total += len(live_tokens) - len(ours_tokens)
        candidate = (total, str(live_name))
        if best is None or candidate < best:
            best = candidate

    return best[1] if best else None



# ---------------------------------------------------------------------------
# Чтение исходных данных
# ---------------------------------------------------------------------------

def load_route_directions(scraped_dir: Path) -> List[Dict[str, Any]]:
    """
    Читает типизированные файлы маршрутов: route_bus_* и route_trolley_*.

    Legacy-файлы route_<номер>_<направление>.json намеренно игнорируются:
    в их именах не отличить автобус от троллейбуса, и они дублируют
    типизированные (проверено ранее — 62 файла, все дубликаты).
    """
    routes: List[Dict[str, Any]] = []

    for vehicle_type, prefix in (("bus", "route_bus_"), ("trolley", "route_trolley_")):
        for path in sorted(scraped_dir.glob(f"{prefix}*.json")):
            suffix = path.stem[len(prefix):]  # например "23_A"
            if "_" not in suffix:
                continue
            route_name, direction = suffix.rsplit("_", 1)

            try:
                raw_stops = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Граф: файл %s не прочитан: %s", path.name, exc)
                continue

            if not isinstance(raw_stops, list) or len(raw_stops) < 2:
                logger.warning("Граф: файл %s пропущен (меньше двух остановок)", path.name)
                continue

            ordered = sorted(
                (s for s in raw_stops if isinstance(s, dict) and s.get("lat") is not None),
                key=lambda s: s.get("order", 0),
            )
            routes.append({
                "vehicle_type": vehicle_type,
                "route_name": route_name,
                "direction": direction,
                "file": path.name,
                "stops": ordered,
            })

    logger.info(
        "Граф: прочитано %d направлений (%d автобусных, %d троллейбусных файлов).",
        len(routes),
        sum(1 for r in routes if r["vehicle_type"] == "bus"),
        sum(1 for r in routes if r["vehicle_type"] == "trolley"),
    )
    return routes


def load_canonical_stops(stops_path: Path) -> List[Dict[str, Any]]:
    """
    Читает stops.json — канонические остановки с ID для Locator.

    Именно эти ID возвращает POST /api/route, поэтому узлы графа обязаны
    ссылаться на них (поле node["stop_id"]).
    """
    try:
        data = json.loads(stops_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Граф: stops.json не прочитан (%s) — узлы будут без stop_id", exc)
        return []
    return [s for s in data if isinstance(s, dict) and s.get("lat") is not None]


# ---------------------------------------------------------------------------
# Канонизация остановок в узлы графа
# ---------------------------------------------------------------------------

def _pick_node_name(names: Sequence[Optional[str]]) -> str:
    """
    Выбирает имя узла из вариантов названий его остановок.

    Правило: реальные (не generic) названия важнее служебных, среди них —
    самое частое, при равенстве — самое короткое. Так «Соборна площа»
    побеждает длинное «пл. Соборна (на вимогу)».
    """
    cleaned = [str(n).strip() for n in names if n and str(n).strip()]
    real_names = [n for n in cleaned if not is_generic_stop_name(n)]
    pool = real_names or cleaned
    if not pool:
        return "Зупинка на вимогу"

    counts: Dict[str, int] = {}
    for name in pool:
        counts[name] = counts.get(name, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], len(item[0]), item[0]))[0][0]


def canonicalize_stops(raw_stops: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[int]]:
    """
    Сливает близкие остановки в канонические узлы графа.

    Алгоритм: union-find по пространственной сетке — все остановки в радиусе
    NODE_MERGE_METERS попадают в один узел (посадка и высадка по разные
    стороны дороги — одна точка для пассажира). Координаты узла — центр
    группы, имя — самое представительное из вариантов (см. _pick_node_name),
    а все варианты сохраняются в aliases: это заготовка под сленг
    («Тралка» → «Театральна площа»).

    Возвращает (узлы, карту «индекс исходной остановки → node_id»).
    """
    if not raw_stops:
        return [], []

    grid = _SpatialGrid()
    for index, stop in enumerate(raw_stops):
        grid.add(index, float(stop["lat"]), float(stop["lon"]))

    parent = list(range(len(raw_stops)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[max(root_a, root_b)] = min(root_a, root_b)

    for index, stop in enumerate(raw_stops):
        lat, lon = float(stop["lat"]), float(stop["lon"])
        for other in grid.candidates(lat, lon):
            if other <= index:
                continue
            other_stop = raw_stops[other]
            distance = haversine_m(lat, lon, float(other_stop["lat"]), float(other_stop["lon"]))
            if distance <= NODE_MERGE_METERS:
                union(index, other)

    groups: Dict[int, List[int]] = {}
    for index in range(len(raw_stops)):
        groups.setdefault(find(index), []).append(index)

    nodes: List[Dict[str, Any]] = []
    index_to_node = [0] * len(raw_stops)
    # Обход групп по возрастанию корня — node_id детерминированы между запусками.
    for root in sorted(groups):
        members = groups[root]
        names = [raw_stops[i].get("name") for i in members]
        name = _pick_node_name(names)
        lat = sum(float(raw_stops[i]["lat"]) for i in members) / len(members)
        lon = sum(float(raw_stops[i]["lon"]) for i in members) / len(members)

        node_id = len(nodes)
        nodes.append({
            "node_id": node_id,
            "name": name,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "aliases": sorted({str(n).strip() for n in names if n and str(n).strip()}),
            "generic": is_generic_stop_name(name),
            "stop_id": None,
            "group_id": None,
            "routes": [],
        })
        for member in members:
            index_to_node[member] = node_id

    return nodes, index_to_node


def attach_stop_ids(
    nodes: List[Dict[str, Any]],
    canonical_stops: List[Dict[str, Any]],
    match_meters: float = STOP_ID_MATCH_METERS,
) -> Dict[str, int]:
    """
    Привязывает узлы графа к каноническим остановкам stops.json.

    Это связующее звено между графом и Locator: POST /api/route возвращает
    ID из stops.json, а роутер работает с узлами — без привязки они друг
    друга «не видят» (именно из-за этого раньше маршрут не строился).
    """
    if not canonical_stops:
        return {"matched": 0, "unmatched": len(nodes)}

    grid = _SpatialGrid()
    for index, stop in enumerate(canonical_stops):
        grid.add(index, float(stop["lat"]), float(stop["lon"]))

    matched = 0
    for node in nodes:
        node_stop_id: Optional[int] = None
        best_distance = match_meters
        for candidate in grid.candidates(node["lat"], node["lon"]):
            stop = canonical_stops[candidate]
            distance = haversine_m(node["lat"], node["lon"], float(stop["lat"]), float(stop["lon"]))
            if distance <= best_distance:
                node_stop_id, best_distance = stop.get("id"), distance
        node["stop_id"] = node_stop_id
        if node_stop_id is not None:
            matched += 1

    return {"matched": matched, "unmatched": len(nodes) - matched}


# ---------------------------------------------------------------------------
# Построение графа
# ---------------------------------------------------------------------------

# fetch_osrm_shapes() удалена вместе с OSRM: геометрия сегмента — прямая между
# остановками (см. build_graph). Кеш data/osrm_cache.json больше не создаётся.


def segment_minutes(meters: float) -> float:
    """
    Время проезда сегмента между двумя остановками.

    Модель: путь / эксплуатационная скорость + выдержка на остановке.
    Коэффициенты вынесены в константы модуля — их заменим на измеренные
    по реальным GPS-трейсам, когда накопим статистику.
    """
    travel_minutes = (meters / 1000.0) / COMMERCIAL_SPEED_KMH * 60.0
    return round(travel_minutes + DWELL_SECONDS / 60.0, 3)


def route_chain_pairs(routes: Dict[str, Dict[str, Any]]) -> Set[Tuple[int, int]]:
    """
    Пары узлов, соседние в цепочке одного направления («одна ветка»).

    Это НЕ пересадка, а «выйти и дойти до следующей остановки того же маршрута»:
    переход по прямой короче проезда, поэтому роутер начинал ходить пешком
    вместо поездки. При пороге 150 м таких пар в графе не было, при 250 м их
    47 из 124 новых (замеры §12.1 брифа) — отсекаем.
    """
    pairs: Set[Tuple[int, int]] = set()
    for route in routes.values():
        chain = route["stops"]
        for left, right in zip(chain, chain[1:]):
            pairs.add((left, right) if left < right else (right, left))
    return pairs


def build_transfers(
    nodes: List[Dict[str, Any]],
    chain_pairs: Optional[Set[Tuple[int, int]]] = None,
    group_of: Optional[Dict[int, int]] = None,
    node_routes: Optional[Dict[int, Set[str]]] = None,
    stats: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """
    Пешие пересадки между разными узлами (<= TRANSFER_MAX_METERS).

    Узлы, слитые в один (ближе NODE_MERGE_METERS), сюда не попадают: переход
    внутри одного узла — это просто выход из машины на той же остановке.
    Остаются реальные пары «остановка на другой стороне / на соседней улице»,
    между которыми пассажир дойдёт пешком.

    Фильтры (нужны при пороге 250 м, см. docs/BRIEF-plan-variants.md §12.1) и
    применяются ТОЛЬКО к полосе > TRANSFER_LEGACY_MAX_METERS:
        chain_pairs — пары, соседние в цепочке одного направления. Пропускаем их
            ТОЛЬКО если на обоих концах тот же набор маршрутов (node_routes):
            тогда пассажир просто проедет, а пешком ходить нечего. Если наборы
            разные — это реальный вариант сесть на маршрут, который идёт лишь от
            соседней остановки (кейс «Училище №15» ↔ «Поліклініка», §1.7 — без
            этого ребра у узла 202 ноль соседей и прямой 20-й недостижим);
        group_of — «узел → группа»: внутри одной группы роутер и так ходит пешком
            (router_layer._expand_to_boardable), явное ребро — дубль группы.
    Рёбра внутри старой полосы остаются без изменений — см. комментарий к
    TRANSFER_LEGACY_MAX_METERS.
    """
    skipped_chain = 0
    skipped_group = 0
    legacy_chain = 0
    legacy_group = 0

    grid = _SpatialGrid()
    for node in nodes:
        grid.add(node["node_id"], node["lat"], node["lon"])

    transfers: List[Dict[str, Any]] = []
    for node in nodes:
        for other_id in grid.candidates(node["lat"], node["lon"]):
            if other_id <= node["node_id"]:
                continue
            other = nodes[other_id]
            distance = haversine_m(node["lat"], node["lon"], other["lat"], other["lon"])
            if distance > TRANSFER_MAX_METERS:
                continue
            chain_key = (node["node_id"], other_id) if node["node_id"] < other_id else (other_id, node["node_id"])
            same_routes = False
            if node_routes is not None:
                same_routes = node_routes.get(node["node_id"], set()) == node_routes.get(other_id, set())
            is_chain = bool(chain_pairs) and chain_key in chain_pairs and same_routes
            group = group_of.get(node["node_id"]) if group_of is not None else None
            is_group = group is not None and group == group_of.get(other_id)
            if distance <= TRANSFER_LEGACY_MAX_METERS:
                # Старая полоса: ребро оставляем, но считаем «мусорные» для отчёта.
                legacy_chain += 1 if is_chain else 0
                legacy_group += 1 if is_group else 0
            else:
                if is_chain:
                    skipped_chain += 1
                    continue
                if is_group:
                    skipped_group += 1
                    continue
            transfers.append({
                "from": node["node_id"],
                "to": other_id,
                "meters": round(distance, 1),
                "walk_minutes": round((distance / 1000.0) / WALK_SPEED_KMH * 60.0, 2),
            })

    if stats is not None:
        stats["transfers_skipped_chain"] = skipped_chain
        stats["transfers_skipped_group"] = skipped_group
        stats["transfers_legacy_chain_kept"] = legacy_chain
        stats["transfers_legacy_group_kept"] = legacy_group
    return transfers


def build_groups(
    nodes: List[Dict[str, Any]],
    max_meters: float = GROUP_NAME_MAX_METERS,
) -> List[Dict[str, Any]]:
    """
    Собирает остановочные группы — «одна точка посадки» для пассажира.

    Группируем узлы с одинаковым нормализованным названием, если они ближе
    GROUP_NAME_MAX_METERS. Внутри группы пассажир спокойно переходит пешком,
    поэтому роутер вправе выбрать ЛЮБОЙ узел группы как точку посадки.

    Зачем это нужно на практике: «пл. Соборна» в данных — два разных узла
    (node 26 и node 150), и маршрут 9A на «Завод Гравітон» отходит только от
    второго. Без групп ответ на фразу «я на соборці, треба на гравітон» был бы
    «прямого маршрута нет», что для пассажира просто неверно.
    """
    by_key: Dict[str, List[Dict[str, Any]]] = {}
    for node in nodes:
        key = normalize_stop_name(node["name"])
        if key:
            by_key.setdefault(key, []).append(node)

    groups: List[Dict[str, Any]] = []

    for key in sorted(by_key):
        members = by_key[key]
        if len(members) < 2:
            continue

        # Single-linkage по расстоянию: близкие узлы одного названия — в одну группу.
        parent = list(range(len(members)))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for first in range(len(members)):
            for second in range(first + 1, len(members)):
                distance = haversine_m(
                    members[first]["lat"], members[first]["lon"],
                    members[second]["lat"], members[second]["lon"],
                )
                if distance <= max_meters:
                    root_first, root_second = find(first), find(second)
                    if root_first != root_second:
                        parent[max(root_first, root_second)] = min(root_first, root_second)

        clusters: Dict[int, List[Dict[str, Any]]] = {}
        for index in range(len(members)):
            clusters.setdefault(find(index), []).append(members[index])

        for root in sorted(clusters, key=lambda r: min(m["node_id"] for m in clusters[r])):
            cluster = sorted(clusters[root], key=lambda m: m["node_id"])
            if len(cluster) < 2:
                continue

            max_pair_meters = max(
                haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
                for position, a in enumerate(cluster)
                for b in cluster[position + 1:]
            )
            groups.append({
                "group_id": len(groups),
                "key": key,
                "name": _pick_node_name([m["name"] for m in cluster]),
                "node_ids": [m["node_id"] for m in cluster],
                "lat": round(sum(m["lat"] for m in cluster) / len(cluster), 6),
                "lon": round(sum(m["lon"] for m in cluster) / len(cluster), 6),
                "radius_meters": round(max_pair_meters, 1),
                "max_walk_minutes": round((max_pair_meters / 1000.0) / WALK_SPEED_KMH * 60.0, 2),
            })

    return groups


def build_graph(
    scraped_dir: Path,
    stops_path: Path,
    live_route_names: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, Any]:
    """
    Собирает полный граф маршрутов из файлов проекта.

    Шаги:
        1. читаем все направления маршрутов и stops.json;
        2. канонизируем остановки в узлы и привязываем ID из stops.json;
        3. для каждого направления строим цепочку узлов и сегменты;
        4. считаем пешие пересадки между узлами;
        5. заполняем обратный индекс «узел → какие маршруты через него идут»;
        6. сопоставляем наши имена маршрутов с подписями живого источника
           (если переданы), чтобы потом находить «первый нужный ТС».

    live_route_names — необязательный словарь вида {"bus": [...], "trolley": [...]}
    с подписями маршрутов из /map/routes/1|2 (см. live_layer).
    """
    route_directions = load_route_directions(scraped_dir)
    canonical_stops = load_canonical_stops(stops_path)

    # Плоский список всех остановок всех направлений + карта «направление → индексы».
    raw_stops: List[Dict[str, Any]] = []
    per_direction_indices: List[List[int]] = []
    for direction in route_directions:
        indices: List[int] = []
        for stop in direction["stops"]:
            indices.append(len(raw_stops))
            raw_stops.append({
                "name": stop.get("name"),
                "lat": float(stop["lat"]),
                "lon": float(stop["lon"]),
            })
        per_direction_indices.append(indices)

    nodes, index_to_node = canonicalize_stops(raw_stops)
    id_stats = attach_stop_ids(nodes, canonical_stops)

    routes: Dict[str, Dict[str, Any]] = {}
    for direction, indices in zip(route_directions, per_direction_indices):
        node_chain: List[int] = []
        for raw_index in indices:
            node_id = index_to_node[raw_index]
            # Соседние остановки могли слиться в один узел — дубли убираем,
            # иначе получим сегмент нулевой длины.
            if node_chain and node_chain[-1] == node_id:
                continue
            node_chain.append(node_id)

        segments: List[Dict[str, Any]] = []
        length_m = 0.0
        for left, right in zip(node_chain, node_chain[1:]):
            left_node, right_node = nodes[left], nodes[right]
            straight = haversine_m(left_node["lat"], left_node["lon"], right_node["lat"], right_node["lon"])
            meters = straight * DETOUR_FACTOR
            length_m += meters
            segments.append({
                "from": left,
                "to": right,
                "meters": round(meters, 1),
                "minutes": segment_minutes(meters),
            })

        key = f'{direction["vehicle_type"]}:{direction["route_name"]}:{direction["direction"]}'
        routes[key] = {
            "key": key,
            "vehicle_type": direction["vehicle_type"],
            "route_name": direction["route_name"],
            "direction": direction["direction"],
            "source_file": direction["file"],
            "stops": node_chain,
            "segments": segments,
            "stops_count": len(node_chain),
            "length_km": round(length_m / 1000.0, 2),
            "one_way_minutes": round(sum(s["minutes"] for s in segments), 1),
            "live_route_name": None,
        }

    # --- Геометрия сегмента: прямая между остановками ---
    # Раньше здесь запрашивалась форма дорог у OSRM (кеш — data/osrm_cache.json).
    # Отказались: линии рисуются ровно как в редакторе маршрутов (app.js) —
    # [[lat, lon], [lat, lon]] по координатам EasyWay. Никакой сети, никаких
    # петель и «левых улиц»; длина/время сегмента как и раньше считаются по
    # прямой с DETOUR_FACTOR, так что модель времени не изменилась.
    for route in routes.values():
        for seg in route["segments"]:
            left_node = nodes[seg["from"]]
            right_node = nodes[seg["to"]]
            seg["shape"] = [
                [left_node["lat"], left_node["lon"]],
                [right_node["lat"], right_node["lon"]],
            ]

    # Обратный индекс: через какие маршруты проходит узел.
    for key, route in routes.items():
        for node_id in route["stops"]:
            if key not in nodes[node_id]["routes"]:
                nodes[node_id]["routes"].append(key)

    # Остановочные группы: узлы одного названия в шаговой доступности.
    # Собираем ДО пересадок: внутри одной группы пассажир и так переходит пешком
    # (router_layer._expand_to_boardable), поэтому явные рёбра внутри группы —
    # дубли, а «соседние остановки одной ветки» — не пересадка вовсе (§12.1).
    groups = build_groups(nodes)
    node_groups: Dict[int, int] = {}
    for group in groups:
        for node_id in group["node_ids"]:
            node_groups[node_id] = group["group_id"]
    for node in nodes:
        node["group_id"] = node_groups.get(node["node_id"])

    transfer_stats: Dict[str, int] = {}
    transfers = build_transfers(
        nodes,
        chain_pairs=route_chain_pairs(routes),
        group_of=node_groups,
        node_routes={node["node_id"]: set(node["routes"]) for node in nodes},
        stats=transfer_stats,
    )

    if live_route_names:
        for route in routes.values():
            route["live_route_name"] = match_live_route_name(
                route["route_name"],
                live_route_names.get(route["vehicle_type"], []),
                route["vehicle_type"],
            )

    return {
        "generated": now_kyiv().strftime("%Y-%m-%d %H:%M:%S"),
        "params": {
            "detour_factor": DETOUR_FACTOR,
            "commercial_speed_kmh": COMMERCIAL_SPEED_KMH,
            "dwell_seconds": DWELL_SECONDS,
            "node_merge_meters": NODE_MERGE_METERS,
            "transfer_max_meters": TRANSFER_MAX_METERS,
            "walk_speed_kmh": WALK_SPEED_KMH,
            "stop_id_match_meters": STOP_ID_MATCH_METERS,
        },
        "stats": {
            "raw_stops": len(raw_stops),
            "nodes": len(nodes),
            "generic_nodes": sum(1 for n in nodes if n["generic"]),
            "groups": len(groups),
            "nodes_in_groups": len(node_groups),
            "routes": len(routes),
            "transfers": len(transfers),
            "transfers_skipped_chain": transfer_stats.get("transfers_skipped_chain", 0),
            "transfers_skipped_group": transfer_stats.get("transfers_skipped_group", 0),
            "transfers_legacy_chain_kept": transfer_stats.get("transfers_legacy_chain_kept", 0),
            "transfers_legacy_group_kept": transfer_stats.get("transfers_legacy_group_kept", 0),
            "stop_ids_matched": id_stats["matched"],
            "stop_ids_unmatched": id_stats["unmatched"],
        },
        "nodes": {str(n["node_id"]): n for n in nodes},
        "routes": routes,
        "transfers": transfers,
        "groups": {str(g["group_id"]): g for g in groups},
    }


def save_graph(graph: Dict[str, Any], path: Path) -> Path:
    """Сохраняет граф в JSON (utf-8, без ASCII-экранирования кириллицы)."""
    path.write_text(json.dumps(graph, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info(
        "Граф сохранён: %s (узлов %d, маршрутов %d, пересадок %d)",
        path, graph["stats"]["nodes"], graph["stats"]["routes"], graph["stats"]["transfers"],
    )
    return path


def load_graph(path: Path) -> Dict[str, Any]:
    """Читает ранее собранный graph.json (пересборка на каждый запрос не нужна)."""
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    base_dir = Path(__file__).resolve().parent
    built = build_graph(base_dir / "scraped_data", base_dir / "stops.json")
    save_graph(built, base_dir / "graph.json")
    print(json.dumps(built["stats"], ensure_ascii=False, indent=2))
