# Use prebuilt bgutil POT provider image as base (has Node.js + provider built)
FROM brainicism/bgutil-ytdlp-pot-provider:2.0.2-node

# Install Python and dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip ffmpeg espeak-ng curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # PO Token Provider (bgutil) — já roda na porta 4416 no container base
    POT_PROVIDER_HOST=127.0.0.1 \
    POT_PROVIDER_PORT=4416

WORKDIR /app

COPY server/api/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

# O provider bgutil já está em /app na imagem base, mas em /app/build
# O entrypoint da imagem base já inicia o provider na porta 4416
# Precisamos apenas iniciar o uvicorn

COPY audioflow /app/audioflow
COPY server /app/server

EXPOSE 8000

# O entrypoint da imagem base inicia o provider, depois executamos uvicorn
CMD ["sh", "-c", "cd /app && exec uvicorn server.api.app:app --host 0.0.0.0 --port ${PORT:-8000}"]