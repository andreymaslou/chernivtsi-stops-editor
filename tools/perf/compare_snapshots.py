# -*- coding: utf-8 -*-
"""
Сверка двух слепков latency_plan.py: время, счётчики и побайтовое равенство планов.

    python tools/perf/compare_snapshots.py before after
    python tools/perf/compare_snapshots.py before after --ignore-keys full_geom

`--ignore-keys` выбрасывает перечисленные поля (на любой глубине) из планов
перед сверкой. Это нужно, когда правка ОСОЗНАННО добавляет поле в ответ — иначе
сверка навсегда покажет «ВСЕ ПЛАНЫ ИДЕНТИЧНЫ: False» и ускорение будет нечем
принять (пример: `leg.full_geom` для хвостов маршрута на карте, см.
`docs/REVIEW-map-ux.md`). Игнорировать можно только осознанно: список
игнорируемых полей печатается в отчёте, а при расхождении показывается путь
первого отличия — чтобы «не идентично» не оставалось без объяснения.
"""
import argparse
import json

from common import out_path, use_utf8_stdout


def load(tag):
    path = out_path("latency_%s.json" % tag)
    if not path.exists():
        raise SystemExit(
            "нет слепка %s — сначала выполните:\n"
            "    python tools/perf/latency_plan.py --tag %s%s" % (
                path, tag,
                "" if tag != "before" else " --rev <хеш ревизии «до»>"))
    return json.loads(path.read_text(encoding="utf-8"))


def drop_keys(value, keys):
    """Убирает ключи (на любой глубине) из структуры плана."""
    if isinstance(value, dict):
        return {key: drop_keys(item, keys)
                for key, item in value.items() if key not in keys}
    if isinstance(value, list):
        return [drop_keys(item, keys) for item in value]
    return value


def first_diff(left, right, path="$"):
    """Путь первого расхождения — «что именно поменялось»."""
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left:
                return "%s.%s: добавлено" % (path, key)
            if key not in right:
                return "%s.%s: удалено" % (path, key)
            found = first_diff(left[key], right[key], "%s.%s" % (path, key))
            if found:
                return found
        return None
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return "%s: длина %d -> %d" % (path, len(left), len(right))
        for index, (first, second) in enumerate(zip(left, right)):
            found = first_diff(first, second, "%s[%d]" % (path, index))
            if found:
                return found
        return None
    if type(left) is not type(right):
        return "%s: тип %s -> %s" % (path, type(left).__name__, type(right).__name__)
    return None if left == right else "%s: %r -> %r" % (path, left, right)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("left", help="тег слепка «до» (например before)")
    parser.add_argument("right", help="тег слепка «после» (например after)")
    parser.add_argument("--ignore-keys", default="",
                        help="поля, которые игнорируются при сверке (через запятую)")
    args = parser.parse_args()
    use_utf8_stdout()
    ignored = {key.strip() for key in args.ignore_keys.split(",") if key.strip()}

    left, right = load(args.left), load(args.right)

    lines = []
    for tag, snapshot in ((args.left, left), (args.right, right)):
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
    if ignored:
        lines.append("игнорируются поля: %s" % ", ".join(sorted(ignored)))
    same = True
    for scenario_left, scenario_right in zip(left["scenarios"], right["scenarios"]):
        for pair in scenario_left["plans"]:
            first = json.loads(scenario_left["plans"][pair]["json"])
            second = json.loads(scenario_right["plans"][pair]["json"])
            if ignored:
                first, second = drop_keys(first, ignored), drop_keys(second, ignored)
            equal = first == second
            same = same and equal
            lines.append("IDENTICAL %-12s %-10s %s%s" % (
                scenario_left["name"], pair, equal,
                "" if equal else "  первое отличие: " + str(first_diff(first, second))))
    lines.append("ВСЕ ПЛАНЫ ИДЕНТИЧНЫ: %s" % same)

    text = "\n".join(lines)
    print(text)
    out_path("compare_%s_vs_%s.txt" % (args.left, args.right)).write_text(
        text + "\n", encoding="utf-8")
    raise SystemExit(0 if same else 1)


if __name__ == "__main__":
    main()
