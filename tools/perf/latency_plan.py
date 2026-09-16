# -*- coding: utf-8 -*-
"""
Слепок планов и тайминги plan() — «до/после» любой правки роутера.

    python tools/perf/latency_plan.py --tag after
    python tools/perf/latency_plan.py --tag before --rev 1e16f98
    python tools/perf/compare_snapshots.py before after

Пишет tools/perf/out/latency_<тег>.json: время, счётчики вызовов и ПОЛНЫЙ JSON
каждого плана — compare_snapshots.py сверяет их побайтово, поэтому ускорение
нельзя принять ценой изменения ответа.
"""
import argparse
import json
import statistics
import time
from datetime import datetime

from common import Counters, load_data, load_router_class, out_path, snapshot_fleet

# Модельные времена: ночь (симулятор «вытягивает» парк к середине дня, поэтому
# прогон не зависит от реального времени запуска) и день.
NIGHT = datetime(2026, 9, 17, 3, 10, 0)
DAY = datetime(2026, 9, 17, 12, 0, 0)
PAIRS = [(107, 166), (181, 166), (166, 107)]
SERIES = 15


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="имя слепка: out/latency_<tag>.json")
    parser.add_argument("--rev", default="HEAD",
                        help="ревизия роутера: HEAD (текущий код), HEAD~1, хеш...")
    args = parser.parse_args()

    app_main, graph, schedule, stops = load_data()
    Router = load_router_class(args.rev)

    report = {"tag": args.tag, "rev": args.rev, "scenarios": []}
    with Counters(Router) as counters:
        for name, now in (("night_0310", NIGHT), ("day_1200", DAY)):
            vehicles = snapshot_fleet(graph, schedule, now)
            router = Router(graph, schedule, stops=stops, assume_in_service=True)
            router.set_live(vehicles, snapshot_at=now)

            entry = {"name": name, "now": now.isoformat(), "vehicles": len(vehicles),
                     "plans": {}}
            for pair in PAIRS:
                router.plan(*pair, now=now)  # прогрев
                counters.reset()
                started = time.perf_counter()
                plan = router.plan(*pair, now=now)
                ms = (time.perf_counter() - started) * 1000
                entry["plans"]["%s->%s" % pair] = {
                    "ms": round(ms, 2),
                    "counters": dict(counters.stats),
                    "json": json.dumps(plan, ensure_ascii=False, sort_keys=True),
                }
                print("%s %s->%s: %.2f ms %s" % (name, pair[0], pair[1], ms, counters.stats))

            times = []
            for _ in range(SERIES):
                started = time.perf_counter()
                router.plan(*PAIRS[0], now=now)
                times.append((time.perf_counter() - started) * 1000)
            times.sort()
            entry["series"] = {"runs": SERIES, "min_ms": round(times[0], 2),
                               "p50_ms": round(statistics.median(times), 2),
                               "max_ms": round(times[-1], 2)}
            print("%s p50=%.2f min=%.2f max=%.2f" % (
                name, entry["series"]["p50_ms"], entry["series"]["min_ms"],
                entry["series"]["max_ms"]))
            report["scenarios"].append(entry)

    target = out_path("latency_%s.json" % args.tag)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("слепок записан: %s" % target)


if __name__ == "__main__":
    main()
