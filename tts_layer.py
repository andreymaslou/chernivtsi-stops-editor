import os
import io
import base64
import logging

logger = logging.getLogger(__name__)

tts_model = None

# Для ElevenLabs нужен ID голоса. По умолчанию берем какой-то красивый (например, Antony или Marcus)
# 21m00Tcm4TlvDq8ikWAM = Rachel, pNInz6obpgDQGcFmaJcg = Adam (популярные)
ELEVENLABS_VOICE_ID = "pNInz6obpgDQGcFmaJcg" # Adam

def init_tts():
    global tts_model
    if tts_model is not None:
        return
    # Пробуем загрузить V4, если нет — фоллбэк на V3
    model_path = os.path.join(os.path.dirname(__file__), 'v4_ua.pt')
    if not os.path.exists(model_path):
        model_path = os.path.join(os.path.dirname(__file__), 'v3_ua.pt')
        
    if not os.path.exists(model_path):
        logger.warning(f"Файл модели Silero не найден. Локальный TTS будет отключен.")
        return

    try:
        import torch
        device = torch.device('cpu')
        logger.info(f"Загрузка модели Silero TTS из {model_path}...")
        tts_model = torch.package.PackageImporter(model_path).load_pickle("tts_models", "model")
        tts_model.to(device)
        logger.info("Модель Silero TTS успешно загружена.")
    except Exception as e:
        logger.error(f"Ошибка при загрузке Silero TTS: {e}")

def normalize_text(text: str) -> str:
    import re
    try:
        from num2words import num2words
        # Находим все числа в тексте
        def replace_num(match):
            num_str = match.group(0)
            try:
                return num2words(int(num_str), lang='uk')
            except Exception:
                return num_str
        return re.sub(r'\d+', replace_num, text)
    except ImportError:
        return text

# Таймаут HTTP-запросов к облачным TTS. Без него зависший/тормозящий API держит
# воркер бесконечно, а клиент ждёт звук с открытым запросом.
TTS_HTTP_TIMEOUT_SEC = 20.0

# MIME отдачи: облака (ElevenLabs, OpenAI tts-1) отдают MP3, локальный Silero — WAV.
MIME_MP3 = "audio/mpeg"
MIME_WAV = "audio/wav"


def generate_tts(text: str, speaker: str = 'mykyta'):
    """Синтез речи. Возвращает кортеж `(base64_аудио, mime)`.

    Приоритет провайдеров: ElevenLabs → OpenAI → локальный Silero (V4/V3).
    `mime` нужен клиенту: облака отдают mp3, Silero — wav, и фронтенд не должен
    угадывать формат (раньше всегда подставлялся `audio/wav`).

    Ключи читаем ВНУТРИ функции: main.py вызывает load_dotenv() уже после импорта
    модуля, поэтому значения, взятые на уровне модуля, «замораживались» и правка
    .env требовала пересборки контейнера (см. коммит 2c804a9).
    """
    OPENAI_TTS_KEY = os.getenv("OPENAI_TTS_KEY")
    ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

    # 1. Попытка ElevenLabs (если есть ключ).
    # Берём httpx, а НЕ requests: requests нет в requirements.txt и в образе он
    # не установлен, поэтому раньше `import requests` падал с ImportError и ветка
    # ElevenLabs молча пропускалась даже с корректным ключом. httpx уже есть в
    # зависимостях проекта (и им же пользуется openai).
    if ELEVENLABS_API_KEY:
        try:
            import httpx
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
            headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
            data = {"text": text, "model_id": "eleven_multilingual_v2"}
            res = httpx.post(url, json=data, headers=headers, timeout=TTS_HTTP_TIMEOUT_SEC)
            if res.status_code == 200:
                logger.info(f"TTS: ElevenLabs (голос {ELEVENLABS_VOICE_ID}, mp3)")
                return base64.b64encode(res.content).decode('utf-8'), MIME_MP3
            logger.error(f"ElevenLabs error {res.status_code}: {res.text[:300]}")
        except Exception as e:
            logger.error(f"ElevenLabs error: {e}")

    # 2. Попытка OpenAI (если есть ключ)
    if OPENAI_TTS_KEY:
        try:
            from openai import OpenAI
            # Создаем отдельный клиент без base_url, чтобы запрос шел напрямую в OpenAI, а не в OpenRouter
            client = OpenAI(api_key=OPENAI_TTS_KEY, timeout=TTS_HTTP_TIMEOUT_SEC)

            # В OpenAI есть голоса: alloy, echo, fable, onyx, nova, shimmer
            # 'onyx' - отличный глубокий мужской голос
            voice = "onyx" if speaker == "mykyta" else "nova"

            response = client.audio.speech.create(
                model="tts-1",
                voice=voice,
                input=text
            )
            logger.info(f"TTS: OpenAI tts-1 (голос {voice}, mp3)")
            return base64.b64encode(response.content).decode('utf-8'), MIME_MP3
        except Exception as e:
            logger.error(f"OpenAI TTS error: {e}")

    # 3. Фолбэк на локальный Silero V4/V3
    global tts_model
    if tts_model is None:
        # Ни одного провайдера: клиент получит 503 и уйдёт на системный голос.
        logger.warning("TTS: недоступны ни облако (нет ключей), ни Silero (модель не загружена)")
        return None, MIME_WAV
    import soundfile as sf
    try:
        text = normalize_text(text)
        audio_tensor = tts_model.apply_tts(text=text, speaker=speaker, sample_rate=48000)
        with io.BytesIO() as wav_io:
            sf.write(wav_io, audio_tensor.numpy(), 48000, format='WAV', subtype='PCM_16')
            wav_io.seek(0)
            logger.info(f"TTS: Silero локальный (голос {speaker}, wav)")
            return base64.b64encode(wav_io.read()).decode('utf-8'), MIME_WAV
    except Exception as e:
        logger.error(f"Ошибка при генерации Silero TTS: {e}")
        return None, MIME_WAV


def generate_tts_base64(text: str, speaker: str = 'mykyta'):
    """Совместимый фасад для вызовов, которым MIME не важен (только base64)."""
    audio_base64, _mime = generate_tts(text, speaker)
    return audio_base64

