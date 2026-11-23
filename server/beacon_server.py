"""Servidor de telemetria (beacon) do AudioFlow.

Toda vez que o app é executado, ele envia um ``POST /beacon`` com infos do PC.
Este servidor guarda (JSONL) e mostra um dashboard com quem abriu o app e quando.

Uso local:
    python server/beacon_server.py [--port 8000] [--data beacons.jsonl] [--token SEGREDO]

Deploy Railway:
    1. Crie um projeto no Railway a partir deste repositório (detecta Python).
    2. Monte um Volume em ``/data`` (senão os beacons se perdem a cada redeploy).
    3. Configure ``BEACON_TOKEN`` nas variáveis de ambiente (opcional).
    4. A porta usada é a ``PORT`` que o Railway injeta automaticamente.

Endpoints:
    GET  /            -> dashboard HTML (auto-refresh)
    GET  /api/beacons -> lista em JSON
    GET  /healthz     -> health check (para o Railway)
    POST /beacon      -> recebe o beacon do app (Bearer opcional)

* Sem token, qualquer um pode registrar beacons falsos. Com token, o app deve
  enviar ``Authorization: Bearer <token>`` (mesma string em BEACON_TOKEN no app).
* O IP exibido vem de ``X-Forwarded-For`` quando atrás de proxy (Railway).
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import html as html_mod
except ImportError:  # pragma: no cover
    html_mod = None


class BeaconStore:
    """Persistência simples em JSONL (uma linha por execução)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")

    def append(self, record: dict) -> None:
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def all(self) -> list[dict]:
        with self._lock:
            if not self.path.exists():
                return []
            lines = self.path.read_text(encoding="utf-8").splitlines()
        records = []
        for line in lines:
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        return records


DASHBOARD = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AudioFlow · Execuções</title>
<style>
  :root { color-scheme: dark; }
  body {
    font-family: "Segoe UI", system-ui, sans-serif; background: #0b0f17;
    color: #eef2f8; margin: 0; padding: 24px;
  }
  h1 { font-size: 20px; letter-spacing: -0.02em; margin: 0 0 4px; }
  .sub { color: #97a3b8; font-size: 13px; margin-bottom: 20px; }
  .stat { display: flex; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }
  .stat div {
    background: #161d2c; border: 1px solid rgba(255,255,255,.08);
    border-radius: 14px; padding: 12px 18px; min-width: 120px;
  }
  .stat b { font-size: 22px; color: #1db954; display: block; }
  .stat span { font-size: 12px; color: #97a3b8; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid rgba(255,255,255,.06); }
  th { color: #6b7689; font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: .05em; }
  td { color: #c6d0e0; }
  .empty { color: #6b7689; padding: 30px 0; text-align: center; }
</style>
</head>
<body>
<h1>AudioFlow · Execuções do app</h1>
<p class="sub">Quem abriu o AudioFlow e quando (atualiza a cada 5s)</p>
<div class="stat">
  <div><b id="total">0</b><span>execuções</span></div>
  <div><b id="hoje">0</b><span>hoje</span></div>
  <div><b id="unicos">0</b><span>hosts únicos</span></div>
</div>
<table>
<thead><tr>
<th>Quando</th><th>IP</th><th>Host</th><th>Usuário</th><th>Sistema</th><th>App</th>
</tr></thead>
<tbody id="rows"></tbody>
</table>
<p class="empty" id="empty">Nenhuma execução registrada ainda.</p>
<script>
async function refresh() {
  const r = await fetch('/api/beacons');
  const list = await r.json();
  const tbody = document.getElementById('rows');
  const empty = document.getElementById('empty');
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  if (!list.length) { empty.style.display = ''; tbody.innerHTML = ''; return; }
  empty.style.display = 'none';
  const today = new Date().toISOString().slice(0, 10);
  const hosts = new Set();
  tbody.innerHTML = list.slice().reverse().map(b => {
    hosts.add(b.hostname || b.ip);
    const d = b.ts ? new Date(b.ts).toLocaleString('pt-BR') : '—';
    const so = b.os ? `${b.os} ${b.os_release || ''}` : '—';
    return `<tr><td>${esc(d)}</td><td>${esc(b.ip)}</td><td>${esc(b.hostname)}</td>
      <td>${esc(b.username)}</td><td>${esc(so)}</td><td>${esc(b.app_version)}</td></tr>`;
  }).join('');
  document.getElementById('total').textContent = list.length;
  document.getElementById('hoje').textContent =
    list.filter(b => (b.ts || '').slice(0, 10) === today).length;
  document.getElementById('unicos').textContent = hosts.size;
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "AudioFlowBeacon/1.0"

    def log_message(self, *_args) -> None:
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1 << 20:  # 1 MB de teto
            return b""
        return self.rfile.read(length)

    def _authorized(self) -> bool:
        token = getattr(self.server, "token", None)
        if not token:
            return True
        expected = f"Bearer {token}"
        return self.headers.get("Authorization") == expected

    def _client_ip(self) -> str:
        fwd = self.headers.get("X-Forwarded-For")
        if fwd:
            return fwd.split(",")[0].strip()
        return self.client_address[0]

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/healthz":
            self._send(200, b'{"status":"ok"}')
            return
        if path == "/api/beacons":
            store: BeaconStore = self.server.store  # type: ignore[attr-defined]
            body = json.dumps(store.all(), ensure_ascii=False).encode("utf-8")
            self._send(200, body)
            return
        self._send(200, DASHBOARD.encode("utf-8"), ctype="text/html; charset=utf-8")

    def do_POST(self) -> None:
        if self.path != "/beacon":
            self._send(404, b'{"error":"not found"}')
            return
        if not self._authorized():
            self._send(401, b'{"error":"unauthorized"}')
            return
        raw = self._read_body()
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            self._send(400, b'{"error":"invalid json"}')
            return
        if not isinstance(payload, dict):
            self._send(400, b'{"error":"invalid payload"}')
            return

        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ip": self._client_ip(),
            **payload,
        }
        store: BeaconStore = self.server.store  # type: ignore[attr-defined]
        store.append(record)
        self._send(204, b"")

    do_PUT = do_POST  # tolerância para clientes que usam PUT


def _default_data() -> str:
    env = os.environ.get("BEACON_DATA")
    if env:
        return env
    if Path("/data").is_dir():
        return "/data/beacons.jsonl"
    return "beacons.jsonl"


def main() -> None:
    parser = argparse.ArgumentParser(description="Servidor de telemetria do AudioFlow")
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("PORT") or "8000")
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--data", default=_default_data(), help="arquivo de armazenamento"
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("BEACON_TOKEN") or "",
        help="token opcional (Bearer) para o /beacon",
    )
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    server.store = BeaconStore(Path(args.data))  # type: ignore[attr-defined]
    server.token = args.token  # type: ignore[attr-defined]

    records = server.store.all()  # type: ignore[attr-defined]
    print(f"AudioFlow Beacon rodando em http://{args.host}:{args.port}")
    print(f"Dados: {args.data} ({len(records)} execuções registradas)")
    if args.token:
        print("Autenticação Bearer ativada.")
    print("Dashboard: abra o navegador em http://localhost:%d" % args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrado.")


if __name__ == "__main__":
    main()
