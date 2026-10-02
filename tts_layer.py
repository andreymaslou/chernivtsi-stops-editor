import os
import io
import base64
import logging

logger = logging.getLogger(__name__)

tts_model = None

# Ключи для облачных API (читаются из .env)
OPENAI_TTS_KEY = os.getenv("OPENAI_TTS_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

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

def generate_tts_base64(text: str, speaker: str = 'mykyta') -> str:
    # 1. Попытка ElevenLabs (если есть ключ)
    if ELEVENLABS_API_KEY:
        import requests
        try:
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
            headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
            data = {"text": text, "model_id": "eleven_multilingual_v2"}
            res = requests.post(url, json=data, headers=headers)
            if res.status_code == 200:
                return base64.b64encode(res.content).decode('utf-8')
            else:
                logger.error(f"ElevenLabs error: {res.text}")
        except Exception as e:
            logger.error(f"ElevenLabs error: {e}")

    # 2. Попытка OpenAI (если есть ключ)
    if OPENAI_TTS_KEY:
        try:
            from openai import OpenAI
            # Создаем отдельный клиент без base_url, чтобы запрос шел напрямую в OpenAI, а не в OpenRouter
            client = OpenAI(api_key=OPENAI_TTS_KEY)
            
            # В OpenAI есть голоса: alloy, echo, fable, onyx, nova, shimmer
            # 'onyx' - отличный глубокий мужской голос
            voice = "onyx" if speaker == "mykyta" else "nova"
            
            response = client.audio.speech.create(
                model="tts-1",
                voice=voice,
                input=text
            )
            return base64.b64encode(response.content).decode('utf-8')
        except Exception as e:
            logger.error(f"OpenAI TTS error: {e}")

    # 3. Фолбэк на локальный Silero V4/V3
    global tts_model
    if tts_model is None:
        return None
    import soundfile as sf
    try:
        text = normalize_text(text)
        audio_tensor = tts_model.apply_tts(text=text, speaker=speaker, sample_rate=48000)
        with io.BytesIO() as wav_io:
            sf.write(wav_io, audio_tensor.numpy(), 48000, format='WAV', subtype='PCM_16')
            wav_io.seek(0)
            return base64.b64encode(wav_io.read()).decode('utf-8')
    except Exception as e:
        logger.error(f"Ошибка при генерации Silero TTS: {e}")
        return None

