# -*- coding: utf-8 -*-
"""
Проверка, что ноги плана — НЕПРЕРЫВНЫЙ кусок цепочки остановок маршрута.

    python tools/perf/check_map_paths.py
    python tools/perf/check_map_paths.py --url http://169.58.82.105:8000 --text "з Калинки до Універу"
    python tools/perf/check_map_paths.py --file tools/perf/out/plan.json

Зачем: emulator.js рисует `legs[].path` полилинией. Если в path попали только
посадка и высадка, Leaflet рисует хорду через полгорода — это была реальная
жалоба «немає прорисованих маршрутів» (см. docs/REVIEW-router-map-paths.md).
Скрипт ловит такую регрессию: ищет путь ноги внутри цепочек остановок графа.
"""
import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import REPO, use_utf8_stdout  # noqa: E402

DEFAULT_TEXT = "Я на Соборці, їду на Гравітон"


def route_coords(graph):
    """{маршрут: {"coords": [(lat, lon), ...], "route_name": ...}} по порядку цепочки."""
    nodes = {int(key): value for key, value in graph["nodes"].items()}
    chains = {}
    for key, route in graph["routes"].items():
        chains[key] = {
            "route_name": str(route.get("route_name") or ""),
            "coords": [
                (float(nodes[int(node)]["lat"]), float(nodes[int(node)]["lon"]))
                for node in route["stops"] if int(node) in nodes
            ],
        }
    return chains


def slice_offset(coords, points):
    """Позиция, с которой points совпадают с цепочкой coords (или None)."""
    for start in range(len(coords) - len(points) + 1):
        if coords[start:start + len(points)] == points:
            return start
    return None


def find_slice(chains, path, hint=None):
    """
    (маршрут, позиция) или (None, None), если путь — не непрерывный кусок цепочки.

    `hint` — название маршрута из ноги плана: короткий путь (2 точки) может
    случайно совпасть с чужой цепочкой, поэтому сначала ищем по своему маршруту.
    """
    points = [tuple(point) for point in path]
    fallback = (None, None)
    for key, entry in chains.items():
        start = slice_offset(entry["coords"], points)
        if start is None:
            continue
        if hint and entry["route_name"] == hint:
            return key, start
        if fallback == (None, None):
            fallback = (key, start)
    return fallback


def resolve_text(text, text_file):
    """
    Текст фразы: --text или --text-file (UTF-8).

    Кириллицу в .cmd-файлах cmd читает в OEM-кодировке и передаёт аргумент
    битым, поэтому для «з Калинки до Універу» надёжнее файл:
        python tools/perf/check_map_paths.py --text-file tools/perf/phrases/kalynka_universytet.txt
    Готовые фразы для дымовых проверок лежат в tools/perf/phrases/.
    """
    if not text_file:
        return text
    return Path(text_file).read_text(encoding="utf-8").strip()


def fetch_plan(url, text, now):
    payload = {"text": text}
    if now:
        payload["now"] = now
    request = urllib.request.Request(
        url.rstrip("/") + "/api/plan",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.loads(response.read().decode("utf-8"))


def main():
    use_utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--text-file", default=None, help="фраза из файла (UTF-8)")
    parser.add_argument("--now", default=None)
    parser.add_argument("--file", default=None, help="готовый ответ /api/plan (JSON)")
    args = parser.parse_args()
    args.text = resolve_text(args.text, args.text_file)

    if args.file:
        plan = json.loads(Path(args.file).read_text(encoding="utf-8"))
    else:
        plan = fetch_plan(args.url, args.text, args.now)

    if not isinstance(plan, dict) or "legs" not in plan:
        # Пустой ответ или ошибка API: без ног проверять нечего, и молчаливое
        # «разрывов нет» здесь было бы ложной зеленью.
        print("НЕ ПЛАН, а: %s" % json.dumps(plan, ensure_ascii=False)[:800])
        return 2

    graph = json.loads((REPO / "graph.json").read_text(encoding="utf-8"))
    chains = route_coords(graph)

    lines = ["total=%s transfers=%s legs=%d" % (
        plan.get("total_min"), plan.get("transfers"), len(plan.get("legs") or []))]
    transit_seen = 0
    broken = 0
    for index, leg in enumerate(plan.get("legs") or []):
        if leg.get("type") != "transit":
            lines.append("leg%d %s at=%s" % (index, leg.get("type"), leg.get("at")))
            continue
        transit_seen += 1
        path = leg.get("path") or []
        key, start = find_slice(chains, path, hint=leg.get("route"))
        ok = len(path) >= 2 and key is not None
        broken += 0 if ok else 1
        lines.append("leg%d transit %-4s %-24s -> %-24s path=%-3d %s" % (
            index, leg.get("route"), leg.get("from"), leg.get("to"), len(path),
            "OK (%s, с %d)" % (key, start) if ok
            else "!! РАЗРЫВ: карта нарисует прямую"))

    text = "\n".join(lines)
    print(text)
    if not transit_seen:
        print("!! в плане нет ни одной ноги-поездки — проверять нечего "
              "(проверьте текст фразы и ключ LLM)")
        return 2
    print("ног с разрывом: %d" % broken)
    return 1 if broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
