FROM python:3.11-slim

# Instalar dependencias del sistema para Playwright/Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libdbus-1-3 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libwayland-client0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instalar Chromium para Playwright
RUN playwright install chromium

COPY monitor_csjn_dajudeco.py .

# Variables de entorno (configurar en Railway/Render/Fly.io)
# TELEGRAM_BOT_TOKEN=...
# TELEGRAM_CHAT_ID=...

CMD ["python", "monitor_csjn_dajudeco.py", "--bot"]
