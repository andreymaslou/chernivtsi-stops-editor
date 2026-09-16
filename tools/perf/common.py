# -*- coding: utf-8 -*-
"""
Общее для замеров роутера: пути, счётчики вызовов, загрузка старой ревизии.

Скрипты этой папки — инструменты разработчика, а не часть рантайма сервера.
Они нужны, чтобы повторять замеры из docs/REVIEW-router-perf.md и
docs/REVIEW-router-latency.md и сверять ответы «до/после».

Запускать из корня репозитория, например:
    python tools/perf/latency_plan.py --tag after
    python tools/perf/compare_snapshots.py before after
    python tools/perf/ab_prod_cycle.py --rev HEAD~4
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "out"

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def out_path(name: str) -> Path:
    """Путь в tools/perf/out/ (папка создаётся, в git не попадает)."""
    OUT.mkdir(parents=True, exist_ok=True)
    return OUT / name


def use_utf8_stdout() -> None:
    """
    Печать в UTF-8: в консоли Windows по умолчанию cp866/cp1251, и русские
    названия остановок превращаются в кашу (в файл при этом всё пишется верно).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def load_data():
    """main + graph.json + routes_schedule.json + stops.json (после правок сленга)."""
    import main as app_main  # noqa: E402  (импорт требует REPO в sys.path)

    graph = json.loads((REPO / "graph.json").read_text(encoding="utf-8"))
    schedule = json.loads((REPO / "routes_schedule.json").read_text(encoding="utf-8"))
    stops = app_main.apply_overrides(app_main.load_stops(app_main.STOPS_PATH))
    return app_main, graph, schedule, stops


def load_router_class(rev: str):
    """
    TransitRouter текущего дерева или из истории git — для честного A/B.

    `rev`: "HEAD"/"WORKTREE" (текущий код), "HEAD~1", хеш коммита и т.п.
    Файл из истории пишется во временную папку и импортируется отдельным
    модулем, поэтому старый роутер не конфликтует с router_layer.
    """
    from router_layer import TransitRouter as current

    if rev.upper() in ("HEAD", "WORKTREE", "CURRENT", "NOW"):
        return current

    source = subprocess.run(
        ["git", "-C", str(REPO), "show", f"{rev}:router_layer.py"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    ).stdout
    if not source:
        raise SystemExit("не удалось прочитать router_layer.py из ревизии %s" % rev)
    target = Path(tempfile.mkdtemp(prefix="perf_router_")) / "router_layer_old.py"
    target.write_text(source, encoding="utf-8", newline="")
    spec = importlib.util.spec_from_file_location("router_layer_old", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.TransitRouter


class Counters:
    """
    Счётчики вызовов горячих методов: обёртки вешаются на класс.

    Использовать как контекстный менеджер, чтобы обёртки снимались сами:
        with Counters(TransitRouter) as counters:
            router.plan(...)
            counters.stats["_haversine_m"]
    """

    NAMES = (
        "_wait_info",
        "_nearest_live_vehicle",
        "_approaching_vehicles",
        "_haversine_m",
    )

    def __init__(self, cls):
        self.cls = cls
        self.stats = {name: 0 for name in self.NAMES}
        self._originals = {}
        for name in self.NAMES:
            raw = cls.__dict__.get(name)
            if raw is None:
                continue  # метод появился/исчез между ревизиями — просто пропускаем
            is_static = isinstance(raw, staticmethod)
            fn = raw.__func__ if is_static else raw
            self._originals[name] = (fn, is_static)
            wrapper = self._wrap(fn, name)
            setattr(cls, name, staticmethod(wrapper) if is_static else wrapper)

    def _wrap(self, fn, name):
        stats = self.stats

        def inner(*args, **kwargs):
            stats[name] += 1
            return fn(*args, **kwargs)

        return inner

    def reset(self):
        for name in self.stats:
            self.stats[name] = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for name, (fn, is_static) in self._originals.items():
            setattr(self.cls, name, staticmethod(fn) if is_static else fn)
        return False


def snapshot_fleet(graph, schedule, now, assume_in_service=True):
    """Парк симулятора на модельное время — детерминированный эталон."""
    from sim_layer import SimLayer  # noqa: E402

    sim = SimLayer(graph, schedule, assume_in_service=assume_in_service)
    return sim.snapshot(now=now)["vehicles"]