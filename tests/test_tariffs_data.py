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
    source_ids = {source["id"] for source in TARIFFS["sources"]}
    assert len(source_ids) >= 8
    for source in TARIFFS["sources"]:
        assert source["url"].startswith("http")
        assert source["checked"]
    # У каждого правила, где цифра неочевидна, должна быть ссылка на источник,
    # и этот источник обязан существовать в списке (иначе ссылка «в никуда»).
    for name, category in TARIFFS["categories"].items():
        for rule in category["rules"]:
            assert rule.get("source"), "правило без source: %s %r" % (name, rule)
            assert rule["source"] in source_ids, rule["source"]


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
    assert _amount("privileged", "bus", scope="privileged_communal") == 0
    assert _amount("privileged", "bus", scope="privileged_private") == 0
    assert _amount("privileged", "bus", scope="else") == 20
    free = TARIFFS["routes"]["privileged_free_bus"]
    assert free == ["1", "6", "7", "8", "8A", "9", "10A", "13", "15", "15K", "23", "24"]
    # Перелік виконкому охоплює і приватні маршрутки (пільгові місця).
    private = TARIFFS["routes"]["privileged_free_bus_private"]
    assert private == ["3", "4", "19", "21", "26", "27", "36", "37"]
    # Документ має окремий розділ «Автобусні маршрути без забезпечення пільгових
    # перевезень» — ці номери не мають бути у жодному з пільгових списків.
    for route in TARIFFS["routes"]["privileged_not_covered_bus"]:
        assert route not in free and route not in private
    # Пенсія сама собою не дає безкоштовного проїзду — потрібне посвідчення.
    assert "посвідчення" in TARIFFS["categories"]["privileged"]["requires_document"]
    assert TARIFFS["routes"]["privileged_seats_share"] == 0.3


def _document_label(route):
    """Метка маршрута как в документе: без дефиса, латиница, верхний регистр."""
    return str(route).upper().replace("А", "A").replace("К", "K").replace("-", "").strip()


def _document_status(route):
    """Статус маршрута по документу: 'privileged' | 'without' | None (не покрыт).

    В документе часть вариантов записана базовым номером (есть «8», нет «8А»;
    есть «15», нет «15К»), а часть — отдельной строкой (9 и 9-А, 10 и 10-А).
    Поэтому сначала точное совпадение, потом откат к базовому номеру.
    """
    privileged = {_document_label(r) for r in TARIFFS["routes"]["privileged_document_entries"]}
    without = {_document_label(r) for r in TARIFFS["routes"]["privileged_not_covered_bus"]}
    label = _document_label(route)
    base = label.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ") or label
    if label in privileged:
        return "privileged"
    if label in without:
        return "without"
    if base in privileged:
        return "privileged"
    if base in without:
        return "without"
    return None


def test_privileged_document_covers_every_bus_route():
    """Все 30 автобусов графа должны быть в одном из двух разделов решения.

    Список не «примерный»: виконком делит маршруты на «на яких відшкодовуються
    втрати…» (18) і «без забезпечення пільгових перевезень» (16). Покриття
    проверяется по базовому номеру, чтобы 8А/15К не выпадали (их в документе
    нет отдельной строкой, но есть их базовые 8 и 15).
    """
    privileged_entries = TARIFFS["routes"]["privileged_document_entries"]
    without_entries = TARIFFS["routes"]["privileged_not_covered_bus"]
    assert len(privileged_entries) == 18
    assert len(without_entries) == 16

    unclassified = [route for route in SCHEDULE["bus"] if _document_status(route) is None]
    assert unclassified == [], "документ не покриває: %s" % unclassified

    # Курируемые списки обязаны совпасть с тем, что следует из документа.
    from_document = {
        _document_label(route)
        for route in SCHEDULE["bus"]
        if _document_status(route) == "privileged"
    }
    curated = {
        _document_label(route)
        for route in (TARIFFS["routes"]["privileged_free_bus"]
                      + TARIFFS["routes"]["privileged_free_bus_private"])
    }
    assert curated == from_document, sorted(from_document ^ curated)


def test_8a_and_15k_are_privileged_as_variants():
    """8А і 15К у документі не мають власного рядка — це варіанти 8 і 15."""
    free = TARIFFS["routes"]["privileged_free_bus"]
    assert "8A" in free and "15K" in free
    assert _document_status("8A") == "privileged"
    assert _document_status("15K") == "privileged"
    # А ось 9А і 10 — окремі рядки документа, і вони БЕЗ пільг.
    assert _document_status("9A") == "without"
    assert _document_status("10") == "without"
    assert _document_status("10A") == "privileged"
    note = TARIFFS["routes"]["privileged_document_note"]
    assert "15K" in note and "8A" in note


def test_privileged_has_official_decision_references():
    """Перелік затверджує виконком — посилання на рішення мусять бути в даних."""
    documents = TARIFFS["routes"]["privileged_documents"]
    acts = " ".join(item["act"] for item in documents)
    assert "673/35" in acts
    assert any(item["date"] == "2022-11-08" for item in documents)
    assert any(item["date"] == "2026-07-29" for item in documents), "маршрут №7"
    for item in documents:
        assert item["url"].startswith("http")


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
