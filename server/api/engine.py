"""Camada web do AudioFlow: sessões, fila e eventos.

Reaproveita o ``DownloadEngine`` do app desktop, mas sem pywebview e sem a
pasta escolhida pelo usuário: cada sessão baixa em ``/tmp/audioflow/<sid>`` e
os arquivos expiram sozinhos.

Pensado para o plano free do Render (512 MB / 0.1 CPU):

* concorrência global 1 (um download por vez);
* fila limitada (padrão 10);
* cota por IP (padrão 5 downloads/hora);
* limite de faixas por link (padrão 50);
* sem ffmpeg: entrega o áudio nativo (M4A/WebM), sem transcodificar.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from audioflow.core import DownloadEngine
from audioflow.spotify_api import SpotifyClient, parse_spotify_url
from audioflow.utils import FORMATS, QUALITIES, SAMPLE_RATES
from audioflow.version import APP_VERSION

log = logging.getLogger("audioflow.web")

DATA_ROOT = Path(os.environ.get("AUDIOFLOW_DATA_DIR", "/tmp/audioflow"))
FILE_TTL_S = int(os.environ.get("AUDIOFLOW_FILE_TTL_MIN", "3")) * 60
MAX_TRACKS = int(os.environ.get("AUDIOFLOW_MAX_TRACKS", "50"))
MAX_QUEUE = int(os.environ.get("AUDIOFLOW_MAX_QUEUE", "10"))
JOBS_PER_HOUR = int(os.environ.get("AUDIOFLOW_JOBS_PER_HOUR", "5"))
MAX_CONCURRENT = max(1, int(os.environ.get("AUDIOFLOW_MAX_CONCURRENT", "1")))
MAX_SESSIONS = max(10, int(os.environ.get("AUDIOFLOW_MAX_SESSIONS", "500")))

# Conversão no servidor: ligue para permitir MP3/FLAC/WAV (exige ffmpeg na imagem).
# Fica desligada por padrão para não gastar CPU/RAM em hospedagem gratuita.
CONVERT = os.environ.get("AUDIOFLOW_ENABLE_CONVERT", "0") == "1"
CONVERT_FORMATS = tuple(f for f in FORMATS if CONVERT or f in ("m4a", "opus"))
_FFMPEG = shutil.which("ffmpeg")


def _ffmpeg_available() -> bool:
    return CONVERT and _FFMPEG is not None


def _encode(src: Path, fmt: str, quality: str, sample_rate: str) -> Path:
    """Transcodifica ``src`` para ``fmt`` com ffmpeg. Devolve o novo caminho."""
    dest = src.with_suffix("." + fmt)
    bitrate = quality if fmt in ("mp3", "m4a", "opus") else None
    cmd = [_FFMPEG, "-y", "-loglevel", "error", "-i", str(src)]
    if fmt == "mp3":
        cmd += ["-c:a", "libmp3lame"]
    elif fmt == "m4a":
        cmd += ["-c:a", "aac"]
    elif fmt == "opus":
        cmd += ["-c:a", "libopus"]
    elif fmt == "flac":
        cmd += ["-c:a", "flac"]
    elif fmt == "wav":
        cmd += ["-c:a", "pcm_s16le"]
    if bitrate:
        cmd += ["-b:a", bitrate + "k"]
    if sample_rate != "auto" and fmt in ("mp3", "flac", "wav"):
        cmd += ["-ar", sample_rate]
    cmd.append(str(dest))
    subprocess.run(cmd, check=True, capture_output=True, timeout=900)
    return dest

SID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
JOB_RE = re.compile(r"^[a-f0-9]{8}$")


def _percent(s: "Session") -> int:
    total = s.total or max(s.started, 1)
    return min(100, round(s.done * 100 / total)) if total else 0


def _progress(s: "Session") -> None:
    total = s.total or max(s.started, 1)
    s.emit({
        "type": "progress",
        "percent": _percent(s),
        "label": f"{s.done} de {total} faixas",
    })


@dataclass
class Session:
    sid: str
    created: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    seq: int = 0
    events: list[tuple[int, dict]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    start_lock: threading.Lock = field(default_factory=threading.Lock)
    engine: DownloadEngine | None = None
    thread: threading.Thread | None = None
    job_id: str | None = None
    out_dir: Path | None = None
    files: list[dict] = field(default_factory=list)
    total: int | None = None
    started: int = 0
    done: int = 0
    meta_cache: dict[str, object] = field(default_factory=dict)
    cancelled: bool = False
    finished_at: float | None = None
    job_name: str | None = None

    @property
    def busy(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def emit(self, payload: dict) -> None:
        with self.lock:
            self.seq += 1
            self.events.append((self.seq, payload))
            # mantém a memória sob controle em sessões longas
            if len(self.events) > 4000:
                del self.events[:2000]

    def events_since(self, since: int) -> dict:
        since = max(0, int(since or 0))
        with self.lock:
            return {
                "seq": self.seq,
                "events": [p for seq, p in self.events if seq > since],
            }

    def reset_job(self, job_id: str, out_dir: Path) -> None:
        self.job_id = job_id
        self.out_dir = out_dir
        self.files = []
        self.total = None
        self.started = 0
        self.done = 0
        self.cancelled = False
        # Marca "recém-criado" até a thread começar; evita que a limpeza
        # apague a sessão na janela entre reset_job e thread.start().
        self.finished_at = time.time()


class Manager:
    """Dono das sessões, da fila global e das cotas."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._sem = threading.Semaphore(MAX_CONCURRENT)
        self._jobs_lock = threading.Lock()
        self._jobs_active = 0
        self._rate: dict[str, list[float]] = {}
        self._rate_lock = threading.Lock()
        self._cleanup_started = False

    # --- sessões -----------------------------------------------------------

    def session(self, sid: str) -> Session:
        if not SID_RE.match(sid or ""):
            raise ValueError("sessão inválida")
        with self._lock:
            session = self._sessions.get(sid)
            if session is None:
                if len(self._sessions) >= MAX_SESSIONS:
                    # descarta a sessão ociosa mais antiga para não crescer sem limite
                    idle = [
                        (s.last_seen, other)
                        for other, s in self._sessions.items()
                        if not s.busy
                    ]
                    if not idle:
                        raise ValueError("servidor ocupado, tente novamente em instantes")
                    _, old_sid = min(idle)
                    self._sessions.pop(old_sid, None)
                    shutil.rmtree(DATA_ROOT / old_sid, ignore_errors=True)
                session = Session(sid=sid)
                self._sessions[sid] = session
            session.last_seen = time.time()
            return session

    def stats(self) -> dict:
        with self._lock:
            sessions = len(self._sessions)
        with self._jobs_lock:
            active = self._jobs_active
        return {"sessions": sessions, "jobs_active": active, "queue_limit": MAX_QUEUE}

    # --- cotas -------------------------------------------------------------

    def _allow(self, ip: str) -> bool:
        # AUDIOFLOW_JOBS_PER_HOUR=0 desliga a cota (sem limite por hora).
        if JOBS_PER_HOUR <= 0:
            return True
        now = time.time()
        with self._rate_lock:
            hits = [t for t in self._rate.get(ip, []) if now - t < 3600]
            if len(hits) >= JOBS_PER_HOUR:
                self._rate[ip] = hits
                return False
            hits.append(now)
            self._rate[ip] = hits
            return True

    # --- download ----------------------------------------------------------

    def start(self, session: Session, url: str, settings: dict, ip: str) -> str:
        # valida o link antes de enfileirar (erro rápido para o usuário)
        parse_spotify_url(url)

        # define formato/qualidade do job (site escolhe; default = m4a/192/auto)
        fmt = settings.get("format", "m4a")
        if fmt not in CONVERT_FORMATS:
            fmt = "m4a"
        qual = str(settings.get("quality", "192"))
        if qual not in QUALITIES:
            qual = "192"
        sr = settings.get("sample_rate", "auto")
        if sr not in SAMPLE_RATES:
            sr = "auto"

        with session.start_lock:
            if session.busy:
                raise RuntimeError("Já existe um download em andamento nesta sessão.")
            if not self._allow(ip):
                msg = (
                    "Limite de downloads por hora atingido. Tente novamente mais tarde."
                    if JOBS_PER_HOUR > 0
                    else f"Limite de {JOBS_PER_HOUR} downloads por hora. Tente novamente mais tarde."
                )
                raise RuntimeError(msg)

            with self._jobs_lock:
                if self._jobs_active >= MAX_QUEUE:
                    raise RuntimeError("Fila cheia no momento. Tente novamente em alguns minutos.")
                self._jobs_active += 1

            try:
                job_id = secrets.token_hex(4)
                out_dir = DATA_ROOT / session.sid / job_id
                out_dir.mkdir(parents=True, exist_ok=True)
                session.reset_job(job_id, out_dir)

                # guarda no session para a thread de conversão
                session.job_format = fmt
                session.job_quality = qual
                session.job_sample_rate = sr

                engine = DownloadEngine(
                    audio_format=fmt,
                    audio_quality=qual,
                    sample_rate=sr,
                    include_track_number=bool(settings.get("tracknum")),
                    max_workers=1,
                    watermark={},
                )
                session.engine = engine
                self._wire(session, engine)

                # define nome do job baseado no link (álbum/playlist/faixa)
                try:
                    kind, item_id = parse_spotify_url(url)
                    client = SpotifyClient()
                    if kind == "track":
                        track = client.get_track(item_id)
                        session.job_name = f"{track.artists} - {track.title}"
                    else:
                        meta = client.get_collection_metadata(item_id, kind)
                        session.job_name = meta.name or "Coleção"
                except Exception:
                    session.job_name = "AudioFlow"

                session.emit({
                    "type": "queued",
                    "message": "Na fila… assim que um download terminar, o seu começa.",
                })
                thread = threading.Thread(
                    target=self._run, args=(session, url, out_dir), daemon=True
                )
                session.thread = thread
                thread.start()
            except Exception:
                with self._jobs_lock:
                    self._jobs_active = max(0, self._jobs_active - 1)
                raise
        return job_id

    def _wire(self, s: Session, engine: DownloadEngine) -> None:
        engine.cb.on_metadata = lambda msg: s.emit(
            {"type": "progress", "percent": _percent(s), "label": msg}
        )
        engine.cb.on_warning = lambda msg: s.emit(
            {"type": "progress", "percent": _percent(s), "label": msg}
        )

        def on_start(track, _path) -> None:
            s.started += 1
            s.emit({
                "type": "track_start",
                "id": track.id,
                "name": track.title,
                "cover": track.cover_url,
            })

        def on_done(track, path) -> None:
            s.done += 1
            entry = self._file_entry(s, path)
            if entry and _ffmpeg_available() and s.job_format != "m4a":
                s.emit({
                    "type": "progress",
                    "percent": _percent(s),
                    "label": f"convertendo para {s.job_format.upper()}…",
                })
                entry = self._convert_entry(entry, s)
            self._add_file(s, entry)
            s.emit({
                "type": "track_ok",
                "id": track.id,
                "name": track.title,
                "cover": track.cover_url,
            })
            # alias usado pelo frontend atual
            s.emit({
                "type": "track_done",
                "id": track.id,
                "name": track.title,
                "cover": track.cover_url,
            })
            _progress(s)

        def on_skip(track, path) -> None:
            s.done += 1
            entry = self._file_entry(s, path)
            if entry and _ffmpeg_available() and s.job_format != "m4a":
                entry = self._convert_entry(entry, s)
            self._add_file(s, entry)
            s.emit({
                "type": "track_skip",
                "id": track.id,
                "name": track.title,
                "cover": track.cover_url,
            })
            _progress(s)

        def on_fail(track, _detail) -> None:
            s.done += 1
            s.emit({
                "type": "track_fail",
                "id": track.id,
                "name": track.title,
                "cover": track.cover_url,
            })
            _progress(s)

        engine.cb.on_track_start = on_start
        engine.cb.on_track_done = on_done
        engine.cb.on_track_skip = on_skip
        engine.cb.on_track_fail = on_fail
        engine.cb.on_finish = lambda ok, skipped, failed: self._finish(
            s, ok, skipped, failed
        )

    def _run(self, s: Session, url: str, out_dir: Path) -> None:
        acquired = False
        try:
            if s.cancelled:
                s.emit({
                    "type": "finish",
                    "summary": "Download cancelado.",
                    "cancelled": True,
                    "files": list(s.files),
                })
                return

            kind, item_id = parse_spotify_url(url)
            if kind == "track":
                s.total = 1
            else:
                meta = s.meta_cache.get(url)
                if meta is None:
                    meta = SpotifyClient().get_collection_metadata(item_id, kind)
                    s.meta_cache[url] = meta
                s.total = meta.track_count or None
                s.emit({
                    "type": "meta",
                    "kind": kind,
                    "name": meta.name or "Coleção",
                    "total": meta.track_count or 0,
                    "cover": meta.cover_url,
                })
                if s.cancelled:
                    s.emit({
                        "type": "finish",
                        "summary": "Download cancelado.",
                        "cancelled": True,
                        "files": list(s.files),
                    })
                    return
                if s.total and s.total > MAX_TRACKS:
                    s.emit({
                        "type": "error",
                        "message": (
                            f"Este link tem {s.total} faixas. O limite do site "
                            f"é {MAX_TRACKS} por download."
                        ),
                    })
                    return

            if s.cancelled:
                s.emit({
                    "type": "finish",
                    "summary": "Download cancelado.",
                    "cancelled": True,
                    "files": list(s.files),
                })
                return

            self._sem.acquire()
            acquired = True
            if s.cancelled:
                s.emit({
                    "type": "finish",
                    "summary": "Download cancelado.",
                    "cancelled": True,
                    "files": list(s.files),
                })
                return
            s.emit({"type": "progress", "percent": 0, "label": "Processando…"})
            s.engine.scrape(url, str(out_dir))
        except Exception as exc:  # noqa: BLE001
            log.exception("falha no job %s", s.job_id)
            s.emit({"type": "error", "message": str(exc)})
        finally:
            if acquired:
                self._sem.release()
            with self._jobs_lock:
                self._jobs_active = max(0, self._jobs_active - 1)
            s.last_seen = time.time()
            s.finished_at = time.time()
            if s.thread is threading.current_thread():
                s.thread = None

    def _file_entry(self, s: Session, path: str) -> dict | None:
        if not s.out_dir or not s.job_id:
            return None
        try:
            p = Path(path)
            if not p.is_file():
                return None
            rel = p.relative_to(s.out_dir).as_posix()
            return {
                "name": p.name,
                "url": f"/api/files/{s.sid}/{s.job_id}/{quote(rel)}",
                "size": p.stat().st_size,
                "path": str(p),
                "_rel": rel,
            }
        except (OSError, ValueError):
            return None

    def _convert_entry(self, entry: dict, s: Session) -> dict | None:
        """Transcodifica o arquivo do job para o formato escolhido no site."""
        original = dict(entry)
        try:
            src = Path(entry["path"])
            out = _encode(src, s.job_format, s.job_quality, s.job_sample_rate)
            if out != src:
                src.unlink(missing_ok=True)
            return self._file_entry(s, str(out))
        except Exception as exc:  # noqa: BLE001
            log.warning("conversão falhou: %s", exc)
            s.emit({
                "type": "progress",
                "percent": _percent(s),
                "label": "conversão falhou, mantendo original",
            })
            return self._file_entry(s, original["path"]) if original.get("path") else None

    @staticmethod
    def _add_file(s: Session, entry: dict | None) -> None:
        if not entry:
            return
        if any(f.get("url") == entry["url"] for f in s.files):
            return
        s.files.append(entry)
        s.emit({"type": "file_ready", **entry})

    def _finish(self, s: Session, ok: int, skipped: int, failed: int) -> None:
        if s.cancelled:
            summary = f"Cancelado: {ok} baixada(s) antes de parar."
        else:
            summary = f"Concluído: {ok} baixada(s)"
            if skipped:
                summary += f", {skipped} já existia(m)"
            if failed:
                summary += f", {failed} falhou(ram)"
            summary += "."
        payload: dict = {
            "type": "finish",
            "summary": summary,
            "cancelled": s.cancelled,
            "files": list(s.files),
        }
        if s.job_id:
            payload["job"] = s.job_id
        if s.files and s.job_id:
            payload["zip"] = f"/api/zip/{s.sid}/{s.job_id}"
        s.emit(payload)

    # --- limpeza -----------------------------------------------------------

    def start_cleanup(self) -> None:
        if self._cleanup_started:
            return
        self._cleanup_started = True

        def loop() -> None:
            while True:
                time.sleep(120)
                self.cleanup()

        threading.Thread(target=loop, name="audioflow-cleanup", daemon=True).start()

    def cleanup(self) -> None:
        now = time.time()
        expired_files: list[Session] = []
        stale_sessions: list[str] = []
        with self._lock:
            for sid, s in list(self._sessions.items()):
                if s.busy:
                    continue
                if s.finished_at is not None:
                    if s.out_dir and now - s.finished_at > FILE_TTL_S:
                        expired_files.append(s)
                elif now - s.created > FILE_TTL_S:
                    stale_sessions.append(sid)
            for sid in stale_sessions:
                self._sessions.pop(sid, None)

        for s in expired_files:
            if s.out_dir:
                shutil.rmtree(s.out_dir, ignore_errors=True)
                s.out_dir = None
            s.files = []
            s.finished_at = None
            s.emit({
                "type": "expired",
                "message": "Os arquivos expiraram e foram apagados.",
            })

        for sid in stale_sessions:
            shutil.rmtree(DATA_ROOT / sid, ignore_errors=True)

        # limpa o histórico de cotas por IP (memória)
        with self._rate_lock:
            for ip, hits in list(self._rate.items()):
                hits = [t for t in hits if now - t < 3600]
                if hits:
                    self._rate[ip] = hits
                else:
                    del self._rate[ip]

        if expired_files or stale_sessions:
            log.info(
                "limpeza: %d job(s) expirado(s), %d sessão(ões) removida(s)",
                len(expired_files),
                len(stale_sessions),
            )


# --- API exposta ao frontend (mesma interface do gui.py) -------------------


class WebApi:
    """Métodos chamados pelo frontend via ``POST /api/call``."""

    def __init__(self, manager: Manager, session: Session, ip: str) -> None:
        self.manager = manager
        self.session = session
        self.ip = ip

    def get_config(self) -> dict:
        return {
            "format": "m4a",
            "quality": 192,
            "sample_rate": "auto",
            "folder": "",
            "tracknum": False,
            "platform": "web",
            "convert_formats": list(CONVERT_FORMATS),
            "watermark": {
                "enabled": False,
                "text": "",
                "position": "middle",
                "volume": 0.3,
                "effects": [],
                "voice": "tts",
            },
        }

    def save_config(self, _settings: dict | None = None) -> None:
        # O site não persiste configuração no servidor.
        return None

    def fetch_meta(self, url: str) -> dict:
        kind, item_id = parse_spotify_url(url)
        client = SpotifyClient()
        if kind == "track":
            track = client.get_track(item_id)
            return {
                "type": "track",
                "name": f"{track.title} — {track.artists}",
                "total": 1,
                "cover": track.cover_url,
            }
        meta = client.get_collection_metadata(item_id, kind)
        self.session.meta_cache[url] = meta
        return {
            "type": kind,
            "name": meta.name or "Coleção",
            "total": meta.track_count or 0,
            "cover": meta.cover_url,
        }

    def download(self, url: str, settings: dict | None = None) -> str:
        self.manager.start(self.session, url, settings or {}, self.ip)
        return "ok"

    def cancel(self) -> None:
        self.session.cancelled = True
        if self.session.engine:
            self.session.engine.cancel()
        self.session.emit({"type": "progress", "percent": 0, "label": "Cancelando…"})

    def get_app_version(self) -> str:
        return APP_VERSION

    def pick_folder(self) -> None:
        return None

    def pick_voice_file(self) -> None:
        return None

    def check_update_async(self) -> None:
        return None

    def download_update(self) -> None:
        return None

    def install_update(self) -> None:
        return None

    def dismiss_update(self, *_args) -> None:
        return None


ALLOWED_METHODS = {
    "get_config",
    "save_config",
    "fetch_meta",
    "download",
    "cancel",
    "get_app_version",
    "pick_folder",
    "pick_voice_file",
    "check_update_async",
    "download_update",
    "install_update",
    "dismiss_update",
}
