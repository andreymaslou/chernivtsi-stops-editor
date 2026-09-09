"""
Быстрый smoke-тест main.py без реального обращения к OpenRouter.
Подменяет call_llm_extract_locations на фиктивную функцию и прогоняет
несколько сценариев через FastAPI TestClient.

Запуск: python _test_api.py
"""

import main
from fastapi.testclient import TestClient

# --- Подмена LLM-вызова: имитируем то, что вернула бы модель ---
FAKE_LLM_RESPONSES = {
    "Як доїхати з калинки до універу?": {"from": "калинки", "to": "універу"},
    "Мені треба з театральної на соборну": {"from": "театральної", "to": "соборну"},
    "З вулиці Головна до ринку": {"from": "вулиці Головна", "to": "ринку"},
    "Тільки до Гравітону": {"from": "", "to": "Гравітону"},
}


def fake_call_llm(user_text: str):
    data = FAKE_LLM_RESPONSES.get(user_text, {"from": "", "to": ""})
    return {"from": data["from"], "to": data["to"]}


main.call_llm_extract_locations = fake_call_llm

client = TestClient(main.app)

with client:
    print("=== /health ===")
    r = client.get("/health")
    print(r.status_code, r.json())

    for text in FAKE_LLM_RESPONSES:
        print(f"\n=== POST /api/route: {text!r} ===")
        r = client.post("/api/route", json={"text": text})
        print(r.status_code, r.json())

    print("\n=== Empty text -> expect 400 ===")
    r = client.post("/api/route", json={"text": "   "})
    print(r.status_code, r.json())
