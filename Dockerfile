FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .

# Системные пакеты больше не нужны: локального синтеза (Silero + PyTorch +
# soundfile) в проекте нет — озвучку отдают облачные TTS по HTTP (httpx).
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
