# -*- coding: utf-8 -*-
"""Нормы выпуска (`data/fleet_norms.json`) и их согласованность.

Данные считаны со скана решения виконкому (14.07.2026) — это количество автобусов
на маршрут в рабочие/выходные дни. Тесты держат структуру и две связи:

* с `data/tariffs.json` (тот же документ делит маршруты на пільгові/без пільг);
* с `routes_schedule.json` (наш граф: у каждого автобуса должна быть норма).
"""
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NORMS = json.loads((REPO / "data" / "fleet_norms.json").read_text(encoding="utf-8"))
TARIFFS = json.loads((REPO / "data" / "tariffs.json").read_text(encoding="utf-8"))
SCHEDULE = json.loads((REPO / "routes_schedule.json").read_text(encoding="utf-8"))

MODES = {"Звичайний", "Маршрутне таксі"}
DAYS = {"щодня", "2,3,4,5,6,7"}


def _label(value) -> str:
    return str(value).upper().replace("А", "A").replace("К", "K").replace("-", "").strip()


def _base(label: str) -> str:
    return label.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ") or label


def _by_label() -> dict:
    return {_label(entry["route"]): entry for entry in NORMS["routes"]}


def test_structure_and_values():
    entries = NORMS["routes"]
    assert len(entries) == 34, "у документі 18 пільгових + 16 без пільг"
    labels = [_label(entry["route"]) for entry in entries]
    assert len(set(labels)) == len(labels), "дублікати маршрутів"

    for entry in entries:
        assert entry["mode"] in MODES, entry
        assert entry["days"] in DAYS, entry
        assert isinstance(entry["buses_weekday"], int) and entry["buses_weekday"] > 0, entry
        assert isinstance(entry["buses_weekend"], int) and entry["buses_weekend"] > 0, entry
        # У вихідні машин не більше, ніж у робочі (у документі це саме так).
        assert entry["buses_weekend"] <= entry["buses_weekday"], entry
        assert entry["name"], entry

    # «Маршрутне таксі» працює 2..7 (у понеділок — ні).
    taxis = [entry for entry in entries if entry["mode"] == "Маршрутне таксі"]
    assert taxis, "у документі такі маршрути є"
    assert all(entry["days"] == "2,3,4,5,6,7" for entry in taxis)


def test_privileged_flags_match_tariffs_data():
    """Один і той самий документ — один і той самий поділ на пільгові/без пільг."""
    by_label = _by_label()
    privileged_from_norms = {label for label, entry in by_label.items() if entry["privileged"]}
    without_from_norms = {label for label, entry in by_label.items() if not entry["privileged"]}
    assert privileged_from_norms == {_label(r) for r in TARIFFS["routes"]["privileged_document_entries"]}
    assert without_from_norms == {_label(r) for r in TARIFFS["routes"]["privileged_not_covered_bus"]}
    assert len(privileged_from_norms) == 18 and len(without_from_norms) == 16


def test_every_graph_bus_has_a_norm():
    """У кожного автобуса нашого графа мусить бути норма (або норма базового номера).

    8A і 15K окремих рядків не мають — вони успадковують норму 8 і 15.
    """
    by_label = _by_label()
    missing = []
    count_in_graph = 0
    for route in SCHEDULE["bus"]:
        label = _label(route)
        entry = by_label.get(label)
        if entry is None:
            entry = by_label.get(_base(label))
        if entry is None or not entry.get("in_graph"):
            missing.append(route)
        else:
            count_in_graph += 1
    assert missing == [], "немає норми випуску для: %s" % missing
    assert count_in_graph == len(SCHEDULE["bus"])
    assert len([e for e in NORMS["routes"] if e["in_graph"]]) == 28
    assert len([e for e in NORMS["routes"] if not e["in_graph"]]) == 6


def test_source_is_documented():
    assert "14.07.2026" in NORMS["read_from"]
    assert "маршрути.pdf" in NORMS["read_from"]
    assert NORMS["checked"] == "2026-09-26"
    assert len(NORMS["notes"]) >= 3
