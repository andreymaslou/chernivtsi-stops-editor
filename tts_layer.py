"""Серверная озвучка эмулятора (TTS).

Движки пробуются по порядку TTS_PROVIDER_ORDER (по умолчанию
elevenlabs,azure,openai); первый, кто ответит, отдаёт звук:

  1. ElevenLabs — основной (model=eleven_multilingual_v2, mp3). Голос приходит
     из UI (?speaker=) и раскрывается в voice_id профиля (VOICE_PROFILES).
  2. Azure Speech — резерв (голоса uk-UA-OstapNeural / uk-UA-PolinaNeural).
  3. OpenAI tts-1 — последний резерв (голоса onyx/nova).

Локального Silero здесь больше нет. Модель v4_ua.pt умела только `mykyta`
(плоское «жестяное» звучание) и `random`, а `random` генерировал НОВЫЙ тембр на
каждом вызове — одна и та же фраза звучала разными голосами. Плюс движок тянул в
образ PyTorch (~200 МБ) ради сомнительного качества.

Если ни один движок не доступен, возвращаем (None, mime): main.api_tts отдаёт 503,
а клиент откатывается на системний голос браузера (Web Speech, web/emulator.js).

Результаты кэшируются на диске (<data>/tts_cache/<engine>/<sha1>.*): сводки плана
повторяются от запроса к запросу, и кэш экономит платные символы ElevenLabs.
"""

import base64
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple
from xml.sax.saxutils import escape as xml_escape

import uk_textnorm

logger = logging.getLogger(__name__)

# MIME отдачи: облачные движки отдают mp3. WAV оставлен для совместимости —
# старый клиент подставлял его по умолчанию.
MIME_MP3 = "audio/mpeg"
MIME_WAV = "audio/wav"

_MIME_EXT = {MIME_MP3: ".mp3", MIME_WAV: ".wav"}

# ---------------------------------------------------------------------------
# Голосовые профили
# ---------------------------------------------------------------------------
# Ключ профиля = значение <option> в UI (уходит в ?speaker=). Внутри — voice_id
# каждого движка. Все голоса ElevenLabs ниже — premade (категория premade из
# GET /v1/voices), то есть доступны на любом тарифе и читают украинский через
# multilingual-модель. Для Azure выбраны родные uk-UA нейронные голоса.
VOICE_PROFILES: Dict[str, Dict[str, str]] = {
    "daniel": {
        "label": "Деніел — диктор",
        "elevenlabs": "onwK4e9ZLuTAKqWW03F9",  # Daniel, Steady Broadcaster
        "azure": "uk-UA-OstapNeural",
        "openai": "onyx",
    },
    "adam": {
        "label": "Адам",
        "elevenlabs": "pNInz6obpgDQGcFmaJgB",  # Adam, Dominant/Firm
        "azure": "uk-UA-OstapNeural",
        "openai": "onyx",
    },
    "george": {
        "label": "Джордж",
        "elevenlabs": "JBFqnCBsd6RMkjVDRZzb",  # George, Warm Storyteller
        "azure": "uk-UA-OstapNeural",
        "openai": "onyx",
    },
    "alice": {
        "label": "Аліса — жіночий",
        "elevenlabs": "Xb7hH8MSUJpSbSDYk0k2",  # Alice, Clear Educator
        "azure": "uk-UA-PolinaNeural",
        "openai": "nova",
    },
    "sarah": {
        "label": "Сара — жіночий",
        "elevenlabs": "EXAVITQu4vr4xnSDxMaL",  # Sarah, Mature/Confident
        "azure": "uk-UA-PolinaNeural",
        "openai": "nova",
    },
    # Прямой выбор родных голосов Azure (uk-UA). Поле "engine" ставит движок
    # первым в очереди; voice_id ElevenLabs намеренно нет — иначе профиль мог
    # бы озвучиться чужим голосом. Резерв при сбое Azure — OpenAI.
    "ostap": {
        "label": "Остап — Azure",
        "engine": "azure",
        "azure": "uk-UA-OstapNeural",
        "openai": "onyx",
    },
    "polina": {
        "label": "Поліна — Azure",
        "engine": "azure",
        "azure": "uk-UA-PolinaNeural",
        "openai": "nova",
    },
}

FALLBACK_VOICE = "daniel"

# Голоса старого Silero-селектора: сохранённый в браузере выбор (mykyta/borys)
# должен продолжать звучать, а не упираться в неизвестный профиль.
LEGACY_VOICE_ALIASES = {"mykyta": "daniel", "borys": "daniel", "random": FALLBACK_VOICE}


def _default_voice_key() -> str:
    """Профиль по умолчанию. Env читаем лениво: load_dotenv() в main.py
    выполняется уже ПОСЛЕ импорта модулей, поэтому значение на уровне модуля
    «замораживалось» бы (см. коммит 2c804a9 про ключи TTS)."""
    key = (os.getenv("TTS_DEFAULT_VOICE") or FALLBACK_VOICE).strip().lower()
    return key if key in VOICE_PROFILES else FALLBACK_VOICE


def resolve_voice(speaker: Optional[str]) -> Tuple[str, Dict[str, str]]:
    """`?speaker=` -> (ключ профиля, сам профиль). Неизвестный — дефолтный."""
    key = (speaker or "").strip().lower()
    key = LEGACY_VOICE_ALIASES.get(key, key)
    if key not in VOICE_PROFILES:
        key = _default_voice_key()
    return key, VOICE_PROFILES[key]


# ---------------------------------------------------------------------------
# Кэш озвучки (Вариант A: хеш всей фразы -> файл)
# ---------------------------------------------------------------------------
# Ключ = sha1(текст + профиль + модель), файлы — в подпапке своего движка:
#   <data>/tts_cache/<engine>/<sha1>.mp3 + <sha1>.json (метаданные).
# Профиль (а не voice_id) в ключе — чтобы переключение движка не переиспользовало
# чужой файл. Каталог лежит на томе ./data:/app/data, поэтому переживает
# пересборку образа (данные не теряются).
#
# Почему Вариант A, а не кеш «по кусочкам»: кеш по фразам дешёв в реализации и
# даёт 100% попаданий на повторяющихся статических фразах («Маршрут не знайдено»,
# «Показую маршрути 9 та 10»). Сводки планов содержат меняющиеся цифры, поэтому
# они попадают в кеш реже — дробление по кускам (числа/цены/шаблоны) оставлено
# как следующий шаг по факту статистики hit/miss из логов.


def _cache_enabled() -> bool:
    return (os.getenv("TTS_CACHE", "1") or "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _data_dir() -> Path:
    """Папка данных. Читаем лениво и по тому же env, что storage.py
    (TRANSGPS_DATA_DIR), чтобы локальный запуск и Docker сходились."""
    return Path(os.getenv("TRANSGPS_DATA_DIR") or (Path(__file__).resolve().parent / "data"))


def _cache_root() -> Path:
    return _data_dir() / "tts_cache"


def _engine_model(engine: str) -> str:
    """Модель/формат, влияющие на звук (входят в ключ кэша). Голос учтён профилем."""
    if engine == "elevenlabs":
        return os.getenv("ELEVENLABS_MODEL") or "eleven_multilingual_v2"
    if engine == "openai":
        return os.getenv("OPENAI_TTS_MODEL") or "tts-1"
    if engine == "azure":
        return os.getenv("AZURE_OUTPUT_FORMAT") or "audio-24khz-48kbitrate-mono-mp3"
    return ""


def _cache_key(text: str, voice_key: str, model: str) -> str:
    raw = "\x00".join((text.strip(), voice_key, model))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _cache_read(engine: str, text: str, voice_key: str):
    """Возвращает (audio_bytes, mime, meta) или None, если файла нет/он битый."""
    if not _cache_enabled():
        return None
    key = _cache_key(text, voice_key, _engine_model(engine))
    folder = _cache_root() / engine
    try:
        meta = json.loads((folder / f"{key}.json").read_text(encoding="utf-8"))
        audio = (folder / meta["file"]).read_bytes()
    except (OSError, KeyError, ValueError):
        return None
    return audio, meta.get("mime", MIME_MP3), meta


def _cache_write(
    engine: str,
    text: str,
    voice_key: str,
    profile: Dict[str, str],
    audio: bytes,
    mime: str,
) -> None:
    """Пишет аудио атомарно (.tmp -> rename) + метаданные. Ошибки кэша глушим:
    недоступный диск не должен ломать озвучку."""
    if not _cache_enabled():
        return
    key = _cache_key(text, voice_key, _engine_model(engine))
    folder = _cache_root() / engine
    name = f"{key}{_MIME_EXT.get(mime, '.bin')}"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / (name + ".tmp")
        tmp.write_bytes(audio)
        tmp.replace(folder / name)
        meta = {
            "engine": engine,
            "voice": voice_key,
            "voice_id": profile.get(engine),
            "model": _engine_model(engine),
            "mime": mime,
            "file": name,
            "text": text,
            "chars": len(text),
            "bytes": len(audio),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        (folder / f"{key}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("TTS: не удалось записать кэш (%s): %s", folder, exc)


def cache_stats() -> Dict[str, int]:
    """Сколько записей уже лежит в кэше по движкам (для диагностики)."""
    stats: Dict[str, int] = {}
    root = _cache_root()
    if not root.is_dir():
        return stats
    for folder in root.iterdir():
        if folder.is_dir():
            stats[folder.name] = len(list(folder.glob("*.json")))
    return stats


# ---------------------------------------------------------------------------
# Движки синтеза
# ---------------------------------------------------------------------------
# Каждый движок возвращает (audio_bytes, mime), либо None если ключа/голоса нет,
# либо бросает исключение при ошибке API (его ловит generate_tts и идёт дальше).


def _timeout() -> float:
    """Таймаут HTTP к облачному TTS: без него зависший API держит воркер
    бесконечно, а клиент ждёт звук с открытым запросом."""
    try:
        return float(os.getenv("TTS_HTTP_TIMEOUT_SEC") or 20.0)
    except (TypeError, ValueError):
        return 20.0


def _synth_elevenlabs(text: str, profile: Dict[str, str]):
    api_key = os.getenv("ELEVENLABS_API_KEY")
    voice_id = profile.get("elevenlabs")
    if not api_key or not voice_id:
        return None
    import httpx

    res = httpx.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        json={"text": text, "model_id": _engine_model("elevenlabs")},
        timeout=_timeout(),
    )
    if res.status_code != 200:
        # 401 «missing permission text_to_speech» — это НЕ про неверный ключ, а про
        # невыданное разрешение в кабинете ElevenLabs. Видно в теле ответа.
        raise RuntimeError(f"ElevenLabs {res.status_code}: {res.text[:200]}")
    return res.content, MIME_MP3


def _azure_region() -> Optional[str]:
    """Идентификатор региона Azure Speech (`westeurope`), а не URL.

    В портале на странице «Keys and Endpoint» рядом лежат две строки: `Location/Region`
    (`westeurope`) и `Endpoint` (`https://westeurope.api.cognitive.microsoft.com/`).
    Вторую копируют чаще, и тогда URL собирался бы как
    `https://https://westeurope.api...` (400/502). Достаём регион из любой формы.
    """
    raw = (os.getenv("AZURE_SPEECH_REGION") or "").strip().lower()
    if not raw:
        return None
    host = raw.removeprefix("https://").removeprefix("http://").split("/")[0]
    region = host.split(".")[0]
    return region or None


def _synth_azure(text: str, profile: Dict[str, str]):
    api_key = os.getenv("AZURE_SPEECH_KEY")
    region = _azure_region()
    voice = profile.get("azure")
    if not api_key or not region or not voice:
        return None
    import httpx

    # Голосу обязателен xml:lang в SSML, иначе Azure отвечает 400.
    ssml = (
        "<speak version='1.0' xml:lang='uk-UA'>"
        f"<voice name='{voice}'>{xml_escape(text)}</voice>"
        "</speak>"
    )
    res = httpx.post(
        f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1",
        headers={
            "Ocp-Apim-Subscription-Key": api_key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": _engine_model("azure"),
            # User-Agent в контракте REST-API указан как обязательный: без него
            # часть регионов отвечает 400 на SSML-запрос.
            "User-Agent": "transgps-emulator/1.0",
        },
        content=ssml.encode("utf-8"),
        timeout=_timeout(),
    )
    if res.status_code != 200:
        raise RuntimeError(f"Azure {res.status_code}: {res.text[:200]}")
    return res.content, MIME_MP3


def _synth_openai(text: str, profile: Dict[str, str]):
    api_key = os.getenv("OPENAI_TTS_KEY")
    voice = profile.get("openai")
    if not api_key or not voice:
        return None
    from openai import OpenAI

    # Отдельный клиент без base_url: иначе запрос ушёл бы в OpenRouter.
    client = OpenAI(api_key=api_key, timeout=_timeout())
    response = client.audio.speech.create(
        model=_engine_model("openai"), voice=voice, input=text
    )
    return response.content, MIME_MP3


_SYNTHESIZERS: Dict[str, Callable[[str, Dict[str, str]], Optional[Tuple[bytes, str]]]] = {
    "elevenlabs": _synth_elevenlabs,
    "azure": _synth_azure,
    "openai": _synth_openai,
}

DEFAULT_PROVIDER_ORDER = ("elevenlabs", "azure", "openai")


def _provider_order():
    """Порядок движков из TTS_PROVIDER_ORDER (неизвестные имена отбрасываем)."""
    raw = os.getenv("TTS_PROVIDER_ORDER")
    names = [p.strip().lower() for p in raw.split(",")] if raw else list(DEFAULT_PROVIDER_ORDER)
    order = [n for n in names if n in _SYNTHESIZERS]
    return order or list(DEFAULT_PROVIDER_ORDER)


def _order_for(profile: Dict[str, str]):
    """Порядок движков для профиля: закреплённый движок профиля — первым."""
    order = _provider_order()
    pinned = (profile.get("engine") or "").strip().lower()
    if pinned in order:
        return [pinned] + [name for name in order if name != pinned]
    return order


def generate_tts(text: str, speaker: str = "daniel"):
    """Синтез речи. Возвращает кортеж (base64_аудио, mime, meta).

    `meta` = {"engine", "cache", "voice", "chars"} — уходит в /api/tts, чтобы по
    ответу было видно, какой движок сработал и был ли это кэш-хит.
    """
    text = (text or "").strip()
    # Підстраховка TN: якщо цифра потрапила в текст мімо шаблонів
    # main.py (повідомлення LLM, довільний текст /api/tts), розкриваємо
    # її словами ДО кешу — інакше TN рухача нормалізує по-своєму
    # (часто без узгодження роду). Кеш-ключ рахується від нормалізованого
    # тексту, тож «34 хвилини» і «тридцять чотири хвилини» — один запис.
    normalized = uk_textnorm.expand_digits(text)
    if normalized != text:
        logger.info(
            "TTS: TN підстраховка розкрила цифри: %r -> %r", text, normalized
        )
        text = normalized
    voice_key, profile = resolve_voice(speaker)
    if not text:
        return None, MIME_MP3, {"engine": None, "cache": "none", "voice": voice_key, "chars": 0}

    order = _order_for(profile)

    # 1. Кэш. Проверяем ДО сети: сводки плана повторяются, и хит не тратит символы.
    for engine in order:
        hit = _cache_read(engine, text, voice_key)
        if hit is not None:
            audio, mime, _meta = hit
            logger.info(
                "TTS: cache HIT (engine=%s, voice=%s, %d байт)", engine, voice_key, len(audio)
            )
            return (
                base64.b64encode(audio).decode("utf-8"),
                mime,
                {"engine": engine, "cache": "hit", "voice": voice_key, "chars": len(text)},
            )

    # 2. Синтез: первый движок, который ответил (остальные — резерв).
    for engine in order:
        try:
            result = _SYNTHESIZERS[engine](text, profile)
        except Exception as exc:  # noqa: BLE001 — падение движка не должно ломать запрос
            logger.error("TTS: движок %s не сработал: %s", engine, exc)
            continue
        if not result:
            continue
        audio, mime = result
        _cache_write(engine, text, voice_key, profile, audio, mime)
        logger.info(
            "TTS: %s MISS -> сгенерировано (voice=%s, %d байт, %d симв.)",
            engine, voice_key, len(audio), len(text),
        )
        return (
            base64.b64encode(audio).decode("utf-8"),
            mime,
            {"engine": engine, "cache": "miss", "voice": voice_key, "chars": len(text)},
        )

    logger.warning(
        "TTS: ни один движок не доступен (%s) — клиент уйдёт на системний голос",
        ", ".join(order),
    )
    return None, MIME_MP3, {"engine": None, "cache": "none", "voice": voice_key, "chars": len(text)}


def generate_tts_base64(text: str, speaker: str = "daniel"):
    """Совместимый фасад для вызовов, которым MIME/метаданные не нужны."""
    audio_base64, _mime, _meta = generate_tts(text, speaker)
    return audio_base64