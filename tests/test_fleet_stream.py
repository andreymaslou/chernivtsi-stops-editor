# -*- coding: utf-8 -*-
"""
Тесты потока парка (SSE) и переключателя источника парка.

Зачем: в эмуляторе появились кнопки «джерело» (auto / GPS / симулятор) —
днём владелец смотрит живой GPS перевозчика, вечером, когда машины уехали в
депо, — как отработал симулятор. Тот же срез, но потоком, отдаёт
/api/fleet/stream: клиент держит одно соединение вместо поллинга /api/live.

Проверяем ровно то, что легко сломать молча: (1) source переопределяет режим
стенда на один запрос, а неверное значение — это 400, а не тихий откат на
.env; (2) поток — валидный SSE и его JSON совпадает с /api/live (иначе
клиент, откатившийся на поллинг, увидел бы другой парк); (3) ошибки первого
снимка потока (нет трекера, битый now) приходят HTTP-статусом, а не пустым
потоком; (4) /api/manifest отдаёт whitelist из .agents/rules/active_routes.md.

Сеть не трогаем: conftest держит PARK_SOURCE=sim, трекера в app_state нет.
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main as app_main

REPO = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO / "data" / "routes_manifest.json"

# Полдень: симулятор гарантированно держит машины на линии, поэтому тесты не
# зависят ни от времени прогона, ни от ASSUME_IN_SERVICE.
MODEL_NOW = "2026-09-17T12:00:00"


def _vehicle(board="3001", route_label="5", vehicle_type="bus", **overrides):
    """Нормализованная машина реального трекера — контракт live_layer."""
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
        "gpstime": "2026-09-17 11:59:55",
        "age_seconds": 5.0,
        "in_depo": False,
        "is_live": True,
        "status": "live",
        "carrier": "",
        "remark": "",
    }
    base.update(overrides)
    return base


class _FakeTracker:
    """Заглушка LiveTracker: фиксированный срез, без опроса сети."""

    def __init__(self, vehicles):
        self._vehicles = vehicles
        self.route_count = 1
        self.poll_count = 1

    def snapshot(self, only_fresh=True, include_depo=False, route_ids=None, vehicle_types=None):
        selected = list(self._vehicles)
        if not include_depo:
            selected = [v for v in selected if not v["in_depo"]]
        if only_fresh:
            selected = [v for v in selected if v["is_live"]]
        return {
            "source": "https://trans-gps.cv.ua",
            "generated_at": "2026-09-17 12:00:00",
            "last_success_at": "2026-09-17 12:00:00",
            "last_error": None,
            "poll_interval_seconds": 5.0,
            "fresh_max_age_seconds": 300.0,
            "counts": {
                "total": len(self._vehicles),
                "live": sum(1 for v in self._vehicles if v["is_live"]),
                "stale": 0,
                "in_depo": sum(1 for v in self._vehicles if v["in_depo"]),
                "unknown_gpstime": 0,
            },
            "returned": len(selected),
            "routes": {},
            "vehicles": selected,
        }


def _sse_frames(body: str):
    """Разбирает тело ответа SSE в [(event, payload)] и проверяет формат кадра."""
    frames = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block or block.startswith("retry:"):
            continue
        event = None
        data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[len("event: "):].strip()
            elif line.startswith("data: "):
                data = line[len("data: "):]
            else:
                raise AssertionError(f"недопустимая строка кадра SSE: {line!r}")
        assert event, f"кадр SSE без имени события: {block!r}"
        frames.append((event, json.loads(data) if data else None))
    return frames


@pytest.fixture(scope="module")
def api_client():
    """Сервер целиком (lifespan один раз на модуль): PARK_SOURCE=sim из conftest."""
    with TestClient(app_main.app) as client:
        yield client


@pytest.fixture
def auto_with_real_vehicle(monkeypatch):
    """Стенд в режиме auto: реальный трекер отдаёт одну свежую машину."""
    monkeypatch.setattr(app_main, "PARK_SOURCE", "auto")
    monkeypatch.setitem(app_main.app_state, "tracker", _FakeTracker([_vehicle(board="7777")]))
    return None


# --- /api/manifest: whitelist активных маршрутов ---------------------------

def test_manifest_returns_whitelist_counts(api_client):
    """/api/manifest: 30 автобусных + 8 троллейбусных маршрутов."""
    resp = api_client.get("/api/manifest")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["counts"] == {"bus": 30, "trolley": 8, "total": 38}
    assert [item["id"] for item in body["bus"]][:2] == ["bus:1", "bus:3"]
    assert [item["id"] for item in body["trolley"]][:2] == ["trolley:1", "trolley:2"]


def test_manifest_matches_repository_file(api_client):
    """Отданное наружу — ровно то, что лежит в data/routes_manifest.json."""
    expected = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    body = api_client.get("/api/manifest").json()

    assert body["version"] == expected["version"]
    assert body["bus"] == expected["bus"]
    assert body["trolley"] == expected["trolley"]


def test_manifest_ids_are_unique(api_client):
    """Кнопки фильтра строятся по id: дубли дали бы двойные кнопки."""
    body = api_client.get("/api/manifest").json()
    ids = [item["id"] for item in body["bus"] + body["trolley"]]

    assert len(ids) == len(set(ids)) == 38


# --- Переключатель источника у /api/live -----------------------------------

def test_live_source_sim_returns_simulator_snapshot(api_client):
    """source=sim: срез симулятора как есть (машины поле source не несут)."""
    resp = api_client.get("/api/live", params={"source": "sim", "now": MODEL_NOW})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "sim"
    assert body["vehicles"], "в полдень симулятор держит машины на линии"
    assert all("source" not in v for v in body["vehicles"])


def test_live_source_overrides_env_auto(api_client, auto_with_real_vehicle):
    """source на запрос важнее PARK_SOURCE: днём GPS, вечером симулятор."""
    mixed = api_client.get("/api/live", params={"now": MODEL_NOW}).json()
    sim_only = api_client.get("/api/live", params={"source": "sim", "now": MODEL_NOW}).json()

    assert mixed["source"] == "mixed"
    assert mixed["by_source"]["real"] == 1
    assert sim_only["source"] == "sim"
    assert all(v["board_number"] != "7777" for v in sim_only["vehicles"]), \
        "реальный трекер не должен попадать в срез при source=sim"


def test_merge_fleet_trolleys_always_from_sim(api_client, monkeypatch):
    """§3.5: троллейбусы из реального GPS никогда не вытесняют симулятор.

    У перевозчика trans-gps.cv.ua реально ездит 1 тролл, охват маршрутов
    нулевой. Симулятор держит все 8 маршрутов с правильным интервалом.
    Инвариант: даже если трекер отдал живого тролла — в mixed-срезе его
    маршрут всё равно берётся из симулятора, а не из GPS.
    """
    # Реальный трекер отдаёт одну живую машину-троллейбус (маршрут «3/3a»).
    trolley = _vehicle(board="T-999", route_label="3/3a", vehicle_type="trolley")
    monkeypatch.setattr(app_main, "PARK_SOURCE", "auto")
    monkeypatch.setitem(app_main.app_state, "tracker", _FakeTracker([trolley]))

    body = api_client.get("/api/live", params={"now": MODEL_NOW}).json()

    assert body["source"] == "mixed"
    sim_trolleys = [
        v for v in body["vehicles"]
        if v.get("source") == "sim" and v.get("vehicle_type") == "trolley"
    ]
    assert sim_trolleys, (
        "симовские троллейбусы должны быть в срезе даже при наличии реального тролла"
    )
    # Маршрут «3» не должен быть захвачен реальным GPS: симовские машины остаются.
    route_3_sim = [v for v in sim_trolleys if "3" in str(v.get("route_label", ""))]
    assert route_3_sim, (
        "маршрут троллейбус-3 должен быть представлен симовскими машинами, "
        "а не только одним реальным бортом"
    )


def test_live_source_gps_without_tracker_is_503(api_client):
    """source=gps при неподнятом трекере — честный 503, а не пустой парк."""
    resp = api_client.get("/api/live", params={"source": "gps"})

    assert resp.status_code == 503
    assert "tracker" in resp.json()["detail"].lower()


def test_live_unknown_source_is_400(api_client):
    """Опечатка в source не должна молча подменяться режимом стенда."""
    resp = api_client.get("/api/live", params={"source": "satellite"})

    assert resp.status_code == 400
    assert "source" in resp.json()["detail"]


# --- Поток парка (SSE): /api/fleet/stream ---------------------------------

def test_stream_once_sends_one_snapshot(api_client):
    """once=true: один кадр snapshot, SSE-заголовки, без буферизации у прокси."""
    resp = api_client.get(
        "/api/fleet/stream",
        params={"source": "sim", "now": MODEL_NOW, "once": "true"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["x-accel-buffering"] == "no"
    assert resp.text.startswith("retry: "), "браузеру нужен интервал переподключения"

    frames = _sse_frames(resp.text)
    assert [event for event, _ in frames] == ["snapshot"]
    payload = frames[0][1]
    assert payload["source"] == "sim"
    assert payload["vehicles"], "кадр должен нести парк, а не пустой список"


def test_stream_limit_sends_requested_frames(api_client):
    """limit=2: два кадра, между ними пауза interval (проверяем быстрым 0.5 с)."""
    resp = api_client.get(
        "/api/fleet/stream",
        params={"source": "sim", "now": MODEL_NOW, "limit": 2, "interval": 0.5},
    )

    assert resp.status_code == 200, resp.text
    frames = _sse_frames(resp.text)
    assert len(frames) == 2
    assert all(event == "snapshot" for event, _ in frames)


def test_stream_snapshot_matches_live_polling(api_client):
    """Поток и поллинг — один JSON: фолбек на /api/live показывает тот же парк."""
    params = {"source": "sim", "now": MODEL_NOW}
    live = api_client.get("/api/live", params=params).json()
    streamed = _sse_frames(
        api_client.get("/api/fleet/stream", params={**params, "once": "true"}).text
    )[0][1]

    assert set(streamed) == set(live)
    assert streamed["vehicles"] == live["vehicles"]
    assert streamed["counts"] == live["counts"]
    assert streamed["routes"] == live["routes"]


def test_vehicle_types_filter_keeps_trolleys_out_of_request(api_client):
    """Тролейбуси ховаються ЗАПИТОМ, а не правкою даних (§3.5).

    Емулятор за замовчуванням просить vehicle_types=bus: у перевізника живий
    охват тролейбусів нульовий, а в ефірі парку потрібні автобуси. Принципово,
    що це фільтр запиту: без параметра тролейбуси в срезі лишаються — інакше
    довелося б правити манифест (whitelist збірки графа, graph_layer.py:729)
    і роутер упав би на наступній перезбірці.
    """
    base = {"source": "sim", "now": MODEL_NOW}
    buses = _sse_frames(
        api_client.get(
            "/api/fleet/stream", params={**base, "vehicle_types": "bus", "once": "true"}
        ).text
    )[0][1]
    everything = _sse_frames(
        api_client.get("/api/fleet/stream", params={**base, "once": "true"}).text
    )[0][1]

    assert buses["vehicles"], "в полдень симулятор держит машины на линии"
    assert {v["vehicle_type"] for v in buses["vehicles"]} == {"bus"}
    assert "trolley" in {v["vehicle_type"] for v in everything["vehicles"]}, \
        "без фильтра троллейбусы должны оставаться в срезе (данные не правим)"

    # Тот же фильтр у поллинга — клиент, откатившийся с потока, видит то же.
    live = api_client.get("/api/live", params={**base, "vehicle_types": "bus"}).json()
    assert live["vehicles"] and {v["vehicle_type"] for v in live["vehicles"]} == {"bus"}


def test_stream_first_failure_is_http_error(api_client):
    """Нет трекера/битый now: HTTP-статус до потока, иначе EventSource молчит вечно."""
    no_tracker = api_client.get("/api/fleet/stream", params={"source": "gps", "once": "true"})
    bad_now = api_client.get("/api/fleet/stream", params={"source": "sim", "now": "вчора"})
    bad_source = api_client.get("/api/fleet/stream", params={"source": "satellite"})

    assert no_tracker.status_code == 503
    assert "text/event-stream" not in no_tracker.headers.get("content-type", "")
    assert bad_now.status_code == 400
    assert bad_source.status_code == 400


def test_stream_interval_bounds_are_enforced(api_client):
    """Слишком частый/редкий интервал — ошибка валидации, а не тихая подмена."""
    too_fast = api_client.get(
        "/api/fleet/stream", params={"source": "sim", "interval": 0.01, "once": "true"}
    )
    too_slow = api_client.get(
        "/api/fleet/stream", params={"source": "sim", "interval": 600, "once": "true"}
    )

    assert too_fast.status_code == 422
    assert too_slow.status_code == 422
