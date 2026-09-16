# -*- coding: utf-8 -*-
"""
Печать цепочек остановок маршрутов из graph.json (диагностика роутера).

    python tools/perf/diag_stop_chain.py
    python tools/perf/diag_stop_chain.py trolley:5:A bus:23:A

Показывает, сколько остановок реально в маршруте, сколько сегментов и время
проезда до каждой остановки — этим проверялось, что Дейкстра «слишком рано»
заканчивает поездку (см. docs/REVIEW-router-map-paths.md).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import REPO, out_path, use_utf8_stdout  # noqa: E402

DEFAULT_ROUTES = ["trolley:5:A", "trolley:5:B", "bus:23:A", "bus:23:B", "bus:5:A", "bus:5:B"]


def main():
    use_utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("routes", nargs="*", default=DEFAULT_ROUTES)
    args = parser.parse_args()

    graph = json.loads((REPO / "graph.json").read_text(encoding="utf-8"))
    nodes = {int(key): value for key, value in graph["nodes"].items()}

    lines = ["graph stats: %s" % json.dumps(graph.get("stats", {}), ensure_ascii=False)]
    for key in args.routes:
        route = graph["routes"].get(key)
        if not route:
            lines.append("\n== %s -> НЕТ ТАКОГО МАРШРУТА" % key)
            continue
        chain = [int(node) for node in route["stops"]]
        prefix = [0.0]
        for segment in route.get("segments", []):
            prefix.append(prefix[-1] + float(segment.get("minutes", 0.0)))
        names = [nodes[node]["name"] if node in nodes else "?" for node in chain]
        lines.append("\n== %s (%s -> %s) остановок:%d сегментов:%d всего:%.1f мин" % (
            key, names[0], names[-1], len(chain), len(prefix) - 1, prefix[-1]))
        for index, (node, name) in enumerate(zip(chain, names)):
            lines.append("   %2d  %-32s  +%.1f мин  node=%d" % (
                index, name, prefix[index], node))

    text = "\n".join(lines)
    print(text)
    target = out_path("diag_stop_chain.txt")
    target.write_text(text + "\n", encoding="utf-8")
    print("\nзаписано: %s" % target)


if __name__ == "__main__":
    main()
