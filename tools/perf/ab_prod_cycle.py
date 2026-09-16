# -*- coding: utf-8 -*-
"""
A/B «старая ревизия против текущего кода» в прод-режиме (set_live + plan на запрос).

    python tools/perf/ab_prod_cycle.py --old 1e16f98
    python tools/perf/ab_prod_cycle.py --old HEAD~1 --new HEAD --pair 181 166

Старый роутер берётся прямо из git (см. common.load_router_class), поэтому
руками ничего копировать не нужно. Это тот замер, по которому принимался
Шаг 4: HEAD 230 мс -> текущее дерево 76 мс (p50, парк 119 машин).
"""
import argparse
import statistics
import time
from datetime import datetime

from common import Counters, load_data, load_router_class, snapshot_fleet

NOW = datetime(2026, 9, 17, 3, 10, 0)
RUNS = 12


def measure(label, cls, now, vehicles, graph, schedule, stops, pair):
    with Counters(cls) as counters:
        router = cls(graph, schedule, stops=stops, assume_in_service=True)
        router.set_live(vehicles, snapshot_at=now)
        router.plan(*pair, now=now)  # прогрев

        times = []
        for _ in range(RUNS):
            router.set_live(vehicles, snapshot_at=now)  # свежий срез, как в проде
            started = time.perf_counter()
            router.plan(*pair, now=now)
            times.append((time.perf_counter() - started) * 1000)
        times.sort()

        counters.reset()
        router.set_live(vehicles, snapshot_at=now)
        started = time.perf_counter()
        router.plan(*pair, now=now)
        cold = (time.perf_counter() - started) * 1000
        print("%-10s прод-цикл p50=%7.2f min=%7.2f max=%7.2f | одиночный %7.2f ms  %s" % (
            label, statistics.median(times), times[0], times[-1], cold,
            {key.lstrip("_"): value for key, value in counters.stats.items()}))
        return statistics.median(times)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", required=True, help="ревизия для сравнения (HEAD~1, хеш)")
    parser.add_argument("--new", default="HEAD", help="текущий код (по умолчанию дерево)")
    parser.add_argument("--pair", nargs=2, type=int, default=[107, 166],
                        metavar=("FROM_STOP", "TO_STOP"))
    args = parser.parse_args()

    _, graph, schedule, stops = load_data()
    vehicles = snapshot_fleet(graph, schedule, NOW)
    pair = tuple(args.pair)
    print("парк: %d машин, время: %s, пара: %s->%s" % (
        len(vehicles), NOW.isoformat(), *pair))

    measure(args.old, load_router_class(args.old), NOW, vehicles,
            graph, schedule, stops, pair)
    measure(args.new, load_router_class(args.new), NOW, vehicles,
            graph, schedule, stops, pair)


if __name__ == "__main__":
    main()
