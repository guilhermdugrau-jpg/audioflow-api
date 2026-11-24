FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg para conversão de áudio (MP3/FLAC/WAV). Imagem slim não tem.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY server/api/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY audioflow /app/audioflow
COPY server /app/server

EXPOSE 8000

# Render injeta $PORT; localmente cai em 8000.
CMD ["sh", "-c", "uvicorn server.api.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
