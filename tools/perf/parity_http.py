# -*- coding: utf-8 -*-
"""
Сверка локального и удалённого сервера: тайминги /health и /api/plan + паритет JSON.

    python tools/perf/parity_http.py
    python tools/perf/parity_http.py --text "з Калинки до Універу" --now 2026-09-17T03:10:00
    python tools/perf/parity_http.py --urls http://127.0.0.1:8000,http://169.58.82.105:8000

Зачем: после деплоя надо убедиться, что удалённый контейнер отвечает тем же
планом байт-в-байт, а не «примерно так же». Плюс /health показывает, сколько
времени съедает сеть: внешние 280 мс при /health 217 мс — это сеть, а не расчёт.

ВАЖНО: без `--now` каждый сервер берёт время своих часов, и планы заведомо
разойдутся (это не баг — просто разное модельное время). Для сверки паритета
всегда передавайте `--now` из дампов, например `--now 2026-09-17T03:10:00`.
"""
import argparse
import json
import statistics
import time
import urllib.request
from pathlib import Path

DEFAULT_TEXT = "Я на Соборці, їду на Гравітон"
DEFAULT_URLS = "http://127.0.0.1:8000,http://169.58.82.105:8000"
RUNS = 8


def timed(url, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=300) as response:
        raw = response.read().decode("utf-8")
    return (time.perf_counter() - started) * 1000, raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--text-file", default=None,
                        help="фраза из файла UTF-8 (кириллица из .cmd-файлов ломается)")
    parser.add_argument("--now", default=None, help="модельное время, ISO (симулятор)")
    parser.add_argument("--urls", default=DEFAULT_URLS, help="базы через запятую")
    args = parser.parse_args()

    if args.text_file:
        args.text = Path(args.text_file).read_text(encoding="utf-8").strip()

    payload = {"text": args.text}
    if args.now:
        payload["now"] = args.now

    raw_by_url = {}
    for base in [item.strip().rstrip("/") for item in args.urls.split(",") if item.strip()]:
        health = []
        for _ in range(RUNS):
            elapsed, _ = timed(base + "/health")
            health.append(elapsed)
        health.sort()

        plans = []
        for _ in range(RUNS):
            elapsed, raw = timed(base + "/api/plan", payload)
            plans.append((elapsed, raw))
        plans.sort(key=lambda item: item[0])

        plan = json.loads(plans[0][1])
        legs = [(leg["type"], leg.get("route"), leg.get("wait_min"), leg.get("eta"),
                 leg.get("live_bus"), len(leg.get("path") or [])) for leg in plan["legs"]]
        raw_by_url[base] = plans[0][1]
        print("%-32s /health p50=%4.0f  /api/plan p50=%4.0f (min %4.0f max %4.0f) ms" % (
            base, statistics.median(health), statistics.median(p[0] for p in plans),
            plans[0][0], plans[-1][0]))
        print("    total=%s transfers=%s %s" % (plan["total_min"], plan["transfers"], legs))

    if len(raw_by_url) > 1:
        values = list(raw_by_url.values())
        same = all(value == values[0] for value in values)
        print("JSON ответа совпадает на всех адресах: %s" % same)
        if not same:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
