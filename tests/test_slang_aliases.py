# -*- coding: utf-8 -*-
"""Smoke/regression tests for curated transport slang aliases."""
from pathlib import Path

import main as app_main


REPO = Path(__file__).resolve().parent.parent


def _locator():
    stops = app_main.apply_overrides(app_main.load_stops(REPO / "stops.json"))
    streets = app_main.load_streets_geojson(REPO / "streets.json")
    return app_main.Locator(stops=stops, streets=streets)


def test_curated_slang_aliases_resolve_to_expected_stops():
    locator = _locator()
    cases = {
        "ЧНУ": 25356,
        "Держунівер": 25356,
        "Чернівецький університет": 25356,
        "медколедж": 103,
        "мед коледж": 103,
        "Мальва": 80,
        "тралдепо": 143,
        "Юність": 105,
        "Юніс": 105,
    }

    for query, expected_id in cases.items():
        stop_id, match_type = locator.locate(query)
        assert stop_id == expected_id, (query, stop_id, match_type)
        assert match_type == "stop", (query, match_type)


def test_existing_unique_slang_aliases_still_resolve():
    locator = _locator()
    cases = {
        "соборка": 65,
        "тралка": 110,
        "театралка": 110,
        "форік": 140,
    }

    for query, expected_id in cases.items():
        stop_id, match_type = locator.locate(query)
        assert stop_id == expected_id, (query, stop_id, match_type)
        assert match_type == "stop", (query, match_type)
