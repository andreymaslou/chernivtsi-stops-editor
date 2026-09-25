# -*- coding: utf-8 -*-
"""Тарифы черновицкого транспорта (`data/tariffs.json`) и их согласованность.

Данные собраны из официальных источников КП ЧТУ (см. `sources` в файле).
Тесты держат две вещи:

1. Ключевые цифры по категориям (дорослий, школяр, студент, пільгова) — чтобы
   их нельзя было «подправить» случайно;
2. Согласованность с уже существующими данными проекта: тариф «дорослий»
   обязан совпадать с `routes_schedule.json` (оттуда его читает роутер), а
   списки маршрутов — существовать в расписании и покрывать его полностью.
"""
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARIFFS = json.loads((REPO / "data" / "tariffs.json").read_text(encoding="utf-8"))
SCHEDULE = json.loads((REPO / "routes_schedule.json").read_text(encoding="utf-8"))


def _normalize_route(route):
    """«8А»/«8A» и «15К»/«15K» — одна и та же метка: расписание и граф пишут
    латиницу, а документы ЧТУ украинскими буквами."""
    return str(route).replace("А", "A").replace("К", "K").upper()


def _amount(category, vehicle, **conditions):
    """Сумма по первому правилу категории с такими же vehicle и условиями.

    Условия живут и в `conditions` (период/дни), и на верхнем уровне правила
    (`scope`, `routes`) — сверяем всё вместе.
    """
    for rule in TARIFFS["categories"][category]["rules"]:
        if rule["vehicle"] != vehicle:
            continue
        rule_fields = dict(rule)
        rule_fields.update(rule.get("conditions", {}))
        if all(rule_fields.get(key) == value for key, value in conditions.items()):
            return rule["amount"]
    raise AssertionError("нет правила для %s / %s / %r" % (category, vehicle, conditions))


def test_meta_and_sources_are_present():
    assert TARIFFS["version"] >= 1
    assert TARIFFS["updated"] == "2026-09-25"
    assert TARIFFS["currency"] == "UAH"
    assert len(TARIFFS["sources"]) >= 5
    for source in TARIFFS["sources"]:
        assert source["url"].startswith("http")
        assert source["checked"]
    # У каждого правила, где цифра неочевидна, должна быть ссылка на источник.
    for name, category in TARIFFS["categories"].items():
        for rule in category["rules"]:
            assert rule.get("source"), "правило без source: %s %r" % (name, rule)


def test_adult_tariff_matches_schedule():
    """Базовый тариф не имеет права разойтись с тем, что читает роутер."""
    for vehicle, key in (("bus", "bus"), ("trolley", "trolley")):
        rates = {int(float(route["tariff"])) for route in SCHEDULE[key].values()}
        assert rates == {TARIFFS["base_fare"][vehicle]}, (vehicle, rates)
        assert _amount("adult", vehicle) == TARIFFS["base_fare"][vehicle]


def test_schoolchild_rules():
    assert _amount("schoolchild", "trolley", period="calendar_year") == 0
    assert _amount("schoolchild", "bus", period="school_year", days="weekdays") == 10
    assert _amount("schoolchild", "bus", period="school_year", days="weekends_holidays") == 20
    assert _amount("schoolchild", "bus", period="summer_break") == 20
    assert TARIFFS["categories"]["schoolchild"]["requires_document"] == "учнівський квиток"


def test_student_rules():
    assert _amount("student", "trolley", period="school_year") == 8
    assert _amount("student", "trolley", period="summer_break") == 16
    assert _amount("student", "bus") == 20
    assert TARIFFS["cards"]["student"]["card_price_grn"] == 200
    assert TARIFFS["cards"]["student"]["preloaded_grn"] == 125


def test_privileged_rules():
    assert _amount("privileged", "trolley") == 0
    assert _amount("privileged", "bus", scope="communal") == 0
    assert _amount("privileged", "bus", scope="else") == 20
    free = TARIFFS["routes"]["privileged_free_bus"]
    assert free == ["1", "6", "7", "8", "8A", "9", "10A", "13", "15", "23", "24"]
    # ЧТУ прямо перелічує «непільгові» маршрути — вони не мають бути у вільному списку.
    for route in TARIFFS["routes"]["privileged_excluded_bus"]:
        assert route not in free
    # Пенсія сама собою не дає безкоштовного проїзду — потрібне посвідчення.
    assert "посвідчення" in TARIFFS["categories"]["privileged"]["requires_document"]


def test_bus_routes_are_fully_classified():
    """Каждый автобусный маршрут обязан быть либо комунальним, либо неизвестным.

    Иначе тариф молча останется без пільг/скидок, и никто этого не заметит.
    """
    schedule_routes = {_normalize_route(route) for route in SCHEDULE["bus"]}
    communal = {_normalize_route(route) for route in TARIFFS["routes"]["communal_bus"]}
    unknown = {_normalize_route(route) for route in TARIFFS["routes"]["private_bus_unknown"]}
    assert communal.isdisjoint(unknown)
    assert communal | unknown == schedule_routes, (
        "не класифіковані: %s" % sorted(schedule_routes - communal - unknown)
    )
    # Пільговий перелік — тільки серед комунальних.
    free = {_normalize_route(route) for route in TARIFFS["routes"]["privileged_free_bus"]}
    assert free <= communal


def test_tickets_and_open_questions():
    assert TARIFFS["tickets"]["day"]["amount"] == 90
    assert TARIFFS["tickets"]["terminal_60min"]["transfer_window_min"] == 60
    # Открытые вопросы фиксируются в данных, а не только в переписке.
    assert len(TARIFFS["open_questions"]) >= 4
