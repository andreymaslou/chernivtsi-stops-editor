"""
Роутер маршрутів Черновцов: план поїздки від зупинки A до B.

Працює поверх графа (graph.json): вузли, маршрути-напрямки, піші пересадки,
зупинкові групи. На вхід — stop_id зі stops.json (їх дає POST /api/route),
на вихід — план у термінах емулятора:

    {"mode": "plan", "transfers": N, "total_min": N, "price_grn": N,
     "legs": [
        {"type": "transit", "vehicle": "bus", "route": "9A",
         "from": "пл. Соборна", "to": "Завод Гравітон",
         "path": [[lat, lon], ...], "travel_min": N, "wait_min": N,
         "live_bus": "1423", "eta": 5.0, "vehicle_state": "live",
         "color": "#ff00ff", "price_grn": 20},
        {"type": "transfer", "kind": "transfer"|"walk", "at": "вул. Головна",
         "walk_min": N, "wait_min": N}],
     "vehicles": [...живі ТС на задіяних маршрутах...]}

Алгоритм — Дейкстра по станах (маршрут_напрямок, вузол):
  * геопошук зупиняється на stop_id, граф живе вузлами; зв'язок — stop_id та
    зупинкові групи («пл. Соборна» = два вузли, 9A на Ґравітон іде від другого);
  * зі стану можна їхати далі по маршруту (ціна — час сегментів) або вийти й
    сісти на інший маршрут на цьому ж вузлі / вузлі групи / у радіусі пішої
    пересадки (ціна — пішки + очікування);
  * очікування — за розкладом (перший/останній рейс, інтервал) з уточненням
    живим GPS: якщо по маршруту в наш бік їде машина — це «перший потрібний ТС».

Обмеження: інтервали усереднені, час у дорозі модельний (сегменти графа),
напрями А/Б за розкладом не розрізняємо (за живими даними — лише у наш бік).
"""

import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

# Скільки пересадок шукаємо максимум (0-2 за ТЗ).
MAX_TRANSFERS = 2

# Далі цієї відстані від маршрутної ланцюжка машину не «прив'язуємо».
MAX_LIVE_SNAP_METERS = 600.0

# Наскільки машині «дозволено» поїхати раніше за пасажира (світлофор, похибка
# GPS-треку). Менше значення — суворіша перевірка «встигаю чи ні».
BOARD_TOLERANCE_MIN = 0.5

# Відсів зустрічних ТС (Напрямок Б на маршруті А) — docs/BRIEF-router-direction.md.
# Допуск на кут між курсом машини й азимутом сегмента: 90° — «їде не туди»,
# менше — поворот, шум GPS або короткий сегмент (тоді перевірка не діє).
OPPOSITE_HEADING_TOLERANCE_DEG = 90.0
# Перевірка курсу має сенс лише для рухомої машини на довгому сегменті:
# у стоячої (світлофор, кінцева) курс — випадкове число.
HEADING_CHECK_MIN_SPEED_KMH = 5.0
HEADING_CHECK_MIN_SEGMENT_M = 50.0

# Резервна прив'язка зупинки до вузла графа за координатами, якщо саме цей
# запис stops.json у граф не потрапив (дублікати однієї зупинки).
MAX_STOP_SNAP_METERS = 200.0

# Якщо ні живого GPS, ні розкладу — середнє очікування за замовчуванням.
DEFAULT_WAIT_MINUTES = 8.0

# Пішохідна швидкість, км/год.
WALK_SPEED_KMH = 4.5


class TransitRouter:
    """Маршрутизатор на графі Черновців."""

    def __init__(
        self,
        graph: Dict[str, Any],
        schedule: Optional[Dict[str, Any]] = None,
        stops: Optional[Sequence[Dict[str, Any]]] = None,
        assume_in_service: bool = False,
    ):
        raw_nodes: Dict[str, Dict[str, Any]] = graph["nodes"]
        self.routes: Dict[str, Dict[str, Any]] = graph["routes"]
        self.nodes: Dict[int, Dict[str, Any]] = {
            int(nid): node for nid, node in raw_nodes.items()
        }

        # Ланцюжки зупинок + префіксні суми часів сегментів: travel(a,b)=prefix[b]-prefix[a].
        self.route_stops: Dict[str, List[int]] = {}
        self.route_pos: Dict[str, Dict[int, int]] = {}
        self.route_prefix: Dict[str, List[float]] = {}
        # Координаты остановок маршрута — в том же порядке, что route_stops.
        # Привязка живого ТС к ближайшей остановке идёт в горячем цикле, где
        # поиск узла в dict по id занимал заметное время: держим готовый список.
        self.route_coords: Dict[str, List[Tuple[float, float]]] = {}
        # Нормализованная подпись маршрута для сверки с меткой ТС перевозчика.
        # Считаем один раз на маршрут, а не в каждом сравнении с машиной.
        self._route_wanted_norm: Dict[str, str] = {}
        # Тип ТС маршрута («bus»/«trolley»). Номера маршрутов у автобусов и
        # троллейбусов независимы: «5» есть и там, и там — это два разных
        # маршрута с разными ланцюжками, поэтому подпись для сверки с парком
        # всегда дополняется типом (см. set_live).
        self.route_vehicle_type: Dict[str, str] = {}
        # Напрямок маршруту (A/B) — щоб відсіяти ТС, які їдуть у інший бік
        # (див. docs/BRIEF-router-direction.md).
        self.route_direction: Dict[str, str] = {}
        # Азимут і довжина кожного сегмента ланцюжка: потрібні для перевірки
        # курсу ТС — реальний трекер поля `direction` не віддає, тому напрямок
        # доводиться визначати за курсом (precompute, щоб не рахувати в циклі).
        self.route_bearings: Dict[str, List[float]] = {}
        self.route_segment_m: Dict[str, List[float]] = {}
        for key, route in self.routes.items():
            chain = [int(node) for node in route["stops"]]
            self.route_stops[key] = chain
            self.route_pos[key] = {node: index for index, node in enumerate(chain)}
            prefix = [0.0]
            for segment in route.get("segments", []):
                prefix.append(prefix[-1] + float(segment.get("minutes", 0.0)))
            self.route_prefix[key] = prefix

            # --- route_coords: розгортаємо shape-точки сегментів ---
            # shape сегмента — пряма між двумя остановками (OSRM вимкнено),
            # тому coords зазвичай == зупинки ланцюжка. Код лишається загальним:
            # якщо геометрію знову розширять, розгортання працює так само.
            # Перша точка кожного наступного сегмента = остання попереднього,
            # тому дубль не додаємо (overlap=1).
            segments = route.get("segments", [])
            coords: List[Tuple[float, float]] = []
            # stop_coord_indices[i] = індекс у coords для i-ї зупинки в chain
            stop_coord_indices: List[int] = []

            if segments:
                for seg_idx, seg in enumerate(segments):
                    shape = seg.get("shape") or []
                    if not shape:
                        # Fallback: просто вузлові координати
                        from_node = seg["from"]
                        to_node = seg["to"]
                        fn = self.nodes.get(from_node, {})
                        tn = self.nodes.get(to_node, {})
                        shape = [
                            [fn.get("lat", 0.0), fn.get("lon", 0.0)],
                            [tn.get("lat", 0.0), tn.get("lon", 0.0)],
                        ]

                    if seg_idx == 0:
                        stop_coord_indices.append(len(coords))
                        for pt in shape:
                            coords.append((float(pt[0]), float(pt[1])))
                    else:
                        # Перша точка сегмента = остання попередня — пропускаємо
                        stop_coord_indices.append(len(coords) - 1)
                        for pt in shape[1:]:
                            coords.append((float(pt[0]), float(pt[1])))

                # Остання зупинка ланцюжка
                stop_coord_indices.append(len(coords) - 1)
            else:
                # Маршрут без сегментів — старий спосіб
                for node in chain:
                    stop_coord_indices.append(len(coords))
                    nd = self.nodes.get(node, {})
                    coords.append((float(nd.get("lat", 0.0)), float(nd.get("lon", 0.0))))

            self.route_coords[key] = coords
            # Зберігаємо індекси зупинок у route_coords (для path-slicing)
            self.route_stop_indices: Dict[str, List[int]] = getattr(self, "route_stop_indices", {})
            self.route_stop_indices[key] = stop_coord_indices

            wanted = route.get("live_route_name") or route.get("route_name") or ""
            self._route_wanted_norm[key] = self.normalize_label(str(wanted))
            self.route_vehicle_type[key] = str(route.get("vehicle_type") or "bus").strip().lower()
            # Напрямок беремо з ключа («trolley:2:A»), а не з полів ТС: це
            # властивість маршруту, і вона є навіть коли трекер її не віддає.
            self.route_direction[key] = self.normalize_direction(key.split(":")[-1])
            bearings: List[float] = []
            lengths: List[float] = []
            for index in range(len(chain) - 1):
                lat1, lon1 = self.route_coords[key][stop_coord_indices[index]]
                lat2, lon2 = self.route_coords[key][stop_coord_indices[index + 1]]
                bearings.append(self._bearing_deg(lat1, lon1, lat2, lon2))
                lengths.append(self._haversine_m(lat1, lon1, lat2, lon2))
            self.route_bearings[key] = bearings
            self.route_segment_m[key] = lengths


        # Вузол -> маршрути, що через нього проходять.
        self.node_routes: Dict[int, Set[str]] = {node: set() for node in self.nodes}
        for key, chain in self.route_stops.items():
            for node in chain:
                self.node_routes[node].add(key)

        # Піші пересадки: (a,b) -> хвилин (неорієнтовано).
        self.transfer_walk: Dict[Tuple[int, int], float] = {}
        for edge in graph.get("transfers", []):
            a, b = int(edge["from"]), int(edge["to"])
            minutes = float(edge.get("walk_minutes", 1.0))
            self.transfer_walk[(a, b)] = minutes
            self.transfer_walk[(b, a)] = minutes

        # Сусіди по пішій пересадці: вузол -> [(вузол, хвилин)]. Індекс потрібен,
        # щоб _expand_to_boardable не сканував увесь transfer_walk (було O(V·E):
        # при 264 пересадках це давало +50% до p50 —
        # tools/perf/compare_snapshots.py f1_before f1_after).
        self.transfer_neighbors: Dict[int, List[Tuple[int, float]]] = {}
        for (a, b), minutes in self.transfer_walk.items():
            self.transfer_neighbors.setdefault(a, []).append((b, minutes))

        # Зупинкові групи.
        self.node_group: Dict[int, Optional[int]] = {}
        self.group_nodes: Dict[int, List[int]] = {}
        for gid, group in graph.get("groups", {}).items():
            members = [int(n) for n in group.get("node_ids", [])]
            self.group_nodes[int(gid)] = members
            for node in members:
                self.node_group[node] = int(gid)
        for node in self.nodes:
            self.node_group.setdefault(node, None)

        # stop_id (stops.json) -> вузли графа.
        self.stop_to_nodes: Dict[int, List[int]] = {}
        for node in self.nodes.values():
            if node.get("stop_id") is not None:
                self.stop_to_nodes.setdefault(int(node["stop_id"]), []).append(
                    int(node["node_id"])
                )

        self.schedule: Dict[str, Dict[str, Dict[str, Any]]] = schedule or {}
        self._live_vehicles: List[Dict[str, Any]] = []
        # Источник среза парка: "real" | "sim" | None (не задан — тесты/инструменты).
        self._fleet_source: Optional[str] = None
        # Индекс «нормализованная подпись маршрута -> машины». Строится в
        # set_live(), чтобы поиск «першого потрібного ТС» не перебирал парк.
        self._live_by_route: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        # Момент, на который построен срез живого парка. Нужен, чтобы понять,
        # успевает ли пассажир на конкретную машину: срез «сейчас», а посадка
        # будет через несколько минут после старта поездки.
        self._live_snapshot_at: Optional[datetime] = None
        # Версия среза парка: растёт вместе с подписью геометрии в set_live().
        # По ней «стареют» записи пространственного кэша (см.
        # _approaching_vehicles).
        self._fleet_version = 0
        # Подпись геометрии парка (см. _fleet_fingerprint): если новый срез
        # принёс те же позиции, кэш остаётся валидным.
        self._fleet_key: Optional[Tuple[Any, ...]] = None
        # Кэш пространственной части поиска «першого потрібного ТС»:
        # (маршрут, вузол посадки) -> (версия парка, [машины за зростанням eta]).
        # Геометрия (прив'язка ТС до ланцюжка зупинок) від часу пасажира не
        # залежить, поэтому считается один раз на срез парка, а не на каждый
        # заход Дейкстры (Шаг 4: haversine давал ~33 тыс. вызовов на один
        # plan() замість тысяч).
        self._spatial_cache: Dict[
            Tuple[str, int], Tuple[int, List[Dict[str, Any]]]
        ] = {}
        # Тестовий режим: всі маршрути вважаємо в роботі незалежно від часу
        # доби (перший/останній рейс ігноруються, очікування — за інтервалом).
        self.assume_in_service = assume_in_service

        # Координаты остановок из stops.json (после правок сленга). Нужны для
        # резервной привязки stop_id -> узел: в stops.json встречаются дубли
        # одной и той же остановки (например, «пл. Театральна» #110 и #4524),
        # а в граф привязывается только одна из записей. Если stop_id не
        # нашёл своего узла — доклеиваем ближайший по координатам.
        self.stop_coords: Dict[int, Tuple[float, float]] = {}
        for raw_stop in stops or []:
            if not isinstance(raw_stop, dict) or raw_stop.get("lat") is None or raw_stop.get("lon") is None:
                continue
            stop_id = int(raw_stop["id"])
            if stop_id in self.stop_coords:
                continue
            self.stop_coords[stop_id] = (float(raw_stop["lat"]), float(raw_stop["lon"]))

    # Поля ТС, от которых зависит пространственный кэш (_approaching_vehicles):
    # позиция, скорость, подпись маршрута и то, что попадает в ответ. Остальные
    # поля среза на геометрию не влияют.
    #
    # `direction` и `heading_deg` — исключение, которое обязано быть здесь: от
    # них зависит, попадёт машина в кандидаты. Машина, яка на кінцевій
    # розвернулася и стоит на месте, имеет те же lat/lon/speed/board, и без этих
    # полей в подписи кэш остался бы с геометрией старого направления.
    # `vehicle_type` — из того же ряда: кандидат ищется по паре (тип, подпись),
    # так что смена типа машины обязана сбрасывать кэш.
    # `source` не влияет на геометрию, но определяет пометку ноги плана
    # (real|sim|sched): если та же машина пришла из другого источника
    # (PARK_SOURCE=auto добрал её симулятором), кэш обязан устареть — иначе
    # живой GPS подменится виртуальным парком без следов в ответе.
    _FLEET_KEY_FIELDS = (
        "lat", "lon", "speed_kmh", "board_number", "is_live", "route_label",
        "direction", "heading_deg", "vehicle_type", "source",
    )

    @classmethod
    def _fleet_fingerprint(
        cls, vehicles: Sequence[Dict[str, Any]]
    ) -> Tuple[Any, ...]:
        """Подпись геометрии парка: изменилась — записи кэша устарели."""
        fields = cls._FLEET_KEY_FIELDS
        return tuple(
            tuple(vehicle.get(field) for field in fields) for vehicle in vehicles
        )

    def set_live(
        self,
        vehicles: Sequence[Dict[str, Any]],
        snapshot_at: Optional[datetime] = None,
        source: Optional[str] = None,
    ) -> None:
        """
        Приймає нормалізований срез живого шару (список ТЗ).

        `source` — звідки прийшов срез: `"real"` (трекер) або `"sim"` (симулятор).
        Роутер не може визначити це сам: сим-машини приходять із `is_live = True`,
        тому сама лише позначка «живий» на стенді (`GPS_SIMULATOR=1`) вводить в
        оману. Значення потрапляє в ногу плану (`source`) і в телеметрію —
        контракт лога, див. `docs/BRIEF-plan-variants.md` §12.2 і
        `docs/REVIEW-f4-wait-display.md` §4.

        Заодно строит индекс «(тип ТС, нормализованная подпись маршрута) ->
        машины»: поиск «першого потрібного ТС» вызывается из Дейкстры тысячи
        раз за один запрос, поэтому перебор всего парка на каждый вызов заменён
        на выборку из словаря. Тип ТС обязан входить в ключ: номера маршрутов
        у автобусов и троллейбусов не пересекаются («5» = два разных маршрута),
        и по голой подписи они бы склеились в одну кучу.

        `snapshot_at` — момент, на который построен срез. Для симулятора это
        время, переданное в `snapshot(now=...)` (парк строится относительно
        него), для реального трекера — «сейчас».
        """
        self._live_vehicles = list(vehicles)
        self._live_snapshot_at = snapshot_at
        self._fleet_source = source
        # Сброс пространственного кэша — только если изменилась ГЕОМЕТРИЯ парка.
        # eta в записях кэша — величина относительная («через сколько минут от
        # позиции приедет»), от `snapshot_at` она не зависит, поэтому новый
        # опрос с теми же координатами не повод считать haversine заново.
        # Прод-эндпоинт зовёт set_live() на каждый запрос, а трекер отдаёт
        # последние опрошенные позиции: без этой проверки кэш был бы всегда
        # холодным. Версия парка растёт вместе с подписью — по ней «стареют»
        # записи (см. _approaching_vehicles).
        fingerprint = self._fleet_fingerprint(self._live_vehicles)
        if fingerprint != self._fleet_key:
            self._fleet_key = fingerprint
            self._fleet_version += 1
            self._spatial_cache.clear()
        # Ключ индекса — пара (тип ТС, подпись): «bus:5» и «trolley:5» —
        # разные маршруты, по одной только подписи они бы слились.
        by_route: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for vehicle in self._live_vehicles:
            label = vehicle.get("route_label") or vehicle.get("route_name") or ""
            key = self.normalize_label(str(label))
            if key:
                vtype = str(vehicle.get("vehicle_type") or "").strip().lower()
                by_route.setdefault((vtype, key), []).append(vehicle)
        self._live_by_route = by_route

    def _resolve_to_nodes(self, stop_id: int) -> List[int]:
        """
        stop_id -> вузли графа.

        Пряма прив'язка (node.stop_id) — основний шлях. Якщо в граф не
        потрапила саме ця запис зупинки (дублікат), шукаємо найближчий узел
        за координатами — від запасу в MAX_STOP_SNAP_METERS.
        """
        resolved = self.stop_to_nodes.get(int(stop_id))
        if resolved:
            return resolved
        coord = self.stop_coords.get(int(stop_id))
        if not coord:
            return []
        lat, lon = coord
        best: Optional[int] = None
        best_distance = MAX_STOP_SNAP_METERS
        for node in self.nodes.values():
            distance = self._haversine_m(lat, lon, node["lat"], node["lon"])
            if distance < best_distance:
                best, best_distance = int(node["node_id"]), distance
        return [best] if best is not None else []

    # ------------------------------------------------------------------
    # Головний публічний метод
    # ------------------------------------------------------------------

    def plan(
        self,
        from_stop_id: int,
        to_stop_id: int,
        now: Optional[datetime] = None,
        max_transfers: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Будує план від from_stop_id до to_stop_id; None — маршрут не знайдено.

        `max_transfers` — жорстке обмеження кількості пересадок для ЦЬОГО прогону
        (None → модульна константа `MAX_TRANSFERS`). Саме параметром, а не правкою
        константи: один обʼєкт `TransitRouter` обслуговує кілька потоків FastAPI,
        і глобальна правка — це гонка між запитами. Другим прогоном будуються
        варіанти плану (`build_variants`, поставка 1, §13 брифа).
        """
        now = now or datetime.now()
        from_nodes = self._resolve_to_nodes(int(from_stop_id))
        to_nodes = self._resolve_to_nodes(int(to_stop_id))
        if not from_nodes or not to_nodes:
            return None

        # Одна й та сама фізична зупинка (або вузли однієї групи) — їхати нікуди.
        from_set = set(from_nodes)
        direct_hit = next((n for n in to_nodes if n in from_set), None)
        if direct_hit is not None:
            return {
                "mode": "plan",
                "from_stop_id": int(from_stop_id),
                "to_stop_id": int(to_stop_id),
                "from_name": self.nodes[direct_hit]["name"],
                "to_name": self.nodes[to_nodes[0]]["name"],
                "transfers": 0,
                "total_min": 0,
                "price_grn": 0,
                "legs": [],
                "vehicles": [],
                "computed_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            }

        # Кэш ожиданий на время одного запроса. Дейкстра заходит в одни и те
        # же пары (маршрут, остановка) тысячи раз, и каждый заход заново
        # перебирал ТС и считал haversine. Кэш именно локальный, а не self.*:
        # один объект TransitRouter обслуживает несколько потоков FastAPI
        # (эндпоинт объявлен обычным def), поэтому общее поле дало бы гонку и
        # ответы «из другого времени».
        wait_cache: Dict[Tuple[str, int], Dict[str, Any]] = {}
        result = self._dijkstra(from_nodes, to_nodes, now, wait_cache, max_transfers=max_transfers)
        return result

    # Максимальное «удорожание временем» второго варианта: если он медленнее
    # дефолта больше, чем на столько минут, карточку не показываем — такой размен
    # человеку уже не интересен, а честнее сказать «другого варианта нет».
    # Замеры §12.6: медиана размена «≤1 пересадка» — +7.5 мин по 44 парам.
    VARIANT_MAX_SLOWDOWN_MIN = 20.0

    def build_variants(
        self,
        from_stop_id: int,
        to_stop_id: int,
        now: Optional[datetime] = None,
        default_plan: Optional[Dict[str, Any]] = None,
        second_max_transfers: int = 1,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """
        Собирает варианты плана для карточек (поставка 1, §13 брифа).

        Первый прогон — обычный (дефолт, минимум времени); его результат уже есть
        у вызывающего, поэтому принимаем его как `default_plan` и не считаем
        заново. Второй прогон — с жёстким ограничением `second_max_transfers`
        («≤1 пересадка»): меньше посадок → меньше тарифов, то есть «дешевле».

        Почему именно второй прогон, а не `transfer penalty`: штраф только
        искажает порядок по времени, а когда варианта нет вовсе — вернёт план с
        пересадкой, и карточка «Прямой» становится ложью (§11 брифа). Жёсткое
        ограничение даёт честный ответ «варианта сейчас нет».

        Схема аддитивная: корневой ответ не меняется, варианты лежат в
        `variants`, а выбранный по умолчанию остаётся в корне (совместимость с UI
        и pytest).

        Возвращает `(variants, note)`: список вариантов (первый — всегда дефолт) и
        человеческую пометку, когда второго варианта сейчас нет.
        """
        now = now or datetime.now()
        default = default_plan if default_plan is not None else self.plan(
            from_stop_id, to_stop_id, now=now
        )
        if default is None:
            return [], "маршрут не знайдено"
        variants = [self._variant_entry(default, "default")]
        if default.get("transfers", 0) <= second_max_transfers:
            return variants, (
                "другого варіанта зараз немає: маршрут уже з ≤%d пересадками"
                % second_max_transfers
            )

        second = self.plan(
            from_stop_id, to_stop_id, now=now, max_transfers=second_max_transfers
        )
        if second is None:
            return variants, (
                "другого варіанта зараз немає: із ≤%d пересадками маршрут не знайдено"
                % second_max_transfers
            )
        if (second["total_min"], second["price_grn"], second["transfers"]) == (
            default["total_min"], default["price_grn"], default["transfers"]
        ):
            return variants, "другого варіанта зараз немає: обидва прогони дали той самий план"
        slowdown = second["total_min"] - default["total_min"]
        if slowdown > self.VARIANT_MAX_SLOWDOWN_MIN:
            return variants, "другого варіанта зараз немає: він на %d хв довший" % slowdown
        variants.append(self._variant_entry(second, "fewer_transfers"))
        return variants, None

    @staticmethod
    def _variant_entry(plan: Dict[str, Any], variant_id: str) -> Dict[str, Any]:
        """
        Один вариант для карточки: цифры `(грн, хв)`, теги и ноги (для карты).

        Теги — короткий смысловой ярлык вместо чтения цифр (§11): «Прямий» при
        0 пересадок, «Швидкий» у дефолта (он оптимум по времени по построению),
        «Дешевий» у второго варианта.
        """
        tags: List[str] = []
        if plan.get("transfers") == 0:
            tags.append("Прямий")
        tags.append("Швидкий" if variant_id == "default" else "Дешевий")
        return {
            "id": variant_id,
            "tags": tags,
            "total_min": plan.get("total_min"),
            "price_grn": plan.get("price_grn"),
            "transfers": plan.get("transfers"),
            "legs": plan.get("legs", []),
            "vehicles": plan.get("vehicles", []),
            "fleet_source": plan.get("fleet_source"),
        }

    # ------------------------------------------------------------------
    # Розширення: куди можна піти/сісти з вузла
    # ------------------------------------------------------------------

    def _expand_to_boardable(self, node: int) -> List[Tuple[int, float]]:
        """[(вузол, хвилин пішки)]: сам вузол, вузли своєї групи, піші пересадки."""
        options: List[Tuple[int, float]] = [(node, 0.0)]
        seen = {node}

        group_id = self.node_group.get(node)
        if group_id is not None:
            for mate in self.group_nodes.get(group_id, []):
                if mate not in seen:
                    seen.add(mate)
                    options.append((mate, self._walk_between(node, mate)))

        for other, minutes in self.transfer_neighbors.get(node, ()):
            if other not in seen:
                seen.add(other)
                options.append((other, minutes))

        return options

    def _walk_between(self, a: int, b: int) -> float:
        """Хвилин пішки між вузлами (явна пересадка або група)."""
        if a == b:
            return 0.0
        edge = self.transfer_walk.get((a, b))
        if edge is not None:
            return edge
        if self.node_group.get(a) is not None and self.node_group.get(a) == self.node_group.get(b):
            distance = self._haversine_m(
                self.nodes[a]["lat"], self.nodes[a]["lon"],
                self.nodes[b]["lat"], self.nodes[b]["lon"],
            )
            return (distance / 1000.0) / WALK_SPEED_KMH * 60.0
        return 0.0

    @staticmethod
    def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """
        Азимут сегмента (0° — північ, за годинниковою) — та сама формула, що в
        симуляторі (`sim_layer._position_at`), тому курс ТС зіставляється з ним
        напряму (і в симуляції, і в реальному трекері `orientation`).
        """
        mid_lat = math.radians((lat1 + lat2) / 2.0)
        dlat = lat2 - lat1
        dlon = (lon2 - lon1) * math.cos(mid_lat)
        if dlat == 0.0 and dlon == 0.0:
            return 0.0
        return math.degrees(math.atan2(dlon, dlat)) % 360.0

    @staticmethod
    def _heading_matches_route(
        vehicle: Dict[str, Any],
        coords: Sequence[Tuple[float, float]],
        bearings: Sequence[float],
        segments: Sequence[float],
        stop_indices: Sequence[int],
        index: int,
        snap_m: float,
    ) -> bool:
        """
        Чи дивиться ТС уздовж ланцюжка — у бік нашої зупинки.

        Курс порівнюємо з азимутом «від машини до наступного вузла ланцюжка», а
        не з азимутом усього сегмента: сегменти бувають по кілометру, і хорда
        такого сегмента на кривій розходиться з реальним напрямком дороги — на
        замірі це давало до 90° похибки й зустрічні машини проходили фільтр.
        Коли машина вже на самому вузлі (`snap_m` малий), беремо азимут
        сегмента, а якщо сегмент короткий — не перевіряємо взагалі.

        Перевірка діє лише там, де вона щось значить: рухома машина
        (`speed_kmh > HEADING_CHECK_MIN_SPEED_KMH`). У стоячої (світлофор,
        кінцева) курс — випадкове число, а без курсу в срезі ми взагалі не
        маємо права відкидати машину: тоді повертаємо True і працює старий
        геометричний підбір.
        """
        # index — індекс ЗУПИНКИ в ланцюжку (не точки route_coords), тому
        # наступний вузол ланцюжка беремо через stop_indices.
        if index + 1 >= len(stop_indices) or index >= len(bearings):
            return True
        speed = float(vehicle.get("speed_kmh") or 0.0)
        if speed < HEADING_CHECK_MIN_SPEED_KMH:
            return True
        heading = vehicle.get("heading_deg")
        lat, lon = vehicle.get("lat"), vehicle.get("lon")
        if heading is None or lat is None or lon is None:
            return True

        if snap_m < HEADING_CHECK_MIN_SEGMENT_M:
            if index >= len(segments) or segments[index] < HEADING_CHECK_MIN_SEGMENT_M:
                return True
            bearing = bearings[index]
        else:
            next_lat, next_lon = coords[stop_indices[index + 1]]
            bearing = TransitRouter._bearing_deg(
                float(lat), float(lon), next_lat, next_lon
            )

        delta = abs((float(heading) - bearing + 180.0) % 360.0 - 180.0)
        return delta <= OPPOSITE_HEADING_TOLERANCE_DEG

    @staticmethod
    def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        radius = 6371000.0
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = (
            math.sin(dphi / 2) ** 2
            + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        )
        return 2 * radius * math.asin(math.sqrt(a))

    # ------------------------------------------------------------------
    # Дейкстра по станах (маршрут, вузол)
    # ------------------------------------------------------------------

    def _dijkstra(
        self,
        from_nodes: List[int],
        to_nodes: List[int],
        now: datetime,
        wait_cache: Optional[Dict[Tuple[str, int], Dict[str, Any]]] = None,
        max_transfers: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Оптимальний маршрут (0-2 пересадки). Вартість — «хвилини від виходу
        з дому»: очікування + у дорозі + пішки. Усі ребра невід'ємні, тому
        перший цільовий стан, витягнутий з купи, є оптимальним.

        Ярлик стану — `(route_key, node)`: усе, від чого залежить майбутнє, бо єдина
        заборона — не сідати вдруге на той самий маршрут після виходу. Раніше тут
        відсіювались УСІ вже використані маршрути, а набір у ярлик не входив: тоді
        дешевший ярлик витісняє дорожчий із кращим набором, і оптимальний план
        губиться (саме так погіршився еталон day 12:00 `181→166`, 50 → 64 хв, після
        розширення графа до 250 м — `docs/REVIEW-f1-transfer-250m.md` §6). Варіант із
        набором у ярлику коректний, але дає ×2–4 станів і +59…93 % до p50 — занадто.
        """
        import heapq

        target_set = set(to_nodes)
        # Зона старту: самі вузли відправлення + їх групи/пересадки. Після
        # першої пересадки вертатися сюди безглуздо (петля «поїхав-приїхав»).
        origin_zone: Set[int] = set()
        for node in from_nodes:
            for board_node, _walk in self._expand_to_boardable(node):
                origin_zone.add(board_node)

        heap: List[Tuple[float, int, str, int]] = []
        dist: Dict[Tuple[str, int], float] = {}
        # prev[стан] = (попередній стан або None, вид, додатково)
        #   вид "start" -> (walk_min, wait_min)
        #   вид "ride"  -> ride_cost
        #   вид "board" -> (walk_min, wait_min, board_node)
        prev: Dict[Tuple[str, int], Any] = {}

        for origin_node in from_nodes:
            for board_node, walk_min in self._expand_to_boardable(origin_node):
                for route_key in self.node_routes.get(board_node, ()):
                    # Пассажир сначала идёт пешком до остановки и только потом
                    # ждёт ТС: ожидание считаем на момент прихода на остановку,
                    # а не на момент начала поездки.
                    board_time = now + timedelta(minutes=walk_min)
                    wait = self._wait_minutes(route_key, board_node, board_time, wait_cache)
                    if wait is None:
                        continue
                    cost = walk_min + wait
                    state = (route_key, board_node)
                    if cost < dist.get(state, math.inf):
                        dist[state] = cost
                        prev[state] = (None, "start", (walk_min, wait))
                        heapq.heappush(heap, (cost, 1, route_key, board_node))

        best_goal: Optional[Tuple[float, float, Tuple[Any, ...]]] = None
        max_boardings = (MAX_TRANSFERS if max_transfers is None else int(max_transfers)) + 1

        while heap:
            cost, boardings, route_key, node = heapq.heappop(heap)
            state = (route_key, node)
            if cost > dist.get(state, math.inf):
                continue

            # Чи можна завершити поїздку звідси (пішки до цільового вузла)?
            for alight, walk_min in self._expand_to_boardable(node):
                if alight in target_set:
                    total = cost + walk_min
                    if best_goal is None or total < best_goal[0]:
                        best_goal = (total, walk_min, state)
            if best_goal is not None and all(item[0] >= best_goal[0] for item in heap):
                break

            pos = self.route_pos[route_key].get(node)
            if pos is None:
                continue

            # 1) Їхати по маршруту до будь-якої наступної зупинки.
            stops = self.route_stops[route_key]
            prefix = self.route_prefix[route_key]
            for nxt_pos in range(pos + 1, len(stops)):
                nxt = stops[nxt_pos]
                ride_cost = prefix[nxt_pos] - prefix[pos]
                new_cost = cost + ride_cost
                nstate = (route_key, nxt)
                if new_cost < dist.get(nstate, math.inf):
                    dist[nstate] = new_cost
                    prev[nstate] = (state, "ride", ride_cost)
                    heapq.heappush(heap, (new_cost, boardings, route_key, nxt))

            # 2) Вийти і сісти на інший маршрут.
            if boardings >= max_boardings:
                continue
            for alight, walk_min in self._expand_to_boardable(node):
                if boardings >= 2 and alight in origin_zone:
                    continue  # не повертаємось до старту після першої пересадки
                for other in self.node_routes.get(alight, ()):
                    # Єдина заборонена петля: вийти й одразу сісти на той самий
                    # маршрут. Раніше тут ще відсіювались усі вже використані
                    # маршрути — саме це й ламало оптимальність (див. docstring).
                    if self._route_name_id(other) == self._route_name_id(route_key):
                        continue
                    # cost уже включает всю предыдущую поездку (ожидания и
                    # перегоны), поэтому посадка на следующую ногу произойдёт
                    # не «сейчас», а через cost плюс пеший переход.
                    board_time = now + timedelta(minutes=cost + walk_min)
                    wait = self._wait_minutes(other, alight, board_time, wait_cache)
                    if wait is None:
                        continue
                    new_cost = cost + walk_min + wait
                    nstate = (other, alight)
                    if new_cost < dist.get(nstate, math.inf):
                        dist[nstate] = new_cost
                        prev[nstate] = (state, "board", (walk_min, wait, alight))
                        heapq.heappush(heap, (new_cost, boardings + 1, other, alight))

        if best_goal is None:
            return None

        total_cost, goal_walk, goal_state = best_goal
        return self._build_plan(
            from_nodes, to_nodes, goal_state, prev, total_cost, goal_walk, now,
            wait_cache,
        )

    # ------------------------------------------------------------------
    # Збірка плану з відновленого шляху
    # ------------------------------------------------------------------

    def _build_plan(
        self,
        from_nodes: List[int],
        to_nodes: List[int],
        goal_state: Tuple[Any, ...],
        prev: Dict[Tuple[Any, ...], Any],
        total_cost: float,
        goal_walk: float,
        now: datetime,
        wait_cache: Optional[Dict[Tuple[str, int], Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Відновлює послідовність станів із prev і збирає legs.

        Стан — `(route_key, node, used)`, а `prev[стан] = (попередній стан, вид,
        додатково)`, тому шлях відновлюється за першим елементом запису.
        """
        sequence: List[Tuple[Any, ...]] = []
        current: Optional[Tuple[Any, ...]] = goal_state
        while current is not None:
            sequence.append(current)
            entry = prev.get(current)
            if entry is None or entry[1] == "start":
                break
            current = entry[0]
        sequence.reverse()

        legs: List[Dict[str, Any]] = []
        used_route_keys: Set[str] = set()
        price_grn = 0
        # Накопленное время от начала поездки до момента посадки на текущую
        # ногу. Нужно, чтобы wait_min/«перший ТС» в ответе считались на то же
        # время прибытия, что и в поиске (Дейкстра), иначе ответ врёт.
        elapsed_min = 0.0

        index = 0
        while index < len(sequence):
            route_key, board_node = sequence[index][0], sequence[index][1]
            used_route_keys.add(route_key)
            entry = prev.get(sequence[index])

            # Стартова прогулянка до місця посадки (група/пересадка).
            if entry is not None and entry[1] == "start":
                walk_min, _wait = entry[2]
                elapsed_min += walk_min
                if walk_min > 0.01:
                    legs.append(
                        self._walk_leg_name(self.nodes[board_node]["name"], walk_min, "walk")
                    )

            # Їдемо маршрутом до останнього підряд вузла цього маршруту.
            ride_nodes = [board_node]
            while index + 1 < len(sequence) and sequence[index + 1][0] == route_key:
                index += 1
                ride_nodes.append(sequence[index][1])
            board_time = now + timedelta(minutes=elapsed_min)
            leg = self._transit_leg(route_key, ride_nodes, board_time, wait_cache)
            legs.append(leg)
            price_grn += leg.get("price_grn", 0)
            elapsed_min += (leg.get("wait_min") or 0.0) + (leg.get("travel_min") or 0.0)

            # Пересадка на наступний маршрут.
            index += 1
            if index < len(sequence):
                nxt_key, nxt_node = sequence[index][0], sequence[index][1]
                if nxt_key != route_key:
                    walk_min, wait_min, _alight = prev[sequence[index]][2]
                    legs.append(
                        self._walk_leg_name(
                            self.nodes[nxt_node]["name"], walk_min, "transfer"
                        )
                    )
                    used_route_keys.add(nxt_key)
                    # Пеший переход уже случился: пассажир стоит на nxt_node
                    # спустя elapsed_min минут от начала поездки.
                    elapsed_min += walk_min
                    wait_info = self._wait_info(
                        nxt_key, nxt_node, now + timedelta(minutes=elapsed_min), wait_cache
                    )
                    legs[-1]["wait_min"] = round(wait_info.get("wait_min") or 0.0, 1)

        # Фінальна прогулянка до цільової зупинки (група/пересадка на фініші).
        if goal_walk > 0.01 and sequence:
            target_name = self.nodes[to_nodes[0]]["name"]
            legs.append(self._walk_leg_name(target_name, goal_walk, "walk"))

        transfers = max(0, len([leg for leg in legs if leg.get("kind") == "transfer"]))
        from_node = from_nodes[0]
        to_node = to_nodes[0]
        vehicles = self._vehicles_for_routes(used_route_keys)
        return {
            "mode": "plan",
            "from_stop_id": int(self.nodes[from_node]["stop_id"]),
            "to_stop_id": int(self.nodes[to_node]["stop_id"]),
            "from_name": self.nodes[from_node]["name"],
            "to_name": self.nodes[to_node]["name"],
            "transfers": transfers,
            "total_min": int(round(total_cost)),
            "price_grn": int(price_grn),
            "legs": legs,
            "vehicles": vehicles,
            # Источник парка и признак «в срезе вообще были живые ТС» — контракт
            # лога телеметрии (§12.2): без этого выборки ночью на симуляторе
            # невозможно отличить от предпочтений реальных пассажиров. В
            # смешанном режиме (PARK_SOURCE=auto) источник у каждой ноги свой
            # (поле source ноги), а корню говорим "mixed".
            "fleet_source": "mixed" if self._fleet_source == "auto"
            else (self._fleet_source or "unknown"),
            "had_live_data": bool(self._live_vehicles),
            "computed_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ------------------------------------------------------------------
    # Ноги плану
    # ------------------------------------------------------------------

    def _transit_leg(
        self,
        route_key: str,
        path_nodes: List[int],
        now: datetime,
        wait_cache: Optional[Dict[Tuple[str, int], Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Нога «їдемо маршрутом»: від першого до останнього вузла підряд."""
        route = self.routes.get(route_key) or {}
        prefix = self.route_prefix[route_key]
        pos_first = self.route_pos[route_key][path_nodes[0]]
        pos_last = self.route_pos[route_key][path_nodes[-1]]
        travel_min = max(0.0, prefix[pos_last] - prefix[pos_first])

        from_name = self.nodes[path_nodes[0]]["name"]
        to_name = self.nodes[path_nodes[-1]]["name"]
        # Дейкстра «їде» одразу до будь-якої наступної зупинки, тож у
        # відновленому шляху лишаються тільки посадка й висадка. Для карти
        # розгортаємо ногу в повну ланцюжок зупинок маршруту між ними —
        # інакше emulator.js малює хорду крізь пів міста замість маршруту
        # (координати вже пораховані: route_coords у тому ж порядку).
        # Переводимо індекси зупинок у індекси route_coords (з прямою
        # геометрією це тотожність, але мапінг лишаємо загальним).
        stop_indices = self.route_stop_indices.get(route_key, list(range(len(self.route_coords[route_key]))))
        coord_first = stop_indices[pos_first] if pos_first < len(stop_indices) else pos_first
        coord_last = stop_indices[pos_last] if pos_last < len(stop_indices) else pos_last

        path = [
            [lat, lon]
            for lat, lon in self.route_coords[route_key][coord_first : coord_last + 1]
        ]
        # Повна геометрія напрямку (від початкової кінцевої до кінцевої) — для
        # «хвостів» на карті: пасажир бачить, куди маршрут іде до і після його
        # ділянки. `path` завжди є НЕПЕРЕРВНИМ срізом `full_geom`
        # (`full_geom[coord_first:coord_last+1]`, це перевіряє
        # test_leg_full_geom_covers_active_path) — інакше хвіст не стикувався б
        # з активною лінією.
        full_geom = [[lat, lon] for lat, lon in self.route_coords[route_key]]

        # Проміжні зупинки ноги (між посадкою та висадкою, виключно) — окремим
        # масивом об'єктів із назвою та координатами. Крапки малюються лише за
        # координатами РЕАЛЬНИХ зупинок, а не за точками геометрії: якщо геометрію
        # колись знову розширять (форма доріг), крапка на кожній точці дала б
        # «пил» на карті.
        leg_coords = self.route_coords[route_key]
        stops: List[Dict[str, Any]] = []
        for position in range(pos_first + 1, pos_last):
            if 0 <= position < len(stop_indices):
                stop_node = self.route_stops[route_key][position]
                lat, lon = leg_coords[stop_indices[position]]
                stops.append({
                    "name": str(self.nodes[stop_node].get("name") or "Зупинка"),
                    "lat": lat,
                    "lon": lon,
                })

        wait = self._wait_info(route_key, path_nodes[0], now, wait_cache)
        wait_min = wait.get("wait_min") or 0.0

        price_grn = 0
        sched = self._schedule_for(route_key)
        if sched and sched.get("tariff"):
            price_grn = int(float(sched["tariff"]))

        live_route_name = route.get("live_route_name")
        route_vtype = str(route.get("vehicle_type") or "bus").strip().lower()
        colour = "#4f8cff"
        for vehicle in self._live_vehicles:
            # Тільки свій тип ТС: колір «5» автобуса не може прийти від
            # тролейбуса «5» (маршрути різні, хоч підпис і збігається).
            if str(vehicle.get("vehicle_type") or "").strip().lower() != route_vtype:
                continue
            if self._route_labels_match(
                str(vehicle.get("route_label") or ""), str(live_route_name or "")
            ):
                colour = vehicle.get("route_colour_hex") or colour
                break

        return {
            "type": "transit",
            "vehicle": route.get("vehicle_type", "bus"),
            "route": route.get("route_name", "?"),
            "from": from_name,
            "to": to_name,
            "path": path,
            "stops": stops,
            "full_geom": full_geom,
            "travel_min": round(travel_min, 1),
            "wait_min": round(wait_min, 1),
            "price_grn": price_grn,
            "live_bus": wait.get("live_bus"),
            "eta": wait.get("eta_min"),
            "vehicle_state": wait.get("vehicle_state"),
            # V3-поля (§12.2): расписанное и фактическое ожидание отдельно, чтобы
            # UI показывал обе цифры без спора, а телеметрия знала источник.
            "schedule_wait_min": None if wait.get("schedule_wait_min") is None
            else round(float(wait["schedule_wait_min"]), 1),
            "live_wait_min": None if wait.get("live_wait_min") is None
            else round(float(wait["live_wait_min"]), 1),
            # "real" | "sim" — борт найден и пришёл из соответствующего слоя,
            # "sched" — живого борта нет, цифра посчитана по расписанию.
            # В смешанном парке (PARK_SOURCE=auto) источник берём у самой
            # машины (wait["source"]), иначе — у всего среза.
            "source": "sched" if wait.get("live_bus") is None
            else (wait.get("source") or self._fleet_source or "unknown"),
            "color": colour,
        }

    def _walk_leg_name(self, at_name: str, walk_min: float, kind: str) -> Dict[str, Any]:
        """Нога пересадки або стартової/фінальної прогулянки."""
        return {
            "type": "transfer",
            "kind": kind,
            "at": at_name,
            "walk_min": round(float(walk_min), 1),
            "wait_min": 0.0,
        }

    # ------------------------------------------------------------------
    # Очікування і «перший потрібний ТС»
    # ------------------------------------------------------------------

    def _schedule_for(self, route_key: str) -> Optional[Dict[str, Any]]:
        route = self.routes.get(route_key)
        if not route:
            return None
        return (self.schedule.get(route["vehicle_type"]) or {}).get(route["route_name"])

    def _wait_minutes(
        self,
        route_key: str,
        board_node: int,
        now: datetime,
        cache: Optional[Dict[Tuple[str, int], Dict[str, Any]]] = None,
    ) -> Optional[float]:
        """Очікування (хв) першого потрібного ТС; None — маршрут зараз не ходить."""
        return self._wait_info(route_key, board_node, now, cache).get("wait_min")

    def _wait_info(
        self,
        route_key: str,
        board_node: int,
        now: datetime,
        cache: Optional[Dict[Tuple[str, int], Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Повна інформація про очікування: wait_min, eta, «перший ТС».

        cache (необов'язковий) живе в межах одного plan(): ключ — (маршрут,
        вузол посадки). Час «now» у ключ не входить навмисно: у межах одного
        розрахунку він сталий, а спільний між запитами кеш дав би відповідь
        «з іншого часу».
        """
        # Ключ включает момент посадки (с точностью до минуты): время поездки
        # стало честным, поэтому в один и тот же узел можно прийти в разное
        # время, и ожидание будет разным. Секунды отбрасываем — расписание
        # всё равно оперирует минутами, а hit-rate кэша сохраняется.
        cache_key = (route_key, board_node, now.replace(second=0, microsecond=0))
        if cache is not None:
            cached = cache.get(cache_key)
            if cached is not None:
                return dict(cached)

        route = self.routes.get(route_key) or {}
        sched = self._schedule_for(route_key)
        now_minutes = now.hour * 60 + now.minute

        headway_min: Optional[float] = None
        in_service = True
        if sched:
            interval = sched.get("interval") or {}
            lo, hi = interval.get("min"), interval.get("max")
            if lo and hi:
                headway_min = (float(lo) + float(hi)) / 2.0
            first = self._minutes_of_day(sched.get("first"))
            last = self._minutes_of_day(sched.get("last"))
            now_in_window = first is None or last is None or (first <= now_minutes <= last)
            if not now_in_window and not self.assume_in_service:
                no_service = {
                    "wait_min": None, "eta_min": None, "live_bus": None,
                    "vehicle_state": "не ходить", "headway_min": headway_min,
                    "schedule_wait_min": None, "live_wait_min": None,
                }
                if cache is not None:
                    cache[cache_key] = no_service
                return no_service

        base_wait = (headway_min / 2.0) if headway_min else DEFAULT_WAIT_MINUTES

        result: Dict[str, Any] = {
            "wait_min": base_wait,
            "eta_min": None,
            "live_bus": None,
            "vehicle_state": None,
            "headway_min": headway_min,
            # V3 (docs/REVIEW-f4-wait-display.md §3): отдаём обе цифры отдельно,
            # чтобы UI мог показать «5.2 хв (за розкладом)» и «живий борт — 6.9 хв»
            # вместо двух спорящих чисел. `wait_min` остаётся min(...) — план и
            # поиск не меняются.
            "schedule_wait_min": base_wait,
            "live_wait_min": None,
        }

        live = self._nearest_live_vehicle(route_key, board_node, now)
        if live is not None:
            result["eta_min"] = live["eta_min"]
            result["live_bus"] = live["live_bus"]
            result["vehicle_state"] = "live" if live["is_live"] else "за розкладом"
            # wait_min — очікування на самій зупинці (від прибуття пасажира),
            # тому порівнюємо його з розкладом, а не з ETA від моменту среза.
            live_wait = live.get("wait_min")
            result["live_wait_min"] = live_wait
            if live_wait is not None and live_wait < base_wait:
                result["wait_min"] = live_wait
            # Джерело борта: кожна машина змішаного парку несе своє
            # (merge_fleet у main.py); для моно-режиму fallback на source
            # всього среза — сим-машини мають is_live=True, тому по цьому
            # полю джерело не визначити (§12.2).
            result["source"] = live.get("source") or self._fleet_source
        if cache is not None:
            cache[cache_key] = result
        return result

    def _nearest_live_vehicle(
        self,
        route_key: str,
        board_node: int,
        board_time: datetime,
    ) -> Optional[Dict[str, Any]]:
        """
        Шукає на маршруті ТС, що їде в наш бік до board_node.

        Робота разделена на две части:

        * пространственная (`_approaching_vehicles`) — хто взагалі їде до цієї
          зупинки і через скільки хвилин: залежить лише від среза парка та
          маршруту, тому считается один раз и живёт в `_spatial_cache`;
        * временная (тут) — пассажир окажется на остановке в `board_time`, а
          срез парка снят в `_live_snapshot_at`. Машина, яка приїде РАНІШЕ за
          пасажира, не підходить — її пропускаємо (це і був баг «неможливих
          пересадок»).

        Список из кэша отсортирован по eta: «первый нужный ТС» — первая
        машина, которая успевает (ожидание на остановке `eta - arrive_min`
        монотонно по eta, поэтому отдельная сортировка не нужна).
        """
        # Скільки хвилин мине від моменту среза парка до нашої посадки.
        arrive_min = 0.0
        if self._live_snapshot_at is not None:
            arrive_min = max(
                0.0, (board_time - self._live_snapshot_at).total_seconds() / 60.0
            )

        for approach in self._approaching_vehicles(route_key, board_node):
            eta = approach["eta_min"]
            if eta < arrive_min - BOARD_TOLERANCE_MIN:
                continue  # машина поїде раніше, ніж пасажир дійде до зупинки
            # Очікування на самій зупинці: від нашого прибуття до машини.
            wait_after_arrival = max(0.0, eta - arrive_min)
            return {
                "eta_min": round(eta, 1),
                "wait_min": max(0.5, round(wait_after_arrival + 0.5, 1)),
                "live_bus": approach["live_bus"],
                "is_live": approach["is_live"],
                "route_label": approach["route_label"],
                # Источник конкретного борта; None — слой его не проставляет
                # (моно-режим), тогда сработает fleet-level source.
                "source": approach.get("source"),
            }
        return None

    def _approaching_vehicles(
        self,
        route_key: str,
        board_node: int,
    ) -> List[Dict[str, Any]]:
        """
        Машини маршруту, що їдуть у наш бік до board_node, за зростанням eta.

        Підпис маршруту графа (live_route_name) зіставляємо з route_label
        машини перевізника; потім прив'язуємо машину до найближчого вузла
        ланцюжка. Якщо вона стоїть ПЕРЕД нашою зупинкою — вона кандидат на
        посадку, а eta рахуємо за префіксними сумами сегментів плюс залишок
        шляху від поточної позиції до найближчої зупинки.

        Результат залежить лише від `(маршрут, вузол)` і поточного среза парка,
        але НЕ від часу посадки пасажира, тому він живёт в `_spatial_cache` до
        следующего `set_live()`. Записи помечены версией парка
        (`_fleet_version`): она растёт на каждом новом срезе, поэтому
        устаревшая геометрия не может попасть в ответ (важно для потоков
        FastAPI: один объект TransitRouter обслуживает несколько запросов).

        Зустрічні ТС відсіюються двома незалежними перевірками (ланцюжки A і B
        ідуть тими самими вулицями, тому геометрія сама їх не розрізняє):
        поле `direction` среза парка (є в симуляторі) і курс ТС проти азимуту
        сегмента (є й у реальному трекері, який `direction` не віддає).
        Деталі й заміри — docs/BRIEF-router-direction.md.
        """
        key = (route_key, board_node)
        cached = self._spatial_cache.get(key)
        if cached is not None and cached[0] == self._fleet_version:
            return cached[1]

        found: List[Dict[str, Any]] = []
        if route_key in self.routes and board_node in self.route_pos.get(route_key, {}):
            wanted_norm = self._route_wanted_norm.get(route_key, "")
            coords = self.route_coords[route_key]
            prefix = self.route_prefix[route_key]
            bearings = self.route_bearings[route_key]
            segments = self.route_segment_m[route_key]
            board_pos = self.route_pos[route_key][board_node]
            route_direction = self.route_direction.get(route_key, "")
            # Только машины своего типа: «5» автобус и «5» троллейбус —
            # разные маршруты, хотя подпись у них одинаковая.
            route_vtype = self.route_vehicle_type.get(route_key, "")

            for vehicle in self._live_by_route.get((route_vtype, wanted_norm), ()):
                # 1) Напрямок із среза парка (симулятор; трекер може віддати в
                #    майбутньому). Стоїть ДО haversine: відсіюємо найдешевше.
                vehicle_direction = self.normalize_direction(vehicle.get("direction"))
                if (
                    vehicle_direction
                    and route_direction
                    and vehicle_direction != route_direction
                ):
                    continue

                lat, lon = vehicle.get("lat"), vehicle.get("lon")
                if lat is None or lon is None:
                    continue

                vlat, vlon = float(lat), float(lon)
                # Грубый отсев в градусах ДО haversine: машину привязываем
                # только к остановкам ближе MAX_LIVE_SNAP_METERS (1° широты ≈
                # 111.32 км). Окно берём с запасом (долгота делится на cos
                # широты), поэтому отбрасываются только заведомо далёкие
                # точки — ответ не меняется.
                lat_window = MAX_LIVE_SNAP_METERS / 111320.0
                lon_window = lat_window / max(0.2, math.cos(math.radians(vlat)))
                # Прив'язка йде до ЗУПИНОК ланцюжка, а не до точок route_coords:
                # board_pos, route_prefix, route_bearings і route_segment_m живуть
                # у просторі зупинок (довжина == len(chain)). З прямою геометрією
                # coords == зупинки і простори збігаються, але мапінг через
                # stop_indices лишаємо: він коректний за будь-якої геометрії —
                # тільки тоді працюють і порівняння з board_pos, і зрізи
                # prefix/bearings/segments нижче.
                stop_indices = self.route_stop_indices.get(
                    route_key, list(range(len(coords)))
                )
                best_idx, best_dist = None, None
                for position in range(len(stop_indices)):
                    nlat, nlon = coords[stop_indices[position]]
                    if abs(nlat - vlat) > lat_window or abs(nlon - vlon) > lon_window:
                        continue
                    distance = self._haversine_m(vlat, vlon, nlat, nlon)
                    if best_dist is None or distance < best_dist:
                        best_idx, best_dist = position, distance
                if best_dist is None or best_dist > MAX_LIVE_SNAP_METERS:
                    continue
                if best_idx >= board_pos:
                    continue  # вже проїхала нашу зупинку

                # 2) Курс: ланцюжки A і B ідуть тими самими вулицями, тому
                #    зустрічна машина прив'язується до НАШОГО ланцюжка. Її курс
                #    протилежний азимуту сегмента — це єдина ознака напрямку в
                #    реальному трекері, який поля `direction` не віддає.
                if not self._heading_matches_route(
                    vehicle, coords, bearings, segments, stop_indices, best_idx, best_dist
                ):
                    continue

                minutes_away = prefix[board_pos] - prefix[best_idx]
                speed = float(vehicle.get("speed_kmh") or 20.0)
                partial_minutes = (
                    (best_dist / 1000.0) / speed * 60.0 if speed > 1.0 else 0.0
                )
                # eta — «через скільки хвилин від моменту среза приїде машина»;
                # саме цю величину UI показує як «буде ~N хв». Округляем только
                # на выходе: временной фильтр должен видеть сырое значение.
                eta = max(1.0, minutes_away + partial_minutes)
                found.append(
                    {
                        "eta_min": eta,
                        "live_bus": vehicle.get("board_number") or "?",
                        "is_live": bool(vehicle.get("is_live")),
                        "route_label": str(vehicle.get("route_label") or ""),
                        # Откуда пришёл борт: "real" | "sim" | None (старый слой
                        # не проставляет поле — тогда работает fleet-level source).
                        # Нужно, чтобы при PARK_SOURCE=auto нога плана не наврала
                        # об источнике машины (§3.3 п.3).
                        "source": vehicle.get("source"),
                    }
                )

            # Сортуємо за eta (сортировка устойчива): временной фильтр выше
            # возьмёт первую подходящую машину — это и есть «перший потрібний
            # ТС» для пассажира, а не просто машина с минимальной eta.
            found.sort(key=lambda item: item["eta_min"])

        self._spatial_cache[key] = (self._fleet_version, found)
        return found

    @staticmethod
    def _route_name_id(route_key: str) -> str:
        """«bus:9A:A» -> «bus:9A» — щоб не пересідати на інший напрямок того ж маршруту."""
        parts = route_key.split(":")
        if len(parts) >= 2:
            return f"{parts[0]}:{parts[1]}"
        return route_key

    @staticmethod
    def normalize_label(value: str) -> str:
        """
        Нормализует подпись маршрута для сверки с меткой ТС перевозчика.

        «6/6a» -> «6a», «2Т» -> «2t», «9A» -> «9a». Кириллические а/б/в
        приводим к латинице: в графе и в трекере они встречаются вперемешку.
        """
        return (
            value.strip().lower()
            .replace("/", "")
            .replace("а", "a").replace("б", "b").replace("в", "v")
        )

    @staticmethod
    def normalize_direction(value: Any) -> str:
        """
        Напрямок маршруту («A»/«B») у єдиному вигляді.

        Граф дає латинські A/B, але в підписах маршрутів проєкт уже зводить
        кирилицю до латиниці (див. normalize_label) — напрямок приходить із
        того самого світу. «Голе» порівняння було б небезпечним: кириличне «А»
        відкинуло б УСІ машини маршруту й `live_bus` став би вічно `null`.
        """
        if value is None:
            return ""
        text = str(value).strip().upper()
        return text.replace("А", "A").replace("Б", "B").replace("В", "V")

    @staticmethod
    def _route_labels_match(label: str, wanted: str) -> bool:
        """«6/6a»=«6A», «1»=«1», «9A»=«9A» — зіставляємо цифри й одну літеру."""
        if not label:
            return False
        norm = TransitRouter.normalize_label
        return norm(label) == norm(wanted)

    @staticmethod
    def _minutes_of_day(value: Any) -> Optional[float]:
        if not value:
            return None
        try:
            hours, minutes = str(value).strip().split(":")
            return int(hours) * 60 + int(minutes)
        except (ValueError, TypeError):
            return None

    def _vehicles_for_routes(self, route_keys: Set[str]) -> List[Dict[str, Any]]:
        """Живі машини на задіяних маршрутах — щоб емулятор показав їх на карті."""
        # Ключ — пара (тип ТС, нормалізована підпис): «5» є і в автобусів, і
        # у тролейбусів, і без типу на карту плану автобусного 5 потрапили б
        # тролейбуси 5 (і навпаки).
        wanted: Set[Tuple[str, str]] = set()
        for key in route_keys:
            route = self.routes.get(key) or {}
            label = route.get("live_route_name") or route.get("route_name")
            if label:
                vtype = str(route.get("vehicle_type") or "bus").strip().lower()
                wanted.add((vtype, self.normalize_label(str(label))))
        if not wanted:
            return []

        picked: List[Dict[str, Any]] = []
        for vehicle in self._live_vehicles:
            vtype = str(vehicle.get("vehicle_type") or "").strip().lower()
            label = str(vehicle.get("route_label") or vehicle.get("route_name") or "")
            if (vtype, self.normalize_label(label)) in wanted:
                picked.append(vehicle)
        return picked
    def earliest_service_minutes(self, stop_id: int) -> Optional[float]:
        """
        Найраніший «перший рейс» серед маршрутів, що обслуговують зупинку.

        Потрібно для чесної підказки вночі: «нічого не їде — перший рейс о 06:00».
        """
        earliest: Optional[float] = None
        for node in self._resolve_to_nodes(int(stop_id)):
            for route_key in self.node_routes.get(node, ()):
                sched = self._schedule_for(route_key)
                if not sched:
                    continue
                first = self._minutes_of_day(sched.get("first"))
                if first is not None and (earliest is None or first < earliest):
                    earliest = first
        return earliest
