FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .

# Ставим системные зависимости: wget (для скачивания модели) и libsndfile1 (для работы soundfile)
RUN apt-get update && apt-get install -y wget libsndfile1 && rm -rf /var/lib/apt/lists/*

# Ставим ЛЕГКУЮ CPU-версию PyTorch, иначе Docker скачает 2.5 ГБ CUDA-драйверов!
RUN pip install --no-cache-dir torch torchaudio --index-url https://download.pytorch.org/whl/cpu

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Скачиваем модель Silero прямо при сборке образа, чтобы она всегда была на сервере
RUN wget -qO /app/v3_ua.pt "https://models.silero.ai/models/tts/ua/v3_ua.pt"

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
