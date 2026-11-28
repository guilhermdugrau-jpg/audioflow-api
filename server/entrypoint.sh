#!/bin/sh
set -e

# Inicia PO Token Provider (bgutil) em background
# Ele roda na porta 4416 e fornece tokens PO para yt-dlp contornar bot detection
echo "[entrypoint] Iniciando PO Token Provider na porta ${POT_PROVIDER_PORT:-4416}..."
cd /app/pot-provider
node build/main.js --port "${POT_PROVIDER_PORT:-4416}" --host "${POT_PROVIDER_HOST:-127.0.0.1}" &

POT_PID=$!

# Aguarda provider ficar pronto (max 30s)
for i in $(seq 1 30); do
    if curl -sf "http://${POT_PROVIDER_HOST:-127.0.0.1}:${POT_PROVIDER_PORT:-4416}/health" >/dev/null 2>&1; then
        echo "[entrypoint] PO Token Provider OK"
        break
    fi
    sleep 1
done

# Trap para matar o provider ao sair
trap 'kill $POT_PID 2>/dev/null; wait $POT_PID 2>/dev/null' EXIT INT TERM

# Inicia uvicorn
cd /app
echo "[entrypoint] Iniciando uvicorn na porta ${PORT:-8000}..."
exec uvicorn server.api.app:app --host 0.0.0.0 --port "${PORT:-8000}"