# -*- coding: utf-8 -*-
"""Тексты переспроса `/api/plan` (mode: "clarify").

Проверяем то, что реально видит пользователь. Если он назвал только одну
половину фразы («до Гравітону»), сервер обязан спросить ТОЛЬКО про вторую и
назвать уже понятую; а при неуверенном совпадении Locator (`low_confidence`) —
честно сказать, что не разобрал название. Раньше на любой clarify приходило
«звідки і куди», хотя пункт назначения был известен.
"""
from fastapi.testclient import TestClient

import main as app_main


def _clarify(monkeypatch, fake, text):
    """Прогон через живой эндпоинт: LLM подменена, парк не запрашивается."""
    monkeypatch.setattr(
        app_main, "call_llm_extract_locations", lambda _text, _fake=fake: dict(_fake)
    )
    app_main._llm_cache.clear()
    with TestClient(app_main.app) as client:
        response = client.post("/api/plan", json={"text": text})
    assert response.status_code == 200
    return response.json()


def test_missing_origin_asks_only_for_origin(monkeypatch):
    """Пункт призначення понят — питаємо лише «а звідки?»."""
    body = _clarify(
        monkeypatch, {"type": "route", "from": "", "to": "Гравітон"}, "до Гравітону"
    )
    assert body["mode"] == "clarify", body
    assert body["to_name"] == 'Завод "Гравітон"'
    assert body["from_name"] is None
    assert body["reask"] is True
    assert "А звідки" in body["note"]
    assert "Гравітон" in body["note"]


def test_missing_destination_asks_only_for_destination(monkeypatch):
    body = _clarify(
        monkeypatch, {"type": "route", "from": "Соборка", "to": ""}, "я на Соборці"
    )
    assert body["mode"] == "clarify", body
    assert body["from_name"] == "пл. Соборна"
    assert body["to_name"] is None
    assert body["reask"] is True
    assert "куди потрібно доїхати" in body["note"]
    assert "Соборна" in body["note"]


def test_low_confidence_keeps_honest_reask(monkeypatch):
    """Абракадабра: Locator видає low_confidence — питаємо обидві половини."""
    body = _clarify(
        monkeypatch,
        {"type": "route", "from": "абракадабра", "to": "Гравітон"},
        "з абракадабри до Гравітону",
    )
    assert body["mode"] == "clarify", body
    assert body["debug_info"]["from_type"] == "low_confidence"
    assert body["reask"] is True
    assert "не до кінця зрозумів назви" in body["note"]


def test_one_sided_query_with_low_confidence_destination(monkeypatch):
    """«до ринку»: назви непевна (low_confidence), але виїзд не названий зовсім.

    Текст мусить звучати як припущення і питати про виїзд, а не звинувачувати
    обидві половини фрази.
    """
    body = _clarify(
        monkeypatch, {"type": "route", "from": "", "to": "ринку"}, "до ринку"
    )
    assert body["mode"] == "clarify", body
    assert body["debug_info"]["from_type"] == "not_specified"
    assert body["debug_info"]["to_type"] == "low_confidence"
    assert body["to_name"] == "Центральний ринок"
    assert body["reask"] is True
    assert "Здається" in body["note"]
    assert "А звідки" in body["note"]
