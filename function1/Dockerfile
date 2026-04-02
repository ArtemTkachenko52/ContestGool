FROM python:3.9-slim

# 1. Системные либы для GUI, ddddocr и Tesseract
RUN apt-get update && apt-get install -y \
    xvfb x11vnc xauth dbus-x11 \
    libnss3 libasound2 libxss1 libxtst6 libgtk-3-0 libgbm1 \
    libgl1 libglib2.0-0 \
    tesseract-ocr tesseract-ocr-rus \
    wget gnupg \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 2. Python зависимости
COPY function1/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip
# Фикс для корректной работы patchright
RUN pip install "pyee>=13,<14"
RUN pip install --no-cache-dir -r requirements.txt

# 3. Установка брендового Google Chrome именно для PATCHRIGHT
# install-deps ставит системные шрифты и либы, чтобы не было белого экрана
RUN python -m patchright install-deps chrome
RUN python -m patchright install chrome

COPY . .

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV DISPLAY=:99

# Запуск через виртуальный экран Xvfb
CMD ["sh", "-c", "Xvfb :99 -screen 0 1280x720x24 -ac & DISPLAY=:99 python -u function1/start_work.py"]
