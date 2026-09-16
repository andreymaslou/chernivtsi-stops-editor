# -*- coding: utf-8 -*-
"""
Сверка двух слепков latency_plan.py: время, счётчики и побайтовое равенство планов.

    python tools/perf/compare_snapshots.py before after
"""
import json
import sys

from common import out_path


def load(tag):
    path = out_path("latency_%s.json" % tag)
    if not path.exists():
        raise SystemExit(
            "нет слепка %s — сначала выполните:\n"
            "    python tools/perf/latency_plan.py --tag %s%s" % (
                path, tag,
                "" if tag != "before" else " --rev <хеш ревизии «до»>"))
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        raise SystemExit(2)
    left, right = load(sys.argv[1]), load(sys.argv[2])

    lines = []
    for tag, snapshot in ((sys.argv[1], left), (sys.argv[2], right)):
        for scenario in snapshot["scenarios"]:
            series = scenario["series"]
            lines.append("[%s] %s vehicles=%d p50=%.2f min=%.2f max=%.2f" % (
                tag, scenario["name"], scenario["vehicles"], series["p50_ms"],
                series["min_ms"], series["max_ms"]))
            for pair, item in scenario["plans"].items():
                counters = item["counters"]
                hot = " ".join("%s=%s" % (key.lstrip("_"), counters[key])
                               for key in ("_wait_info", "_nearest_live_vehicle",
                                           "_haversine_m") if key in counters)
                lines.append("   %-10s %8.2f ms  %s" % (pair, item["ms"], hot))

    lines.append("")
    same = True
    for scenario_left, scenario_right in zip(left["scenarios"], right["scenarios"]):
        for pair in scenario_left["plans"]:
            equal = (scenario_left["plans"][pair]["json"]
                     == scenario_right["plans"][pair]["json"])
            same = same and equal
            lines.append("IDENTICAL %-12s %-10s %s" % (
                scenario_left["name"], pair, equal))
    lines.append("ВСЕ ПЛАНЫ ИДЕНТИЧНЫ: %s" % same)

    text = "\n".join(lines)
    print(text)
    out_path("compare_%s_vs_%s.txt" % (sys.argv[1], sys.argv[2])).write_text(
        text + "\n", encoding="utf-8")
    raise SystemExit(0 if same else 1)


if __name__ == "__main__":
    main()
