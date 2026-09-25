# -*- coding: utf-8 -*-
"""Сленговые правки (`data/slang_overrides.json`) теперь в репозитории.

Файл правится из админки (`/ui/admin.html`) и раньше жил только на сервере,
поэтому свежий клон терял 59 подтверждённых разговорных названий. Тесты держат
три вещи:

* файл обязан лежать в репозитории (иначе пропадают правки — см. `.gitignore`);
* структуру: id существуют в `stops.json`, псевдонимы непустые и без дублей;
* правки действительно доезжают до поиска (`Locator` знает «Мальва», «тралдепо»).
"""
import json
from pathlib import Path

import main as app_main
from slang_store import apply_overrides

REPO = Path(__file__).resolve().parent.parent
OVERRIDES_PATH = REPO / "data" / "slang_overrides.json"
STOPS = json.loads((REPO / "stops.json").read_text(encoding="utf-8"))
STOP_IDS = {str(stop["id"]) for stop in STOPS}


def _overrides() -> dict:
    assert OVERRIDES_PATH.exists(), (
        "data/slang_overrides.json обязан быть в репозитории (git add -f): "
        "иначе свежий клон теряет все подтверждённые разговорные названия"
    )
    return json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))


def _merged() -> dict:
    return {str(stop["id"]): stop for stop in apply_overrides(app_main.load_stops(REPO / "stops.json"))}


def test_file_lives_in_repo_and_structure_is_sane():
    data = _overrides()
    assert data["version"] == 1
    assert data["updated"], "в файле должен быть час последней правки"

    entries = data["stops"]
    assert len(entries) >= 50, "тут лежат все подтверждённые разговорные названия"

    for stop_id, entry in entries.items():
        assert stop_id.isdigit(), stop_id
        assert stop_id in STOP_IDS, "правка на несуществующую остановку: %s" % stop_id
        assert entry.get("aliases"), entry
        seen = set()
        for alias in entry["aliases"]:
            assert isinstance(alias, str) and alias.strip(), (stop_id, alias)
            assert alias.strip().lower() not in seen, "дубль псевдонима: %s" % alias
            seen.add(alias.strip().lower())
        if "name" in entry:
            # Пустая строка — это «ничего не переименовываем»: apply_overrides
            # трактует её как отсутствие значения, поэтому в файле она бессмысленна.
            assert entry["name"].strip(), (stop_id, "пустое имя лучше не писать")
        if "generic" in entry:
            assert isinstance(entry["generic"], bool), (stop_id, entry["generic"])
        assert entry.get("updated"), stop_id


def test_every_alias_from_the_file_reaches_the_search_list():
    """Правка из админки — это не «на память»: она обязана попасть в остановки."""
    merged = _merged()
    entries = _overrides()["stops"]

    hidden = 0
    for stop_id, entry in entries.items():
        stop = merged.get(stop_id)
        if entry.get("generic") is True:
            hidden += 1
            continue
        assert stop is not None, "остановка %s пропала из поиска" % stop_id
        aliases = {alias.lower() for alias in stop["aliases"]}
        for alias in entry["aliases"]:
            assert alias.strip().lower() in aliases, (stop_id, alias)

    # Скрытых этим файлом остановок — ровно столько, сколько помечено generic=True
    # (у «на вимогу» это происходит автоматически, без правки).
    assert len(merged) == len(STOP_IDS) - hidden


def test_locator_resolves_override_only_aliases():
    """Псевдонимы, которых нет в `stops.json`: без файла эти запросы не работали."""
    stops = apply_overrides(app_main.load_stops(REPO / "stops.json"))
    streets = app_main.load_streets_geojson(REPO / "streets.json")
    locator = app_main.Locator(stops=stops, streets=streets)

    native = {str(stop["id"]): {alias.lower() for alias in (stop.get("aliases") or [])} for stop in STOPS}

    cases = {"Мальва": 80, "тралдепо": 143, "Юність": 105}
    for query, expected_id in cases.items():
        assert query.lower() not in native[str(expected_id)], (
            "%s есть и в stops.json — кейс не про этот файл" % query
        )
        stop_id, match_type = locator.locate(query)
        assert stop_id == expected_id, (query, stop_id, match_type)
        assert match_type == "stop", (query, match_type)


def test_drizhzavod_is_stop_81_only():
    """Владелец подтвердил истину: «Дріжзавод» — это остановка **#81**.

    Псевдоним лежал на #79 «Меблева фабрика» (правка 2026-09-16) и #80 «Стадіон
    «Мальва»» (2026-09-25) — они в 70 м друг от друга, похоже на промах мышью
    на карте. Сверка с OSM: #81 в 11 м от узла «Дріжджзавод» (1007282335), #79 —
    от «Меблева Фабрика», #80 — от «Стадіон «Мальва»».
    """
    entries = _overrides()["stops"]
    owners = sorted(
        stop_id for stop_id, entry in entries.items()
        if "дріжзавод" in {alias.lower() for alias in entry["aliases"]}
    )
    assert owners == ["81"], "псевдонім має бути рівно в однієї остановки"
    assert "79" not in entries, "у #79 нічого не лишилося — запис прибрано"

    stops = apply_overrides(app_main.load_stops(REPO / "stops.json"))
    streets = app_main.load_streets_geojson(REPO / "streets.json")
    locator = app_main.Locator(stops=stops, streets=streets)

    assert locator.locate("дріжзавод")[0] == 81
    # Соседи не потеряли свои собственные названия.
    assert locator.locate("Мальва")[0] == 80
    assert locator.locate("Меблева фабрика")[0] == 79


def test_no_alias_shadows_another_stop_native_name():
    """Строгий инвариант, который и поймал «дріжзавод».

    Псевдоним не имеет права совпадать с **родным именем другой** остановки:
    иначе запрос уходит не туда (при равном скоре `process.extractOne` берёт
    первый индекс). Именно так «дріжзавод» уезжал на #79 из-за записи в #80.

    Обратное разрешено и нормально: один псевдоним на нескольких остановках
    одного места — «соборка» ведёт на четыре платформы пл. Соборна, «калинка» —
    на семь остановок рынка. Это городская система, а не ошибка данных.
    """
    stops = apply_overrides(app_main.load_stops(REPO / "stops.json"))

    native: dict = {}
    alias_owners: dict = {}
    for stop in stops:
        native.setdefault((stop.get("name") or "").strip().lower(), set()).add(str(stop["id"]))
        for alias in stop.get("aliases") or []:
            alias_owners.setdefault(alias.strip().lower(), set()).add(str(stop["id"]))

    offenders = {
        text: {"alias_of": sorted(ids), "native_of": sorted(native[text] - ids)}
        for text, ids in alias_owners.items()
        if native.get(text, set()) - ids
    }
    assert offenders == {}, "псевдонім перекриває чуже рідне ім'я: %s" % offenders

