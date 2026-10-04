# -*- coding: utf-8 -*-
"""
Голосовой мониторинг маршрутов (идея 2026-09-28, docs/ideas/): «покажи
дев'ятку і десятку» — это не маршрут А→Б, а просьба показать машины названных
линий. Проверяем три слоя:

  * промпт LLM — новый интент monitor_routes и правило нормализации номеров;
  * локальный разбор/нормализация номеров (нужен и без модели — без ключа
    OpenRouter, а также когда модель вернула слово вместо цифры);
  * контракт /api/plan и /api/route: mode="monitor_routes", requested_routes,
    routes с ключами кнопок фильтра, speech; остановок и ног нет вовсе.
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main as app_main


def _fake_client(create):
    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )


@pytest.fixture
def api_client():
    """Сервер целиком: lifespan читает манифест активных маршрутов (38)."""
    with TestClient(app_main.app) as client:
        yield client


# --- Промпт и локальный разбор номеров --------------------------------------

def test_prompt_teaches_monitor_intent_and_normalization():
    """Промпт обязан требовать monitor_routes и цифровые номера, а не слова."""
    prompt = app_main.build_system_prompt("соборка, гравитон")
    assert "monitor_routes" in prompt
    assert "дев'ятка" in prompt and "десятка" in prompt
    assert "9а" in prompt and "9A" in prompt  # правило «литера латиницей»
    assert "НІКОЛИ не вигадуй номер" in prompt


def test_parse_route_numbers_handles_slang_and_declensions():
    """Сленг и порядковые в падежах — «дев'ятку», а не «дев'ятка»."""
    assert app_main.parse_route_numbers("покажи дев'ятку і десятку") == ["9", "10"]
    assert app_main.parse_route_numbers("покажи одиничку") == ["1"]
    assert app_main.parse_route_numbers("а де зараз п'ятірка з десяткою") == ["5", "10"]
    assert app_main.parse_route_numbers("хочу бачити дванадцятку") == ["12"]


def test_parse_route_numbers_requires_marker_for_digits():
    """Цифры без слова-маркера — это адрес, а не маршрут («вулиця 9»)."""
    assert app_main.parse_route_numbers("де зараз 9А") == ["9A"]
    assert app_main.parse_route_numbers("покажи 5 тролейбус") == ["5"]
    assert app_main.parse_route_numbers("як доїхати до вулиці 9") == []
    assert app_main.parse_route_numbers("з Соборки до Гравітону") == []


def test_parse_route_numbers_does_not_match_day_of_week():
    """«п'ятниця» — не маршрут 5: окончания для основы намеренно узкие."""
    assert app_main.parse_route_numbers("у п'ятницю покажи дев'ятку") == ["9"]
    assert app_main.parse_route_numbers("покажи розклад на п'ятницю") == []


def test_parse_route_numbers_normalizes_letters_and_duplicates():
    """«9а» = «9A»; повтор номера не дублируется в фразе."""
    assert app_main.parse_route_numbers("покажи 9а та 9А") == ["9A"]
    assert app_main.parse_route_numbers("покажи 15к") == ["15K"]
    assert app_main.parse_route_numbers("де зараз 3/3a") == ["3/3A"]
    assert app_main.parse_route_numbers("покажи 8 і 8") == ["8"]


def test_clean_route_list_fixes_model_answers():
    """Модель вернула слово или «маршрут №10» — приводим к цифрам локально."""
    assert app_main._clean_route_list(["дев'ятка", "10"]) == ["9", "10"]
    assert app_main._clean_route_list(["9а", "дев'ятку", "9A"]) == ["9A", "9"]
    assert app_main._clean_route_list(["маршрут №15к"]) == ["15K"]
    assert app_main._clean_route_list([10, "десятий"]) == ["10"]
    assert app_main._clean_route_list("якась абракадабра") == []
    assert app_main._clean_route_list(None) == []


# --- Сопоставление с активными маршрутами города ----------------------------

def test_resolve_requested_routes_maps_to_filter_keys(api_client):
    """Ключ = ключ кнопки фильтра в UI («bus|9A»), тип ТС входит в ключ."""
    found, missing = app_main.resolve_requested_routes(["9", "10"])

    assert [item["key"] for item in found] == ["bus|9", "bus|10"]
    assert missing == []
    assert found[0]["type"] == "bus" and found[0]["label"] == "9"


def test_resolve_requested_routes_accepts_letters_and_aliases(api_client):
    """«9а» = «9A», «3/3a» — алиас троллейбуса 3 из live_names."""
    found_9a, _ = app_main.resolve_requested_routes(["9а"])
    found_3, _ = app_main.resolve_requested_routes(["3/3a"])

    assert [item["key"] for item in found_9a] == ["bus|9A"]
    assert [item["key"] for item in found_3] == ["trolley|3"]


def test_resolve_requested_routes_returns_both_types_for_same_number(api_client):
    """«5» есть и у автобусов, и у троллейбусов — отдаём оба, не половину."""
    found, missing = app_main.resolve_requested_routes(["5"])

    assert [item["key"] for item in found] == ["bus|5", "trolley|5"]
    assert missing == []


def test_resolve_requested_routes_reports_unknown_number(api_client):
    """Номера вне активных маршрутов городa честно идут в missing."""
    found, missing = app_main.resolve_requested_routes(["9", "42", "777"])

    assert found and [item["key"] for item in found] == ["bus|9"]
    assert missing == ["42", "777"]


# --- Голосовая фраза --------------------------------------------------------

def test_monitor_speech_phrases():
    """Одна фраза — «маршрут дев'ять», дві і більше — «маршрути дев'ять та десять»."""
    routes = [{"key": "bus|9", "type": "bus", "label": "9"},
              {"key": "bus|10", "type": "bus", "label": "10"}]
    assert app_main.build_monitor_speech(routes[:1], []) == "Показую маршрут дев'ять."
    assert app_main.build_monitor_speech(routes, []) == "Показую маршрути дев'ять та десять."
    assert app_main.build_monitor_speech(routes, ["42"]) == (
        "Показую маршрути дев'ять та десять, На жаль, маршрут сорок два зараз не працює.")


def test_monitor_speech_reads_letter_in_ukrainian():
    """«9A» звучить як «дев'ять а» — як і в озвучці планов."""
    routes = [{"key": "bus|9A", "type": "bus", "label": "9A"}]

    assert app_main.build_monitor_speech(routes, []) == "Показую маршрут дев'ять а."


def test_monitor_speech_is_empty_without_routes_and_missing():
    """Пустой разбор — молчание: текст-подсказка идёт в панель, не в голос."""
    assert app_main.build_monitor_speech([], []) == ""



# --- Контракт /api/plan и /api/route ----------------------------------------

def _monitor_stub(routes):
    """LLM-заглушка: интент мониторинга с заданными номерами."""
    return lambda text: {"type": "monitor_routes", "routes": list(routes)}


def test_plan_monitor_intent_returns_routes_without_stops(api_client, monkeypatch):
    """«Покажи 9 і 10»: остановок и ног нет вовсе, фильтр и озвучка на месте."""
    monkeypatch.setattr(
        app_main, "call_llm_extract_locations", _monitor_stub(["дев'ятка", "10"])
    )

    resp = api_client.post("/api/plan", json={"text": "покажи дев'ятку і десятку"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "monitor_routes"
    # Модель вернула слово — в ответ клиенту всё равно уходят цифры.
    assert body["requested_routes"] == ["9", "10"]
    assert [item["key"] for item in body["routes"]] == ["bus|9", "bus|10"]
    assert body["missing_routes"] == []
    assert body["speech"]["text"] == "Показую маршрути дев'ять та десять."
    assert "на карті лише їхні машини" in body["message"]
    # Геопоиск и роутер не участвуют: точек в фразе нет вовсе.
    assert "legs" not in body
    assert body.get("from_stop_id") is None and body.get("to_stop_id") is None


def test_plan_monitor_reports_unknown_route(api_client, monkeypatch):
    """Номер вне активных озвучивается отдельно, найденные — показываются."""
    monkeypatch.setattr(app_main, "call_llm_extract_locations", _monitor_stub(["9", "42"]))

    body = api_client.post("/api/plan", json={"text": "покажи 9 і 42"}).json()

    assert body["mode"] == "monitor_routes"
    assert [item["key"] for item in body["routes"]] == ["bus|9"]
    assert body["missing_routes"] == ["42"]
    assert "не працює" in body["speech"]["text"]


def test_plan_monitor_works_without_llm(api_client, monkeypatch):
    """Модель недоступна — сленг-номера всё равно дают режим мониторинга."""
    monkeypatch.setattr(
        app_main, "call_llm_extract_locations",
        lambda text: {"type": "error", "from": "", "to": ""},
    )

    body = api_client.post("/api/plan", json={"text": "покажи дев'ятку"}).json()

    assert body["mode"] == "monitor_routes"
    assert [item["key"] for item in body["routes"]] == ["bus|9"]


def test_route_endpoint_returns_monitor_mode(api_client, monkeypatch):
    """/api/route (старый клиент) отдаёт тот же режим — схема его знает."""
    monkeypatch.setattr(app_main, "call_llm_extract_locations", _monitor_stub(["9", "10"]))

    body = api_client.post("/api/route", json={"text": "покажи 9 і 10"}).json()

    assert body["mode"] == "monitor_routes"
    assert body["requested_routes"] == ["9", "10"]
    assert [item["key"] for item in body["routes"]] == ["bus|9", "bus|10"]
    assert body["speech"]["text"] == "Показую маршрути дев'ять та десять."
    assert body["from_stop_id"] is None and body["to_stop_id"] is None


def test_route_intent_is_not_turned_into_monitor(api_client, monkeypatch):
    """Обычный А→Б остаётся планом: интенты не смешиваются."""
    monkeypatch.setattr(
        app_main, "call_llm_extract_locations",
        lambda text: {"type": "route", "from": "Соборка", "to": "Гравітон"},
    )

    body = api_client.post("/api/plan", json={"text": "з Соборки до Гравітону"}).json()

    assert body["mode"] == "plan"
    assert body["legs"]


# --- Разбор ответа LLM ------------------------------------------------------

def test_llm_monitor_intent_normalizes_words(monkeypatch):
    """Слово от модели («дев'ятку») становится цифрой до ответа клиенту."""
    def create(*, model, messages, temperature, max_tokens):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(
                content='{"type":"monitor_routes","routes":["дев\'ятку","10"]}')
        )])

    monkeypatch.setattr(app_main, "OPENROUTER_MODELS", ["free-1"])
    monkeypatch.setattr(app_main, "llm_client", _fake_client(create))
    app_main._llm_cache.clear()

    result = app_main.call_llm_extract_locations("покажи дев'ятку і десятку")

    assert result == {"type": "monitor_routes", "routes": ["9", "10"]}


def test_llm_monitor_without_routes_advances_to_next_model(monkeypatch):
    """Интент без номеров — брак: пробуем следующую модель."""
    calls = []

    def create(*, model, messages, temperature, max_tokens):
        calls.append(model)
        content = ('{"type":"monitor_routes","routes":[]}' if model == "free-1"
                   else '{"type":"monitor_routes","routes":["7"]}')
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=content))])

    monkeypatch.setattr(app_main, "OPENROUTER_MODELS", ["free-1", "free-2"])
    monkeypatch.setattr(app_main, "llm_client", _fake_client(create))
    app_main._llm_cache.clear()

    result = app_main.call_llm_extract_locations("покажи сімку")

    assert calls == ["free-1", "free-2"]
    assert result == {"type": "monitor_routes", "routes": ["7"]}


# --- Геометрия линий для карты (идея 2026-09-29, п.2) -----------------------

def test_monitor_response_carries_route_shapes(api_client, monkeypatch):
    """Линии маршрутов для карты: оба направления и координаты самого города.

    У клиента своей геометрии нет (`osm_routes.json` в проекте не существует,
    `graph.json` в браузер не грузим), поэтому сервер отдаёт цепочки остановок
    прямо в ответе — те же, что уезжают в `leg.full_geom` плана.
    """
    monkeypatch.setattr(app_main, "call_llm_extract_locations", _monitor_stub(["9", "10"]))

    body = api_client.post("/api/plan", json={"text": "покажи 9 і 10"}).json()

    shapes = {item["key"]: item for item in body["shapes"]}
    assert set(shapes) == {"bus|9", "bus|10"}
    for shape in shapes.values():
        assert len(shape["directions"]) == 2, "у міського маршруту є обидва напрямки"
        for direction in shape["directions"]:
            assert len(direction["coords"]) >= 2, "лінія з однієї точки — не лінія"
            lat, lon = direction["coords"][0]
            # Чернівці: 48.2–48.4 / 25.8–26.1. Проверка ловит «широту 0» и
            # случайно попавшую чужую геометрию.
            assert 48.1 < lat < 48.5 and 25.7 < lon < 26.2, direction["coords"][0]
    # Порядок направлений — как в графе: A, затем B.
    assert shapes["bus|9"]["directions"][0]["direction"] == "A"


def test_route_shapes_only_for_requested(api_client, monkeypatch):
    """Линия рисуется только для запрошенного маршрута."""
    monkeypatch.setattr(app_main, "call_llm_extract_locations", _monitor_stub(["10"]))

    body = api_client.post("/api/plan", json={"text": "покажи 10"}).json()

    assert [item["key"] for item in body["shapes"]] == ["bus|10"]


def test_route_shapes_match_keys_with_letter(api_client, monkeypatch):
    """«9а» = «9A»: ключ фильтра и геометрия обязаны сойтись."""
    monkeypatch.setattr(app_main, "call_llm_extract_locations", _monitor_stub(["9а"]))

    body = api_client.post("/api/plan", json={"text": "покажи 9а"}).json()

    assert [item["key"] for item in body["routes"]] == ["bus|9A"]
    assert [item["key"] for item in body["shapes"]] == ["bus|9A"]


def test_route_shapes_empty_without_routes(api_client):
    """Нет маршрутов — нет и геометрии (и никаких исключений)."""
    assert app_main.route_shapes_for([]) == []


def test_route_shapes_empty_for_unknown_number(api_client, monkeypatch):
    """Номер вне активных маршрутов: нет ключей — нет и геометрии."""
    monkeypatch.setattr(app_main, "call_llm_extract_locations", _monitor_stub(["42"]))

    body = api_client.post("/api/plan", json={"text": "покажи 42"}).json()

    assert body["routes"] == [] and body["shapes"] == []
    assert body["missing_routes"] == ["42"]


# --- Тип ТС в фразе и колізія номера (бриф Gemini 2026-10-01) ---------------

def test_vehicle_type_hint_reads_both_spellings():
    """«автобус» → bus, «тролейбус»/«троллейбус» → trolley, оба → both, ничего → ''."""
    assert app_main._vehicle_type_hint("покажи 4 автобус") == "bus"
    assert app_main._vehicle_type_hint("автобус 4") == "bus"
    assert app_main._vehicle_type_hint("хочу бачити 5 тролейбус") == "trolley"
    assert app_main._vehicle_type_hint("де зараз 5 троллейбус") == "trolley"
    assert app_main._vehicle_type_hint("покажи 4") == ""
    # Оба типа названы разом — это осознанный выбор «оба», не путать с «не назван».
    assert app_main._vehicle_type_hint("автобус і тролейбус 5") == "both"


def test_resolve_requested_routes_filters_by_explicit_type(api_client):
    """«4 автобус» отсекает троллейбус 4 — тип назван строго."""
    bus, bus_missing = app_main.resolve_requested_routes(["4"], "bus")
    trolley, trolley_missing = app_main.resolve_requested_routes(["4"], "trolley")

    assert [item["key"] for item in bus] == ["bus|4"] and bus_missing == []
    assert [item["key"] for item in trolley] == ["trolley|4"] and trolley_missing == []


def test_resolve_requested_routes_number_is_not_fuzzy(api_client):
    """«8» не захоплює ні «8A», ні чужі букви: лише рівні номери."""
    found, missing = app_main.resolve_requested_routes(["8"])

    assert [item["key"] for item in found] == ["bus|8", "trolley|8"]
    assert missing == []
    # «4T» — це алиас троллейбуса 4, а не окремий «номер T».
    four_t, _ = app_main.resolve_requested_routes(["4T"])
    assert [item["key"] for item in four_t] == ["trolley|4"]


def test_speech_ordinal_masculine():
    """Порядковий числівник для озвучки; літери й дроби не порядкові."""
    assert app_main._speech_ordinal_masc("1") == "перший"
    assert app_main._speech_ordinal_masc("4") == "четвертий"
    assert app_main._speech_ordinal_masc("10") == "десятий"
    assert app_main._speech_ordinal_masc("21") == "двадцять перший"
    assert app_main._speech_ordinal_masc("43") == "сорок третій"
    assert app_main._speech_ordinal_masc("9A") == ""
    assert app_main._speech_ordinal_masc("3/3a") == ""


def test_monitor_speech_disambiguates_shared_number():
    """Один номер у двох типів ТС — озвучка називає тип, а не «4 та 4»."""
    routes = [{"key": "bus|4", "type": "bus", "label": "4"},
              {"key": "trolley|4", "type": "trolley", "label": "4"}]

    speech = app_main.build_monitor_speech(routes, [])

    assert speech == "Показую четвертий автобус та четвертий тролейбус."
    assert "4 та 4" not in speech


def test_monitor_message_disambiguates_shared_number():
    """Панель ответа тоже называет тип, а не склеивает «4 та 4»."""
    routes = [{"key": "bus|4", "type": "bus", "label": "4"},
              {"key": "trolley|4", "type": "trolley", "label": "4"}]

    message = app_main._monitor_routes_message(routes, [])

    assert message.startswith("Показую четвертий автобус та четвертий тролейбус")
    assert "на карті лише їхні машини" in message


def test_plan_monitor_filters_bus_from_phrase(api_client, monkeypatch):
    """«покажи 4 автобус»: на карті лише автобус, троллейбус 4 не їде."""
    monkeypatch.setattr(app_main, "call_llm_extract_locations", _monitor_stub(["4"]))

    body = api_client.post("/api/plan", json={"text": "покажи 4 автобус"}).json()

    assert body["mode"] == "monitor_routes"
    assert [item["key"] for item in body["routes"]] == ["bus|4"]
    assert [item["key"] for item in body["shapes"]] == ["bus|4"]
    assert body["speech"]["text"] == "Показую маршрут чотири."


def test_plan_monitor_filters_trolley_from_phrase(api_client, monkeypatch):
    """«покажи 4 тролейбус»: на карті лише тролейбус."""
    monkeypatch.setattr(app_main, "call_llm_extract_locations", _monitor_stub(["4"]))

    body = api_client.post("/api/plan", json={"text": "покажи 4 тролейбус"}).json()

    assert [item["key"] for item in body["routes"]] == ["trolley|4"]
    assert body["speech"]["text"] == "Показую маршрут чотири."


def test_plan_monitor_shared_number_asks_clarify(api_client, monkeypatch):
    """Без типу «4» — не показуємо навмання, а питаємо: автобус/тролейбус/обидва."""
    monkeypatch.setattr(app_main, "call_llm_extract_locations", _monitor_stub(["4"]))

    body = api_client.post("/api/plan", json={"text": "покажи 4"}).json()

    assert body["mode"] == "monitor_clarify"
    assert body["reask"] is True
    assert body["requested_routes"] == ["4"]
    assert body["ambiguous_routes"] == ["4"]
    assert "автобусів" in body["message"] and "тролейбусів" in body["message"]
    options = {opt["id"]: opt for opt in body["clarify_options"]}
    assert list(options) == ["bus", "trolley", "both"]
    assert [r["key"] for r in options["bus"]["routes"]] == ["bus|4"]
    assert [r["key"] for r in options["trolley"]["routes"]] == ["trolley|4"]
    assert [r["key"] for r in options["both"]["routes"]] == ["bus|4", "trolley|4"]
    # Каждый вариант несёт готовую геометрию и свою фразу — клиент не парсит заново.
    assert [s["key"] for s in options["trolley"]["shapes"]] == ["trolley|4"]
    assert options["both"]["speech"]["text"] == (
        "Показую четвертий автобус та четвертий тролейбус.")
    assert options["bus"]["speech"]["text"] == "Показую маршрут чотири."


def test_plan_monitor_ambiguous_list_gives_three_options(api_client, monkeypatch):
    """Список «1,3,4,6,23,39»: спільні 1,3,4,6 уточнюємо; 23,39 — завжди автобус."""
    monkeypatch.setattr(
        app_main, "call_llm_extract_locations",
        _monitor_stub(["1", "3", "4", "6", "23", "39"]),
    )

    body = api_client.post("/api/plan", json={"text": "покажи 1, 3, 4, 6, 23, 39"}).json()

    assert body["mode"] == "monitor_clarify"
    assert body["ambiguous_routes"] == ["1", "3", "4", "6"]
    options = {opt["id"]: [r["key"] for r in opt["routes"]] for opt in body["clarify_options"]}
    assert options["bus"] == ["bus|1", "bus|3", "bus|4", "bus|6", "bus|23", "bus|39"]
    assert options["trolley"] == [
        "trolley|1", "trolley|3", "trolley|4", "trolley|6", "bus|23", "bus|39"]
    assert options["both"] == [
        "bus|1", "trolley|1", "bus|3", "trolley|3", "bus|4", "trolley|4",
        "bus|6", "trolley|6", "bus|23", "bus|39"]


def test_plan_monitor_unambiguous_number_skips_clarify(api_client, monkeypatch):
    """«9» є лише в автобуса — питати нема про що, показуємо одразу."""
    monkeypatch.setattr(app_main, "call_llm_extract_locations", _monitor_stub(["9"]))

    body = api_client.post("/api/plan", json={"text": "покажи дев'ятку"}).json()

    assert body["mode"] == "monitor_routes"
    assert [item["key"] for item in body["routes"]] == ["bus|9"]


def test_plan_monitor_unambiguous_list_skips_clarify(api_client, monkeypatch):
    """Список без спільних номерів (9,23,39) — однозначний, без карточок."""
    monkeypatch.setattr(app_main, "call_llm_extract_locations", _monitor_stub(["9", "23", "39"]))

    body = api_client.post("/api/plan", json={"text": "покажи 9, 23, 39"}).json()

    assert body["mode"] == "monitor_routes"
    assert [item["key"] for item in body["routes"]] == ["bus|9", "bus|23", "bus|39"]


def test_plan_monitor_both_types_named_no_clarify(api_client, monkeypatch):
    """«автобус 4 і тролейбус 4» — тип назван (both), уточнення не потрібне."""
    monkeypatch.setattr(app_main, "call_llm_extract_locations", _monitor_stub(["4"]))

    body = api_client.post(
        "/api/plan", json={"text": "покажи автобус 4 і тролейбус 4"}).json()

    assert body["mode"] == "monitor_routes"
    assert [item["key"] for item in body["routes"]] == ["bus|4", "trolley|4"]


def test_monitor_clarify_question_and_speech():
    """Вопрос уточнения: тире в панели, точка в голосе (движки читают «—» криво)."""
    assert app_main._monitor_clarify_question(["4"]) == (
        "Маршрут 4 є і в автобусів, і в тролейбусів — що показати?")
    assert app_main._monitor_clarify_speech(["4"]) == (
        "Маршрут чотири є і в автобусів, і в тролейбусів. Що показати?")
    assert app_main._monitor_clarify_question(["1", "3", "4", "6"]).startswith("Маршрути 1, 3, 4 та 6")
