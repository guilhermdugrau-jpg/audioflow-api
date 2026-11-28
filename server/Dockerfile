# Force fresh build - 2026-11-28-v2
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    BUILD_VERSION=2026112802

# Instala dependências do sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg espeak-ng curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copia requirements e instala
COPY server/api/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copia código
COPY audioflow /app/audioflow
COPY server /app/server

EXPOSE 8000

CMD ["uvicorn", "server.api.app:app", "--host", "0.0.0.0", "--port", "8000"]