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
        "vehicle", "route", "from", "to", "path", "travel_min", "wait_min",
        "price_grn", "live_bus", "eta", "vehicle_state", "color",
    }
    for leg in transit:
        assert leg_fields <= set(leg), "потеряны поля ноги, их читает эмулятор"
        assert leg["travel_min"] >= 0
        assert leg["wait_min"] is None or leg["wait_min"] >= 0
        assert leg["path"], "у ноги должен быть непустой путь для карты"


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
    expected = [list(point) for point in router.route_coords[route_key][pos_first:pos_last + 1]]

    assert leg["path"] == expected
    assert len(leg["path"]) == pos_last - pos_first + 1 > 2, (
        "в путь ноги не попали промежуточные остановки"
    )
    assert leg["travel_min"] > 0


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


