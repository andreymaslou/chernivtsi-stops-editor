"""Профили Azure (ostap/polina): прямой выбор родных голосов uk-UA.

Движки подменены фейками — тест не ходит в сеть и не пишет на диск.
"""

import tts_layer


def _fake_engines(monkeypatch, failing=()):
    """Фейки фиксируют, какой движок и с каким профилем вызвали."""
    calls = []

    def make(name):
        def fake(text, profile):
            calls.append((name, profile.get(name)))
            if name in failing:
                raise RuntimeError(f"{name} down")
            if not profile.get(name):
                return None  # как реальные движки: нет голоса -> None
            return b"\x00fake", "audio/mpeg"
        return fake

    for engine in ("elevenlabs", "azure", "openai"):
        monkeypatch.setitem(tts_layer._SYNTHESIZERS, engine, make(engine))
    monkeypatch.setattr(tts_layer, "_cache_enabled", lambda: False)
    monkeypatch.delenv("TTS_PROVIDER_ORDER", raising=False)
    return calls


def test_azure_profiles_exist_with_native_voices():
    assert tts_layer.VOICE_PROFILES["ostap"]["azure"] == "uk-UA-OstapNeural"
    assert tts_layer.VOICE_PROFILES["polina"]["azure"] == "uk-UA-PolinaNeural"
    # У закреплённых профилей нет voice_id ElevenLabs — чужой голос исключён.
    assert "elevenlabs" not in tts_layer.VOICE_PROFILES["ostap"]
    assert "elevenlabs" not in tts_layer.VOICE_PROFILES["polina"]


def test_pinned_profile_goes_to_azure_first(monkeypatch):
    calls = _fake_engines(monkeypatch)
    _audio, _mime, meta = tts_layer.generate_tts("тест", "ostap")
    assert meta["engine"] == "azure"
    assert meta["voice"] == "ostap"
    assert calls[0] == ("azure", "uk-UA-OstapNeural")


def test_regular_profile_keeps_default_order(monkeypatch):
    calls = _fake_engines(monkeypatch)
    _audio, _mime, meta = tts_layer.generate_tts("тест", "daniel")
    assert meta["engine"] == "elevenlabs"
    assert calls[0][0] == "elevenlabs"


def test_pinned_profile_falls_back_to_openai_not_elevenlabs(monkeypatch):
    calls = _fake_engines(monkeypatch, failing=("azure",))
    _audio, _mime, meta = tts_layer.generate_tts("тест", "polina")
    assert meta["engine"] == "openai"
    assert [name for name, _ in calls] == ["azure", "elevenlabs", "openai"]
    # ElevenLabs вызван, но без voice_id -> вернул None и не озвучил.
    assert calls[1] == ("elevenlabs", None)
