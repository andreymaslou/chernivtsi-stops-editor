# -*- coding: utf-8 -*-
"""Гарантия украинского ответа и аварийного разбора без доступного LLM."""
from types import SimpleNamespace

import main as app_main


def _fake_client(create):
    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )


def test_emergency_parser_handles_typical_ukrainian_phrases():
    cases = {
        "з Соборки до Гравітону": {"from": "Соборки", "to": "Гравітону"},
        "я на Соборці, треба на Гравітон": {"from": "Соборці", "to": "Гравітон"},
        "від Калинки до Універу": {"from": "Калинки", "to": "Універу"},
        "Соборка → Гравітон": {"from": "Соборка", "to": "Гравітон"},
    }
    for text, expected in cases.items():
        assert app_main.emergency_extract_locations(text) == expected


def test_fallback_runs_after_all_models_fail(monkeypatch):
    def fail(*, model, messages, temperature, max_tokens):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(app_main, "OPENROUTER_MODELS", ["free-1", "free-2", "free-3"])
    monkeypatch.setattr(app_main, "llm_client", _fake_client(fail))
    app_main._llm_cache.clear()

    result = app_main.extract_locations_with_fallback("з Соборки до Гравітону")

    assert result["type"] == "fallback"
    assert result["from"] == "Соборки"
    assert result["to"] == "Гравітону"
    assert "Гравітону" in result["message"]


def test_invalid_json_advances_to_next_model(monkeypatch):
    calls = []

    def create(*, model, messages, temperature, max_tokens):
        calls.append(model)
        if model == "free-1":
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="not json"))]
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"type":"route","from":"Соборка","to":"Гравітон"}')
            )]
        )

    monkeypatch.setattr(app_main, "OPENROUTER_MODELS", ["free-1", "free-2"])
    monkeypatch.setattr(app_main, "llm_client", _fake_client(create))
    app_main._llm_cache.clear()

    result = app_main.call_llm_extract_locations("маршрут до Гравітону")

    assert calls == ["free-1", "free-2"]
    assert result == {"type": "route", "from": "Соборка", "to": "Гравітон"}


def test_api_returns_ukrainian_manual_input_when_everything_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        app_main,
        "call_llm_extract_locations",
        lambda text: {"type": "error", "from": "", "to": ""},
    )

    # TestClient поднимает lifespan с текущими данными проекта.
    from fastapi.testclient import TestClient

    with TestClient(app_main.app) as client:
        response = client.post("/api/plan", json={"text": "з Соборки до Гравітону"})

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "plan", body
    assert body["from_stop_id"] is not None
    assert body["to_stop_id"] is not None


def test_api_returns_ukrainian_response_when_llm_and_parser_cannot_understand(monkeypatch):
    monkeypatch.setattr(
        app_main,
        "call_llm_extract_locations",
        lambda text: {"type": "error", "from": "", "to": ""},
    )

    from fastapi.testclient import TestClient

    with TestClient(app_main.app) as client:
        response = client.post("/api/plan", json={"text": "яка сьогодні погода"})

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "manual_input"
    assert "Напишіть" in body["message"]
    assert body["message"].encode("utf-8").decode("utf-8") == body["message"]
