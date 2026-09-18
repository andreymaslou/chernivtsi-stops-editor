# -*- coding: utf-8 -*-
"""
Стресс-тест сложных пересадочных хабов: «перший потрібний ТС» обязан быть
БЛИЖАЙШЕЙ по eta машиной своего маршрута, которая успевает к пассажиру.

Что проверяем математически (а не «на глаз»). Роутер выбирает ТС в
`TransitRouter._nearest_live_vehicle()` так:

    candidates = _approaching_vehicles(route, узел)   # отсортированы по eta ↑
    первая машина, у которой eta >= arrive_min - BOARD_TOLERANCE_MIN

`arrive_min` — сколько минут пройдёт от среза парка до прихода пассажира на
остановку. Мы ловим ВСЕ фактические вызовы `_nearest_live_vehicle()` (и список
кандидатов, который видит роутер), независимо пересчитываем `arrive_min` из
`board_time` и `_live_snapshot_at` и падаем, если:

    * выбранная машина НЕ первая среди «успевших» (впереди была другая —
      ровно тот баг, от которого страдают хабы с десятками маршрутов);
    * роутер вернул `None`, хотя подходящая машина была;
    * роутер вернул машину, хотя подходящих не было;
    * `eta` / `wait_min` не совпали с пересчётом от сырой eta кандидата;
    * список кандидатов не отсортирован по eta или eta < 1.0;
    * борт выбранного ТС отсутствует в парке на этом (тип, маршрут).

Проверяются ВСЕ вызовы, которые делает один `plan()` (Дейкстра заходит и в
узлы, которые не попали в итоговый план) — это и есть «жёсткий» стресс-тест.

Запуск (детерминированно, без сервера — данные берутся из симулятора):

    python tools/perf/check_hubs.py
    python tools/perf/check_hubs.py --now 2026-09-17T12:00:00
    python tools/perf/check_hubs.py -v                       # печатать каждый вызов

Проверка ЖИВОГО API (инвариант насколько позволяет /api/live + /api/plan):

    python tools/perf/check_hubs.py --url http://127.0.0.1:8000
    python tools/perf/check_hubs.py --url https://emulator.transgps.cv.ua -u admin:пароль

Внимание: через HTTP `eta` машин недоступен (`/api/live` отдаёт позиции, но не
ожидание), поэтому HTTP-режим проверяет, что `live_bus` из плана реально есть в
`/api/live` на этом маршруте (борт привязан к живому ТС, а не выдуман).

Отчёт пишется в tools/perf/out/check_hubs.json (в git не попадает).
"""
import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from common import load_data, out_path, snapshot_fleet, use_utf8_stdout
from router_layer import BOARD_TOLERANCE_MIN, TransitRouter

# --- Сложные фразы (хабы) ---------------------------------------------------
#
# phrase       — то, что уходит в /api/plan (LLM сам снимает падежные окончания);
# from_query/to_query — НОМИНАТИВНЫЕ формы для офлайн-резолвера Locator: он
#   требует именно словарную форму («Медколедж», но не «Медколеджу»), иначе
#   получает low_confidence и план не строится. Так тест не зависит от LLM/API.
# expect — аргумент приёмки (зачем кейс тут), печатается в отчёте.
CASES: Tuple[Dict[str, str], ...] = (
    {
        "phrase": "від Медколеджу на Соборку",
        "from_query": "Медколедж", "to_query": "Соборка",
        "expect": "одна нога, живой ТС (9A) — база",
    },
    {
        "phrase": "з Соборки на Медколедж",
        "from_query": "Соборка", "to_query": "Медколедж",
        "expect": "две ноги, обе с живым ТС — выбор при пересадке",
    },
    {
        "phrase": "з Гравітону на Соборку",
        "from_query": "Гравітон", "to_query": "Соборка",
        "expect": "две пересадки, три ноги — тяжёлый хаб",
    },
    {
        "phrase": "від Медколеджу на Гравітон",
        "from_query": "Медколедж", "to_query": "Гравітон",
        "expect": "живая нога + нога без ТС (None) в одном плане",
    },
    {
        "phrase": "від Калинки до Універу",
        "from_query": "Калинка", "to_query": "Універ",
        "expect": "ни один ТС не успевает -> live_bus: null (проверка None-ветки)",
    },
)

REFERENCE_NOW = "2026-09-17T12:00:00"   # дневной парк (125 ТС), детерминированно

# --- Цветной вывод ----------------------------------------------------------
_GREEN, _RED, _YELLOW, _DIM, _RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
_COLOR = True


def paint(text: str, code: str) -> str:
    return f"{code}{text}{_RESET}" if _COLOR else text


def ok_tag() -> str:
    return paint("OK", _GREEN)


def fail_tag() -> str:
    return paint("FAIL", _RED)


# --- Инструментирование роутера (только чтение + обёртки, файл не меняется) --
class RouterRecorder:
    """
    Записывает фактические вызовы `_nearest_live_vehicle()` и списки
    кандидатов из `_approaching_vehicles()`. Обёртки снимаются на выходе из
    `with` (тот же приём, что в common.Counters — класс не правится).
    """

    def __init__(self, cls) -> None:
        self.cls = cls
        self.calls: List[Dict[str, Any]] = []
        self.candidates: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
        self._orig_nearest = cls._nearest_live_vehicle
        self._orig_approach = cls._approaching_vehicles

    def __enter__(self) -> "RouterRecorder":
        recorder = self

        def nearest(router_self, route_key, board_node, board_time):
            result = recorder._orig_nearest(router_self, route_key, board_node, board_time)
            recorder.calls.append({
                "route_key": route_key,
                "board_node": board_node,
                "board_time": board_time,
                "result": result,
            })
            return result

        def approach(router_self, route_key, board_node):
            found = recorder._orig_approach(router_self, route_key, board_node)
            recorder.candidates[(route_key, board_node)] = found
            return found

        self.cls._nearest_live_vehicle = nearest
        self.cls._approaching_vehicles = approach
        return self

    def __exit__(self, *exc) -> bool:
        self.cls._nearest_live_vehicle = self._orig_nearest
        self.cls._approaching_vehicles = self._orig_approach
        return False


def build_fleet_index(fleet: List[Dict[str, Any]]) -> Dict[Tuple[str, str], set]:
    """(тип ТС, нормализованная метка) -> набор бортовых номеров из парка."""
    index: Dict[Tuple[str, str], set] = {}
    for vehicle in fleet:
        vtype = str(vehicle.get("vehicle_type") or "").strip().lower()
        label = TransitRouter.normalize_label(str(vehicle.get("route_label") or ""))
        board = str(vehicle.get("board_number") or "")
        index.setdefault((vtype, label), set()).add(board)
    return index


# --- Ядро проверки: инвариант «перший потрібний ТС» ------------------------
def verify_call(router, record: Dict[str, Any], fleet_index: Dict[Tuple[str, str], set]) -> Dict[str, Any]:
    """
    Независимо пересчитывает ожидание для одного факта вызова
    `_nearest_live_vehicle()` и сверяет с тем, что вернул роутер.
    """
    route_key = record["route_key"]
    board_node = record["board_node"]
    board_time = record["board_time"]
    result = record["result"]
    problems: List[str] = []

    candidates = router._approaching_vehicles(route_key, board_node)
    snapshot_at = router._live_snapshot_at
    arrive_min = 0.0
    if snapshot_at is not None:
        arrive_min = max(0.0, (board_time - snapshot_at).total_seconds() / 60.0)

    # Инвариант сортировки: роутер опирается на «первого в списке».
    etas = [c["eta_min"] for c in candidates]
    if etas != sorted(etas):
        problems.append("кандидаты не отсортированы по eta: %s" % [round(e, 1) for e in etas])
    for candidate in candidates:
        if candidate["eta_min"] < 1.0 - 1e-9:
            problems.append("eta < 1.0 у %s: %r" % (candidate.get("live_bus"), candidate["eta_min"]))

    # «Успевшие» = те, кто приедет не раньше, чем пассажир будет на остановке.
    eligible = [c for c in candidates if c["eta_min"] >= arrive_min - BOARD_TOLERANCE_MIN]
    expected = eligible[0] if eligible else None

    chosen_board = result.get("live_bus") if result else None

    if result is None and expected is not None:
        problems.append(
            "вернул None, хотя подходит %s (eta %.1f) при arrive_min=%.1f"
            % (expected.get("live_bus"), expected["eta_min"], arrive_min))
    if result is not None and expected is None:
        problems.append(
            "вернул %s, хотя подходящих ТС нет (arrive_min=%.1f)" % (chosen_board, arrive_min))
    if result is not None and expected is not None:
        if chosen_board != expected.get("live_bus"):
            problems.append(
                "выбран %s, но первой в очереди была %s (eta %.1f vs %.1f) — пропущена машина впереди"
                % (chosen_board, expected.get("live_bus"),
                   result.get("eta_min") if result.get("eta_min") is not None else -1.0,
                   expected["eta_min"]))
        else:
            exp_eta = round(expected["eta_min"], 1)
            if result.get("eta_min") is None or abs(result["eta_min"] - exp_eta) > 0.06:
                problems.append("eta не совпала: %r vs %r" % (result.get("eta_min"), exp_eta))
            exp_wait = max(0.5, round(max(0.0, expected["eta_min"] - arrive_min) + 0.5, 1))
            if result.get("wait_min") is None or abs(result["wait_min"] - exp_wait) > 0.06:
                problems.append("wait_min не совпал: %r vs %r" % (result.get("wait_min"), exp_wait))

    # Борт обязан существовать в парке на этом (тип ТС, маршрут) — иначе
    # роутер пообещал «живую» машину, которой нет.
    vtype = router.route_vehicle_type.get(route_key, "")
    norm = router._route_wanted_norm.get(route_key, "")
    known = fleet_index.get((vtype, norm), set())
    if result is not None and chosen_board not in (None, "", "?") and chosen_board not in known:
        problems.append("борт %s не найден в парке (%s|%s)" % (chosen_board, vtype, norm))
    for candidate in candidates:
        board = candidate.get("live_bus")
        if board not in (None, "", "?") and board not in known:
            problems.append("кандидат %s не найден в парке (%s|%s)" % (board, vtype, norm))

    # Диагностика: кто стоял «перед» выбранной и почему пропущен.
    skipped: List[Tuple[Any, float]] = []
    for candidate in candidates:
        if chosen_board is not None and candidate.get("live_bus") == chosen_board:
            break
        if candidate["eta_min"] < arrive_min - BOARD_TOLERANCE_MIN:
            skipped.append((candidate.get("live_bus"), round(candidate["eta_min"], 1)))

    return {
        "route_key": route_key,
        "board_node": board_node,
        "board_time": board_time.strftime("%Y-%m-%d %H:%M"),
        "arrive_min": round(arrive_min, 1),
        "candidates": [{"bus": c.get("live_bus"), "eta": round(c["eta_min"], 1)} for c in candidates],
        "eligible_first": expected.get("live_bus") if expected else None,
        "chosen": chosen_board,
        "chosen_eta": result.get("eta_min") if result else None,
        "chosen_wait": result.get("wait_min") if result else None,
        "skipped_before": skipped,
        "problems": problems,
        "ok": not problems,
    }


def leg_route_key_prefix(leg: Dict[str, Any]) -> str:
    """«bus:9A:A»/«bus:9A:B» — оба направления ноги маршрута «9A» типа bus."""
    return "%s:%s:" % (leg.get("vehicle"), leg.get("route"))


def match_leg_calls(calls: List[Dict[str, Any]], leg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Вызовы роутера, относящиеся к ноге (маршрут + выбранный борт)."""
    prefix = leg_route_key_prefix(leg)
    want_board = leg.get("live_bus")
    matched = []
    for call in calls:
        if not call["route_key"].startswith(prefix):
            continue
        got = call["result"].get("live_bus") if call["result"] else None
        if got == want_board:
            matched.append(call)
    return matched


# --- Офлайн-прогон (детерминированный, без сервера) ------------------------
def _load_router_and_locator(now):
    app_main, graph, schedule, stops = load_data()
    streets = app_main.load_streets_geojson(app_main.STREETS_PATH)
    locator = app_main.Locator(stops=stops, streets=streets)
    fleet = snapshot_fleet(graph, schedule, now, assume_in_service=True)
    router = TransitRouter(graph, schedule, stops=stops, assume_in_service=True)
    router.set_live(fleet, snapshot_at=now)
    name_by_id = {str(stop.get("id")): stop.get("name") for stop in stops}
    return router, locator, name_by_id, fleet, build_fleet_index(fleet)


def _print_leg(index: int, leg: Dict[str, Any], status: str, verified: int) -> None:
    if leg.get("type") != "transit":
        print("   %s нога %d: пересадка/пешком %s (%s мин)"
              % (status, index, leg.get("at"), leg.get("walk_min")))
        return
    print("   %s нога %d: %s %s  live_bus=%s  eta=%s  wait=%s  (вызовов: %d)" % (
        status, index, leg.get("vehicle"), leg.get("route"),
        leg.get("live_bus"), leg.get("eta"), leg.get("wait_min"), verified))


def _print_failed(check: Dict[str, Any]) -> None:
    print("   %s %s узел %s board_time=%s arrive_min=%s выбран=%s"
          % (fail_tag(), check["route_key"], check["board_node"],
             check["board_time"], check["arrive_min"], check["chosen"]))
    for problem in check["problems"]:
        print("        - %s" % problem)
    if check["candidates"]:
        print("        кандидаты (eta): %s" % ", ".join(
            "%s=%.1f" % (c["bus"], c["eta"]) for c in check["candidates"]))


def _dedupe_calls(calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Одинаковые визиты (маршрут, узел, момент посадки) проверяем один раз."""
    unique: Dict[Tuple[Any, Any, Any], Dict[str, Any]] = {}
    for call in calls:
        unique.setdefault((call["route_key"], call["board_node"], call["board_time"]), call)
    return list(unique.values())


def run_offline(now: datetime, cases, verbose: bool) -> Dict[str, Any]:
    router, locator, name_by_id, fleet, fleet_index = _load_router_and_locator(now)
    print("Парк симулятора на %s: %d ТС (виртуальный, детерминированный)"
          % (now.strftime("%Y-%m-%d %H:%M"), len(fleet)))

    reports: List[Dict[str, Any]] = []
    total_calls = total_failed = 0

    for number, case in enumerate(cases, 1):
        print("\n%s %d/%d  «%s»  (%s)"
              % (paint("КЕЙС", _YELLOW), number, len(cases), case["phrase"], case["expect"]))
        from_id, from_type = locator.locate(case["from_query"])
        to_id, to_type = locator.locate(case["to_query"])
        report: Dict[str, Any] = {"phrase": case["phrase"], "ok": False, "problems": []}

        if from_id is None or to_id is None or "low_confidence" in (from_type, to_type):
            report["problems"].append(
                "фраза не разрешается в остановки: from=%r(%s) to=%r(%s)"
                % (case["from_query"], from_type, case["to_query"], to_type))
            print("   %s %s" % (fail_tag(), report["problems"][-1]))
            reports.append(report)
            total_failed += 1
            continue

        from_name = name_by_id.get(str(from_id), "?")
        to_name = name_by_id.get(str(to_id), "?")
        report["from"] = [from_id, from_name]
        report["to"] = [to_id, to_name]

        with RouterRecorder(TransitRouter) as recorder:
            plan = router.plan(from_id, to_id, now=now)

        if plan is None or plan.get("mode") != "plan":
            report["problems"].append("роутер не построил план (mode=%s)"
                                      % (plan.get("mode") if plan else None))
            print("   %s %s" % (fail_tag(), report["problems"][-1]))
            reports.append(report)
            total_failed += 1
            continue

        print("   %s (id=%s) -> %s (id=%s): пересадок %s, всего %s хв"
              % (from_name, from_id, to_name, to_id, plan.get("transfers"), plan.get("total_min")))

        # Дейкстра зовёт _nearest_live_vehicle многократно — проверяем каждый
        # уникальный визит (это и есть стресс-тест на хабах с массой маршрутов).
        checks = [verify_call(router, record, fleet_index)
                  for record in _dedupe_calls(recorder.calls)]
        if not checks:
            report["problems"].append("план не вызвал _nearest_live_vehicle — нет живого слоя")
            print("   %s план не вызвал _nearest_live_vehicle" % fail_tag())
            reports.append(report)
            total_failed += 1
            continue
        failed = [check for check in checks if not check["ok"]]
        failed_route_nodes = {(check["route_key"], check["board_node"]) for check in failed}
        total_calls += len(checks)
        total_failed += len(failed)

        for index, leg in enumerate(plan.get("legs") or [], 1):
            if leg.get("type") != "transit":
                _print_leg(index, leg, paint("--", _DIM), 0)
                continue
            matched = _dedupe_calls(match_leg_calls(recorder.calls, leg))
            leg_ok = bool(matched) and not any(
                (call["route_key"], call["board_node"]) in failed_route_nodes for call in matched)
            _print_leg(index, leg, ok_tag() if leg_ok else fail_tag(), len(matched))

        for check in checks if verbose else []:
            print("      %s %s узел %s arrive=%.1f выбран=%s"
                  % (ok_tag() if check["ok"] else fail_tag(), check["route_key"],
                     check["board_node"], check["arrive_min"], check["chosen"]))
        for check in failed:
            _print_failed(check)

        report["calls_checked"] = len(checks)
        report["calls_failed"] = len(failed)
        report["problems"] = [problem for check in failed for problem in check["problems"]]
        report["legs"] = [
            {"type": leg.get("type"), "vehicle": leg.get("vehicle"), "route": leg.get("route"),
             "live_bus": leg.get("live_bus"), "eta": leg.get("eta"), "wait_min": leg.get("wait_min")}
            for leg in (plan.get("legs") or [])
        ]
        report["ok"] = not failed
        print("   %s проверено вызовов: %d, нарушений: %d"
              % (ok_tag() if report["ok"] else fail_tag(), len(checks), len(failed)))
        reports.append(report)

    return {"mode": "offline", "now": now.isoformat(), "cases": reports,
            "calls_checked": total_calls, "calls_failed": total_failed}


# --- HTTP-прогон: проверка ЖИВОГО API --------------------------------------
def _http_json(url: str, payload: Optional[Dict[str, Any]] = None,
               auth: Optional[str] = None, timeout: int = 120) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if auth:
        token = base64.b64encode(auth.encode("utf-8")).decode("ascii")
        headers["Authorization"] = "Basic " + token
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def run_http(base: str, now_iso: str, cases, auth: Optional[str], verbose: bool) -> Dict[str, Any]:
    """
    Через HTTP `eta` ТС недоступен, поэтому проверяем связность: каждый
    `live_bus` из плана обязан существовать в `/api/live` на своём маршруте
    (борт привязан к реальному живому ТС, а не выдуман).
    """
    fleet_url = "%s/api/live?now=%s&only_fresh=true&include_depo=false" % (base, now_iso)
    snapshot = _http_json(fleet_url, auth=auth)
    fleet = snapshot.get("vehicles") or []
    index = build_fleet_index(fleet)
    print("\n%s HTTP-режим: %s — в /api/live %d ТС"
          % (paint("==>", _YELLOW), base, len(fleet)))

    checks_failed = 0
    case_reports: List[Dict[str, Any]] = []

    for number, case in enumerate(cases, 1):
        print("\n%s %d/%d  «%s»"
              % (paint("КЕЙС", _YELLOW), number, len(cases), case["phrase"]))
        report: Dict[str, Any] = {"phrase": case["phrase"], "ok": True, "problems": []}
        try:
            plan = _http_json(base + "/api/plan", {"text": case["phrase"], "now": now_iso}, auth=auth)
        except urllib.error.HTTPError as exc:
            report["ok"] = False
            report["problems"].append("HTTP %s на /api/plan" % exc.code)
            print("   %s HTTP %s: %s" % (fail_tag(), exc.code, exc.reason))
            checks_failed += 1
            case_reports.append(report)
            continue

        if plan.get("mode") != "plan":
            report["ok"] = False
            report["problems"].append("mode=%s (не план)" % plan.get("mode"))
            print("   %s сервер вернул mode=%s" % (fail_tag(), plan.get("mode")))
            checks_failed += 1
            case_reports.append(report)
            continue

        print("   %s -> %s: пересадок %s, всего %s хв"
              % (plan.get("from_name"), plan.get("to_name"),
                 plan.get("transfers"), plan.get("total_min")))

        for leg in [item for item in (plan.get("legs") or []) if item.get("type") == "transit"]:
            bus = leg.get("live_bus")
            vtype = str(leg.get("vehicle") or "").strip().lower()
            label = TransitRouter.normalize_label(str(leg.get("route") or ""))
            if bus is None:
                print("   %s %s %s: live_bus=null (по расписанию — живой ТС не обещан)"
                      % (ok_tag(), vtype, leg.get("route")))
                continue
            if bus in index.get((vtype, label), set()):
                print("   %s %s %s: live_bus=%s eta=%s wait=%s — борт есть в /api/live"
                      % (ok_tag(), vtype, leg.get("route"), bus, leg.get("eta"), leg.get("wait_min")))
            else:
                report["ok"] = False
                report["problems"].append(
                    "live_bus=%s (%s %s) отсутствует в /api/live" % (bus, vtype, leg.get("route")))
                print("   %s %s %s: live_bus=%s НЕТ в /api/live"
                      % (fail_tag(), vtype, leg.get("route"), bus))
                checks_failed += 1

        report["legs"] = plan.get("legs")
        print("   %s" % (ok_tag() if report["ok"] else fail_tag()))
        case_reports.append(report)

    return {"mode": "http", "base": base, "fleet": len(fleet),
            "checks_failed": checks_failed, "cases": case_reports}


# --- Точка входа -----------------------------------------------------------
def main() -> int:
    global _COLOR
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--now", default=REFERENCE_NOW,
                        help="модельное время ISO-8601 (по умолчанию %s)" % REFERENCE_NOW)
    parser.add_argument("--url", default=None,
                        help="база живого API для доп. проверки, напр. http://127.0.0.1:8000")
    parser.add_argument("-u", "--user", default=None,
                        help="логин:пароль для Basic Auth (если API закрыт nginx)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="печатать каждый проверенный вызов _nearest_live_vehicle")
    parser.add_argument("--no-color", action="store_true", help="без ANSI-цветов")
    args = parser.parse_args()

    use_utf8_stdout()
    if args.no_color or not sys.stdout.isatty():
        _COLOR = False

    now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)

    report = run_offline(now, CASES, args.verbose)
    if args.url:
        base = args.url.strip().rstrip("/")
        try:
            report["http"] = run_http(base, args.now, CASES, args.user, args.verbose)
        except Exception as exc:  # noqa: BLE001 — недоступный сервер не должен ронять офлайн-итог
            print("\n%s HTTP-режим недоступен: %s" % (fail_tag(), exc))
            report["http"] = {"mode": "http", "base": base, "checks_failed": 1, "error": str(exc)}

    report_path = out_path("check_hubs.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    http_failed = int((report.get("http") or {}).get("checks_failed") or 0)
    total_failed = report["calls_failed"] + http_failed

    print("\n" + "=" * 68)
    print("ИТОГ: кейсов %d, проверено вызовов %d, нарушений %d%s"
          % (len(CASES), report["calls_checked"], report["calls_failed"],
             ", HTTP-нарушений %d" % http_failed if report.get("http") else ""))
    print("отчёт: %s" % report_path)
    if total_failed:
        print("%s «перший потрібний ТС» выбирается неверно — см. детали выше" % fail_tag())
        return 1
    print("%s все проверки прошли: выбранная машина — ближайшая по eta" % ok_tag())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
