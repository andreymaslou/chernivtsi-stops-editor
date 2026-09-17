# STATUS: chernivtsi-stops-editor

Обновлено: 2026-09-17. Это точка входа для новой сессии (человек или ИИ):
что развёрнуто, что уже сделано, что проверять и что осталось.

## 1. Что где крутится

| Что | Где | Как поднять |
|---|---|---|
| Эмулятор + API (прод) | `http://169.58.82.105:8000` (контейнер `api_router`, `restart=always`) | `bash tools`-скрипт деплоя: `C:\Temp\remote_update.sh` через ssh (`git pull` → `docker compose up -d --build` → health) |
| UI | `/ui/emulator.html` (эмулятор), `/ui/admin.html` (сленг + жалобы), `/docs` (Swagger) | — |
| Локальный dev | `0.0.0.0:8000` | `cd chernivtsi-stops-editor && python main.py` |
| Тесты | локально | `python -m pytest -q` (сейчас 10 passed) |

⚠️ Авторизации нет: порт 8000 открыт в интернет (см. открытые задачи).

## 2. Что уже сделано (история коммитов)

Всего 50 коммитов, репозиторий начат 2026-08-18. Ключевое по темам:

**Каркас эмулятора** (`f1749fb`, `f894b26`, `8fcf3de`, `87cb222`, `abdec38`):
живой GPS-слой + граф маршрутов, роутер с 0–2 пересадками и `/api/plan`,
виртуальный парк `sim_layer` (`GPS_SIMULATOR=1`) для тестов в любое время суток,
детерминированные цвета маршрутов, `/api/plan` и `/api/live` работают в рамках
одного модельного времени.

**Оптимизация роутера** (`docs/REVIEW-router-perf.md`):
`cb17ad0` — бриф на оптимизацию; `3d656a1` — Шаг 1, индекс парка `_live_by_route`;
`337b740` — Шаг 2, мемоизация ожиданий внутри `plan()`; `de2c21c` — Шаг 3, отсев
за градусами до haversine; `425d45f` — итог: `plan()` p50 2688 → 89 мс (×30),
end-to-end 1.77 → 0.22 с; `bfbac2d` — pytest-каркас с фиксированным `now`.

**Корректность времени** (`docs/REVIEW-router-correctness.md`):
`ecad622` — Шаги 1–2, ожидание считается на момент прихода пассажира;
`2cd903b` — Шаг 3, «первый нужный ТС» обязан приезжать ПОСЛЕ пассажира
(`BOARD_TOLERANCE_MIN`); `d309093` — разбор результата, end-to-end 0.32 с.

**Карта** (`docs/REVIEW-router-map-paths.md`):
`b7f4a9b` — фикстура закрепляет `snapshot_at=now` (без неё HEAD был красный);
`5eeea8f` — `leg.path` = непрерывный ланцюжок остановок маршрута (был хордой из
2 точек → «немає прорисованих маршрутів»). `path` — это остановки, не геометрия.

**Латентность** (`docs/BRIEF-router-latency.md`, `docs/REVIEW-router-latency.md`):
`1e16f98` — бриф Gemini (Шаг 4 + гигиена); `8f89da0` — Шаг 4: `_approaching_vehicles()`
и `_spatial_cache`, кэш геометрии помечен версией среза и подписью парка
(haversine 33 168 → 133, прод-цикл p50 230 → 76 мс, 6/6 эталонных планов
byte-identical); `38ccdac` — разбор с метриками и правками к брифу.

**Гигиена и инструменты:** `7d1eee7` — `.gitignore`, `.dockerignore`,
`.env.example` (его требовал `DEPLOY_EMULATOR.md`, файла не было);
`31d4dcc` — из индекса убран локальный мусор; `1e13bb3` — `tools/perf/`
(замеры и сверки, см. `tools/perf/README.md`).

## 3. Быстрые проверки (5 команд)

```powershell
cd c:\Users\YA\.gemini\antigravity\scratch\chernivtsi-stops-editor
python -m pytest -q
python tools\perf\parity_http.py --text-file tools\perf\phrases\soborna_graviton.txt --now 2026-09-17T03:10:00
python tools\perf\check_map_paths.py --text-file tools\perf\phrases\kalynka_universytet.txt --url http://169.58.82.105:8000
python tools\perf\ab_prod_cycle.py --old 1e16f98
```

Ожидаемое: `10 passed`; паритет JSON локально/VPS = `True`; по фразе «з Калінки
до Універу» нога 5 идёт 15 точками (`OK (trolley:5:A)`), «ног с разрывом: 0»;
A/B ≈ 235 → 150 мс (со счётчиками) и haversine 33 168 → 133.

## 4. Грабли (чтобы не наступить снова)

1. `set_live()` **внутри** обработчика запроса (`main.py`) — поэтому кэш
   геометрии сбрасывается не по факту вызова, а по смене подписи позиций.
2. `bench3.py` устарел: счётчики не подключены, `set_live` без `snapshot_at`
   (другой режим, +800 `_wait_info`). Используйте `tools/perf`.
3. Поиск «фраза → остановки» идёт через LLM: без `--now` локальный сервер и VPS
   дают разные планы (разные часы и разный парк симулятора), а кириллица в
   `.cmd`-файле вообще ломается в OEM-кодировке — передавайте `--text-file`.
4. `path` ноги — цепочка остановок; для трассировки по улицам нужны OSM way-линии.

## 5. Открытые задачи

1. **HTTPS + basic auth** (нужен домен): `deploy/nginx-emulator.conf` +
   `htpasswd` + `certbot --nginx`; после этого закрыть порт 8000 наружу.
2. **Ротация ключа OpenRouter** (ключ ранее светился в открытом виде).
3. **Геометрия дорог на карте**: OSM way-линии вместо прямых между остановками.
4. `docs/DECISIONS.md` / `AGENTS.md`: правило «писатель ≠ проверяющий»,
   порядок бриф (Gemini) → реализация (Cline) → разбор (Claude/Cline).
5. Косметика: `/favicon.ico` отдаёт 404.

## 6. Если начинаете новую сессию

Контекст специально вынесен в файлы, чтобы стартовая сессия ничего не теряла.
Скопируйте агенту такое вступление:

> Прочитай `docs/STATUS.md`, `docs/REVIEW-router-latency.md`,
> `docs/REVIEW-router-perf.md`, `docs/REVIEW-router-correctness.md`,
> `docs/REVIEW-router-map-paths.md` и `tools/perf/README.md`.
> Правило проекта: **писатель ≠ проверяющий** — бриф пишет один (Gemini),
> реализует другой (Cline), проверяем машинно (`python -m pytest -q`,
> `tools/perf/compare_snapshots.py`, `tools/perf/ab_prod_cycle.py`), и только
> потом коммит → push → деплой на VPS.
> Задача: <что делаем>.

Служебное: рабочие файлы прошлых сессий лежали в `C:\Temp` (скрипты, слепки,
ssh-обвязка деплоя `remote_update.sh`, `.cmd` для внешних проверок) — их можно
удалять, всё значимое перенесено в `tools/perf/` и `docs/`.
