# Use prebuilt bgutil POT provider image as base
FROM brainicism/bgutil-ytdlp-pot-provider:latest AS pot-base

# Final stage
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # PO Token Provider (bgutil) — roda em background na porta 4416
    POT_PROVIDER_HOST=127.0.0.1 \
    POT_PROVIDER_PORT=4416

# Instala ffmpeg, espeak-ng, curl
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg espeak-ng curl \
    && rm -rf /var/lib/apt/lists/*

# Copia provider compilado do bgutil (já incluído na imagem base)
# A imagem brainicism/bgutil-ytdlp-pot-provider já tem o server compilado em /app
COPY --from=pot-base /app /app/pot-provider

WORKDIR /app

COPY server/api/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Script de entrypoint
COPY server/entrypoint2.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

COPY audioflow /app/audioflow
COPY server /app/server

EXPOSE 8000

CMD ["/app/entrypoint.sh"]