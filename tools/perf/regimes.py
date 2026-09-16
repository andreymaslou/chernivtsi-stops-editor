# -*- coding: utf-8 -*-
"""
Три режима расчёта на одном парке — чтобы не путать цифры между собой.

    python tools/perf/regimes.py [--rev HEAD]

  A) set_live(..., snapshot_at=now) — как зовёт прод-эндпоинт /api/plan;
  B) set_live(vehicles) без snapshot_at — режим старого bench3.py
     (arrive_min = 0: Дейкстра обходит больше состояний, поэтому тут медленнее);
  C) прод-цикл: на каждый запрос свой set_live (важно для кэша геометрии:
     он живёт, пока срез парка не изменился).
"""
import argparse
import statistics
import time
from datetime import datetime

from common import Counters, load_data, load_router_class, snapshot_fleet

NOW = datetime(2026, 9, 17, 3, 10, 0)
PAIR = (107, 166)
RUNS = 15


def series(router, counters, label, now, refill=None):
    """Прогрев + серия запросов; refill — колбэк перед каждым запросом."""
    router.plan(*PAIR, now=now)  # прогрев
    counters.reset()
    started = time.perf_counter()
    router.plan(*PAIR, now=now)
    single = (time.perf_counter() - started) * 1000

    times = []
    for _ in range(RUNS):
        if refill is not None:
            refill()
        started = time.perf_counter()
        router.plan(*PAIR, now=now)
        times.append((time.perf_counter() - started) * 1000)
    times.sort()
    print("%-42s single=%7.2f p50=%7.2f min=%7.2f max=%7.2f  %s" % (
        label, single, statistics.median(times), times[0], times[-1],
        {key.lstrip("_"): value for key, value in counters.stats.items()}))
    return statistics.median(times)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rev", default="HEAD", help="ревизия роутера (по умолчанию текущий код)")
    args = parser.parse_args()

    _, graph, schedule, stops = load_data()
    Router = load_router_class(args.rev)
    vehicles = snapshot_fleet(graph, schedule, NOW)
    print("ревизия: %s, парк: %d машин, пара: %s->%s" % (args.rev, len(vehicles), *PAIR))

    with Counters(Router) as counters:
        router = Router(graph, schedule, stops=stops, assume_in_service=True)
        router.set_live(vehicles, snapshot_at=NOW)
        series(router, counters, "A snapshot_at=now (прод-вызов)", NOW)

        router = Router(graph, schedule, stops=stops, assume_in_service=True)
        router.set_live(vehicles)
        series(router, counters, "B без snapshot_at (режим bench3)", NOW)

        router = Router(graph, schedule, stops=stops, assume_in_service=True)
        router.set_live(vehicles, snapshot_at=NOW)
        series(router, counters, "C прод-цикл (set_live на каждый запрос)", NOW,
               refill=lambda: router.set_live(vehicles, snapshot_at=NOW))


if __name__ == "__main__":
    main()
