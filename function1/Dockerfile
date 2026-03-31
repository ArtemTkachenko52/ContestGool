FROM python:3.9-slim

# 1. Системные либы (Chrome + ddddocr + Tesseract)
RUN apt-get update && apt-get install -y \
    xvfb x11vnc xauth dbus-x11 \
    libnss3 libasound2 libxss1 libxtst6 libgtk-3-0 libgbm1 \
    # Исправленные пакеты для графики и ddddocr:
    libgl1 libglib2.0-0 \
    tesseract-ocr tesseract-ocr-rus \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 2. Python зависимости
COPY function1/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip
# Фикс конфликта pyee для patchright
RUN pip install "pyee>=13,<14"
RUN pip install --no-cache-dir -r requirements.txt

# 3. Установка браузера через модуль Python (фикс ошибки 'not found')
RUN python -m patchright install chromium
RUN python -m patchright install-deps chromium || true

COPY . .

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Запуск через виртуальный экран для корректной работы браузера в фоне
CMD ["sh", "-c", "Xvfb :99 -screen 0 1280x720x24 & DISPLAY=:99 python -u function1/start_work.py"]
