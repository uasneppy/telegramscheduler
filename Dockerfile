FROM python:3.11-slim

WORKDIR /app

# Install dependencies first so code changes don't bust this layer
RUN pip install --no-cache-dir \
    "python-telegram-bot==20.8" \
    "apscheduler>=3.11.0" \
    "pytz>=2025.2" \
    "pillow>=11.3.0" \
    "tzdata>=2025.2"

COPY config.py main.py ./
COPY bot/ ./bot/

# Database and uploaded media live here - mount a volume to persist them
ENV DATA_DIR=/data
VOLUME ["/data"]

CMD ["python", "main.py"]
