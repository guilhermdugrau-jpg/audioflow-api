"""Configuração persistente e utilitários de caminho do AudioFlow."""

from __future__ import annotations

import json
import logging
import os
import sys

log = logging.getLogger("audioflow")

FORMATS = ("mp3", "m4a", "opus", "flac", "wav")
QUALITIES = ("128", "192", "256", "320")
SAMPLE_RATES = ("auto", "44100", "48000")

def android_app_dir() -> str:
    """Diretório privado do app no Android (sempre gravável). Vazio se não for Android."""
    return os.environ.get("ANDROID_PRIVATE") or os.environ.get("ANDROID_APP_PATH") or ""


def is_android() -> bool:
    return bool(android_app_dir() or os.environ.get("ANDROID_ARGUMENT"))


def _default_download_path() -> str:
    priv = android_app_dir()
    if priv:
        return os.path.join(priv, "downloads")
    return os.path.join(os.path.expanduser("~"), "Music", "AudioFlow")


DEFAULTS = {
    "download_path": _default_download_path(),
    "format": "mp3",
    "quality": "192",
    "sample_rate": "auto",
    "include_track_number": False,
    "artist_first": False,
    "loose_match": False,
    "max_workers": 4,
    "skip_update_version": "",
    "watermark_enabled": False,
    "watermark_text": "LJCD",
    "watermark_position": "middle",
    "watermark_volume": 0.3,
    "watermark_effects": [],
    "watermark_voice": "tts",
}


def config_dir() -> str:
    priv = android_app_dir()
    if priv:
        path = os.path.join(priv, "audioflow")
    else:
        base = os.environ.get("APPDATA") if sys.platform == "win32" else None
        if base is None:
            base = os.path.expanduser("~")
        path = os.path.join(base, "AudioFlow")
    os.makedirs(path, exist_ok=True)
    return path


def config_path() -> str:
    return os.path.join(config_dir(), "config.json")


def log_dir() -> str:
    priv = android_app_dir()
    if priv:
        path = os.path.join(priv, "audioflow", "logs")
    else:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if sys.platform == "win32" and base:
            path = os.path.join(base, "AudioFlow", "logs")
        elif sys.platform == "darwin":
            path = os.path.join(os.path.expanduser("~"), "Library", "Logs", "AudioFlow")
        else:
            base = os.environ.get("XDG_STATE_HOME", os.path.join(os.path.expanduser("~"), ".local", "state"))
            path = os.path.join(base, "audioflow", "logs")
    return path


def log_file_path() -> str:
    return os.path.join(log_dir(), "audioflow.log")


def setup_logging() -> str:
    if any(getattr(h, "_audioflow", False) for h in log.handlers):
        return log_file_path()
    from logging.handlers import RotatingFileHandler

    os.makedirs(log_dir(), exist_ok=True)
    handler = RotatingFileHandler(
        log_file_path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8", delay=True
    )
    handler._audioflow = True
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    level = logging.DEBUG if os.environ.get("AUDIOFLOW_DEBUG") else logging.INFO
    log.setLevel(level)
    log.addHandler(handler)
    log.propagate = False
    log.info("==== audioflow session start ====")
    return log_file_path()


def load_config() -> dict:
    defaults = dict(DEFAULTS)
    try:
        with open(config_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return defaults
        for key, default in defaults.items():
            if key in data:
                value = data[key]
                if key == "format" and value not in FORMATS:
                    continue
                if key == "quality" and value not in QUALITIES:
                    continue
                if key == "sample_rate" and value not in SAMPLE_RATES:
                    continue
                if key == "watermark_enabled" and not isinstance(value, bool):
                    continue
                if key == "watermark_position" and value not in ("start", "middle", "end"):
                    continue
                if key == "watermark_volume":
                    try:
                        value = float(value)
                    except (TypeError, ValueError):
                        continue
                    if not 0.0 <= value <= 1.0:
                        continue
                if key == "watermark_effects" and not isinstance(value, list):
                    continue
                if key == "watermark_voice" and not isinstance(value, str):
                    continue
                defaults[key] = value
        return defaults
    except (OSError, json.JSONDecodeError):
        return defaults


def save_config(cfg: dict) -> None:
    try:
        with open(config_path(), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except OSError as exc:
        log.warning("não foi possível salvar a config: %s", exc)


def get_ffmpeg_path() -> str | None:
    """Caminho do ffmpeg: embutido na pasta bin/ do projeto, bundled (PyInstaller), depois PATH."""
    # No servidor web (Render free) desligamos o ffmpeg de propósito: o áudio
    # sai nativo (M4A/WebM), sem transcodificar, economizando CPU e RAM.
    if os.environ.get("AUDIOFLOW_DISABLE_FFMPEG") == "1":
        return None
    project_bin = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
    if sys.platform == "win32":
        if os.path.exists(os.path.join(project_bin, "ffmpeg.exe")):
            return project_bin
    elif os.path.exists(os.path.join(project_bin, "ffmpeg")):
        return project_bin
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS
        candidates = [os.path.join(base, "ffmpeg", "ffmpeg.exe"), os.path.join(base, "ffmpeg", "ffmpeg")]
        for candidate in candidates:
            if os.path.exists(candidate):
                return os.path.dirname(candidate)
    import shutil

    found = shutil.which("ffmpeg")
    return os.path.dirname(found) if found else None
