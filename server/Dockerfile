FROM python:3.12-slim

ARG BUILD_DATE=2026112705
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    BUILD_DATE=$BUILD_DATE \
    # PO Token Provider (bgutil) — roda em background na porta 4416
    POT_PROVIDER_HOST=127.0.0.1 \
    POT_PROVIDER_PORT=4416

# Instala Node.js 22, ffmpeg, espeak-ng, curl, git
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gnupg ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get install -y --no-install-recommends ffmpeg espeak-ng git \
    && rm -rf /var/lib/apt/lists/*

# Build bgutil POT provider
WORKDIR /pot
RUN git clone --single-branch --branch 2.0.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git . \
    && npm ci \
    && npx tsc

WORKDIR /app

COPY server/api/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copia provider compilado
RUN cp -r /pot/build /app/pot-provider && cp -r /pot/node_modules /app/pot-provider/node_modules

# Script de entrypoint
COPY server/entrypoint2.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

COPY audioflow /app/audioflow
COPY server /app/server

EXPOSE 8000

CMD ["/app/entrypoint.sh"]