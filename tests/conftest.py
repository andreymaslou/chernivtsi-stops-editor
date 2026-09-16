# -*- coding: utf-8 -*-
"""
Общие фикстуры тестов роутера: детерминированный прогон на данных проекта.

Ключевая идея: модельное время (`now`) фиксировано, а парк ТС берётся из
симулятора, поэтому `plan()` полностью воспроизводим. Это «золотой эталон»
для проверок корректности и регрессий — см. docs/BRIEF-router-correctness.md
и docs/REVIEW-router-perf.md.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import main as app_main  # noqa: E402  (нужен Locator + загрузка stops.json)
from router_layer import TransitRouter  # noqa: E402
from sim_layer import SimLayer  # noqa: E402

# Модельное время эталона. Ночь выбрана намеренно: симулятор «вытягивает»
# парк к середине рабочего дня, поэтому тест не зависит от времени запуска.
NOW = datetime(2026, 9, 17, 3, 10, 0)

# Эталон на данных от 2026-09-17: столько ТС строит симулятор на NOW.
EXPECTED_FLEET_SIZE = 119


@pytest.fixture(scope="session")
def now() -> datetime:
    return NOW


@pytest.fixture(scope="session")
def data():
    """Граф, расписание и остановки проекта (читаются один раз за прогон)."""
    graph = json.loads((REPO / "graph.json").read_text(encoding="utf-8"))
    schedule = json.loads((REPO / "routes_schedule.json").read_text(encoding="utf-8"))
    stops = app_main.apply_overrides(app_main.load_stops(app_main.STOPS_PATH))
    return graph, schedule, stops


@pytest.fixture(scope="session")
def sim(data):
    graph, schedule, _ = data
    return SimLayer(graph, schedule, assume_in_service=True)


@pytest.fixture
def fleet(sim, now):
    """Срез виртуального парка на модельное время."""
    return sim.snapshot(now=now)["vehicles"]


@pytest.fixture
def router(data, fleet, now):
    """Роутер в тестовом режиме (как на стенде: GPS_SIMULATOR=1)."""
    graph, schedule, stops = data
    r = TransitRouter(graph, schedule, stops=stops, assume_in_service=True)
    # Парк построен относительно `now`, поэтому он же — «момент среза»
    # (в main.py эту роль играет plan_now). Без него не с чем сверять ETA
    # машин и время прибытия пассажира.
    r.set_live(fleet, snapshot_at=now)
    return r
