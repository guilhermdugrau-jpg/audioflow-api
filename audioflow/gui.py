"""Host pywebview do AudioFlow (UI em HTML/CSS/JS).

A janela é um WebView2 (EdgeChromium) renderizando o frontend em
``audioflow/web/``. O JavaScript chama métodos da classe ``Api`` via
``window.pywebview.api``; o Python envia eventos de progresso de volta por
``window.audioflow_event(...)``.

O motor (core.DownloadEngine) roda em thread própria; os callbacks apenas
encaminham eventos ao frontend — a janela nunca trava.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .core import DownloadEngine
from .spotify_api import SpotifyClient, parse_spotify_url
from .telemetry import send_beacon
from .updater import (
    UpdateError,
    apply_pending_update,
    apply_update,
    check_for_update,
    download_update as updater_download,
    pending_exe,
)
from .utils import get_ffmpeg_path, is_android, load_config, save_config
from .version import APP_VERSION, BEACON_URL

try:
    import webview
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "pywebview não está instalado. Rode: pip install pywebview"
    ) from exc

log = logging.getLogger("audioflow.gui")

APP_NAME = "AudioFlow"
WINDOW_SIZE = (820, 900)
MIN_SIZE = (380, 640)
BG_COLOR = "#0b0f17"


def _web_dir() -> str:
    """Pasta dos assets do frontend (funciona em dev e no executável)."""
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "audioflow", "web")


class _WebHandler(SimpleHTTPRequestHandler):
    """Serve os assets do frontend de forma local e silenciosa.

    O app carrega a UI por HTTP local (127.0.0.1) em vez de file://, porque o
    WebView2 não carrega imagens remotas (capas do Spotify) em páginas file://.

    No Android (WebView sem pywebview) o mesmo servidor também expõe a API:
        POST /api/call          {method, args} -> {ok, result} | {ok, error}
        GET  /api/events?since=N -> {seq, events}
    """

    server_version = "AudioFlow/1.0"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, directory=_web_dir(), **kwargs)

    def log_message(self, *args) -> None:  # noqa: ANN002
        pass

    def _send_json(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/events":
            since = parse_qs(urlsplit(self.path).query).get("since", ["0"])
            api = getattr(self.server, "api", None)
            if api is None:
                self._send_json(500, {"seq": 0, "events": []})
                return
            self._send_json(200, api.events_since(int(since[0] or 0)))
            return
        if path == "/api/health":
            self._send_json(200, {"ok": True})
            return
        return super().do_GET()

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path != "/api/call":
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        api = getattr(self.server, "api", None)
        if api is None:
            self._send_json(500, {"ok": False, "error": "app não iniciado"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1 << 20:  # 1 MB de teto
            self._send_json(413, {"ok": False, "error": "payload grande demais"})
            return
        raw = self.rfile.read(length)
        try:
            req = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            self._send_json(400, {"ok": False, "error": "json inválido"})
            return
        method = req.get("method") if isinstance(req, dict) else None
        args = req.get("args") or []
        if not isinstance(method, str) or method.startswith("_") or not hasattr(api, method):
            self._send_json(404, {"ok": False, "error": f"método {method!r} indisponível"})
            return
        try:
            result = getattr(api, method)(*args)
        except Exception as exc:
            log.exception("erro no método HTTP %s", method)
            self._send_json(500, {"ok": False, "error": str(exc)})
            return
        self._send_json(200, {"ok": True, "result": result})


# Portas fixas de fallback para o servidor web local (WinError 10055).
_FALLBACK_PORTS = (23100, 23107, 23114, 23121, 23128)


def _start_web_server(api=None) -> ThreadingHTTPServer:
    """Sobe o servidor HTTP local com tolerância ao WinError 10055.

    Primeiro tenta uma porta efêmera (127.0.0.1:0). Se o Windows negar o bind
    (buffer de soquetes/portas efêmeras esgotado — WinError 10055), cai para
    portas fixas da faixa 231xx com algumas tentativas.
    """
    last_err: Exception | None = None
    for port in (0, *_FALLBACK_PORTS):
        for _ in range(3):
            try:
                server = ThreadingHTTPServer(("127.0.0.1", port), _WebHandler)
                server.api = api  # type: ignore[attr-defined]
                threading.Thread(target=server.serve_forever, daemon=True).start()
                log.info("servidor web local em 127.0.0.1:%d", server.server_address[1])
                return server
            except OSError as exc:
                last_err = exc
                time.sleep(0.3)
    raise OSError(f"não foi possível subir o servidor web local: {last_err}")


class Api:
    """Métodos expostos ao JavaScript (window.pywebview.api.*)."""

    def __init__(self) -> None:
        self._window: webview.Window | None = None
        self._engine: DownloadEngine | None = None
        self._thread: threading.Thread | None = None
        self._started = 0
        self._done = 0
        self._total: int | None = None
        self._meta_cover: str | None = None
        self._update_checking = False
        self._update_downloading = False
        self._last_update: dict | None = None
        self._ev_lock = threading.Lock()
        self._ev_seq = 0
        self._events: list[tuple[int, dict]] = []

    @staticmethod
    def _is_android() -> bool:
        return is_android()

    def _platform(self) -> str:
        return "android" if self._is_android() else "desktop"

    # --- ponte Python -> JS ------------------------------------------------

    def _emit(self, payload: dict) -> None:
        with self._ev_lock:
            self._ev_seq += 1
            self._events.append((self._ev_seq, payload))
        if self._window is not None:
            try:
                js = f"window.audioflow_event({json.dumps(payload, ensure_ascii=False)})"
                self._window.evaluate_js(js)
            except Exception:
                log.exception("falha ao enviar evento ao frontend")

    def events_since(self, seq: int) -> dict:
        """Eventos enfileirados a partir de `seq` (usado pelo frontend via HTTP)."""
        seq = int(seq or 0)
        with self._ev_lock:
            events = [p for s, p in self._events if s > seq]
            return {"seq": self._ev_seq, "events": events}

    # --- config ------------------------------------------------------------

    def get_config(self) -> dict:
        cfg = load_config()
        return {
            "format": cfg.get("format", "mp3"),
            "quality": int(cfg.get("quality", 192)),
            "folder": cfg.get("download_path", ""),
            "tracknum": bool(cfg.get("include_track_number", False)),
            "platform": self._platform(),
            "watermark": {
                "enabled": bool(cfg.get("watermark_enabled", False)),
                "text": cfg.get("watermark_text", "LJCD"),
                "position": cfg.get("watermark_position", "middle"),
                "volume": float(cfg.get("watermark_volume", 0.3)),
                "effects": list(cfg.get("watermark_effects") or []),
                "voice": cfg.get("watermark_voice", "tts"),
            },
        }

    def save_config(self, settings: dict) -> None:
        cfg = load_config()
        if isinstance(settings, dict):
            if settings.get("format") in ("mp3", "m4a", "opus", "flac", "wav"):
                cfg["format"] = settings["format"]
            if str(settings.get("quality", "")) in ("128", "192", "256", "320"):
                cfg["quality"] = str(settings["quality"])
            if settings.get("folder"):
                cfg["download_path"] = settings["folder"]
            if isinstance(settings.get("tracknum"), bool):
                cfg["include_track_number"] = settings["tracknum"]
            wm = settings.get("watermark")
            if isinstance(wm, dict):
                if isinstance(wm.get("enabled"), bool):
                    cfg["watermark_enabled"] = wm["enabled"]
                if isinstance(wm.get("text"), str) and wm["text"].strip():
                    cfg["watermark_text"] = wm["text"].strip()
                if wm.get("position") in ("start", "middle", "end"):
                    cfg["watermark_position"] = wm["position"]
                try:
                    vol = float(wm.get("volume", 0.3))
                except (TypeError, ValueError):
                    vol = 0.3
                cfg["watermark_volume"] = max(0.0, min(1.0, vol))
                if isinstance(wm.get("effects"), list):
                    cfg["watermark_effects"] = [e for e in wm["effects"] if e in ("echo", "reverb", "deep", "radio")]
                if isinstance(wm.get("voice"), str):
                    cfg["watermark_voice"] = wm["voice"]
        save_config(cfg)

    def pick_voice_file(self) -> str | None:
        """Seleciona um arquivo de voz personalizado (wav/mp3/ogg/flac)."""
        if self._is_android():
            return None
        if self._window is not None:
            try:
                chosen = self._window.create_file_dialog(
                    webview.FILE_DIALOG,
                    file_types=(
                        "Áudio (*.wav;*.mp3;*.ogg;*.flac)",
                        "Todos (*.*)",
                    ),
                    directory=os.path.expanduser("~"),
                )
                if isinstance(chosen, list) and chosen:
                    return chosen[0]
            except Exception:
                log.exception("diálogo de voz via webview falhou")
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            try:
                return filedialog.askopenfilename(
                    parent=root,
                    initialdir=os.path.expanduser("~"),
                    filetypes=[
                        ("Áudio", "*.wav *.mp3 *.ogg *.flac"),
                        ("Todos", "*.*"),
                    ],
                )
            finally:
                root.destroy()
        except Exception:
            log.exception("diálogo de voz via tkinter falhou")
        return None

    def pick_folder(self) -> str | None:
        # No Android a pasta é fixa (diretório do app); não há seletor de pastas.
        if self._is_android():
            return None
        if self._window is not None:
            try:
                chosen = self._window.create_file_dialog(
                    webview.FOLDER_DIALOG,
                    directory=self._engine_folder() or os.path.expanduser("~"),
                )
                if isinstance(chosen, list) and chosen:
                    return chosen[0]
            except Exception:
                log.exception("diálogo de pasta via webview falhou")
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            try:
                return filedialog.askdirectory(
                    parent=root,
                    initialdir=self._engine_folder() or os.path.expanduser("~"),
                )
            finally:
                root.destroy()
        except Exception:
            log.exception("diálogo de pasta via tkinter falhou")
        return None

    def _engine_folder(self) -> str | None:
        cfg = load_config()
        folder = cfg.get("download_path")
        return folder if isinstance(folder, str) and folder else None

    # --- metadados (contador de músicas ao colar o link) -------------------

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
        return {
            "type": kind,
            "name": meta.name or "Coleção",
            "total": meta.track_count or 0,
            "cover": meta.cover_url,
        }

    # --- download ----------------------------------------------------------

    def download(self, url: str, settings: dict | None = None) -> str:
        settings = settings or {}
        format_ = settings.get("format") or "mp3"
        quality = str(settings.get("quality") or 192)
        folder = settings.get("folder") or self._engine_folder() or ""
        tracknum = bool(settings.get("tracknum", False))

        if get_ffmpeg_path() is None:
            native = self._is_android() and format_ in ("m4a", "opus")
            if not native:
                raise RuntimeError(
                    "ffmpeg não encontrado — instale via `winget install ffmpeg` "
                    "ou rode o binário pré-empacotado."
                )

        self.save_config(settings)

        wm = settings.get("watermark") if isinstance(settings.get("watermark"), dict) else {}
        self._engine = DownloadEngine(
            audio_format=format_,
            audio_quality=quality,
            include_track_number=tracknum,
            watermark={
                "enabled": bool(wm.get("enabled", False)),
                "text": wm.get("text") or "LJCD",
                "position": wm.get("position") or "middle",
                "volume": float(wm.get("volume") or 0.3),
                "effects": list(wm.get("effects") or []),
                "voice": wm.get("voice") or "tts",
            },
        )
        self._started = 0
        self._done = 0
        self._total = None
        self._meta_cover = None

        try:
            kind, item_id = parse_spotify_url(url)
            if kind != "track":
                meta = SpotifyClient().get_collection_metadata(item_id, kind)
                if meta.track_count:
                    self._total = meta.track_count
                self._meta_cover = meta.cover_url
        except Exception:
            self._total = None

        self._wire_callbacks()
        self._thread = threading.Thread(
            target=self._run, args=(url, folder), daemon=True
        )
        self._thread.start()
        return "ok"

    def _wire_callbacks(self) -> None:
        engine = self._engine
        engine.cb.on_metadata = lambda msg: self._emit({"type": "progress", "percent": 0, "label": msg})
        engine.cb.on_track_start = self._on_track_start
        engine.cb.on_track_done = lambda t, _f: self._on_track_end(t, "ok")
        engine.cb.on_track_skip = lambda t, _f: self._on_track_end(t, "skip")
        engine.cb.on_track_fail = lambda t, _f: self._on_track_end(t, "fail")
        engine.cb.on_finish = self._on_finish

    def _track_cover(self, track) -> str | None:
        return track.cover_url or self._meta_cover

    def _on_track_start(self, track, _path) -> None:
        self._started += 1
        self._emit({
            "type": "track_start",
            "id": track.id,
            "name": track.title,
            "cover": self._track_cover(track),
        })

    def _on_track_end(self, track, status: str) -> None:
        self._done += 1
        self._emit({
            "type": f"track_{status}",
            "id": track.id,
            "name": track.title,
            "cover": self._track_cover(track),
        })
        total = self._total or max(self._started, 1)
        percent = round(self._done * 100 / total)
        self._emit({
            "type": "progress",
            "percent": percent,
            "label": f"{self._done} de {total} faixas",
        })

    def _on_finish(self, ok: int, skipped: int, failed: int) -> None:
        summary = f"Concluído: {ok} baixadas"
        if skipped:
            summary += f", {skipped} já existiam"
        if failed:
            summary += f", {failed} falharam"
        summary += "."
        self._emit({"type": "finish", "summary": summary})

    def _run(self, url: str, folder: str) -> None:
        try:
            self._engine.scrape(url, folder)
        except Exception as exc:
            log.exception("erro no download")
            self._emit({"type": "error", "message": str(exc)})

    def cancel(self) -> None:
        if self._engine:
            self._engine.cancel()

    # --- atualização -------------------------------------------------------

    def get_app_version(self) -> str:
        return APP_VERSION

    def _update_payload(self, info) -> dict | None:
        return {
            "type": "update_available",
            "version": info.version,
            "current": APP_VERSION,
            "notes": info.notes or "",
            "size": info.asset_size,
        }

    def check_update_async(self) -> None:
        """Verifica atualizações em thread própria; resultado via eventos."""
        if self._update_checking or self._update_downloading:
            return
        if os.environ.get("AUDIOFLOW_NO_UPDATE") or self._is_android():
            return
        self._update_checking = True

        def _run() -> None:
            try:
                info = check_for_update(APP_VERSION)
                if info is None:
                    self._update_checking = False
                    return
                cfg = load_config()
                if cfg.get("skip_update_version") == info.version:
                    self._update_checking = False
                    return
                self._last_update = info
                self._emit(self._update_payload(info))
            except Exception:
                log.exception("verificação de atualização falhou")
                self._emit({"type": "update_error"})
            finally:
                self._update_checking = False

        threading.Thread(target=_run, daemon=True).start()

    def download_update(self) -> str:
        """Baixa a atualização em thread própria; progresso via eventos."""
        if self._is_android():
            self._emit({"type": "update_error"})
            return "ok"
        if self._update_downloading:
            return "ok"
        self._update_downloading = True

        def _run() -> None:
            try:
                info = check_for_update(APP_VERSION)
                if info is None:
                    self._emit({
                        "type": "update_download_error",
                        "message": "Nenhuma atualização disponível.",
                    })
                    return

                def _progress(downloaded: int, total: int, speed: float) -> None:
                    percent = round(downloaded * 100 / total) if total else 0
                    self._emit({
                        "type": "update_download_progress",
                        "percent": percent,
                        "downloaded": downloaded,
                        "total": total,
                        "speed": int(speed),
                    })

                updater_download(info, _progress)
                self._emit({"type": "update_download_done"})
            except UpdateError as exc:
                log.exception("falha ao baixar a atualização")
                self._emit({"type": "update_download_error", "message": str(exc)})
            except Exception:
                log.exception("falha inesperada na atualização")
                self._emit({
                    "type": "update_download_error",
                    "message": "Falha inesperada ao atualizar.",
                })
            finally:
                self._update_downloading = False

        threading.Thread(target=_run, daemon=True).start()
        return "ok"

    def install_update(self) -> str:
        """Aplica a atualização baixada e reinicia o app."""
        if self._is_android():
            return "ok"
        try:
            apply_update(pending_exe(), restart=True)
        except UpdateError as exc:
            log.exception("não foi possível aplicar a atualização")
            raise RuntimeError(str(exc)) from exc

        def _close() -> None:
            time.sleep(1.5)
            if self._window is not None:
                try:
                    self._window.destroy()
                except Exception:
                    log.exception("falha ao fechar a janela após atualizar")

        threading.Thread(target=_close, daemon=True).start()
        return "ok"

    def dismiss_update(self, info: dict | None = None) -> None:
        """"Mais tarde": suprime o aviso para esta versão (config local)."""
        version = (info or {}).get("version")
        if version:
            cfg = load_config()
            cfg["skip_update_version"] = version
            save_config(cfg)


def _selftest(window, api) -> None:
    """Auto-teste oculto (AUDIOFLOW_SELFTEST=1): baixa uma faixa pela
    ponte JS real e grava o resultado em AUDIOFLOW_SELFTEST_OUT."""
    import json as _json

    url = os.environ.get("AUDIOFLOW_SELFTEST_URL", "")
    folder = os.environ.get(
        "AUDIOFLOW_SELFTEST_FOLDER",
        os.path.join(os.environ.get("TEMP", "."), "audioflow_selftest"),
    )
    out = os.environ.get(
        "AUDIOFLOW_SELFTEST_OUT",
        os.path.join(os.environ.get("TEMP", "."), "audioflow_selftest.json"),
    )
    os.makedirs(folder, exist_ok=True)

    def _finish(result: dict) -> None:
        try:
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(result))
        except OSError:
            pass
        try:
            window.destroy()
        except Exception:
            pass

    def _run() -> None:
        if not url:
            _finish({"ok": False, "error": "AUDIOFLOW_SELFTEST_URL vazio"})
            return
        try:
            update_ui_ok = False
            window.evaluate_js(
                "window.__selftestEvents = [];"
                "const _af = window.audioflow_event;"
                "window.audioflow_event = (p) => {"
                "  const o = (typeof p === 'string') ? JSON.parse(p) : p;"
                "  window.__selftestEvents.push(o); _af(p);"
                "};"
            )
            if os.environ.get("AUDIOFLOW_SELFTEST_UPDATE"):
                window.evaluate_js(
                    "window.audioflow_event({type:'update_available',"
                    "version:'v9.9.9', current:'1.0.0', notes:'notas de teste', size:10485760})"
                )
                version = ""
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    time.sleep(0.4)
                    try:
                        version = window.evaluate_js(
                            "document.getElementById('app-version').textContent"
                        )
                    except Exception:
                        version = ""
                    if version:
                        break
                modal_hidden = window.evaluate_js(
                    "document.getElementById('update-modal').hidden"
                )
                subtitle = window.evaluate_js(
                    "document.getElementById('update-modal-subtitle').textContent"
                )
                if modal_hidden is True or "v9.9.9" not in (subtitle or ""):
                    return _finish({"ok": False, "error": "modal de atualização não renderizou"})
                if version != APP_VERSION:
                    bridge = window.evaluate_js(
                        "JSON.stringify({"
                        "pywebview: typeof window.pywebview,"
                        "api: typeof (window.pywebview && window.pywebview.api),"
                        "get_version: typeof (window.pywebview && window.pywebview.api && window.pywebview.api.get_app_version)"
                        "})"
                    )
                    window.evaluate_js(
                        "window.__probe='pending';"
                        "Promise.all([window.pywebview.api.get_app_version(),"
                        "window.pywebview.api.get_config()])"
                        ".then(([v,c])=>{window.__probe=JSON.stringify({version:v,config_ok:!!c});})"
                        ".catch(e=>{window.__probe='ERR '+e;});"
                        "setTimeout(()=>{if(window.__probe==='pending')window.__probe='HUNG';},5000);"
                    )
                    time.sleep(6)
                    probe = window.evaluate_js("window.__probe || ''")
                    flags = window.evaluate_js(
                        "JSON.stringify({initRan:!!window.__initRan, versionSet:!!window.__versionSet,"
                        "api:typeof(window.pywebview&&window.pywebview.api)})"
                    )
                    return _finish({
                        "ok": False,
                        "error": f"versão no rodapé inesperada: {version!r} (esperado {APP_VERSION})",
                        "bridge": bridge,
                        "probe": probe,
                        "flags": flags,
                    })
                footer = window.evaluate_js(
                    "document.getElementById('footer-note').textContent"
                )
                if "Lucas" not in (footer or ""):
                    return _finish({"ok": False, "error": f"rodapé não atualizado: {footer!r}"})
                update_ui_ok = True
            window.evaluate_js(
                "window.pywebview.api.download(%s, {format:'mp3', quality:192, folder:%s, tracknum:false})"
                % (json.dumps(url), json.dumps(folder))
            )
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                time.sleep(2)
                raw = window.evaluate_js(
                    "JSON.stringify(window.__selftestEvents || [])"
                )
                if not raw or raw == "[]":
                    continue
                events = _json.loads(raw)
                if any(e.get("type") == "finish" for e in events):
                    result = {"ok": True, "folder": folder}
                    if os.environ.get("AUDIOFLOW_SELFTEST_UPDATE"):
                        result["update_ui"] = "ok" if update_ui_ok else "missing"
                    return _finish(result)
                if any(
                    e.get("type") == "error" for e in events
                ):
                    return _finish({"ok": False, "error": "evento de erro no app"})
            _finish({"ok": False, "error": "timeout aguardando finish"})
        except Exception as exc:
            log.exception("selftest falhou")
            _finish({"ok": False, "error": str(exc)})

    threading.Timer(3.0, _run).start()


def run() -> None:
    # Atualização baixada no boot anterior: aplica e sai (o bat relança o app).
    try:
        if apply_pending_update():
            log.info("atualização pendente aplicada; reiniciando")
            return
    except Exception:
        log.exception("falha ao aplicar atualização pendente")

    api = Api()
    server = _start_web_server(api)
    try:
        host, port = server.server_address[:2]
        index = f"http://{host}:{port}/index.html"
        window = webview.create_window(
            APP_NAME,
            url=index,
            width=WINDOW_SIZE[0],
            height=WINDOW_SIZE[1],
            min_size=MIN_SIZE,
            resizable=True,
            background_color=BG_COLOR,
            js_api=api,
        )
    except Exception as exc:
        log.error("falha ao criar a janela: %s", exc)
        server.shutdown()
        raise

    api._window = window

    # Beacon discreto de execução (se configurado): envia em thread própria.
    if BEACON_URL:
        threading.Thread(
            target=send_beacon, args=(BEACON_URL, WINDOW_SIZE), daemon=True
        ).start()

    def _on_closed() -> None:
        if api._engine:
            api._engine.cancel()
        try:
            server.shutdown()
        except Exception:
            pass

    window.events.closed += _on_closed
    if os.environ.get("AUDIOFLOW_SELFTEST"):
        _selftest(window, api)
    webview.start()


if __name__ == "__main__":
    run()
