# Build stage for bgutil POT provider (Node.js)
FROM node:22-slim AS pot-provider
WORKDIR /pot
RUN git clone --single-branch --branch 2.0.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git . \
    && npm ci \
    && npx tsc

# Final stage
ARG CACHE_BUST=2026112703
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # PO Token Provider (bgutil) — roda em background na porta 4416
    POT_PROVIDER_HOST=127.0.0.1 \
    POT_PROVIDER_PORT=4416

# ffmpeg para conversão de áudio (MP3/FLAC/WAV), espeak-ng para TTS, curl para healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg espeak-ng curl \
    && rm -rf /var/lib/apt/lists/*

# Build info
RUN echo "=== BUILD: $(date) ===" && echo "CACHE_BUST=$CACHE_BUST"

# Copy built POT provider from build stage
COPY --from=pot-provider /pot/build /app/pot-provider
COPY --from=pot-provider /pot/node_modules /app/pot-provider/node_modules

WORKDIR /app

COPY server/api/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Script de entrypoint que sobe o PO token provider + uvicorn
COPY server/entrypoint2.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

COPY audioflow /app/audioflow
COPY server /app/server

EXPOSE 8000

CMD ["/app/entrypoint.sh"]