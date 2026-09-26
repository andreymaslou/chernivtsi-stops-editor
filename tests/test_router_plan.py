# -*- coding: utf-8 -*-
"""
Эталонные проверки роутера (chernivtsi-stops-editor).

Здесь фиксируются ДВА вида требований:

1. Контракт и воспроизводимость (проходят всегда) — структура ответа
   `/api/plan`, детерминизм симулятора, поведение расписания вне окна.
2. Инвариант корректности пересадки — «перший ТС» на пересадке обязан
   приезжать ПОСЛЕ прибытия пассажира на остановку. Раньше нарушался
   (ожидание и ETA считались от `now`); исправлено Шагом 3
   (см. docs/BRIEF-router-correctness.md).
"""
import json
from datetime import timedelta

import pytest

import router_layer
from router_layer import TransitRouter

# Эталонные пары остановок.
PAIR_DIRECT = (107, 166)     # пл. Соборна -> Завод «Гравітон» (1 пересадка)
PAIR_TRANSFERS = (181, 166)  # Калинівський ринок -> Завод «Гравітон» (2 пересадки)


def test_plan_is_deterministic(router, now):
    """Одинаковый вход -> одинаковый выход (база для всех эталонов)."""
    first = json.dumps(router.plan(*PAIR_DIRECT, now=now), ensure_ascii=False, sort_keys=True)
    second = json.dumps(router.plan(*PAIR_DIRECT, now=now), ensure_ascii=False, sort_keys=True)
    assert first == second


def test_plan_response_contract(router, now):
    """Набор полей ответа и ног — контракт для UI эмулятора."""
    plan = router.plan(*PAIR_DIRECT, now=now)
    assert plan is not None

    top_level = {
        "mode", "from_stop_id", "to_stop_id", "from_name", "to_name",
        "transfers", "total_min", "price_grn", "legs", "vehicles", "computed_at",
    }
    assert top_level <= set(plan)
    assert plan["mode"] == "plan"

    transit = [leg for leg in plan["legs"] if leg["type"] == "transit"]
    assert transit, "в плане должна быть хотя бы одна нога-поездка"

    leg_fields = {
        "vehicle", "route", "from", "to", "path", "stops", "full_geom", "travel_min", "wait_min",
        "price_grn", "live_bus", "eta", "vehicle_state", "color",
    }
    for leg in transit:
        assert leg_fields <= set(leg), "потеряны поля ноги, их читает эмулятор"
        assert leg["travel_min"] >= 0
        assert leg["wait_min"] is None or leg["wait_min"] >= 0
        assert leg["path"], "у ноги должен быть непустой путь для карты"
        assert all(
            {"name", "lat", "lon"} <= set(stop) and stop["name"]
            for stop in leg["stops"]
        ), "промежуточные остановки должны содержать имя и координаты"
        assert len(leg["full_geom"]) >= len(leg["path"]) >= 2, (
            "full_geom (хвіст маршруту) не може бути коротшим за активну ділянку"
        )


def test_fleet_is_reproducible(sim, now):
    """Парк симулятора детерминирован — эталон для сравнения снимков."""
    vehicles = sim.snapshot(now=now)["vehicles"]
    assert len(vehicles) > 0
    assert all(v["is_live"] for v in vehicles)
    assert all(v["route_label"] for v in vehicles)


def test_out_of_window_route_has_no_service(data, now):
    """Вне окна работы маршрут помечается «не ходить» (прод-режим)."""
    graph, schedule, stops = data
    router = TransitRouter(graph, schedule, stops=stops, assume_in_service=False)

    candidate = None
    for route_name, sched in (schedule.get("bus") or {}).items():
        if not (sched.get("first") and sched.get("last")):
            continue
        for route_key in router.routes:
            if route_key == f"bus:{route_name}:A":
                candidate = route_key
                break
        if candidate:
            break

    if candidate is None:
        pytest.skip("в расписании нет автобусного маршрута с окном работы")

    board_node = router.route_stops[candidate][0]
    info = router._wait_info(candidate, board_node, now.replace(hour=3, minute=0))
    assert info["wait_min"] is None
    assert info["vehicle_state"] == "не ходить"


def test_no_boarding_of_vehicle_that_leaves_before_arrival(router, now):
    """
    ИНВАРИАНТ: если в ноге показан конкретный ТС («сідати: X, буде ~N хв»),
    он не должен уезжать раньше, чем пассажир доедет до остановки посадки.

    Время прибытия считаем независимо от реализации — накоплением по ногам:
    walk/ride/wait предыдущих ног. Это и есть «золотой» критерий корректности.
    """
    plan = router.plan(*PAIR_TRANSFERS, now=now)
    assert plan is not None

    arrival_min = 0.0
    missed = []
    for leg in plan["legs"]:
        if leg["type"] == "transfer":
            arrival_min += leg.get("walk_min") or 0.0
            continue

        bus, eta = leg.get("live_bus"), leg.get("eta")
        # eta — это «через сколько минут от сейчас придёт машина».
        # Если она придёт раньше, чем мы окажемся на остановке, — мы её не увидим.
        if bus and eta is not None and eta < arrival_min - 0.5:
            missed.append({
                "route": leg.get("route"),
                "bus": bus,
                "eta_min": eta,
                "passenger_arrival_min": round(arrival_min, 1),
            })

        arrival_min += (leg.get("wait_min") or 0.0) + (leg.get("travel_min") or 0.0)

    assert not missed, f"в плане есть ТС, которые уезжают раньше пассажира: {missed}"


def _chain_node_id(router, route_key, name_part):
    """Id узла маршрута по части имени (устойчиво к перенумерации графа)."""
    for node_id in router.route_stops[route_key]:
        if name_part in router.nodes[node_id]["name"]:
            return node_id
    return None


def _slice_offset(coords, path):
    """Позиция, с которой path совпадает с цепочкой координат маршрута."""
    for start in range(len(coords) - len(path) + 1):
        if [tuple(point) for point in coords[start:start + len(path)]] == path:
            return start
    return None


def test_transit_leg_path_is_continuous_route_slice(router, now):
    """Путь ноги обязан быть НЕПРЕРЫВНЫМ куском цепочки остановок маршрута.

    Иначе Leaflet рисует хорду через полгорода вместо маршрута (жалоба «немає
    прорисованих маршрутів»): Дейкстра едет сразу до любой следующей остановки,
    и в leg["path"] когда-то попадали только посадка и высадка.
    """
    for pair in (PAIR_DIRECT, PAIR_TRANSFERS):
        plan = router.plan(*pair, now=now)
        assert plan is not None
        for leg in [item for item in plan["legs"] if item["type"] == "transit"]:
            path = [tuple(point) for point in leg["path"]]
            assert len(path) >= 2, f"нога без линии на карте: {leg['route']}"
            assert any(
                _slice_offset(coords, path) is not None
                for coords in router.route_coords.values()
            ), (
                "путь ноги не является непрерывным срезом маршрута — карта "
                f"нарисует прямую: route={leg['route']} точек={len(path)}"
            )


def test_ride_path_contains_all_intermediate_stops(router, now):
    """Регрессия жалобы на карту: у длинной поездки в path ВСЕ остановки.

    Эталон — trolley:5:A «Калинівський ринок → Стадіон Мальва»: девять
    остановок подряд; раньше в path оставалось две точки.
    """
    route_key = "trolley:5:A"
    if route_key not in router.routes:
        pytest.skip("в графе нет trolley:5:A")

    board = _chain_node_id(router, route_key, "Калинівський ринок")
    alight = _chain_node_id(router, route_key, "Мальва")
    if board is None or alight is None:
        pytest.skip("в цепочке trolley:5:A нет эталонных остановок")

    positions = router.route_pos[route_key]
    pos_first, pos_last = positions[board], positions[alight]
    assert pos_last > pos_first, "эталонные остановки идут не по порядку"

    leg = router._transit_leg(route_key, [board, alight], now)

    # Геометрия маршрута — прямая между остановками, но path всё равно
    # обязан быть НЕПРЕРЫВНЫМ срезом цепочки: все промежуточные остановки
    # на месте (старый баг — в path оставались только посадка и высадка).
    # Через stop_indices, чтобы тест пережил и возврат расширенной геометрии.
    stop_indices = router.route_stop_indices.get(route_key, list(range(len(router.route_coords[route_key]))))
    coord_first = stop_indices[pos_first] if pos_first < len(stop_indices) else pos_first
    coord_last = stop_indices[pos_last] if pos_last < len(stop_indices) else pos_last
    expected = [list(point) for point in router.route_coords[route_key][coord_first:coord_last + 1]]

    assert leg["path"] == expected
    assert len(leg["path"]) >= pos_last - pos_first + 1 > 2, (
        "path обязан содержать все промежуточные остановки ноги"
    )
    assert leg["travel_min"] > 0

    expected_stop_names = [
        router.nodes[router.route_stops[route_key][position]]["name"]
        for position in range(pos_first + 1, pos_last)
    ]
    expected_stop_coords = [
        list(router.route_coords[route_key][stop_indices[position]])
        for position in range(pos_first + 1, pos_last)
    ]
    assert [stop["name"] for stop in leg["stops"]] == expected_stop_names
    assert [[stop["lat"], stop["lon"]] for stop in leg["stops"]] == expected_stop_coords


def test_leg_full_geom_covers_active_path(router, now):
    """`full_geom` (хвіст маршруту) мусить містити активну ділянку як сріз.

    Фронт малює хвіст як `full_geom` із вирізаною ділянкою `path`. Якщо `path`
    не є неперервним срізом `full_geom`, хвіст «не стикується» з активною
    лінією — головний візуальний ризик цієї правки, тому це інваріант.
    """
    for pair in (PAIR_DIRECT, PAIR_TRANSFERS):
        plan = router.plan(*pair, now=now)
        assert plan is not None
        for leg in [item for item in plan["legs"] if item["type"] == "transit"]:
            full = [tuple(point) for point in leg["full_geom"]]
            path = [tuple(point) for point in leg["path"]]
            assert len(full) >= len(path) >= 2, f"хвіст коротший за ногу: {leg['route']}"
            offset = _slice_offset(full, path)
            assert offset is not None, (
                "активна ділянка ноги не є неперервним срізом full_geom — "
                f"хвіст на карті не зійдеться з лінією: route={leg['route']}"
            )


def test_spatial_cache_does_not_change_result(router, now):
    """Кэш геометрии (Шаг 4) — только ускорение, а не новая логика.

    Сравниваем два прогона одного запроса: с кэшем `_spatial_cache` и с
    имитацией старого поведения (кэш очищается перед каждым заходом, поэтому
    haversine считается заново). Ответы обязаны совпасть байт-в-байт.
    """
    cached_plan = router.plan(*PAIR_TRANSFERS, now=now)
    assert cached_plan is not None
    assert router._spatial_cache, "геометрия должна была закэшироваться"

    original = TransitRouter._nearest_live_vehicle

    def uncached(self, *args, **kwargs):
        self._spatial_cache = {}  # как было до Шага 4: считаем каждый раз
        return original(self, *args, **kwargs)

    TransitRouter._nearest_live_vehicle = uncached
    try:
        fresh_plan = router.plan(*PAIR_TRANSFERS, now=now)
    finally:
        TransitRouter._nearest_live_vehicle = original

    assert json.dumps(fresh_plan, ensure_ascii=False, sort_keys=True) == json.dumps(
        cached_plan, ensure_ascii=False, sort_keys=True
    )


def test_same_fleet_keeps_spatial_cache(router, fleet, now, data):
    """Тот же парк (даже с другим snapshot_at) не сбрасывает кэш геометрии.

    Прод-эндпоинт зовёт set_live() на каждый запрос (main.py), а трекер отдаёт
    последние опрошенные позиции: если сбрасывать кэш на каждом вызове, он
    всегда холодный. eta в кэше — относительная величина, от `snapshot_at` не
    зависит, поэтому смена только метки времени кэш не ломает.
    """
    first = router.plan(*PAIR_DIRECT, now=now)
    version = router._fleet_version
    cached = dict(router._spatial_cache)
    assert cached, "геометрия должна была закэшироваться"

    router.set_live(
        [dict(vehicle) for vehicle in fleet],  # новый список, те же позиции
        snapshot_at=now + timedelta(minutes=1),
    )

    assert router._fleet_version == version, "кэш сбросили без нужды"
    assert router._spatial_cache == cached

    # Возвращаем исходный момент среза: ответ обязан совпасть байт-в-байт.
    router.set_live([dict(vehicle) for vehicle in fleet], snapshot_at=now)
    again = router.plan(*PAIR_DIRECT, now=now)
    assert json.dumps(again, ensure_ascii=False, sort_keys=True) == json.dumps(
        first, ensure_ascii=False, sort_keys=True
    )


def test_spatial_cache_drops_stale_fleet(router, data, fleet, now):
    """Новый срез парка обязан сбросить кэш геометрии.

    Иначе после опроса GPS ответ строился бы по позициям прошлого среза.
    Проверяем и версию кэша, и сам результат: он должен совпасть с ответом
    «чистого» роутера на том же парке.
    """
    router.plan(*PAIR_DIRECT, now=now)  # прогреваем кэш прошлым срезом
    version = router._fleet_version

    moved = [dict(vehicle) for vehicle in fleet]
    for vehicle in moved:
        vehicle["lat"] = float(vehicle["lat"]) + 0.02  # машины уехали
    router.set_live(moved, snapshot_at=now)

    assert router._fleet_version > version, "версия парка не поднялась"
    assert not router._spatial_cache, "кэш геометрии не сброшен в set_live()"

    after = router.plan(*PAIR_DIRECT, now=now)

    graph, schedule, stops = data
    fresh = TransitRouter(graph, schedule, stops=stops, assume_in_service=True)
    fresh.set_live(moved, snapshot_at=now)
    expected = fresh.plan(*PAIR_DIRECT, now=now)

    assert json.dumps(after, ensure_ascii=False, sort_keys=True) == json.dumps(
        expected, ensure_ascii=False, sort_keys=True
    )


# ---------------------------------------------------------------------------
# Направление ТС: docs/BRIEF-router-direction.md
# ---------------------------------------------------------------------------

def _fleet_vehicle(router, route_key, index, direction=None, heading=None,
                   speed=25.0, board="T-001"):
    """Одна машина на ланцюжку маршруту: стоїть рівно на вузлі (snap без похибки)."""
    # `index` — індекс ЗУПИНКИ в ланцюжку (так само, як board_pos/bearings у
    # тестах нижче). Після OSRM route_coords розширений формою доріг, тому
    # координати зупинки беремо через route_stop_indices — інакше машина
    # встане на точку геометрії дороги, а прив'яжеться до іншої зупинки.
    stop_indices = router.route_stop_indices.get(
        route_key, list(range(len(router.route_coords[route_key])))
    )
    lat, lon = router.route_coords[route_key][stop_indices[index]]
    route = router.routes[route_key]
    label = route.get("live_route_name") or route.get("route_name") or ""
    vehicle = {
        "board_number": board,
        # Тип ТС обов'язковий: пошук машини маршруту йде за парою
        # (тип, підпис) — інакше «5» автобуса і тролейбуса плутались би.
        "vehicle_type": route.get("vehicle_type", "bus"),
        "route_label": str(label),
        "route_name": str(label),
        "lat": lat,
        "lon": lon,
        "speed_kmh": speed,
        "heading_deg": heading,
        "is_live": True,
        "status": "live",
    }
    if direction is not None:
        vehicle["direction"] = direction
    return vehicle


def _route_for_direction_tests(router, min_stops=6):
    """Маршрут із достатньо довгим ланцюжком і відомим напрямком (A/B)."""
    for key, chain in router.route_stops.items():
        if len(chain) >= min_stops and router.route_direction.get(key):
            return key
    return None


def test_opposite_direction_vehicle_is_not_approaching(data, now):
    """Встречная машина (Напрямок Б) не «під'їжджає» до зупинки маршруту А.

    Ланцюжки A і B ідуть тими самими вулицями, тому геометрично машина стоїть
    «перед» зупинкою — але вона їде від неї.
    """
    graph, schedule, stops = data
    router = TransitRouter(graph, schedule, stops=stops, assume_in_service=True)
    route_key = _route_for_direction_tests(router)
    assert route_key, "в графе нет маршрута с направлением"

    board_node = router.route_stops[route_key][4]
    behind = 2  # узел перед остановкой посадки
    heading = router.route_bearings[route_key][behind]  # курс «в нашу сторону»
    other = "B" if router.route_direction[route_key] == "A" else "A"

    fleet = [_fleet_vehicle(router, route_key, behind, direction=other, heading=heading)]
    router.set_live(fleet, snapshot_at=now)
    assert router._approaching_vehicles(route_key, board_node) == [], (
        "встречная машина попала в «подъезжающие»"
    )

    fleet[0]["direction"] = router.route_direction[route_key]
    router.set_live(fleet, snapshot_at=now)
    assert [c["live_bus"] for c in router._approaching_vehicles(route_key, board_node)] == ["T-001"]


def test_direction_is_normalized(data, now):
    """Кириллица и регистр в направлении не ломают подбор.

    «Голое» сравнение отбросило бы ВСЕ машины маршрута, и `live_bus` стал бы
    вечно `null` — это хуже бага, который чиним.
    """
    graph, schedule, stops = data
    router = TransitRouter(graph, schedule, stops=stops, assume_in_service=True)
    route_key = _route_for_direction_tests(router)
    assert route_key, "в графе нет маршрута с направлением"

    board_node = router.route_stops[route_key][4]
    behind = 2
    heading = router.route_bearings[route_key][behind]
    wanted = router.route_direction[route_key]

    for variant in (wanted, wanted.lower(), {"A": "А", "B": "Б"}[wanted]):
        fleet = [_fleet_vehicle(router, route_key, behind, direction=variant, heading=heading)]
        router.set_live(fleet, snapshot_at=now)
        assert router._approaching_vehicles(route_key, board_node), (
            f"направление {variant!r} ошибочно отбросило машину"
        )


def test_heading_filter_rejects_opposite_vehicle(data, now):
    """Реальный трекер поля `direction` не отдаёт — направление видно по курсу."""
    graph, schedule, stops = data
    router = TransitRouter(graph, schedule, stops=stops, assume_in_service=True)

    route_key = index = None
    for key, chain in router.route_stops.items():
        for position in range(0, len(chain) - 3):
            if router.route_segment_m[key][position] >= 60.0:
                route_key, index = key, position
                break
        if route_key:
            break
    assert route_key is not None, "нет маршрута с сегментом ≥ 60 м"

    board_node = router.route_stops[route_key][index + 2]
    bearing = router.route_bearings[route_key][index]

    fleet = [_fleet_vehicle(router, route_key, index, heading=bearing)]
    router.set_live(fleet, snapshot_at=now)
    assert router._approaching_vehicles(route_key, board_node), "попутная машина отброшена"

    fleet[0]["heading_deg"] = (bearing + 180.0) % 360.0
    router.set_live(fleet, snapshot_at=now)
    assert router._approaching_vehicles(route_key, board_node) == [], (
        "встречный курс не отсечён, хотя поля direction в срезе нет"
    )

    # Стоящая машина (светофор, кінцева): курс — шум, отбрасывать нельзя.
    fleet[0]["speed_kmh"] = 1.0
    router.set_live(fleet, snapshot_at=now)
    assert router._approaching_vehicles(route_key, board_node), (
        "стоящую машину отбросили по курсу — а он у неё случайный"
    )


def test_live_snap_stays_in_stop_index_space(data, now):
    """Прив'язка машини рахується в просторі ЗУПИНОК (регресія «вічний None»).

    `board_pos`, `route_prefix`, `route_bearings` і `route_segment_m` живуть в
    індексах ЗУПИНОК (довжина == len(chain)). Коли цикл прив'язки ітерував по
    точках розширеної геометрії, `best_idx` був індексом точки геометрії й
    порівнювався з `board_pos` зупинки — кожна машина відкидалась як «та, що
    вже проїхала нашу зупинку», `live_bus` ставав вічно None, і золотий бейдж
    «ваша посадка» нікому було підсвічувати. З прямою геометрією простори
    збігаються, але інваріант `route_stop_indices` тримаємо явно.
    """
    graph, schedule, stops = data
    router = TransitRouter(graph, schedule, stops=stops, assume_in_service=True)
    route_key = _route_for_direction_tests(router, min_stops=6)
    assert route_key, "у графі немає маршруту з напрямком і довгим ланцюжком"
    chain = router.route_stops[route_key]
    coords = router.route_coords[route_key]
    stop_indices = router.route_stop_indices.get(
        route_key, list(range(len(coords))))
    # Інваріант просторів: route_stop_indices переводить індекси зупинок у
    # індекси route_coords. Пряма геометрія дає тотожність; якщо геометрію
    # колись знову розширять — мапінг мусить лишитись тієї ж довжини й
    # строго зростаючим, інакше прив'язка машин знову поїде.
    assert len(stop_indices) == len(chain), "route_stop_indices не покриває ланцюжок"
    assert stop_indices == sorted(stop_indices), "route_stop_indices не зростає"
    if len(coords) == len(chain):
        assert stop_indices == list(range(len(chain))), (
            "пряма геометрія: індекси зупинок мають бути тотожні")

    behind = len(chain) // 2 - 1               # зупинка посередині ланцюжка
    board_node = chain[behind + 2]
    fleet = [_fleet_vehicle(router, route_key, behind,
                            direction=router.route_direction[route_key])]
    router.set_live(fleet, snapshot_at=now)

    found = [item["live_bus"]
             for item in router._approaching_vehicles(route_key, board_node)]
    assert found == ["T-001"], (
        "машина стоїть на зупинці %d (це ~%d-а точка геометрії), а посадка %d: %s" % (
            behind, stop_indices[behind], behind + 2, found))


def test_same_number_different_vehicle_type_stays_separate(router, now):
    """Регрессия: «5» автобус і «5» тролейбус — це два різні маршрути.

    Номери маршрутів у автобусів і тролейбусів незалежні, тому прив'язка
    парку до маршруту завжди доповнюється типом ТС. Раніше індекс парку
    будувався за голеною підписою маршруту: машини «5» обох типів склеювалися
    в одну купу, і план автобусом 5 показував тролейбуси 5 (і навпаки).
    """
    bus_key, trolley_key = "bus:5:A", "trolley:5:A"
    for key in (bus_key, trolley_key):
        assert key in router.routes, f"у графі немає маршруту {key}"
    assert router.routes[bus_key]["route_name"] == router.routes[trolley_key]["route_name"]
    assert router.routes[bus_key]["vehicle_type"] != router.routes[trolley_key]["vehicle_type"]

    # Індекс парку не змішує типи за однією лише підписою маршруту.
    mixed = [
        label
        for (vtype, label), vehicles in router._live_by_route.items()
        if len({str(v.get("vehicle_type")) for v in vehicles}) > 1
    ]
    assert not mixed, f"маршрути склеєні за підписою без обліку типу: {mixed}"

    # На карту плану автобусного 5 не мають потрапляти тролейбуси 5.
    bus_only = router._vehicles_for_routes({bus_key})
    trolley_only = router._vehicles_for_routes({trolley_key})
    assert bus_only and trolley_only, "обох маршрутів має бути хоча б по машині"
    assert all(v["vehicle_type"] == "bus" for v in bus_only), "у план автобуса 5 попали тролейбуси"
    assert all(v["vehicle_type"] == "trolley" for v in trolley_only), "у план тролейбуса 5 попали автобуси"
    assert not set(map(id, bus_only)) & set(map(id, trolley_only))


def test_foreign_vehicle_type_is_not_first_bus(data, now):
    """Тролейбус «5», що стоїть на ланцюжку автобусного 5 — не «перший потрібний ТС»."""
    graph, schedule, stops = data
    router = TransitRouter(graph, schedule, stops=stops, assume_in_service=True)

    route_key = "bus:5:A"
    assert route_key in router.routes
    board_node = router.route_stops[route_key][4]

    # Тролейбус із тою самою підписою маршруту, фізично на нашому ланцюжку.
    foreign = _fleet_vehicle(router, route_key, 2, board="T-777")
    foreign["vehicle_type"] = "trolley"
    router.set_live([foreign], snapshot_at=now)
    assert [c["live_bus"] for c in router._approaching_vehicles(route_key, board_node)] == [], (
        "тролейбус 5 привязался к автобусному маршруту 5"
    )

    # Та сама машина, але свого типу — обязана быть «першим потрібним ТС».
    own = dict(foreign)
    own["vehicle_type"] = "bus"
    router.set_live([own], snapshot_at=now)
    assert [c["live_bus"] for c in router._approaching_vehicles(route_key, board_node)] == ["T-777"], (
        "своя машина маршрута отброшена"
    )


def test_vehicle_without_direction_and_heading_is_kept(data, now):
    """Срез без `direction` и без курса — работает прежний геометрический подбор."""
    graph, schedule, stops = data
    router = TransitRouter(graph, schedule, stops=stops, assume_in_service=True)
    route_key = _route_for_direction_tests(router)
    assert route_key, "в графе нет маршрута с направлением"

    board_node = router.route_stops[route_key][4]
    fleet = [_fleet_vehicle(router, route_key, 2)]  # ни direction, ни heading_deg
    router.set_live(fleet, snapshot_at=now)
    assert [c["live_bus"] for c in router._approaching_vehicles(route_key, board_node)] == ["T-001"]


def test_fleet_fingerprint_tracks_direction(router, fleet, now):
    """Разворот на кінцевій (та сама позиція, інший напрямок) скидає кэш геометрии.

    Інакше в `_spatial_cache` лишилася б геометрія, порахована для старого
    напрямку: машина стоїть на місці, тож lat/lon/speed/board не змінилися.
    """
    router.plan(*PAIR_DIRECT, now=now)
    version = router._fleet_version
    assert router._spatial_cache, "геометрия должна была закэшироваться"

    turned = [dict(vehicle) for vehicle in fleet]
    for vehicle in turned:
        if vehicle.get("direction"):
            vehicle["direction"] = "B" if vehicle["direction"] == "A" else "A"
    router.set_live(turned, snapshot_at=now)

    assert router._fleet_version > version, "кэш не сброшен при смене направления"
    assert not router._spatial_cache


def test_transfer_graph_invariants(data):
    """Инварианты сборки переходов: порог, дубли групп и «соседние остановки одной ветки».

    Фільтр «не зшивати сусідні зупинки однієї гілки» діє лише в новій смузі
    (> 150 м) і лише коли набір маршрутів на обох кінцях однаковий — інакше
    відрізається реальна пересадка (див. docs/REVIEW-f1-transfer-250m.md §4).
    """
    graph, _schedule, _stops = data
    nodes = {int(key): value for key, value in graph["nodes"].items()}
    limit = float(graph["params"]["transfer_max_meters"])

    chain_routes: dict = {}
    for route in graph["routes"].values():
        chain = route["stops"]
        for left, right in zip(chain, chain[1:]):
            key = (min(left, right), max(left, right))
            chain_routes.setdefault(key, set()).add(
                (route.get("vehicle_type"), route.get("route_name"))
            )

    group_of: dict = {}
    for gid, group in graph["groups"].items():
        for node in group["node_ids"]:
            group_of[int(node)] = int(gid)

    for edge in graph["transfers"]:
        a, b = int(edge["from"]), int(edge["to"])
        assert edge["meters"] <= limit + 0.05, "переход длиннее порога"
        same_group = group_of.get(a) is not None and group_of.get(a) == group_of.get(b)
        if edge["meters"] > 150.0:
            # Легаси-полоса (< 150 м) сохраняется как есть намеренно: там рёбра
            # внутри группы — дубли, но их удаление ломает уже принятые планы
            # (см. TRANSFER_LEGACY_MAX_METERS), поэтому проверяем только новую.
            assert not same_group, (
                "в новой полосе оставлено ребро внутри одной группы — дубль: "
                "роутер и так ходит пешком внутри группы"
            )
        if edge["meters"] > 150.0 and (a, b) in chain_routes:
            left = set(nodes[a]["routes"])
            right = set(nodes[b]["routes"])
            assert left != right, (
                "в новой полосе оставлено ребро «соседние остановки одной ветки» "
                "при одинаковом наборе маршрутов: пешком идти некуда, можно проехать"
            )


def test_direct_bus_20_reachable_from_uchylische(data):
    """§1.7: порог 250 м обязан вернуть связку «Училище №15» ↔ «Поліклініка».

    Узел 202 стоял без единого пешего соседа: первая версия фильтра срезала
    пару 202↔140 (они идут подряд в цепочке `bus:23:B`), хотя наборы маршрутов
    на концах разные — то есть это настоящая пересадка, а не проход вдоль
    своего маршрута. Без этого ребра прямой автобус 20 недостижим.
    """
    graph, _schedule, _stops = data
    limit = float(graph["params"]["transfer_max_meters"])
    if limit < 250.0:
        pytest.skip("граф собран с порогом %s м — кейс §1.7 относится к 250 м" % limit)

    nodes = {int(key): value for key, value in graph["nodes"].items()}
    assert nodes[202]["name"].startswith("Училище"), "сменились id узлов — тест надо обновить"

    pairs = {(int(t["from"]), int(t["to"])) for t in graph["transfers"]}
    assert (140, 202) in pairs or (202, 140) in pairs, (
        "потеряна связка «Училище №15» ↔ «Поліклініка» (140): прямой 20-й снова недостижим"
    )
    neighbours = {
        (int(t["to"]) if int(t["from"]) == 202 else int(t["from"]))
        for t in graph["transfers"]
        if 202 in (int(t["from"]), int(t["to"]))
    }
    assert neighbours, "у узла 202 нет ни одного пешего соседа"

def test_variants_offer_fewer_transfers(router, now):
    """Второй прогон «≤1 пересадка» даёт дешёвый вариант карточкой (§13 брифа).

    Пара — «Кінотеатр Жовтень → Юність» (61→105): дефолт 2 пересадки,
    второй прогон даёт 1 пересадку и меньше денег.

    Примітка: попередня пара (169, 68) тримала transfers=2 лише завдяки
    ghost-нозі (travel_min=0, фікс P0, 2026-09-26). Після фіксу той маршрут
    коректно будується за 1 пересадку — пара більше не підходить як еталон
    для перевірки «дефолт ≥2 пересадки».
    """
    pair = (61, 105)
    default = router.plan(*pair, now=now)
    assert default is not None and default["transfers"] >= 2

    variants, note = router.build_variants(*pair, now=now, default_plan=default)

    assert note is None, "для этой пары вариант должен быть доступен: %r" % note
    assert len(variants) == 2
    first, second = variants

    # Корневой ответ не подменяется: первый вариант повторяет дефолт.
    assert (first["total_min"], first["price_grn"], first["transfers"]) == (
        default["total_min"], default["price_grn"], default["transfers"])
    assert first["id"] == "default" and "Швидкий" in first["tags"]

    # Второй вариант — честное «дешевле»: меньше посадок и меньше денег.
    assert second["id"] == "fewer_transfers" and "Дешевий" in second["tags"]
    assert second["transfers"] < default["transfers"]
    assert second["price_grn"] < default["price_grn"]
    assert second["total_min"] >= default["total_min"]
    assert second["legs"], "у варианта должны быть ноги — их рисует карта"


def test_ghost_leg_169_68_is_fixed(router, now):
    """Регресія P0: пара (169, 68) раніше мала ghost-ногу (travel_min=0).

    До фіксу: transit '39' «вул. Турецька»→«вул. Турецька», travel_min=0,
    transfers=2, price=52. Після фіксу — transfers=1, price=32, жодної
    transit-ноги з travel_min < 0.5.
    """
    plan = router.plan(169, 68, now=now)
    assert plan is not None
    assert plan["transfers"] == 1, (
        f"ghost-нога повернулась: transfers={plan['transfers']}, price={plan['price_grn']}")
    assert plan["price_grn"] <= 36, (
        f"ціна завищена (ghost-нога?): {plan['price_grn']} грн")
    for leg in plan["legs"]:
        if leg["type"] == "transit":
            assert leg["travel_min"] >= 0.5, (
                f"ghost-нога в плані: маршрут {leg['route']}, travel_min={leg['travel_min']}")


def test_variants_are_honest_when_there_is_no_second(router, now):
    """Когда fewer_transfers варианта нет, API говорит об этом текстом.

    Пара (107, 166): дефолт вже з ≤1 пересадкою — картки «Дешевий»
    (fewer_transfers) взятись нема звідки. Після Q1 може з'явитись «Прямий»
    (direct) — це нормально, «другий варіант» у сенсі §13 = fewer_transfers.
    """
    pair = (107, 166)
    default = router.plan(*pair, now=now)
    assert default is not None

    variants, note = router.build_variants(*pair, now=now, default_plan=default)

    ids = [v["id"] for v in variants]
    assert "fewer_transfers" not in ids, f"fewer_transfers з'явився: {ids}"
    # note == None — коректно коли вже є 'direct' + 'default' (2 карточки показані,
    # пояснення «немає варіанту» не потрібне). Якщо direct відсутній — пасажир
    # бачить лише 1 карточку, note зобов'язаний пояснити чому другої немає.
    if "direct" not in ids:
        assert note and "немає" in note, f"note мав описати відсутність: {note!r}"


def test_max_transfers_is_a_parameter_not_a_global(router, now):
    """Ограничение пересадок — параметр вызова; глобальная константа не меняется.

    Иначе второй прогон («≤1 пересадка») правил бы общую константу, а один
    `TransitRouter` обслуживает несколько потоков FastAPI — это гонка между
    запросами (docs/BRIEF-plan-variants.md §13, F3).

    Пара (169,68) замінена на (61,105): (169,68) після фіксу P0 (ghost-нога
    travel_min=0) коректно будується за 1 пересадку, тому two["transfers"]
    дорівнює 1 і умова 1 < two["transfers"] не виконується.
    """
    pair = (61, 105)
    before = router_layer.MAX_TRANSFERS

    two = router.plan(*pair, now=now)
    one = router.plan(*pair, now=now, max_transfers=1)

    assert router_layer.MAX_TRANSFERS == before, (
        "прогон изменил глобальную MAX_TRANSFERS — это гонка между запросами")
    assert two is not None and one is not None
    assert one["transfers"] <= 1 < two["transfers"]


def test_no_transit_leg_with_zero_travel(router, now):
    """Жодна transit-нога не повинна мати travel_min < 0.5 хв.

    Баг: Дейкстра будувала план де пасажир сідав і одразу виходив
    на тій самій зупинці (travel_min = 0). Така нога — сміття в плані.
    """
    for from_stop, to_stop in [
        (107, 166),   # Соборна -> Гравітон
        (181, 166),   # Калинівка -> Гравітон
        (65, 166),    # ринок -> Гравітон
    ]:
        plan = router.plan(from_stop, to_stop, now=now)
        if plan is None:
            continue
        for leg in plan["legs"]:
            if leg["type"] == "transit":
                assert leg["travel_min"] >= 0.5, (
                    f"Маршрут {leg['route']}: travel_min={leg['travel_min']} < 0.5 "
                    f"(from={from_stop}, to={to_stop})"
                )


def test_direct_variant_appears_when_direct_route_exists(router, now):
    """Якщо є прямий маршрут (0 пересадок) — перша карточка «Прямий».

    Перевіряємо що build_variants повертає варіант з id='direct' і тегом
    'Прямий' коли дефолтний план має пересадки, але є прямий маршрут.
    Пара підбирається динамічно: шукаємо будь-яку пару де
    plan(max_transfers=0) is not None AND plan(max_transfers=2)["transfers"]>0.
    """
    # Знаходимо пару де є прямий маршрут але дефолт обирає з пересадкою
    found = None
    for from_s, to_s in [(107, 166), (181, 166), (61, 105), (65, 166)]:
        direct = router.plan(from_s, to_s, now=now, max_transfers=0)
        default = router.plan(from_s, to_s, now=now)
        if direct is not None and default is not None and default["transfers"] > 0:
            slowdown = direct["total_min"] - default["total_min"]
            if slowdown <= 20:
                found = (from_s, to_s, direct, default)
                break

    if found is None:
        pytest.skip("не знайдено пари з прямим маршрутом повільнішим за дефолт")

    from_s, to_s, direct_plan, default_plan = found
    variants, note = router.build_variants(from_s, to_s, now=now, default_plan=default_plan)

    ids = [v["id"] for v in variants]
    assert "direct" in ids, f"варіант 'direct' відсутній: {ids}, note={note}"

    direct_card = next(v for v in variants if v["id"] == "direct")
    assert "Прямий" in direct_card["tags"]
    assert direct_card["transfers"] == 0
    assert variants[0]["id"] == "direct", "карточка 'Прямий' має бути першою"


def test_no_direct_variant_when_default_is_already_direct(router, now):
    """Якщо дефолтний план вже прямий — окремої карточки 'direct' не виникає.

    Тег 'Прямий' може бути у дефолтній карточці, але дублювати не потрібно.

    Пара (68, 73) — ланцюжок bus:10:A: дефолт уже прямий (transfers=0), тобто
    прямий маршрут і є оптимумом за часом, і карточка 'direct' була б дублем.
    (107, 166) для цієї перевірки не годиться: там дефолт іде з пересадкою, і
    після Q1 карточка «Прямий» з'являється ЗАКОННО.
    """
    pair = (68, 73)
    default = router.plan(*pair, now=now)
    if default is None or default["transfers"] != 0:
        pytest.skip("дефолт не є прямим для цієї пари")

    variants, _ = router.build_variants(*pair, now=now, default_plan=default)
    ids = [v["id"] for v in variants]
    assert ids.count("direct") == 0, "дублювати 'direct' не потрібно коли дефолт вже прямий"
    assert ids[0] == "default"