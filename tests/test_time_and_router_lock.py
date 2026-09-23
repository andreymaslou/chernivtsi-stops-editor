"""Регрессии временной модели и защиты общего TransitRouter."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import main
from live_layer import normalize_vehicle, parse_gpstime
from time_utils import APP_TIMEZONE, as_kyiv


def test_gps_time_is_kyiv_and_utc_now_does_not_make_it_stale():
    parsed = parse_gpstime("2026-09-23 12:00:00")
    assert parsed is not None
    assert parsed.tzinfo == APP_TIMEZONE

    vehicle = normalize_vehicle(
        {
            "gpstime": "2026-09-23 12:00:00",
            "lat": 48.29,
            "lng": 25.93,
            "idBusTypes": 1,
            "routeId": 1,
            "inDepo": False,
        },
        {"name": "9", "colour_hex": "#4f8cff"},
        now=datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc),
    )

    assert vehicle["age_seconds"] == 0.0
    assert vehicle["status"] == "live"
    assert vehicle["is_live"] is True


def test_naive_dates_are_backward_compatible_as_kyiv():
    naive = datetime(2026, 9, 23, 12, 0)
    aware = as_kyiv(naive)
    assert aware.tzinfo == APP_TIMEZONE
    assert aware.replace(tzinfo=None) == naive


def test_router_normalizes_snapshot_and_plan_times(router, now):
    assert router._live_snapshot_at is not None
    assert router._live_snapshot_at.tzinfo == APP_TIMEZONE

    naive_plan = router.plan(107, 166, now=now)
    aware_plan = router.plan(107, 166, now=as_kyiv(now))
    assert naive_plan is not None
    assert aware_plan is not None
    assert naive_plan["computed_at"] == aware_plan["computed_at"]


class _FakeRouter:
    def __init__(self):
        self.active = 0
        self.max_active = 0
        self._state_lock = threading.Lock()

    def _enter(self):
        with self._state_lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def _leave(self):
        with self._state_lock:
            self.active -= 1

    def set_live(self, fleet, snapshot_at=None, source=None):
        self._enter()
        time.sleep(0.005)
        self._leave()

    def plan(self, from_stop_id, to_stop_id, now=None):
        self._enter()
        time.sleep(0.01)
        self._leave()
        return {"mode": "plan", "legs": [], "total_min": 1, "price_grn": 0}

    def build_variants(self, from_stop_id, to_stop_id, now=None, default_plan=None):
        self._enter()
        time.sleep(0.01)
        self._leave()
        return [], None


def test_router_calculation_lock_serializes_shared_state():
    router = _FakeRouter()
    now = datetime(2026, 9, 23, 12, 0, tzinfo=APP_TIMEZONE)

    def run(index):
        return main._calculate_plan_locked(
            router, 1, 2, [{"id": index}], now, "sim", now
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [future.result(timeout=2) for future in [pool.submit(run, i) for i in range(8)]]

    assert len(results) == 8
    assert router.max_active == 1
    assert router.active == 0
