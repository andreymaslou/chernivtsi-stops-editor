# -*- coding: utf-8 -*-
"""
Эталонные проверки роутера (chernivtsi-stops-editor).

Здесь фиксируются ДВА вида требований:

1. Контракт и воспроизводимость (проходят всегда) — структура ответа
   `/api/plan`, детерминизм симулятора, поведение расписания вне окна.
2. Инвариант корректности пересадки (помечен `xfail`) — «перший ТС» на
   пересадке обязан приезжать ПОСЛЕ прибытия пассажира на остановку.
   Сейчас нарушается: ожидание считается от `now`, а не от времени прибытия
   (см. docs/BRIEF-router-correctness.md). Метка снимается вместе с фиксом.
"""
import json

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


@pytest.mark.xfail(
    reason="известный баг: ожидание и «перший ТС» на пересадке считаются от now, "
           "а не от времени прибытия пассажира (BRIEF-router-correctness)",
    strict=False,
)
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
