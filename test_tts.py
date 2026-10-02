import torch
import soundfile as sf
import os

print("Загружаем локальную модель Silero TTS (v3_ua.pt)...")
device = torch.device('cpu')

# Загружаем скачанный файл напрямую!
local_model_path = 'v3_ua.pt'
model = torch.package.PackageImporter(local_model_path).load_pickle("tts_models", "model")
model.to(device)

text = "Наступна зупинка — Соборна площа. План: 36 хвилин, з пересадкою на тролейбус."
print(f"\nГенерируем аудио для текста:\n'{text}'")

# mykyta - дикторский голос, borys - другой мужской
audio_tensor = model.apply_tts(text=text,
                               speaker='mykyta',
                               sample_rate=48000)

output_file = 'test_stop.wav'
# Используем soundfile для сохранения
sf.write(output_file, audio_tensor.numpy(), 48000)

print(f"\nГотово! Файл сохранен по пути: {os.path.abspath(output_file)}")
