"""Профіли голосів: закріплені Azure (Остап/Поліна) і порядок движків.

Движки підменені фейками — тест не ходить в мережу і не пише на диск.
"""

import tts_layer


def _fake_engines(monkeypatch, failing=()):
    """Фейки фіксують, який движок і з яким профілем викликали."""
    calls = []

    def make(name):
        def fake(text, profile):
            calls.append((name, profile.get(name)))
            if name in failing:
                raise RuntimeError(f"{name} down")
            if not profile.get(name):
                return None  # як реальні движки: немає голосу -> None
            return b"\x00fake", "audio/mpeg"
        return fake

    for engine in ("elevenlabs", "azure", "openai"):
        monkeypatch.setitem(tts_layer._SYNTHESIZERS, engine, make(engine))
    monkeypatch.setattr(tts_layer, "_cache_enabled", lambda: False)
    monkeypatch.delenv("TTS_PROVIDER_ORDER", raising=False)
    return calls


def test_only_pinned_azure_profiles_exist():
    # У списку — тільки задіяні голоси: пара «чоловік + жінка» на Azure.
    assert set(tts_layer.VOICE_PROFILES) == {"ostap", "polina"}
    assert tts_layer.VOICE_PROFILES["ostap"]["engine"] == "azure"
    assert tts_layer.VOICE_PROFILES["ostap"]["azure"] == "uk-UA-OstapNeural"
    assert tts_layer.VOICE_PROFILES["polina"]["engine"] == "azure"
    assert tts_layer.VOICE_PROFILES["polina"]["azure"] == "uk-UA-PolinaNeural"
    # У закріплених профілів немає voice_id ElevenLabs — чужий голос виключений.
    assert "elevenlabs" not in tts_layer.VOICE_PROFILES["ostap"]
    assert "elevenlabs" not in tts_layer.VOICE_PROFILES["polina"]
    # Дефолтний голос — Остап (чоловічий), не мертвий ElevenLabs-профіль.
    assert tts_layer.FALLBACK_VOICE == "ostap"


def test_pinned_profile_goes_to_azure_first(monkeypatch):
    calls = _fake_engines(monkeypatch)
    _audio, _mime, meta = tts_layer.generate_tts("тест", "ostap")
    assert meta["engine"] == "azure"
    assert meta["voice"] == "ostap"
    assert calls[0] == ("azure", "uk-UA-OstapNeural")


def test_pinned_profile_falls_back_to_openai_not_elevenlabs(monkeypatch):
    calls = _fake_engines(monkeypatch, failing=("azure",))
    _audio, _mime, meta = tts_layer.generate_tts("тест", "polina")
    assert meta["engine"] == "openai"
    assert [name for name, _ in calls] == ["azure", "elevenlabs", "openai"]
    # ElevenLabs викликаний, але без voice_id -> повернув None і не озвучив.
    assert calls[1] == ("elevenlabs", None)


def test_unknown_speaker_falls_back_to_ostap(monkeypatch):
    # Невідомий speaker -> Остап (Azure), а не ElevenLabs.
    calls = _fake_engines(monkeypatch)
    _audio, _mime, meta = tts_layer.generate_tts("тест", "немає такого голосу")
    assert meta["voice"] == "ostap"
    assert meta["engine"] == "azure"
    assert calls[0] == ("azure", "uk-UA-OstapNeural")


def test_legacy_silero_aliases_resolve_to_ostap(monkeypatch):
    # Старий вибір браузера (mykyta/borys/random) мусить далі звучати.
    _fake_engines(monkeypatch)
    for legacy in ("mykyta", "borys", "random"):
        _audio, _mime, meta = tts_layer.generate_tts("тест", legacy)
        assert meta["voice"] == "ostap", legacy
