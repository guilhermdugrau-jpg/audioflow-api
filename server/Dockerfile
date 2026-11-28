FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # PO Token Provider (bgutil) — roda em background na porta 19999
    POT_PROVIDER_HOST=127.0.0.1 \
    POT_PROVIDER_PORT=19999

# ffmpeg para conversão de áudio (MP3/FLAC/WAV), espeak-ng para TTS, curl para healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg espeak-ng curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY server/api/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY audioflow /app/audioflow
COPY server /app/server

# Script de entrypoint que sobe o PO token provider + uvicorn
COPY server/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8000

CMD ["/app/entrypoint.sh"]
