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
        for key, route in self.routes.items():
            chain = [int(node) for node in route["stops"]]
            self.route_stops[key] = chain
            self.route_pos[key] = {node: index for index, node in enumerate(chain)}
            prefix = [0.0]
            for segment in route.get("segments", []):
                prefix.append(prefix[-1] + float(segment.get("minutes", 0.0)))
            self.route_prefix[key] = prefix
            self.route_coords[key] = [
                (float(self.nodes[node]["lat"]), float(self.nodes[node]["lon"]))
                if node in self.nodes
                else (0.0, 0.0)  # битый узел: ТС к нему просто не привяжется
                for node in chain
            ]
            wanted = route.get("live_route_name") or route.get("route_name") or ""
            self._route_wanted_norm[key] = self.normalize_label(str(wanted))

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
        # Индекс «нормализованная подпись маршрута -> машины». Строится в
        # set_live(), чтобы поиск «першого потрібного ТС» не перебирал парк.
        self._live_by_route: Dict[str, List[Dict[str, Any]]] = {}
        # Момент, на который построен срез живого парка. Нужен, чтобы понять,
        # успевает ли пассажир на конкретную машину: срез «сейчас», а посадка
        # будет через несколько минут после старта поездки.
        self._live_snapshot_at: Optional[datetime] = None
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

    def set_live(
        self,
        vehicles: Sequence[Dict[str, Any]],
        snapshot_at: Optional[datetime] = None,
    ) -> None:
        """
        Приймає нормалізований срез живого шару (список ТЗ).

        Заодно строит индекс «нормализованная подпись маршрута -> машины»:
        поиск «першого потрібного ТС» вызывается из Дейкстры тысячи раз за
        один запрос, поэтому перебор всего парка на каждый вызов заменён на
        выборку из словаря.

        `snapshot_at` — момент, на который построен срез. Для симулятора это
        время, переданное в `snapshot(now=...)` (парк строится относительно
        него), для реального трекера — «сейчас».
        """
        self._live_vehicles = list(vehicles)
        self._live_snapshot_at = snapshot_at
        by_route: Dict[str, List[Dict[str, Any]]] = {}
        for vehicle in self._live_vehicles:
            label = vehicle.get("route_label") or vehicle.get("route_name") or ""
            key = self.normalize_label(str(label))
            if key:
                by_route.setdefault(key, []).append(vehicle)
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
    ) -> Optional[Dict[str, Any]]:
        """Будує план від from_stop_id до to_stop_id; None — маршрут не знайдено."""
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
        result = self._dijkstra(from_nodes, to_nodes, now, wait_cache)
        return result

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

        for (a, b), minutes in self.transfer_walk.items():
            if a == node and b not in seen:
                seen.add(b)
                options.append((b, minutes))
            elif b == node and a not in seen:
                seen.add(a)
                options.append((a, minutes))

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
    ) -> Optional[Dict[str, Any]]:
        """
        Оптимальний маршрут (0-2 пересадки). Вартість — «хвилини від виходу
        з дому»: очікування + у дорозі + пішки. Усі ребра невід'ємні, тому
        перший цільовий стан, витягнутий з купи, є оптимальним.
        """
        import heapq

        target_set = set(to_nodes)
        # Зона старту: самі вузли відправлення + їх групи/пересадки. Після
        # першої пересадки вертатися сюди безглуздо (петля «поїхав-приїхав»).
        origin_zone: Set[int] = set()
        for node in from_nodes:
            for board_node, _walk in self._expand_to_boardable(node):
                origin_zone.add(board_node)

        heap: List[Tuple[float, int, str, int, frozenset]] = []
        dist: Dict[Tuple[str, int], float] = {}
        # prev[(key,node)] = (prev_key, prev_node, вид, додатково)
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
                    state = (route_key, board_node)
                    cost = walk_min + wait
                    route_name = self._route_name_id(route_key)
                    if cost < dist.get(state, math.inf):
                        dist[state] = cost
                        prev[state] = ("start", board_node, "start", (walk_min, wait))
                        heapq.heappush(
                            heap, (cost, 1, route_key, board_node, frozenset((route_name,)))
                        )

        best_goal: Optional[Tuple[float, float, Tuple[str, int]]] = None
        max_boardings = MAX_TRANSFERS + 1

        while heap:
            cost, boardings, route_key, node, used = heapq.heappop(heap)
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
                    prev[nstate] = (route_key, node, "ride", ride_cost)
                    heapq.heappush(heap, (new_cost, boardings, route_key, nxt, used))

            # 2) Вийти і сісти на інший маршрут.
            if boardings >= max_boardings:
                continue
            for alight, walk_min in self._expand_to_boardable(node):
                if boardings >= 2 and alight in origin_zone:
                    continue  # не повертаємось до старту після першої пересадки
                for other in self.node_routes.get(alight, ()):
                    other_name = self._route_name_id(other)
                    if other_name == self._route_name_id(route_key) or other_name in used:
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
                        prev[nstate] = (route_key, node, "board", (walk_min, wait, alight))
                        heapq.heappush(
                            heap, (new_cost, boardings + 1, other, alight, used | {other_name})
                        )

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
        goal_state: Tuple[str, int],
        prev: Dict[Tuple[str, int], Any],
        total_cost: float,
        goal_walk: float,
        now: datetime,
        wait_cache: Optional[Dict[Tuple[str, int], Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Відновлює послідовність станів із prev і збирає legs."""
        sequence: List[Tuple[str, int]] = []
        current: Optional[Tuple[str, int]] = goal_state
        while current is not None:
            sequence.append(current)
            entry = prev.get(current)
            if entry is None or entry[2] == "start":
                break
            current = (entry[0], entry[1])
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
            route_key, board_node = sequence[index]
            used_route_keys.add(route_key)
            entry = prev.get(sequence[index])

            # Стартова прогулянка до місця посадки (група/пересадка).
            if entry is not None and entry[2] == "start":
                walk_min, _wait = entry[3]
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
                nxt_key, nxt_node = sequence[index]
                if nxt_key != route_key:
                    walk_min, wait_min, _alight = prev[sequence[index]][3]
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
        path = [
            [self.nodes[node]["lat"], self.nodes[node]["lon"]]
            for node in path_nodes
        ]

        wait = self._wait_info(route_key, path_nodes[0], now, wait_cache)
        wait_min = wait.get("wait_min") or 0.0

        price_grn = 0
        sched = self._schedule_for(route_key)
        if sched and sched.get("tariff"):
            price_grn = int(float(sched["tariff"]))

        live_route_name = route.get("live_route_name")
        colour = "#4f8cff"
        for vehicle in self._live_vehicles:
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
            "travel_min": round(travel_min, 1),
            "wait_min": round(wait_min, 1),
            "price_grn": price_grn,
            "live_bus": wait.get("live_bus"),
            "eta": wait.get("eta_min"),
            "vehicle_state": wait.get("vehicle_state"),
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
        }

        live = self._nearest_live_vehicle(route_key, board_node, now)
        if live is not None:
            result["eta_min"] = live["eta_min"]
            result["live_bus"] = live["live_bus"]
            result["vehicle_state"] = "live" if live["is_live"] else "за розкладом"
            # wait_min — очікування на самій зупинці (від прибуття пасажира),
            # тому порівнюємо його з розкладом, а не з ETA від моменту среза.
            live_wait = live.get("wait_min")
            if live_wait is not None and live_wait < base_wait:
                result["wait_min"] = live_wait
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

        Підпис маршруту графа (live_route_name) зіставляємо з route_label
        машини перевізника; потім прив'язуємо машину до найближчого вузла
        ланцюжка. Якщо вона стоїть ПЕРЕД нашою зупинкою — вона «перший
        потрібний ТС» з ETA за префіксними сумами сегментів.

        Ключове: срез парка знято в `_live_snapshot_at`, а пасажир опиниться
        на зупинці в `board_time`. Машина, яка приїде РАНІШЕ за пасажира, нам
        не підходить — її пропускаємо (це і був баг «неможливих пересадок»).
        """
        route = self.routes.get(route_key)
        if not route:
            return None

        # Скільки хвилин мине від моменту среза парка до нашої посадки.
        arrive_min = 0.0
        if self._live_snapshot_at is not None:
            arrive_min = max(
                0.0, (board_time - self._live_snapshot_at).total_seconds() / 60.0
            )
        wanted_norm = self._route_wanted_norm.get(route_key) or self.normalize_label(
            str(route.get("live_route_name") or route.get("route_name") or "")
        )
        if not wanted_norm or board_node not in self.route_pos.get(route_key, {}):
            return None

        coords = self.route_coords[route_key]
        prefix = self.route_prefix[route_key]
        board_pos = self.route_pos[route_key][board_node]

        candidates: List[Tuple[float, Dict[str, Any]]] = []
        for vehicle in self._live_by_route.get(wanted_norm, ()):
            lat, lon = vehicle.get("lat"), vehicle.get("lon")
            if lat is None or lon is None:
                continue

            vlat, vlon = float(lat), float(lon)
            # Грубый отсев в градусах ДО haversine: машину привязываем только
            # к остановкам ближе MAX_LIVE_SNAP_METERS (1° широты ≈ 111.32 км).
            # Окно берём с запасом (долгота делится на cos широты), поэтому
            # отбрасываются только заведомо далёкие точки — ответ не меняется.
            lat_window = MAX_LIVE_SNAP_METERS / 111320.0
            lon_window = lat_window / max(0.2, math.cos(math.radians(vlat)))
            best_idx, best_dist = None, None
            for index, (nlat, nlon) in enumerate(coords):
                if abs(nlat - vlat) > lat_window or abs(nlon - vlon) > lon_window:
                    continue
                distance = self._haversine_m(vlat, vlon, nlat, nlon)
                if best_dist is None or distance < best_dist:
                    best_idx, best_dist = index, distance
            if best_dist is None or best_dist > MAX_LIVE_SNAP_METERS:
                continue
            if best_idx >= board_pos:
                continue  # вже проїхала нашу зупинку

            minutes_away = prefix[board_pos] - prefix[best_idx]
            speed = float(vehicle.get("speed_kmh") or 20.0)
            partial_minutes = (
                (best_dist / 1000.0) / speed * 60.0 if speed > 1.0 else 0.0
            )
            # eta — «через скільки хвилин від моменту среза приїде машина»;
            # саме цю величину UI показує як «буде ~N хв».
            eta = max(1.0, minutes_away + partial_minutes)
            if eta < arrive_min - BOARD_TOLERANCE_MIN:
                continue  # машина поїде раніше, ніж пасажир дійде до зупинки

            # Очікування на самій зупинці: від нашого прибуття до машини.
            wait_after_arrival = max(0.0, eta - arrive_min)
            candidates.append(
                (
                    wait_after_arrival,
                    {
                        "eta_min": round(eta, 1),
                        "wait_min": max(0.5, round(wait_after_arrival + 0.5, 1)),
                        "live_bus": vehicle.get("board_number") or "?",
                        "is_live": bool(vehicle.get("is_live")),
                        "route_label": str(vehicle.get("route_label") or ""),
                    },
                )
            )

        if not candidates:
            return None
        # Сортуємо за очікуванням на зупинці — це і є «перший потрібний ТС»
        # для пасажира, а не найменша ETA від моменту среза парка.
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

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
        wanted_labels: Set[str] = set()
        for key in route_keys:
            route = self.routes.get(key) or {}
            label = route.get("live_route_name") or route.get("route_name")
            if label:
                wanted_labels.add(str(label))
        if not wanted_labels:
            return []

        picked: List[Dict[str, Any]] = []
        for vehicle in self._live_vehicles:
            label = str(vehicle.get("route_label") or vehicle.get("route_name") or "")
            if any(self._route_labels_match(label, w) for w in wanted_labels):
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
