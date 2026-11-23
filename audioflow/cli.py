"""CLI do AudioFlow — o mesmo motor do app com rosto de terminal.

Subcomandos: download, info, status, config, doctor.
Saída NDJSON com --json (para scripts), texto legível sem ele.
Exit codes: 0 ok, 1 falha parcial, 2 uso incorreto, 3 erro fatal.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import sys
import threading
import time

from .core import Callbacks, DownloadEngine, MANIFEST_FILENAME
from .spotify_api import SpotifyClient, parse_spotify_url
from .updater import UpdateError, check_for_update, download_update, pending_exe
from .utils import FORMATS, QUALITIES, SAMPLE_RATES, get_ffmpeg_path, load_config, save_config, setup_logging
from .version import APP_VERSION

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2
EXIT_FATAL = 3


def _attach_parent_console() -> None:
    """Num build windowed (sem console), conecta no console do processo pai.

    Permite que `AudioFlow.exe download <url>` mostre a saída no cmd/PowerShell
    de onde foi chamado. Em builds com console ou via python, não faz nada.
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    try:
        broken = sys.stdout is None or sys.stdout.fileno() < 0
    except (AttributeError, OSError, ValueError):
        broken = True
    if not broken:
        return
    import ctypes

    if ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
        sys.stdout = open("CONOUT$", "w", buffering=1, encoding="utf-8", errors="replace")  # noqa: SIM115
        sys.stderr = open("CONOUT$", "w", buffering=1, encoding="utf-8", errors="replace")  # noqa: SIM115


class _Emitter:
    """Saída thread-safe: linhas humanas ou eventos NDJSON."""

    def __init__(self, as_json: bool = False, quiet: bool = False) -> None:
        self.as_json = as_json
        self.quiet = quiet
        self._lock = threading.Lock()

    def event(self, name: str, **fields) -> None:
        with self._lock:
            if self.as_json:
                print(json.dumps({"event": name, **fields}, ensure_ascii=False), flush=True)
            elif not self.quiet:
                self._human(name, fields)

    def error(self, message: str, code: str = "error", hint: str | None = None) -> None:
        if self.as_json:
            self.event("error", code=code, message=message, hint=hint)
        else:
            print(f"erro: {message}", file=sys.stderr, flush=True)
            if hint:
                print(f"  dica: {hint}", file=sys.stderr, flush=True)

    def _human(self, name: str, f: dict) -> None:
        if name == "track_done":
            print(f"  ✓ {os.path.basename(f['file'])}", flush=True)
        elif name == "track_skip":
            print(f"  = {os.path.basename(f['file'])} (já existe)", flush=True)
        elif name == "track_fail":
            print(f"  ✗ {f.get('title', '')}: {f.get('detail', '')}", flush=True)
        elif name == "warning":
            print(f"  ! {f['message']}", flush=True)
        elif name == "run_summary":
            print(
                f"done: {f['ok']} ok, {f['skipped']} já existentes, {f['failed']} falhas "
                f"em {f['elapsed_s']}s -> {f['folder']}",
                flush=True,
            )


def _engine_from_config(cfg: dict) -> DownloadEngine:
    return DownloadEngine(
        audio_format=cfg["format"],
        audio_quality=cfg["quality"],
        sample_rate=cfg["sample_rate"],
        include_track_number=cfg["include_track_number"],
        artist_first=cfg["artist_first"],
        loose_match=cfg["loose_match"],
        max_workers=cfg.get("max_workers") or None,
    )


def cmd_download(args, cfg: dict) -> int:
    out_dir = os.path.abspath(os.path.expanduser(args.out or cfg["download_path"]))
    emitter = _Emitter(args.json, args.quiet)

    try:
        kind, _ = parse_spotify_url(args.url)
    except ValueError as exc:
        emitter.error(str(exc), code="invalid_url", hint="esperado open.spotify.com/{playlist,album,track}/... ou spotify: uri")
        return EXIT_FATAL

    if get_ffmpeg_path() is None:
        emitter.error(
            "ffmpeg não encontrado", code="ffmpeg_missing",
            hint="os binários do AudioFlow embutem o ffmpeg; na instalação por código, instale via winget/brew/apt",
        )
        return EXIT_FATAL

    engine = _engine_from_config(cfg)
    cancel = threading.Event()

    def _sigint(_sig, _frame):
        if cancel.is_set():
            sys.exit(EXIT_FATAL)
        cancel.set()
        engine.cancel()
        emitter.error("parando após as faixas em andamento (Ctrl+C de novo para forçar)")

    with contextlib.suppress(Exception):
        signal.signal(signal.SIGINT, _sigint)

    landed = [0]
    skipped = [0]

    def _done(t, f):
        landed[0] += 1
        emitter.event("track_done", title=t.title, file=f)

    def _skip(t, f):
        skipped[0] += 1
        emitter.event("track_skip", file=f)

    engine.cb.on_track_done = _done
    engine.cb.on_track_skip = _skip
    engine.cb.on_track_fail = lambda t, d: emitter.event("track_fail", title=t.title, detail=d)
    engine.cb.on_warning = lambda m: emitter.event("warning", message=m)
    engine.cb.on_metadata = lambda m: emitter.event("metadata", message=m)

    t0 = time.monotonic()
    engine.cb.on_finish = lambda ok, sk, fa: _summary(ok, sk, fa, t0)

    def _summary(ok, sk, fa, t0):
        emitter.event(
            "run_summary",
            ok=ok, skipped=sk, failed=fa,
            elapsed_s=round(time.monotonic() - t0, 1),
            folder=out_dir, exit_code=1 if fa else 0,
        )

    emitter.event("run_started", url=args.url, type=kind, folder=out_dir, format=engine.audio_format)
    try:
        code = engine.scrape(args.url, out_dir)
    except Exception as exc:
        emitter.error(f"execução falhou: {exc}", code="run_failed", hint="rode `audioflow doctor`")
        return EXIT_FATAL
    return code


def cmd_info(args, cfg: dict) -> int:
    emitter = _Emitter(args.json)
    try:
        kind, item_id = parse_spotify_url(args.url)
    except ValueError as exc:
        emitter.error(str(exc), code="invalid_url")
        return EXIT_FATAL

    try:
        client = SpotifyClient()
        if kind == "track":
            track = client.get_track(item_id)
            payload = {
                "type": "track", "id": track.id, "title": track.title,
                "artists": track.artists, "album": track.album or "",
                "release_date": track.release_date or "",
                "duration_ms": track.duration_ms or 0,
            }
        else:
            meta = client.get_collection_metadata(item_id, kind)
            tracks = [
                {"id": t.id, "title": t.title, "artists": t.artists, "duration_ms": t.duration_ms or 0}
                for t in client.iter_collection_tracks(item_id, kind)
            ]
            payload = {
                "type": kind, "id": item_id, "name": meta.name, "owner": meta.owner or "",
                "track_count": len(tracks), "tracks": tracks,
            }
    except Exception as exc:
        emitter.error(f"não foi possível buscar metadados: {exc}", code="metadata_fetch_failed")
        return EXIT_FATAL

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif kind == "track":
        print(f"{payload['title']} - {payload['artists']}  [track]")
    else:
        print(f"{payload['name']}  [{payload['type']}, {payload['track_count']} faixas]")
        for t in payload["tracks"]:
            print(f"  {t['title']} - {t['artists']}")
    return EXIT_OK


def cmd_status(args, cfg: dict) -> int:
    folder = os.path.abspath(os.path.expanduser(args.folder or cfg["download_path"]))
    emitter = _Emitter(args.json)
    manifest = os.path.join(folder, MANIFEST_FILENAME)
    entries: list[dict] = []
    if os.path.exists(manifest):
        with contextlib.suppress(OSError), open(manifest, encoding="utf-8") as fh:
            for line in fh:
                with contextlib.suppress(ValueError):
                    entries.append(json.loads(line))
    on_disk = [e for e in entries if os.path.exists(os.path.join(folder, e.get("file", "")))]
    missing = [e for e in entries if e not in on_disk]
    exts = tuple(f".{fmt}" for fmt in FORMATS)
    audio_files = sorted(n for n in os.listdir(folder) if n.lower().endswith(exts)) if os.path.isdir(folder) else []
    payload = {
        "folder": folder, "audio_files": len(audio_files),
        "manifest_entries": len(entries), "recorded_but_missing": len(missing),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"{folder}: {len(audio_files)} arquivos de áudio, {len(missing)} registrados mas ausentes")
    return EXIT_OK


def cmd_config(args, cfg: dict) -> int:
    emitter = _Emitter(args.json)
    registry = {
        "format": FORMATS, "quality": QUALITIES, "sample_rate": SAMPLE_RATES,
    }
    if args.set:
        for pair in args.set:
            key, sep, value = pair.partition("=")
            if not sep:
                emitter.error(f"--set espera chave=valor, recebeu {pair!r}", code="usage")
                return EXIT_USAGE
            if key not in cfg:
                emitter.error(f"chave desconhecida: {key}", code="usage")
                return EXIT_USAGE
            if key in registry and value not in registry[key]:
                emitter.error(f"{key} deve ser um de: {', '.join(registry[key])}", code="usage")
                return EXIT_USAGE
            if key in ("include_track_number", "artist_first", "loose_match"):
                if value.lower() not in ("true", "false"):
                    emitter.error(f"{key} deve ser true ou false", code="usage")
                    return EXIT_USAGE
                value = value.lower() == "true"
            cfg[key] = value
        save_config(cfg)
    visible = dict(cfg)
    if args.json:
        print(json.dumps(visible, ensure_ascii=False, indent=2))
    else:
        for key in sorted(visible):
            print(f"{key} = {visible[key]}")
    return EXIT_OK


def cmd_doctor(args, cfg: dict) -> int:
    emitter = _Emitter(args.json)
    checks: list[dict] = []
    ffmpeg = get_ffmpeg_path()
    checks.append({"check": "ffmpeg", "ok": ffmpeg is not None, "detail": ffmpeg or "não encontrado"})
    try:
        SpotifyClient().get_track("4uLU6hMCjMI75M1A2tKUQC")
        checks.append({"check": "spotify_metadata", "ok": True, "detail": "endpoint de embed acessível"})
    except Exception as exc:
        checks.append({"check": "spotify_metadata", "ok": False, "detail": str(exc)})
    try:
        import yt_dlp

        checks.append({"check": "yt_dlp", "ok": True, "detail": f"versão {yt_dlp.version.__version__}"})
    except Exception as exc:
        checks.append({"check": "yt_dlp", "ok": False, "detail": str(exc)})
    try:
        import mutagen

        checks.append({"check": "mutagen", "ok": True, "detail": f"versão {mutagen.version_string}"})
    except Exception as exc:
        checks.append({"check": "mutagen", "ok": False, "detail": str(exc)})

    all_ok = all(c["ok"] for c in checks)
    if args.json:
        print(json.dumps({"ok": all_ok, "checks": checks}, ensure_ascii=False, indent=2))
    else:
        for c in checks:
            print(f"  {'✓' if c['ok'] else '✗'} {c['check']}: {c['detail']}")
        print("tudo ok" if all_ok else "problemas encontrados — corrija as linhas ✗ acima")
    return EXIT_OK if all_ok else EXIT_FATAL


def cmd_update(args, cfg: dict) -> int:
    emitter = _Emitter(args.json)
    if args.action == "status":
        pending = pending_exe()
        payload = {
            "version": APP_VERSION,
            "pending_installer": pending.exists(),
            "pending_path": str(pending) if pending.exists() else None,
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(
                f"versão atual: {APP_VERSION}; "
                + ("instalador pendente pronto para aplicar" if pending.exists() else "sem instalação pendente")
            )
        return EXIT_OK

    if args.action == "apply":
        from .updater import apply_pending_update

        if apply_pending_update():
            print("atualização pendente aplicada — reinicie o app para concluir.")
        else:
            print("nada para aplicar.")
        return EXIT_OK

    try:
        info = check_for_update(APP_VERSION)
    except UpdateError as exc:
        emitter.error(str(exc), code="update_check_failed")
        return EXIT_FATAL

    if info is None:
        if args.json:
            print(json.dumps({"version": APP_VERSION, "update_available": False}, ensure_ascii=False, indent=2))
        else:
            print(f"você está na versão mais recente ({APP_VERSION}).")
        return EXIT_OK

    if args.action == "check":
        payload = {
            "version": APP_VERSION,
            "update_available": True,
            "new_version": info.version,
            "notes": info.notes,
            "size_bytes": info.asset_size,
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            size = info.asset_size
            size_txt = f" ({size / (1024 * 1024):.1f} MB)" if size else ""
            print(f"atualização disponível: {info.version}{size_txt}")
            print(info.notes or "sem notas")
        return EXIT_OK

    # download
    def _progress(downloaded: int, total: int, speed: float) -> None:
        emitter.event("update_progress", downloaded=downloaded, total=total, speed=round(speed))

    try:
        path = download_update(info, _progress)
    except UpdateError as exc:
        emitter.error(str(exc), code="update_download_failed")
        return EXIT_FATAL

    if args.json:
        print(json.dumps({"downloaded": True, "path": str(path)}, ensure_ascii=False, indent=2))
    else:
        print(f"atualização baixada e verificada: {path}")
        print("reinicie o app (ou rode `audioflow update apply`) para instalar.")
    return EXIT_OK


def build_parser(cfg: dict) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audioflow",
        description="AudioFlow: baixa playlists, álbuns e faixas do Spotify como áudio local com tags e capa.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "exemplos:\n"
            '  audioflow download "https://open.spotify.com/playlist/..."\n'
            '  audioflow download "<url>" --format flac --quality 320 --out ~/Music\n'
            '  audioflow download "<url>" --json\n'
            '  audioflow info "<url>" --json\n'
            '  audioflow config --set format=m4a\n'
            "  audioflow update check\n"
            "  audioflow doctor\n"
        ),
    )
    parser.add_argument("--version", "-V", action="version", version=f"AudioFlow {APP_VERSION}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    dl = sub.add_parser("download", help="baixa playlist, álbum ou faixa")
    dl.add_argument("url", help="URL/URI do Spotify")
    dl.add_argument("--out", "-o", default=None, help=f"pasta de destino (padrão: {cfg['download_path']})")
    dl.add_argument("--format", "-f", choices=FORMATS, default=None, help=f"formato (padrão: {cfg['format']})")
    dl.add_argument("--quality", "-q", choices=QUALITIES, default=None, help=f"bitrate kbps (padrão: {cfg['quality']})")
    dl.add_argument("--sample-rate", choices=SAMPLE_RATES, default=None, help="taxa de amostragem")
    dl.add_argument("--track-numbers", action="store_true", default=None, help="prefixar arquivos com o número da faixa")
    dl.add_argument("--no-track-numbers", action="store_true", default=None)
    dl.add_argument("--artist-first", action="store_true", default=None, help='nomear "Artista - Faixa"')
    dl.add_argument("--loose-match", action="store_true", default=None, help="aceitar resultado aproximado quando não houver match")
    dl.add_argument("--json", action="store_true", help="saída NDJSON")
    dl.add_argument("--quiet", "-Q", action="store_true", help="suprime progresso")
    dl.set_defaults(func=cmd_download)

    info = sub.add_parser("info", help="mostra metadados sem baixar")
    info.add_argument("url", help="URL/URI do Spotify")
    info.add_argument("--json", action="store_true")
    info.set_defaults(func=cmd_info)

    status = sub.add_parser("status", help="mostra o que há numa pasta de download")
    status.add_argument("folder", nargs="?", default=None)
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    config = sub.add_parser("config", help="mostra ou altera as configurações salvas")
    config.add_argument("--set", action="append", metavar="chave=valor")
    config.add_argument("--json", action="store_true")
    config.set_defaults(func=cmd_config)

    doctor = sub.add_parser("doctor", help="verifica ffmpeg, Spotify, yt-dlp e mutagen")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    update = sub.add_parser("update", help="verifica e baixa atualizações")
    update.add_argument("action", choices=["check", "status", "download", "apply"])
    update.add_argument("--json", action="store_true")
    update.set_defaults(func=cmd_update)

    return parser


def main(argv: list[str] | None = None) -> int:
    _attach_parent_console()
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")
    setup_logging()
    cfg = load_config()
    parser = build_parser(cfg)
    args = parser.parse_args(argv)
    if args.command == "download":
        overrides = {}
        if args.format is not None:
            overrides["format"] = args.format
        if args.quality is not None:
            overrides["quality"] = args.quality
        if args.sample_rate is not None:
            overrides["sample_rate"] = args.sample_rate
        for flag, key in (("track_numbers", "include_track_number"), ("artist_first", "artist_first"), ("loose_match", "loose_match")):
            if getattr(args, flag, None) is True:
                overrides[key] = True
            if flag in ("track_numbers",) and getattr(args, "no_track_numbers", None) is True:
                overrides["include_track_number"] = False
        cfg.update(overrides)
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
