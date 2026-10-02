import os
import io
import base64
import logging

logger = logging.getLogger(__name__)

tts_model = None

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
    global tts_model
    if tts_model is None:
        return None
    import soundfile as sf
    try:
        # Переводим цифры в слова, так как Silero V3/V4 может их пропускать
        text = normalize_text(text)
        
        # Для v4_ua может не быть спикера 'mykyta', там часто используется 'random' или 'v4_ua' (по умолчанию 'mykyta' есть в V3).
        # Silero V4 UA поддерживает mykyta.
        audio_tensor = tts_model.apply_tts(text=text, speaker=speaker, sample_rate=48000)
        with io.BytesIO() as wav_io:
            sf.write(wav_io, audio_tensor.numpy(), 48000, format='WAV', subtype='PCM_16')
            wav_io.seek(0)
            return base64.b64encode(wav_io.read()).decode('utf-8')
    except Exception as e:
        logger.error(f"Ошибка при генерации TTS: {e}")
        return None
