FROM python:3.9-slim

# 1. Устанавливаем ВСЕ системные зависимости (включая критические для Chrome)
RUN apt-get update && apt-get install -y \
    xvfb \
    x11vnc \
    xauth \
    dbus-x11 \
    libnss3 \
    libasound2 \
    libxss1 \
    libxtst6 \
    libgtk-3-0 \
    libgbm1 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 2. Python зависимости
COPY function1/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# 3. Установка браузера
RUN playwright install chromium

# --- ИСПРАВЛЕННАЯ СТРОКА: Игнорируем ошибку шрифтов ---
RUN playwright install-deps chromium || true

COPY . .

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

CMD ["python", "-u", "function1/start_work.py"]
