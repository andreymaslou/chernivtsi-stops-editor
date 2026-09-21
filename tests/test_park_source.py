# -*- coding: utf-8 -*-
"""
Тесты приоритета источников парка (поставка 2, §3 брифа «Идея A»).

merge_fleet решает, какие машины увидит пользователь: маршрут со свежей
реальной машиной вне депо целиком берётся из трекера, остальные — из
симулятора, чтобы демо не было пустым. Интеграционные тесты гоняют собранный
парк через /api/live и /api/plan. PARK_SOURCE принудительно sim (conftest),
чтобы эталоны не зависели от трекера и наличия интернета.
"""
import pytest
from fastapi.testclient import TestClient

import main as app_main

# Эталонная пара из test_router_plan.py: пл. Соборна -> Завод «Гравітон».
PLAN_TEXT = "з площі Соборної до заводу Гравітон"
PLAN_NOW = "2026-09-17T03:10:00"


def _vehicle(board="2001", route_label="5", vehicle_type="bus", **overrides):
    """Нормализованная машина — как отдают оба слоя парка."""
    base = {
        "imei": f"imei-{board}",
        "vehicle_id": 1,
        "board_number": board,
        "vehicle_type": vehicle_type,
        "route_id": 105,
        "route_name": route_label,
        "route_label": route_label,
        "route_colour_name": None,
        "route_colour_hex": "#ff00ff",
        "lat": 48.3,
        "lon": 25.9,
        "speed_kmh": 20.0,
        "heading_deg": 90.0,
        "gpstime": "2026-09-21 12:00:00",
        "age_seconds": 5.0,
        "in_depo": False,
        "is_live": True,
        "status": "live",
        "carrier": "",
        "remark": "",
        "direction": "A",
    }
    base.update(overrides)
    return base


# --- Чистая функция слияния: правило приоритета источников -------------------

def test_merge_prefers_real_when_route_has_fresh_real():
    """Свежая реальная машина на маршруте — берём только реальные."""
    real = [_vehicle(board="3001", route_label="5")]
    sim = [_vehicle(board="SIM-5-001", route_label="5")]

    merged = app_main.merge_fleet(real, sim)

    assert [v["board_number"] for v in merged] == ["3001"]
    assert merged[0]["source"] == "real"


def test_merge_fills_route_without_real_with_sim():
    """Маршрут без свежих реальных машин — заполняем виртуальными."""
    real = [_vehicle(board="3001", route_label="5")]
    sim = [
        _vehicle(board="SIM-5-001", route_label="5"),
        _vehicle(board="SIM-9A-001", route_label="9A"),
    ]

    merged = app_main.merge_fleet(real, sim)

    by_route = {v["route_label"]: v["source"] for v in merged}
    assert by_route == {"5": "real", "9A": "sim"}


def test_merge_stale_real_does_not_block_simulator():
    """Старый трек не даёт приоритета: дыра должна остаться видимой."""
    real = [_vehicle(board="3001", route_label="5", is_live=False, status="stale", age_seconds=999.0)]
    sim = [_vehicle(board="SIM-5-001", route_label="5")]

    merged = app_main.merge_fleet(real, sim)

    boards = {v["board_number"]: v["source"] for v in merged}
    # Свежесть проверяется до слияния (§3.3 п.2): stale-машина приоритет не
    # забирает, но из ответа не пропадает — клиент видит её «за розкладом».
    assert boards["SIM-5-001"] == "sim"


def test_merge_depo_real_does_not_block_simulator():
    """Машина в депо — маршрут не занят, симулятор должен закрыть направление."""
    real = [_vehicle(board="3001", route_label="5", in_depo=True, status="depo")]
    sim = [_vehicle(board="SIM-5-001", route_label="5")]

    merged = app_main.merge_fleet(real, sim)

    boards = {v["board_number"]: v["source"] for v in merged}
    assert boards["SIM-5-001"] == "sim"


def test_merge_same_label_different_types_are_different_routes():
    """«5» автобус и «5» троллейбус — разные маршруты, один другой не закрывает."""
    real = [_vehicle(board="3001", route_label="5", vehicle_type="trolley")]
    sim = [_vehicle(board="SIM-5-001", route_label="5", vehicle_type="bus")]

    merged = app_main.merge_fleet(real, sim)

    assert len(merged) == 2
    assert all(v["source"] == "real" for v in merged if v["vehicle_type"] == "trolley")
    assert all(v["source"] == "sim" for v in merged if v["vehicle_type"] == "bus")


def test_merge_marks_every_vehicle_with_source():
    """Каждая машина несёт source — иначе UI не нарисует бейдж «SIM»."""
    merged = app_main.merge_fleet(
        [_vehicle(board="3001", route_label="5")],
        [_vehicle(board="SIM-9A-001", route_label="9A")],
    )
    assert {v["source"] for v in merged} == {"real", "sim"}


def test_merge_does_not_mutate_inputs():
    """Слияние чистое: слои отдают срезы, и портить их нельзя."""
    real = [_vehicle(board="3001", route_label="5")]
    sim = [_vehicle(board="SIM-9A-001", route_label="9A")]

    app_main.merge_fleet(real, sim)

    assert "source" not in real[0]
    assert "source" not in sim[0]


# --- Интеграция: /api/plan и /api/live --------------------------------------

@pytest.fixture
def api_client():
    """Запущенный сервер: lifespan собирает сим-парк (PARK_SOURCE=sim)."""
    with TestClient(app_main.app) as client:
        yield client


def test_plan_uses_simulator_fleet_in_test_mode(api_client, plan_llm_stub):
    """Эталонный план строится на виртуальном парке, источник — sim."""
    resp = api_client.post("/api/plan", json={"text": PLAN_TEXT, "now": PLAN_NOW})

    assert resp.status_code == 200, resp.text
    plan = resp.json()
    assert plan["mode"] == "plan", plan.get("note")
    assert plan["fleet_source"] == "sim"

    transit = [leg for leg in plan["legs"] if leg["type"] == "transit"]
    assert transit, "в плане должна быть хотя бы одна нога-поездка"
    # Сим-парк помечает машины самим роутером через fleet-level source,
    # ноги без живого борта — "sched" (живого трекера в тестах нет).
    assert all(leg["source"] in ("sim", "sched") for leg in transit)


class _FakeTracker:
    """Заглушка LiveTracker: отдаёт фиксированный срез без опроса сети.

    Повторяет контракт live_layer.snapshot(): те же поля, те же фильтры,
    и НЕ добавляет direction — реальный трекер его не отдаёт (§3.2).
    """

    def __init__(self, vehicles):
        self._vehicles = vehicles
        self.route_count = 1
        self.poll_count = 1

    def snapshot(self, only_fresh=True, include_depo=False,
                 route_ids=None, vehicle_types=None):
        selected = list(self._vehicles)
        if route_ids:
            wanted = {int(rid) for rid in route_ids}
            selected = [v for v in selected if v["route_id"] in wanted]
        if vehicle_types:
            wanted = {str(t).strip().lower() for t in vehicle_types}
            selected = [v for v in selected if v["vehicle_type"] in wanted]
        if not include_depo:
            selected = [v for v in selected if not v["in_depo"]]
        if only_fresh:
            selected = [v for v in selected if v["is_live"]]
        return {
            "source": "https://trans-gps.cv.ua",
            "generated_at": "2026-09-17 03:10:00",
            "last_success_at": "2026-09-17 03:10:00",
            "last_error": None,
            "poll_interval_seconds": 5.0,
            "fresh_max_age_seconds": 300.0,
            "counts": {
                "total": len(self._vehicles),
                "live": sum(1 for v in self._vehicles if v["is_live"]),
                "stale": sum(1 for v in self._vehicles if v["status"] == "stale"),
                "in_depo": sum(1 for v in self._vehicles if v["in_depo"]),
                "unknown_gpstime": 0,
            },
            "returned": len(selected),
            "routes": {},
            "vehicles": selected,
        }


@pytest.fixture
def auto_mode(park_source_is_sim, monkeypatch):
    """Смешанный режим на один тест: PARK_SOURCE=auto."""
    monkeypatch.setattr(app_main, "PARK_SOURCE", "auto")
    return None


def test_live_sim_mode_returns_pure_simulator_snapshot(api_client):
    """PARK_SOURCE=sim: /api/live отдаёт срез симулятора как есть (без source)."""
    resp = api_client.get("/api/live", params={"now": PLAN_NOW})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "sim"
    # Сим-парк — моно-режим: поле source машинами не расставляется,
    # это делает только merge_fleet в смешанном режиме.
    assert all("source" not in v for v in body["vehicles"])


def test_live_auto_mode_merges_both_sources(api_client, sim, now, fleet, auto_mode, monkeypatch):
    """PARK_SOURCE=auto: маршрут со свежей реальной машиной — только реальный."""
    # Маршрут берём из самого сим-парка — точно существует на NOW.
    probe = fleet[0]
    real = [_vehicle(
        board="7777",
        route_label=probe["route_label"],
        vehicle_type=probe["vehicle_type"],
        route_id=probe["route_id"],
        is_live=True,
        in_depo=False,
    )]
    # У реального трекера нет direction — сим-машины его имеют, и смешанный
    # срез это допускает (строгость фильтра направления разная, §3.2).
    real[0].pop("direction")
    monkeypatch.setitem(app_main.app_state, "sim_layer", sim)
    monkeypatch.setitem(app_main.app_state, "tracker", _FakeTracker(real))

    resp = api_client.get("/api/live", params={"now": PLAN_NOW})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "mixed"
    assert body["by_source"]["real"] == 1

    on_route = [
        v for v in body["vehicles"]
        if v["route_label"] == probe["route_label"] and v["vehicle_type"] == probe["vehicle_type"]
    ]
    assert on_route, "маршрут-зонд должен присутствовать в срезе"
    assert all(v["source"] == "real" for v in on_route), \
        "со свежей реальной машиной маршрут весь берётся из трекера"
    assert not any(v["source"] == "sim" for v in on_route), \
        "симулятор не должен подмешиваться на занятый маршрут"


def test_live_auto_mode_fills_holes_with_simulator(api_client, sim, fleet, auto_mode, monkeypatch):
    """Маршруты без свежих реальных машин остаются за симулятором."""
    probe = fleet[0]
    real = [_vehicle(
        board="7777",
        route_label=probe["route_label"],
        vehicle_type=probe["vehicle_type"],
        route_id=probe["route_id"],
    )]
    real[0].pop("direction")
    monkeypatch.setitem(app_main.app_state, "sim_layer", sim)
    monkeypatch.setitem(app_main.app_state, "tracker", _FakeTracker(real))

    resp = api_client.get("/api/live", params={"now": PLAN_NOW})

    body = resp.json()
    sim_left = [v for v in body["vehicles"] if v["source"] == "sim"]
    assert sim_left, "остальные маршруты должны остаться виртуальными"
    assert all(
        (v["route_label"], v["vehicle_type"]) != (probe["route_label"], probe["vehicle_type"])
        for v in sim_left
    ), "симулятор не должен дублировать занятый маршрут"


@pytest.fixture
def plan_llm_stub(monkeypatch):
    """/api/plan без реального вызова LLM: точки подставляются напрямую."""
    monkeypatch.setattr(
        app_main, "call_llm_extract_locations",
        lambda text: {"from": "пл. Соборна", "to": "Завод «Гравітон»"},
    )
    return None


def test_plan_marks_leg_source_by_vehicle_in_auto_mode(
    api_client, sim, fleet, auto_mode, plan_llm_stub, monkeypatch
):
    """PARK_SOURCE=auto: нога плана помечается источником своей машины.

    Сначала строим план на сим-парке и находим борд первой ноги — точно
    проходит фильтры роутера. Потом тот же борт присылаем «от трекера»:
    в смешанном срезе он обязан остаться «перший потрібний ТС», а источник
    ноги — real (а не fleet-level, иначе телеметрия соврёт про бакет).
    """
    resp = api_client.post("/api/plan", json={"text": PLAN_TEXT, "now": PLAN_NOW})
    assert resp.status_code == 200, resp.text
    base = resp.json()
    assert base["mode"] == "plan", base.get("note")
    assert base["fleet_source"] == "sim"

    transit = [leg for leg in base["legs"] if leg["type"] == "transit"]
    assert transit
    leg = transit[0]
    board = leg.get("live_bus")
    assert board, "нога должна найти живой борт — иначе проверять нечего"

    probe = next(v for v in fleet if v["board_number"] == board)
    real = [dict(probe)]
    # Реальный трекер не отдаёт direction — роутер тогда проверяет курс
    # (heading_deg); для этой же машины направление у нас подтверждённое.
    real[0].pop("direction", None)
    monkeypatch.setitem(app_main.app_state, "sim_layer", sim)
    monkeypatch.setitem(app_main.app_state, "tracker", _FakeTracker(real))

    resp = api_client.post("/api/plan", json={"text": PLAN_TEXT, "now": PLAN_NOW})
    assert resp.status_code == 200, resp.text
    mixed = resp.json()
    assert mixed["mode"] == "plan"
    # Смешанный парк: корень говорит «mixed», а каждая нога — свой источник.
    assert mixed["fleet_source"] == "mixed"

    mixed_leg = next(
        leg for leg in mixed["legs"]
        if leg["type"] == "transit" and leg.get("live_bus") == board
    )
    assert mixed_leg["source"] == "real", "борт пришёл из трекера — нога обязана это сказать"

