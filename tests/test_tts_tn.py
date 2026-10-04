"""Підстраховка TN в tts_layer: цифри -> слова перед синтезом і кешем.

Рухачі підміняємо фейком, щоб тест не ліз у мережу і не писав
на диск: _cache_enabled -> False вимикає і читання, і запис.
"""

import tts_layer


def _capture(monkeypatch):
    """Замінюємо всі движки фейком, що фіксує отриманий текст."""
    captured = {}

    def fake_synth(text, profile):
        captured["text"] = text
        return b"\x00fake-mp3", "audio/mpeg"

    for engine in ("elevenlabs", "azure", "openai"):
        monkeypatch.setitem(tts_layer._SYNTHESIZERS, engine, fake_synth)
    monkeypatch.setattr(tts_layer, "_cache_enabled", lambda: False)
    return captured


def test_generate_tts_expands_digits_before_synth(monkeypatch):
    """«номер 9, 32хв» -> слова ДО рухача (TN ElevenLabs не потрібна)."""
    captured = _capture(monkeypatch)
    audio, mime, meta = tts_layer.generate_tts("номер 9, приблизно 32хв")
    assert captured["text"] == "номер дев'ять, приблизно тридцять дві хвилини"
    assert audio  # base64 не порожній
    assert mime == "audio/mpeg"
    assert meta["engine"] == "elevenlabs"
    assert meta["cache"] == "miss"


def test_generate_tts_expands_standalone_numbers(monkeypatch):
    captured = _capture(monkeypatch)
    tts_layer.generate_tts("маршрути 9 та 10")
    assert captured["text"] == "маршрути дев'ять та десять"


def test_generate_tts_expands_clock_time(monkeypatch):
    captured = _capture(monkeypatch)
    tts_layer.generate_tts("прибуття о 14:41")
    assert captured["text"] == "прибуття о чотирнадцята сорок одна"


def test_generate_tts_keeps_route_labels(monkeypatch):
    """«8A» — лейбл: літера поруч із цифрою, підстраховка не чіпає."""
    captured = _capture(monkeypatch)
    tts_layer.generate_tts("автобус 8A")
    assert captured["text"] == "автобус 8A"


def test_generate_tts_clean_text_untouched(monkeypatch):
    captured = _capture(monkeypatch)
    text = ("Поїздка автобусом номер дев'ять, "
            "приблизно тридцять чотири хвилини.")
    tts_layer.generate_tts(text)
    assert captured["text"] == text


def test_generate_tts_cache_key_on_normalized_text(monkeypatch):
    """Ключ кешу рахується від нормалізованого тексту: цифрова
    і словесна форма фрази дають один запис."""
    captured = _capture(monkeypatch)
    tts_layer.generate_tts("34 хвилини")
    assert captured["text"] == "тридцять чотири хвилини"
    import uk_textnorm

    key_from_digits = tts_layer._cache_key(
        uk_textnorm.expand_digits("34 хвилини"),
        "daniel", "eleven_multilingual_v2",
    )
    key_from_words = tts_layer._cache_key(
        "тридцять чотири хвилини", "daniel", "eleven_multilingual_v2",
    )
    assert key_from_digits == key_from_words
