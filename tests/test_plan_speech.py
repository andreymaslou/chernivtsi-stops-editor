"""Озвучка плана и подсказка мест для LLM.

Проверяем то, что реально ломается: склонение украинских числительных
(«1 хвилина» / «2 хвилини» / «5 хвилин»), падеж после «з пересадкою на»
(«на тролейбус», а не «на тролейбусом») и отсутствие мусора в подсказке мест.
"""

import main


def _plan(legs, total_min=34, price_grn=20):
    return {"total_min": total_min, "price_grn": price_grn, "legs": legs}


def _bus(route, vehicle="bus"):
    return {"type": "transit", "vehicle": vehicle, "route": route}


def test_single_leg_speech_names_the_route():
    text = main.build_plan_speech(_plan([_bus("9")]))
    assert text.startswith("Поїздка автобусом номер 9")
    assert "34 хвилини" in text
    assert "20 гривень" in text
    assert text.endswith(".")


def test_trolley_is_named_as_trolley():
    text = main.build_plan_speech(_plan([_bus("3", vehicle="trolley")]))
    assert "тролейбусом номер 3" in text


def test_transfer_uses_accusative_after_na():
    """«з пересадкою на тролейбус», а не «на тролейбусом»."""
    legs = [
        _bus("10"),
        {"type": "transfer", "kind": "walk", "walk_min": 6},
        _bus("3", vehicle="trolley"),
    ]
    text = main.build_plan_speech(_plan(legs, total_min=49, price_grn=40))
    assert "з пересадкою на тролейбус номер 3" in text
    assert "тролейбусом номер 3" not in text


def test_all_legs_are_spoken_with_two_transfers():
    """Дві пересадки — три ноги, і голос мусить назвати кожну.

    Було: «перша + остання», тому середня нога зникала мовчки (план
    bus:8A + bus:5 + trolley:2 звучав як «8 а … на номер 2»).
    """
    legs = [
        _bus("10A"),
        {"type": "transfer", "kind": "walk", "walk_min": 4},
        _bus("5"),
        {"type": "transfer", "kind": "walk", "walk_min": 6},
        _bus("3", vehicle="trolley"),
    ]
    text = main.build_plan_speech(_plan(legs, total_min=60, price_grn=36))
    assert "автобусом номер 10 а" in text
    assert "автобусом номер 5" in text  # середня нога більше не губиться
    assert "з пересадкою на тролейбус номер 3" in text
    # Ноги зв'язує «потім», а падеж останньої лишається знахідним.
    assert ", потім " in text
    assert "тролейбусом номер 3" not in text


def test_middle_leg_keeps_instrumental_case():
    """Середня нога — орудний відмінок: «тролейбусом номер 39»."""
    legs = [_bus("13"), _bus("39", vehicle="trolley"), _bus("2")]
    text = main.build_plan_speech(_plan(legs))
    assert "Поїздка автобусом номер 13, потім тролейбусом номер 39" in text
    assert "з пересадкою на автобус номер 2" in text


def test_ukrainian_plural_forms():
    assert main._uk_plural(1, "хвилина", "хвилини", "хвилин") == "хвилина"
    assert main._uk_plural(2, "хвилина", "хвилини", "хвилин") == "хвилини"
    assert main._uk_plural(5, "хвилина", "хвилини", "хвилин") == "хвилин"
    # 11, 12-14 и 21 берут форму «хвилин», как в украинском.
    assert main._uk_plural(11, "хвилина", "хвилини", "хвилин") == "хвилин"
    assert main._uk_plural(12, "хвилина", "хвилини", "хвилин") == "хвилин"
    assert main._uk_plural(21, "хвилина", "хвилини", "хвилин") == "хвилина"


def test_letter_suffix_spoken_separately():
    text = main.build_plan_speech(_plan([_bus("5A")]))
    assert "номер 5 а" in text


def test_free_ticket_has_no_price():
    text = main.build_plan_speech(_plan([_bus("9")], price_grn=0))
    assert "гривень" not in text


def test_plan_without_transit_legs_gives_empty_speech():
    assert main.build_plan_speech(_plan([])) == ""
    assert main.build_plan_speech({"total_min": 10, "price_grn": 20}) == ""


def test_places_hint_is_built_and_bounded():
    stops = [
        {"id": 1, "name": "пл. Соборна", "aliases": ["соборка", "на соборці"]},
        {"id": 2, "name": "вул. Городоцька", "aliases": []},
        {"id": 3, "name": "Завод Гравітон", "aliases": ["гравитон", "гравітону"]},
    ]
    hint = main._collect_places_hint(stops)
    # Сленг попадает в подсказку — именно его модель не знает.
    assert "соборка" in hint
    assert "гравитон" in hint
    # Служебные пометки вырезаются.
    junk = main._collect_places_hint([{"id": 4, "name": "x", "aliases": ["маг (зроби сам)"]}])
    assert "(" not in junk
    # Длина ограничена.
    many = [{"id": i, "name": f"зупинка номер {i}", "aliases": []} for i in range(500)]
    hint_many = main._collect_places_hint(many)
    assert len(hint_many) <= main.PLACES_HINT_MAX_CHARS
    # Разделители не выталкивают строку за лимит.
    assert hint_many.endswith(",") is False


def test_system_prompt_contains_places():
    prompt = main.build_system_prompt("соборка, гравитон")
    assert "соборка" in prompt
    assert "{places}" not in prompt
    # Без списка мест промпт всё равно валиден.
    assert "список" in main.build_system_prompt("").lower()
