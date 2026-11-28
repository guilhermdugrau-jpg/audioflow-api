"""API HTTP do AudioFlow para o site (FastAPI).

Endpoints:
    POST /api/call          {method, args} -> {ok, result} | {ok, error}
    GET  /api/events?since=N -> {seq, events}
    GET  /api/search?q=...   -> busca pública (sem login) por nome
    GET  /api/files/{sid}/{job}/{path} -> arquivo
    GET  /api/zip/{sid}/{job}          -> todos os arquivos em .zip
    GET  /api/health        -> status
    GET  /                  -> frontend (audioflow/web) em dev

A sessão vem do header ``X-AudioFlow-Session`` (UUID gerado no navegador).
Não há cookie: assim o site no Cloudflare Pages pode chamar a API no Render
sem dor de cabeça com CORS/credenciais.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import re
import secrets
import tempfile
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .engine import (
    ALLOWED_METHODS,
    DATA_ROOT,
    JOB_RE,
    SID_RE,
    Manager,
    WebApi,
)
from audioflow.watermark import (
    DEFAULT_VOICE,
    EFFECTS as WM_EFFECTS,
    VOICE_PRESETS,
    render_voice_preview,
)
from audioflow import search_api

log = logging.getLogger("audioflow.api")

API_TOKEN = os.environ.get("AUDIOFLOW_API_TOKEN", "").strip()
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("AUDIOFLOW_ALLOWED_ORIGINS", "*").split(",")
    if o.strip()
]
WEB_DIR = Path(__file__).resolve().parents[2] / "audioflow" / "web"

manager = Manager()

# Limite leve para a busca pública não martelar o catálogo externo.
_SEARCH_HITS: dict[str, list[float]] = {}
_SEARCH_LOCK = threading.Lock()
SEARCH_PER_MIN = 40


def _allow_search(ip: str) -> bool:
    now = time.time()
    with _SEARCH_LOCK:
        if len(_SEARCH_HITS) > 2000:
            for key in [
                k for k, v in _SEARCH_HITS.items() if not v or now - v[-1] > 120
            ]:
                _SEARCH_HITS.pop(key, None)
        hits = [t for t in _SEARCH_HITS.get(ip, []) if now - t < 60]
        if len(hits) >= SEARCH_PER_MIN:
            _SEARCH_HITS[ip] = hits
            return False
        hits.append(now)
        _SEARCH_HITS[ip] = hits
        return True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    manager.start_cleanup()
    log.info("AudioFlow API pronta (data=%s)", DATA_ROOT)
    yield


app = FastAPI(title="AudioFlow API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/api/"):
        # Eventos e arquivos mudam a cada job: não deixe o navegador cachear.
        response.headers["Cache-Control"] = "no-store"
    return response


class CallRequest(BaseModel):
    method: str
    args: list = Field(default_factory=list)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


def _sid(request: Request, header: str | None) -> str:
    sid = (header or "").strip()
    if SID_RE.match(sid):
        return sid
    # fallback para clientes que não mandam o header (ex.: curl)
    seed = f"{_client_ip(request)}|{request.headers.get('user-agent', '')}"
    return "ip-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _check_auth(request: Request, token: str | None = None) -> None:
    if not API_TOKEN:
        return
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer ") and secrets.compare_digest(auth[7:], API_TOKEN):
        return
    if token and secrets.compare_digest(token, API_TOKEN):
        return
    raise HTTPException(status_code=401, detail="unauthorized")


def _session(request: Request, header: str | None):
    _check_auth(request)
    try:
        return manager.session(_sid(request, header))
    except ValueError as exc:
        detail = str(exc)
        status = 429 if "ocupado" in detail else 400
        raise HTTPException(status_code=status, detail=detail) from exc


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "app": "audioflow", "stats": manager.stats()}


@app.get("/api/search")
def search_catalog(
    request: Request,
    q: str = "",
    types: str = "track,album,playlist",
    limit: int = 12,
) -> dict:
    """Busca por nome no catálogo público — sem login, sem token de usuário."""
    query = re.sub(r"\s+", " ", (q or "").strip())[:120]
    if len(query) < 2:
        return {"query": query, "results": []}
    if not _allow_search(_client_ip(request)):
        raise HTTPException(
            status_code=429,
            detail="Muitas buscas em pouco tempo. Aguarde um instante.",
        )
    kinds = tuple(
        t.strip()
        for t in (types or "").split(",")
        if t.strip() in ("track", "album", "playlist")
    ) or ("track", "album", "playlist")
    try:
        results = search_api.search(query, kinds=kinds, limit=limit)
    except search_api.SearchError as exc:
        log.warning("busca falhou: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Busca indisponível agora. Tente novamente em instantes.",
        ) from exc
    return {"query": query, "results": results}


@app.get("/api/events")
def events(
    request: Request,
    since: int = 0,
    x_audioflow_session: str | None = Header(default=None),
) -> dict:
    session = _session(request, x_audioflow_session)
    return session.events_since(since)


@app.post("/api/call")
def call(
    payload: CallRequest,
    request: Request,
    x_audioflow_session: str | None = Header(default=None),
):
    session = _session(request, x_audioflow_session)
    method = payload.method
    if (
        not isinstance(method, str)
        or method.startswith("_")
        or method not in ALLOWED_METHODS
    ):
        return JSONResponse(
            {"ok": False, "error": f"método {method!r} indisponível"},
            status_code=404,
        )
    api = WebApi(manager, session, _client_ip(request))
    try:
        result = getattr(api, method)(*payload.args)
    except Exception as exc:  # noqa: BLE001
        log.warning("erro em %s: %s", method, exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True, "result": result}


def _safe_job(sid: str, job: str) -> Path:
    if not SID_RE.match(sid or "") or not JOB_RE.match(job or ""):
        raise HTTPException(status_code=404, detail="not found")
    folder = (DATA_ROOT / sid / job).resolve()
    try:
        folder.relative_to(DATA_ROOT.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="not found") from exc
    if not folder.is_dir():
        raise HTTPException(status_code=404, detail="not found")
    return folder


@app.get("/api/files/{sid}/{job}/{file_path:path}")
def download_file(
    sid: str,
    job: str,
    file_path: str,
    request: Request,
    token: str | None = None,
):
    _check_auth(request, token)
    folder = _safe_job(sid, job)
    target = (folder / file_path).resolve()
    if folder not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media, filename=target.name)


@app.get("/api/watermark/preview")
def watermark_preview(
    text: str = "Lucas CDs",
    voice: str = DEFAULT_VOICE,
    effects: str = "",
    volume: float = 0.45,
):
    """Gera (e cacheia) um MP3 curto só com a voz da assinatura."""
    clean = re.sub(r"\s+", " ", text or "").strip()[:80] or "Lucas CDs"
    voice_key = voice if voice in VOICE_PRESETS else DEFAULT_VOICE
    eff = [e.strip() for e in (effects or "").split(",") if e.strip() in WM_EFFECTS][:4]
    vol = max(0.05, min(1.0, float(volume or 0.45)))
    key = hashlib.sha1(
        f"{clean}|{voice_key}|{','.join(eff)}|{vol:.2f}".encode("utf-8", "replace")
    ).hexdigest()[:16]
    preview_dir = Path(tempfile.gettempdir()) / "audioflow-previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    out = preview_dir / f"{key}.mp3"
    if not out.is_file() or out.stat().st_size < 1000:
        tmp = render_voice_preview(clean, voice_key, eff, vol)
        if not tmp:
            raise HTTPException(status_code=503, detail="TTS indisponível no momento")
        os.replace(tmp, out)
    return FileResponse(out, media_type="audio/mpeg", filename="audioflow-assinatura.mp3")


@app.get("/api/zip/{sid}/{job}")
def download_zip(sid: str, job: str, request: Request, token: str | None = None):
    _check_auth(request, token)
    folder = _safe_job(sid, job)
    # pega o nome do job da sessão
    session = manager.session(sid)
    job_name = getattr(session, "job_name", None)
    safe_name = re.sub(r"[^A-Za-z0-9 _\-]", "", job_name or "audioflow").strip() or "audioflow"
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    try:
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(folder.rglob("*")):
                if path.is_file() and not path.name.startswith("."):
                    zf.write(path, arcname=path.relative_to(folder).as_posix())
        return FileResponse(
            tmp.name,
            media_type="application/zip",
            filename=f"{safe_name}.zip",
            background=BackgroundTask(os.unlink, tmp.name),
        )
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise


# Frontend local (no Render o site fica no Cloudflare Pages).
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
