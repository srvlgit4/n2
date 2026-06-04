FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

ENV PYTHONUNBUFFERED=1
ENV TELEGRAM_BOT_TOKEN=8436841638:AAFz0JFN8fXxHqy5eQGFDLXeCUwn0JLcF4w

CMD ["python", "bot.py"]
