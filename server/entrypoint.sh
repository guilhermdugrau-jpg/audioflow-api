#!/bin/sh
set -e

# Inicia PO Token Provider (bgutil) em background
# Ele roda na porta 19999 e fornece tokens PO para yt-dlp contornar bot detection
echo "[entrypoint] Iniciando PO Token Provider na porta ${POT_PROVIDER_PORT:-19999}..."
python -m bgutil.ytdlp_pot.provider \
    --host "${POT_PROVIDER_HOST:-127.0.0.1}" \
    --port "${POT_PROVIDER_PORT:-19999}" \
    --po-token-ttl 3600 \
    --visitor-data-ttl 86400 &

POT_PID=$!

# Aguarda provider ficar pronto (max 15s)
for i in $(seq 1 15); do
    if curl -sf "http://${POT_PROVIDER_HOST:-127.0.0.1}:${POT_PROVIDER_PORT:-19999}/health" >/dev/null 2>&1; then
        echo "[entrypoint] PO Token Provider OK"
        break
    fi
    sleep 1
done

# Trap para matar o provider ao sair
trap 'kill $POT_PID 2>/dev/null; wait $POT_PID 2>/dev/null' EXIT INT TERM

# Inicia uvicorn
echo "[entrypoint] Iniciando uvicorn na porta ${PORT:-8000}..."
exec uvicorn server.api.app:app --host 0.0.0.0 --port "${PORT:-8000}"