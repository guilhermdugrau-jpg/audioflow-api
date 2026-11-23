"""Download de atualizações com retomada, progresso e velocidade.

O instalador é baixado para o diretório de atualização do app junto com os
assets de verificação; ao final, ``verify_installer`` confere assinatura e
checksum. Falhas de rede tentam retomar via Range a partir do trecho já
gravado; se o servidor não suportar, recomeça do zero (com limite de tentativas).
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Callable, Optional

import requests

from ..version import CHECKSUM_ASSET, SIGNATURE_ASSET, UPDATE_USER_AGENT
from .apply import update_dir
from .manifest import ReleaseInfo
from .verify import UpdateError, verify_installer

_CHUNK = 256 * 1024
_MAX_TRIES = 3

# Tipo do callback de progresso: (downloaded_bytes, total_bytes, bytes_por_seg).
ProgressCallback = Callable[[int, int, float], None]


def _read_remote_size(url: str) -> Optional[int]:
    """Content-Length da URL via HEAD (None quando indisponível)."""
    try:
        resp = requests.head(url, timeout=15, allow_redirects=True)
        if resp.ok:
            return int(resp.headers.get("Content-Length") or 0) or None
    except requests.RequestException:
        pass
    return None


def _stream_file(
    url: str,
    dest: Path,
    resume_at: int,
    progress: Optional[ProgressCallback],
) -> int:
    """Baixa ``url`` para ``dest``, retomando a partir de ``resume_at``.

    Retorna o tamanho final do arquivo. Levanta ``UpdateError`` em falha.
    """
    headers = {"User-Agent": UPDATE_USER_AGENT}
    if resume_at > 0:
        headers["Range"] = f"bytes={resume_at}-"

    with requests.get(url, stream=True, timeout=30, headers=headers) as resp:
        if resume_at > 0 and resp.status_code == 416:
            # Já temos o arquivo completo.
            return resume_at
        if resume_at > 0 and resp.status_code != 206:
            # Servidor não aceitou Range: reinicia do zero.
            resume_at = 0
        if resp.status_code not in (200, 206):
            raise UpdateError(f"Falha ao baixar (HTTP {resp.status_code}).")

        total = resume_at
        content_length = resp.headers.get("Content-Length")
        if content_length and resp.status_code == 206:
            total += int(content_length)
        elif content_length and resp.status_code == 200:
            total = int(content_length)

        mode = "ab" if resume_at else "wb"
        downloaded = resume_at
        window: list[tuple[float, int]] = []
        last_report = 0.0

        with open(dest, mode) as fh:
            for chunk in resp.iter_content(_CHUNK):
                if not chunk:
                    continue
                fh.write(chunk)
                downloaded += len(chunk)
                now = time.monotonic()
                window.append((now, len(chunk)))
                window = [w for w in window if now - w[0] <= 2.0]
                if progress and now - last_report >= 0.15:
                    elapsed = window[-1][0] - window[0][0] if len(window) > 1 else 0
                    speed = (
                        sum(w[1] for w in window) / elapsed if elapsed > 0 else 0
                    )
                    last_report = now
                    progress(downloaded, total or downloaded, speed)
    return downloaded


def download_update(
    info: ReleaseInfo,
    progress: Optional[ProgressCallback] = None,
) -> Path:
    """Baixa e verifica o instalador da release; devolve o caminho final.

    O instalador verificado fica em ``<update_dir>/AudioFlow.exe`` e os assets
    de verificação em ``AudioFlow.sha256``/``AudioFlow.sig`` ao lado.
    """
    target_dir = update_dir()
    target_dir.mkdir(parents=True, exist_ok=True)

    installer = target_dir / "AudioFlow.exe"
    checksum_file = target_dir / CHECKSUM_ASSET
    signature_file = target_dir / SIGNATURE_ASSET

    _download_with_retries(
        info.installer_url, installer, "instalador", progress
    )
    _download_with_retries(info.checksum_url, checksum_file, "checksum", None)
    _download_with_retries(info.signature_url, signature_file, "assinatura", None)

    verify_installer(installer, checksum_file, signature_file)
    return installer


def _download_with_retries(
    url: str,
    dest: Path,
    label: str,
    progress: Optional[ProgressCallback],
) -> None:
    """Baixa um asset com retomada e limite de tentativas."""
    size = _read_remote_size(url)
    for attempt in range(1, _MAX_TRIES + 1):
        try:
            resume = dest.stat().st_size if dest.exists() else 0
            if size and resume > size:
                resume = 0  # arquivo parcial maior que o original: refazer
            downloaded = _stream_file(url, dest, resume, progress)
            if size and downloaded != size:
                raise UpdateError("Download incompleto (tamanho divergente).")
            return
        except UpdateError:
            raise
        except requests.RequestException as exc:
            if attempt == _MAX_TRIES:
                raise UpdateError(
                    f"Falha ao baixar {label} após {_MAX_TRIES} tentativas."
                ) from exc
            time.sleep(1.5 * attempt)
    raise UpdateError(f"Não foi possível baixar o {label}.")
