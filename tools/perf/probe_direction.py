# -*- coding: utf-8 -*-
"""
Направление ТС в подборе «першого потрібного ТС»: замер «до/после».

    python tools/perf/probe_direction.py                   # варианты парка
    python tools/perf/probe_direction.py --rev 916c286     # + сравнение с ревизией git

Важно: `--rev HEAD`/`WORKTREE` — это ТЕКУЩИЙ код (см. common.load_router_class),
то есть сравнение с самим собой. Ревизия «до» задаётся хешем или `HEAD~1`.

Зачем: ланцюжки Напрямків A и B идут по одним улицам, поэтому встречная машина
привязывается к ближайшему узлу НАШЕГО ланцюжка и, если её индекс < board_pos,
попадает в «подъезжающие» (см. docs/BRIEF-router-direction.md). Скрипт считает:

  * сколько пар (маршрут, остановка) получают встречную машину как кандидата;
  * сколько планов и ног на эталонном наборе пар меняют ответ
    (live_bus / eta / wait_min).

Два независимых замера:
  1. РЕВИЗИИ — старый router_layer.py из git против текущего дерева (нужны
     разные ревизии: `--rev HEAD` осмыслен, пока фикс не закоммичен);
  2. ВАРИАНТЫ ПАРКА — один и тот же код на срезе с полем `direction` и без него.
     Второй вариант — прокси реального трекера: live_layer.normalize_vehicle()
     поля `direction` не отдаёт вообще, и фильтр по нему там ничего не значит.

Пишет tools/perf/out/probe_direction.json (в git не попадает).
"""
import argparse
import json
from datetime import datetime

from common import load_data, load_router_class, out_path, snapshot_fleet, use_utf8_stdout

# Модельное время: день, парк симулятора детерминирован.
DAY = datetime(2026, 9, 17, 12, 0, 0)

# Эталонные пары: узлы, которые чаще всего пересадочные/конечные.
STOP_NAMES = [
    'пл. Соборна', 'Кінотеатр "Чернівці"', 'Завод "Гравітон"',
    "Центральний ринок", "Калинівський ринок", "Автовокзал",
]
LEG_KEYS = ("live_bus", "eta", "wait_min")


def transit_legs(plan):
    return [leg for leg in plan.get("legs", []) if leg.get("type") == "transit"]


def leg_diff(before, after):
    return (before.get("live_bus"), before.get("eta"), before.get("wait_min")) != \
           (after.get("live_bus"), after.get("eta"), after.get("wait_min"))


def build(cls, graph, schedule, stops, vehicles, now):
    router = cls(graph, schedule, stops=stops, assume_in_service=True)
    router.set_live([dict(vehicle) for vehicle in vehicles], snapshot_at=now)
    return router


def wrong_direction_candidates(router_loose, router_strict, direction_of):
    """Пары (маршрут, остановка), где «свободный» роутер принимает встречную машину."""
    rows = []
    for key, chain in router_strict.route_stops.items():
        for pos, node in enumerate(chain):
            strict_boards = {c["live_bus"]
                             for c in router_strict._approaching_vehicles(key, node)}
            for cand in router_loose._approaching_vehicles(key, node):
                if cand["live_bus"] in strict_boards:
                    continue
                direction = direction_of.get(cand["live_bus"])
                if direction in (None, ""):
                    continue  # поле неизвестно — это не «встречная», а fallback
                rows.append({
                    "route": key, "pos": pos, "node": node,
                    "board": cand["live_bus"], "direction": direction,
                    "eta_min": round(cand["eta_min"], 1),
                })
    return rows


def current_wrong_direction(router, direction_of):
    """
    Главная метрика приёмки: кандидаты ТЕКУЩЕГО роутера, у которых направление
    известно и НЕ совпадает с направлением маршрута. До фикса — сотни,
    после — ноль. Не зависит ни от ревизий, ни от вариантов парка.
    """
    rows = []
    for key, chain in router.route_stops.items():
        wanted = key.split(":")[-1].strip().upper()
        for pos, node in enumerate(chain):
            for cand in router._approaching_vehicles(key, node):
                direction = direction_of.get(cand["live_bus"])
                if direction and str(direction).strip().upper() != wanted:
                    rows.append({
                        "route": key, "pos": pos, "board": cand["live_bus"],
                        "direction": direction, "eta_min": round(cand["eta_min"], 1),
                    })
    return rows


def compare_plans(router_a, router_b, pairs, now, limit=8):
    """Сколько планов/ног меняют live_bus|eta|wait_min между двумя роутерами."""
    changed_plans, changed_legs, examples = 0, 0, []
    for from_id, to_id, from_name, to_name in pairs:
        plan_a = router_a.plan(from_id, to_id, now=now)
        plan_b = router_b.plan(from_id, to_id, now=now)
        legs_a, legs_b = transit_legs(plan_a), transit_legs(plan_b)
        changed = len(legs_a) != len(legs_b)
        for leg_a, leg_b in zip(legs_a, legs_b):
            if leg_diff(leg_a, leg_b):
                changed_legs += 1
                changed = True
                if len(examples) < limit:
                    examples.append({
                        "pair": [from_name, to_name], "route": leg_a.get("route"),
                        "before": [leg_a.get(k) for k in LEG_KEYS],
                        "after": [leg_b.get(k) for k in LEG_KEYS],
                    })
        changed_plans += 1 if changed else 0
    return {"plans": len(pairs), "changed_plans": changed_plans,
            "changed_legs": changed_legs, "examples": examples}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rev", default=None,
                        help="ревизия «до» из истории git (хеш, HEAD~1); "
                             "без него считается только сравнение вариантов парка")
    parser.add_argument("--now", default=None, help="модельное время, ISO (по умолчанию день)")
    args = parser.parse_args()

    use_utf8_stdout()
    now = datetime.fromisoformat(args.now) if args.now else DAY
    _, graph, schedule, stops = load_data()
    Old = load_router_class(args.rev) if args.rev else None
    New = load_router_class("WORKTREE")

    fleet = snapshot_fleet(graph, schedule, now)
    fleet_no_dir = []
    for vehicle in fleet:
        stripped = dict(vehicle)
        stripped.pop("direction", None)
        fleet_no_dir.append(stripped)
    direction_of = {v["board_number"]: v.get("direction") for v in fleet}

    new_dir = build(New, graph, schedule, stops, fleet, now)
    new_no_dir = build(New, graph, schedule, stops, fleet_no_dir, now)

    pairs = []
    for from_name in STOP_NAMES:
        for to_name in STOP_NAMES:
            from_id = next((int(s["id"]) for s in stops if s["name"] == from_name), None)
            to_id = next((int(s["id"]) for s in stops if s["name"] == to_name), None)
            if from_id and to_id and from_id != to_id:
                pairs.append((from_id, to_id, from_name, to_name))

    wrong_now = current_wrong_direction(new_dir, direction_of)
    report = {
        "rev": args.rev, "now": now.isoformat(), "vehicles": len(fleet),
        "pairs": len(pairs),
        # Метрика приёмки: у текущего кода не должно быть ни одного кандидата
        # с известным и несовпадающим направлением.
        "current_wrong_direction": {
            "count": len(wrong_now), "examples": wrong_now[:5],
        },
        # «Без поля direction» — прокси реального трекера (live_layer поля не
        # отдаёт), поэтому здесь видно ровно то, что отсекает фильтр в проде.
        "variant_no_direction": {
            "candidates": len(wrong_direction_candidates(new_no_dir, new_dir, direction_of)),
            "plans": compare_plans(new_no_dir, new_dir, pairs, now),
        },
        "rev_before": None,
    }
    if Old is not None:
        old_dir = build(Old, graph, schedule, stops, fleet, now)
        report["rev_before"] = {
            "wrong_direction": len(current_wrong_direction(old_dir, direction_of)),
            "candidates": len(wrong_direction_candidates(old_dir, new_dir, direction_of)),
            "plans": compare_plans(old_dir, new_dir, pairs, now),
        }

    print("срез: %s, ТС: %d, пар: %d, ревизия «до»: %s" % (
        now.isoformat(), len(fleet), len(pairs), args.rev or "не задана"))
    print("current_wrong_direction (метрика приёмки, должно быть 0): %d" % len(wrong_now))
    for example in wrong_now[:4]:
        print("   %s idx=%s: %s (направление %s), eta %.1f" % (
            example["route"], example["pos"], example["board"],
            example["direction"], example["eta_min"]))
    for name in ("variant_no_direction", "rev_before"):
        block = report[name]
        if block is None:
            continue
        plans = block["plans"]
        print("%s: встречных кандидатов %d; планов изменено %d/%d, ног %d" % (
            name, block["candidates"], plans["changed_plans"], plans["plans"],
            plans["changed_legs"]))
        if "wrong_direction" in block:
            print("   wrong_direction на ревизии «до»: %d (на текущем коде: %d)" % (
                block["wrong_direction"], len(wrong_now)))
        for example in plans["examples"][:4]:
            print("   %s (маршрут %s): %s -> %s" % (
                " -> ".join(example["pair"]), example["route"],
                example["before"], example["after"]))

    target = out_path("probe_direction.json")
    target.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("отчёт: %s" % target)


if __name__ == "__main__":
    main()
