# -*- coding: utf-8 -*-
"""
Профиль одного plan(): счётчики вызовов + cProfile top-10 + масштабирование по парку.

    python tools/perf/bench_counters.py                 # текущий код
    python tools/perf/bench_counters.py --rev HEAD~1    # как было до правки

Это замена прежнему C:\\Temp\\bench3.py: там счётчики были объявлены, но не
подключены к классу, а set_live() вызывался без snapshot_at (другой режим —
см. tools/perf/regimes.py). Здесь считается ровно то, что зовёт прод.
"""
import argparse
import cProfile
import io
import pstats
import statistics
import time
from datetime import datetime

from common import Counters, load_data, load_router_class, snapshot_fleet

NOW = datetime(2026, 9, 17, 3, 10, 0)
SERIES = 20


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rev", default="HEAD", help="ревизия роутера")
    parser.add_argument("--pair", nargs=2, type=int, default=[107, 166],
                        metavar=("FROM_STOP", "TO_STOP"))
    parser.add_argument("--scale", nargs="*", type=int, default=[2, 4],
                        help="во сколько раз раздуть парк (проверка масштабируемости)")
    args = parser.parse_args()

    app_main, graph, schedule, stops = load_data()
    Router = load_router_class(args.rev)
    vehicles = snapshot_fleet(graph, schedule, NOW)
    pair = tuple(args.pair)

    print("=== %s: парк %d машин, snapshot_at=now, пара %s->%s ===" % (
        args.rev, len(vehicles), *pair))
    with Counters(Router) as counters:
        router = Router(graph, schedule, stops=stops, assume_in_service=True)
        router.set_live(vehicles, snapshot_at=NOW)
        router.plan(*pair, now=NOW)  # прогрев

        counters.reset()
        started = time.perf_counter()
        plan = router.plan(*pair, now=NOW)
        print("один plan(): %.2f ms %s" % (
            (time.perf_counter() - started) * 1000,
            {key.lstrip("_"): value for key, value in counters.stats.items()}))
        print("legs=%d transfers=%d total=%s" % (
            len(plan["legs"]), plan["transfers"], plan["total_min"]))

        times = []
        for _ in range(SERIES):
            started = time.perf_counter()
            router.plan(*pair, now=NOW)
            times.append((time.perf_counter() - started) * 1000)
        times.sort()
        print("p50 по %d прогонам: min=%.2f p50=%.2f p95=%.2f max=%.2f ms" % (
            SERIES, times[0], statistics.median(times),
            times[int(len(times) * 0.95) - 1], times[-1]))

        profile = cProfile.Profile()
        profile.enable()
        router.plan(*pair, now=NOW)
        profile.disable()
        stream = io.StringIO()
        pstats.Stats(profile, stream=stream).sort_stats("tottime").print_stats(8)
        print(stream.getvalue())

        print("=== масштабирование по парку ===")
        for multiplier in args.scale:
            big = [dict(vehicle) for _ in range(multiplier) for vehicle in vehicles]
            router.set_live(big, snapshot_at=NOW)
            counters.reset()
            started = time.perf_counter()
            router.plan(*pair, now=NOW)
            elapsed = (time.perf_counter() - started) * 1000
            print("vehicles=%4d: %8.1f ms %s" % (
                len(big), elapsed,
                {key.lstrip("_"): value for key, value in counters.stats.items()}))
            if elapsed > 20000:
                print("  (слишком долго — дальше не масштабируем)")
                break


if __name__ == "__main__":
    main()
