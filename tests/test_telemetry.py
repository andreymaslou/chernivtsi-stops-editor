# -*- coding: utf-8 -*-
"""
Тесты эндпоинта телеметрии выбора варианта плана (поставка 1, §13.3 брифа).

Журнал — logs/telemetry_plan_choices.jsonl. В тестах путь подменяется на
временную папку, чтобы записи не мусорили в репозитории.
"""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import main as app_main


def _payload(**overrides):
    """Валидный оффер из двух карточек — как его пришлёт эмулятор."""
    base = {
        "ts": "2026-09-21T13:00:00",
        "from_stop_id": 169,
        "to_stop_id": 68,
        "offer": [
            {
                "id": "default",
                "tags": ["Швидкий"],
                "total_min": 37,
                "price_grn": 52,
                "transfers": 2,
                "wait_min": 6.2,
                "source": "sim",
                "legs_signature": "trolley:3:B|walk|bus:9A:A",
            },
            {
                "id": "fewer_transfers",
                "tags": ["Дешевий"],
                "total_min": 44,
                "price_grn": 36,
                "transfers": 1,
                "wait_min": 5.1,
                "source": "sched",
                "legs_signature": "bus:23:B|bus:33:A",
            },
        ],
        "default_variant_id": "default",
        "variant_order": ["fewer_transfers", "default"],
        "chosen_variant_id": "fewer_transfers",
        "device_id": "anon-9f2c",
        "client": "web-emulator",
    }
    base.update(overrides)
    return base


@pytest.fixture
def telemetry_log(tmp_path, monkeypatch):
    """Путь журнала — во временную папку, иначе тесты засорят репозиторий."""
    path = tmp_path / "logs" / "telemetry_plan_choices.jsonl"
    monkeypatch.setattr(app_main, "TELEMETRY_LOG_PATH", path)
    return path


def _read_records(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

def test_plan_choice_is_logged(telemetry_log):
    with TestClient(app_main.app) as client:
        resp = client.post("/api/telemetry/plan_choice", json=_payload())

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert telemetry_log.exists(), "файл журнала не создан"

    records = _read_records(telemetry_log)
    assert len(records) == 1
    record = records[0]

    # Контрактные поля клиента сохранены как есть.
    assert record["from_stop_id"] == 169
    assert record["to_stop_id"] == 68
    assert record["default_variant_id"] == "default"
    assert record["chosen_variant_id"] == "fewer_transfers"
    assert record["variant_order"] == ["fewer_transfers", "default"]
    assert record["device_id"] == "anon-9f2c"
    assert record["client"] == "web-emulator"

    # Оффер записан целиком, вместе с source — без него ночной стенд на
    # симуляторе не отделить от предпочтений реальных пассажиров (§13.4).
    assert [variant["id"] for variant in record["offer"]] == ["default", "fewer_transfers"]
    assert record["offer"][0]["source"] == "sim"
    assert record["offer"][0]["legs_signature"] == "trolley:3:B|walk|bus:9A:A"

    # Сервер добавил свою метку приёма — часы клиента могут гулять, а для
    # анализа нужна достоверная хронология.
    assert record["received_at"]


def test_plan_choice_without_selection_logs_null(telemetry_log):
    """Карточки показали, но выбора не сделали — обязательная метрика (§13.3)."""
    with TestClient(app_main.app) as client:
        resp = client.post(
            "/api/telemetry/plan_choice", json=_payload(chosen_variant_id=None))

    assert resp.status_code == 200
    assert _read_records(telemetry_log)[0]["chosen_variant_id"] is None


def test_plan_choice_appends_not_overwrites(telemetry_log):
    """Каждый запрос дописывает новую строку — это поток, а не последний результат."""
    with TestClient(app_main.app) as client:
        assert client.post(
            "/api/telemetry/plan_choice", json=_payload(device_id="dev-1")).status_code == 200
        assert client.post(
            "/api/telemetry/plan_choice", json=_payload(device_id="dev-2")).status_code == 200
        assert client.post(
            "/api/telemetry/plan_choice", json=_payload(device_id="dev-3")).status_code == 200

    assert [r["device_id"] for r in _read_records(telemetry_log)] == [
        "dev-1", "dev-2", "dev-3"]



def test_plan_choice_accepts_string_stop_ids(telemetry_log):
    """stop_id приходит и числом, и строкой — клиент имеет право на любой тип."""
    with TestClient(app_main.app) as client:
        resp = client.post(
            "/api/telemetry/plan_choice",
            json=_payload(from_stop_id="169", to_stop_id="68"))

    assert resp.status_code == 200
    assert _read_records(telemetry_log)[0]["from_stop_id"] == "169"


def test_plan_choice_rejects_incomplete_payload(telemetry_log):
    """Без обязательных полей запись не делается — мусор в журнал не пишем."""
    broken = _payload()
    del broken["offer"]
    del broken["device_id"]

    with TestClient(app_main.app) as client:
        resp = client.post("/api/telemetry/plan_choice", json=broken)

    assert resp.status_code == 422
    assert not telemetry_log.exists(), "битый запрос всё же записался"


def test_concurrent_writes_do_not_interleave(telemetry_log):
    """Параллельные запросы не теряют и не вклинивают строки (thread-safety).

    FastAPI выполняет синхронные эндпоинты в пуле потоков, поэтому запись в
    журнал реально идёт параллельно — lock гарантирует целостность потока.
    Эндпоинт вызываем напрямую из потоков, а не через TestClient: каждый
    контекст TestClient поднимает lifespan и пишет в общий глобальный
    app_state, что само по себе гонка, не связанная с журналом.
    """
    payloads = [_payload(device_id="dev-%02d" % i) for i in range(20)]

    def post(payload):
        return app_main.log_plan_choice(app_main.PlanChoiceTelemetry(**payload))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(post, payloads))

    assert all(r["status"] == "ok" for r in results)
    records = _read_records(telemetry_log)
    assert len(records) == len(payloads), "часть записей потерялась"
    assert sorted(r["device_id"] for r in records) == sorted(
        p["device_id"] for p in payloads)
