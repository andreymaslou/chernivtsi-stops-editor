# -*- coding: utf-8 -*-
"""Сверка норм выпуска из решения виконкома с интервалами и парком симулятора.

    python tools/perf/check_fleet_norms.py
    python tools/perf/check_fleet_norms.py --now 2026-09-17T12:00

Нормы (`data/fleet_norms.json`) — это количество автобусов на маршрут в рабочие и
выходные дни из скана решения виконкому. Здесь мы проверяем ими два независимых
источника:

1. `routes_schedule.json`: средний интервал `(min+max)/2` должен соответствовать
   норме — при N автобусах на маршруте и времени круга `2 × one_way_minutes`
   ожидаемый интервал ≈ круг / N;
2. симулятор (`sim_layer`): сколько машин он реально строит на маршруте в 12:00
   рабочего дня — должно быть близко к норме, иначе интервалы в расписании
   завышены/занижены.

Отчёт пишется в `tools/perf/out/fleet_norms_report.json` (в git не попадает).
Расхождения — не обязательно ошибка: нормы выпуска не знают о пробках и о том,
что часть машин может работать на другом маршруте, поэтому скрипт печатает
коэффициенты, а не «правильные» цифры.
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

from common import load_data, out_path, use_utf8_stdout
from sim_layer import SimLayer

REPO = Path(__file__).resolve().parents[2]
NORMS = json.loads((REPO / "data" / "fleet_norms.json").read_text(encoding="utf-8"))

# Расхождение считаем значимым при отклонении больше этого множителя.
TOLERANCE = 1.5


def norm_label(value) -> str:
    """«8А»/«8A» и «10-А»/«10A» — одна и та же метка."""
    return str(value).upper().replace("А", "A").replace("К", "K").replace("-", "").strip()


def base_label(label: str) -> str:
    """Базовая часть метки: «10A» -> «10», «15K» -> «15»."""
    return label.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ") or label


def build_groups(schedule):
    """«Норма -> наши метки»: у 8 и 15 норма одна, а меток у нас две (8A/15K)."""
    norm_keys = {norm_label(entry["route"]) for entry in NORMS["routes"]}
    groups = {}
    for label in schedule["bus"]:
        key = norm_label(label)
        if key not in norm_keys:
            key = base_label(key)
        if key in norm_keys:
            groups.setdefault(key, []).append(label)
    return groups


def time_to_minutes(value):
    """«06:30» -> 390."""
    try:
        hours, minutes = str(value).split(":")[:2]
        return int(hours) * 60 + int(minutes)
    except (AttributeError, ValueError):
        return None


def schedule_metrics(graph, schedule, labels):
    """Що каже розклад: інтервал, час круга і «автобуси з розкладу».

    «Автобуси з розкладу» = час круга / інтервал: саме стільки машин потрібно,
    щоб тримати такий інтервал (прямий аналог норми випуску).
    """
    headways = []
    one_way = []
    for label in labels:
        entry = schedule["bus"].get(label) or {}
        interval = entry.get("interval") or {}
        if interval.get("min") and interval.get("max"):
            headways.append((float(interval["min"]) + float(interval["max"])) / 2.0)
        for suffix in ("A", "B"):
            direction = graph["routes"].get("bus:%s:%s" % (label, suffix)) or {}
            if direction.get("one_way_minutes"):
                one_way.append(float(direction["one_way_minutes"]))
    headway = (sum(headways) / len(headways)) if headways else None
    round_trip = (sum(one_way) / len(one_way)) * 2.0 if one_way else None
    implied_buses = (round_trip / headway) if (round_trip and headway) else None
    return headway, round_trip, implied_buses



def sim_directions(sim):
    """Напрямки симулятора: що він реально взяв у роботу (інтервали/вікно)."""
    grouped = {}
    for direction in sim.directions:
        grouped.setdefault(norm_label(direction.get("route_name")), []).append(direction)
    return grouped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--now", default="2026-09-17T12:00",
                        help="модельное время (рабочий день, день)")
    args = parser.parse_args()
    use_utf8_stdout()
    now = datetime.fromisoformat(args.now)

    _, graph, schedule, _stops = load_data()
    groups = build_groups(schedule)
    sim = SimLayer(graph, schedule, assume_in_service=True)
    sim_by_route = sim_directions(sim)
    fleet_size = len(sim.snapshot(now=now)["vehicles"])

    report = {"now": now.isoformat(), "fleet_size": fleet_size,
              "tolerance": TOLERANCE, "routes": [], "problems": []}
    print("%-6s %-6s %-9s %-7s %-7s %-6s %s" % (
        "марш", "норма", "інт.розкл", "круг", "з розкл", "коеф", "нотатки"))

    for entry in NORMS["routes"]:
        key = norm_label(entry["route"])
        labels = groups.get(key)
        if not labels:
            continue

        headway, round_trip, implied_buses = schedule_metrics(graph, schedule, labels)
        buses_norm = entry["buses_weekday"]

        notes = []
        ratio = None
        if implied_buses and buses_norm:
            ratio = implied_buses / buses_norm
            if ratio > TOLERANCE:
                notes.append("розклад вимагає x%.2f машин від норми" % ratio)
            elif ratio < 1.0 / TOLERANCE:
                notes.append("розклад вимагає лише x%.2f від норми" % ratio)

        # Симулятор мусить брати вікно й інтервал САМЕ з розкладу (не дефолтні).
        for label in labels:
            sched = schedule["bus"].get(label) or {}
            interval = sched.get("interval") or {}
            expected_headway = None
            if interval.get("min") and interval.get("max"):
                expected_headway = (float(interval["min"]) + float(interval["max"])) / 2.0
            expected_first = time_to_minutes(sched.get("first"))
            for direction in sim_by_route.get(norm_label(label)) or []:
                if direction.get("vehicle_type") != "bus":
                    continue
                if expected_headway and abs(float(direction["headway_min"]) - expected_headway) > 0.01:
                    notes.append("симулятор узяв інший інтервал (%s)" % direction.get("key"))
                if expected_first is not None and abs(float(direction["first_min"]) - expected_first) > 0.01:
                    notes.append("симулятор узяв інший початок (%s)" % direction.get("key"))

        if entry["mode"] != "Звичайний":
            notes.append("%s / %s" % (entry["mode"], entry["days"]))

        row = {
            "route": entry["route"],
            "our_labels": labels,
            "buses_norm_weekday": buses_norm,
            "buses_norm_weekend": entry["buses_weekend"],
            "schedule_headway_min": round(headway, 2) if headway else None,
            "round_trip_min": round(round_trip, 2) if round_trip else None,
            "buses_from_schedule": round(implied_buses, 2) if implied_buses else None,
            "ratio": round(ratio, 2) if ratio else None,
            "notes": notes,
        }
        report["routes"].append(row)
        if notes:
            report["problems"].append(row)
        print("%-6s %-6s %-9s %-7s %-7s %-6s %s" % (
            entry["route"], buses_norm, row["schedule_headway_min"],
            row["round_trip_min"], row["buses_from_schedule"], row["ratio"],
            "; ".join(notes) or "ок"))

    worst = sorted(
        (row for row in report["routes"] if row["ratio"]),
        key=lambda row: abs(row["ratio"] - 1.0), reverse=True)[:5]
    report["worst"] = [row["route"] for row in worst]
    print("\nнайбільші розбіжності норми й розкладу: %s"
          % ", ".join("%s (x%.2f)" % (row["route"], row["ratio"]) for row in worst))

    target = out_path("fleet_norms_report.json")
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("маршрутів у звіті: %d, з нотатками: %d, парк симулятора: %d"
          % (len(report["routes"]), len(report["problems"]), fleet_size))
    print("звіт: %s" % target)
    print("звіт: %s" % target)


if __name__ == "__main__":
    main()

