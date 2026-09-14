FROM python:3.11-slim

WORKDIR /app

# Сначала копируем только requirements.txt, чтобы слой с зависимостями
# кэшировался и не переустанавливался при изменении кода
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь остальной код проекта
COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
