# -*- coding: utf-8 -*-
"""Метки маршрутов и поиск расписания.

Регресс, который эти тесты закрывают: в `routes_schedule.json` метки двух
маршрутов были кириллицей («8А», «15К»), а граф отдаёт латиницу («8A», «15K»).
`_schedule_for()` возвращал `None`, поэтому у ног этих маршрутов

* `price_grn` становилась **0** (итог плана «Соборка → Гравітон» — 36 вместо 56);
* ожидание падало на константу `DEFAULT_WAIT_MINUTES` вместо интервала;
* ночной запрет «не ходить» не срабатывал — маршрут считался ходящим круглосуточно.

Замер до фикса: 37 из 234 планов (15.8 %) задевали 15K или 8A, и каждый такой
раз цена ноги была нулевой.
"""
import json
from pathlib import Path

from router_layer import TransitRouter

REPO = Path(__file__).resolve().parent.parent
SCHEDULE = json.loads((REPO / "routes_schedule.json").read_text(encoding="utf-8"))


def test_schedule_labels_are_latin():
    """Расписание читает не только роутер, но и симулятор — и без нормализации.

    Кириллица в метках снова «обнулит» цену и расписание маршрута, поэтому
    держим файл в латинице (роутер умеет складывать, но данные должны быть
    однозначными).
    """
    for vehicle in ("bus", "trolley"):
        bad = [label for label in SCHEDULE[vehicle] if any(ord(ch) > 127 for ch in label)]
        assert bad == [], "кириллица в метках расписания: %s" % bad


def test_normalize_label_folds_cyrillic_letters():
    assert TransitRouter.normalize_label("15К") == TransitRouter.normalize_label("15K")
    assert TransitRouter.normalize_label("8А") == TransitRouter.normalize_label("8A")
    # Слэш убирается — унаследованное поведение, на нём завязана сверка с трекером.
    assert TransitRouter.normalize_label("3/3a") == "33a"


def test_every_graph_route_finds_its_schedule(data):
    """Инвариант: маршрут из графа не может молча остаться без расписания."""
    graph, schedule, stops = data
    router = TransitRouter(graph, schedule, stops=stops, assume_in_service=True)
    missing = [key for key in graph["routes"] if router._schedule_for(key) is None]
    assert missing == [], "маршруты без расписания: %s" % missing


def test_every_route_has_a_tariff(data):
    """Нога без тарифа = 0 грн в итоговой цене; так молча «подешевел» 8A/15K."""
    graph, schedule, stops = data
    router = TransitRouter(graph, schedule, stops=stops, assume_in_service=True)
    for key, _route in graph["routes"].items():
        sched = router._schedule_for(key)
        assert sched, "нет расписания для %s" % key
        assert sched.get("tariff"), "нет тарифа для %s" % key


def test_schedule_lookup_survives_cyrillic_labels(data):
    """Даже если файл снова придёт с «8А»/«15К», роутер обязан найти расписание."""
    graph, schedule, stops = data
    cyrillic = json.loads(json.dumps(schedule))
    cyrillic["bus"]["8А"] = cyrillic["bus"].pop("8A")
    cyrillic["bus"]["15К"] = cyrillic["bus"].pop("15K")
    router = TransitRouter(graph, cyrillic, stops=stops, assume_in_service=True)
    for key in ("bus:8A:A", "bus:15K:A"):
        resolved = router._schedule_for(key)
        assert resolved is not None, key
        assert int(float(resolved["tariff"])) == 20, key


def test_reference_plans_have_no_free_legs(router, now):
    """Ни одна нога-поездка эталонных планов не должна стоить 0 грн."""
    for pair in ((107, 166), (181, 166), (65, 166)):
        plan = router.plan(*pair, now=now)
        if plan is None:
            continue
        for leg in plan["legs"]:
            if leg.get("type") != "transit":
                continue
            assert leg["price_grn"] > 0, (pair, leg)
