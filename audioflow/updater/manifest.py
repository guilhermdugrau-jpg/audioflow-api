"""Consulta de releases no GitHub e comparação de versões.

A API pública do GitHub (``/repos/{repo}/releases/latest``) não exige
autenticação e retorna os assets da release. Daqui derivamos ``ReleaseInfo``
com todos os dados que a UI precisa (versão, notas, tamanho, URLs).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import requests

from ..version import (
    CHECKSUM_ASSET,
    INSTALLER_ASSET,
    SIGNATURE_ASSET,
    UPDATE_REPO,
    UPDATE_USER_AGENT,
)

_TIMEOUT = (5, 20)  # (connect, read) segundos


@dataclass(frozen=True)
class ReleaseInfo:
    """Metadados da release mais recente disponível."""

    version: str
    notes: str
    published_at: Optional[str]
    asset_name: str
    asset_size: Optional[int]
    installer_url: str
    checksum_url: str
    signature_url: str


def parse_version(value: str) -> tuple[int, ...]:
    """Normaliza 'v1.2.3-beta' -> (1, 2, 3); devolve tupla comparável."""
    match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", value)
    if not match:
        return (0,)
    return tuple(int(g) for g in match.groups() if g is not None)


def compare_versions(a: str, b: str) -> int:
    """-1 se a < b, 0 se igual, +1 se a > b (comparação semântica)."""
    va, vb = parse_version(a), parse_version(b)
    for x, y in zip(va, vb):
        if x != y:
            return -1 if x < y else 1
    if len(va) == len(vb):
        return 0
    return -1 if len(va) < len(vb) else 1


class UpdateError(RuntimeError):
    """Erro de atualização com mensagem amigável para o usuário."""


def _latest_release(repo: str) -> Optional[dict]:
    """Consulta ``releases/latest``; None em 404 (sem releases)."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    resp = requests.get(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": UPDATE_USER_AGENT},
        timeout=_TIMEOUT,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _find_asset(release: dict, name: str) -> Optional[dict]:
    return next((a for a in release.get("assets", []) if a.get("name") == name), None)


def fetch_latest_release(repo: str = UPDATE_REPO) -> Optional[ReleaseInfo]:
    """Retorna ``ReleaseInfo`` da release mais recente (None se não houver).

    Levanta ``UpdateError`` para falhas de rede/HTTP (para a UI silenciar).
    """
    try:
        release = _latest_release(repo)
    except requests.RequestException as exc:
        raise UpdateError("Não foi possível consultar atualizações (rede).") from exc
    if release is None:
        return None

    installer = _find_asset(release, INSTALLER_ASSET)
    checksum = _find_asset(release, CHECKSUM_ASSET)
    signature = _find_asset(release, SIGNATURE_ASSET)
    if installer is None or checksum is None or signature is None:
        # Release incompleta não é candidata a atualização.
        return None

    published = release.get("published_at")
    return ReleaseInfo(
        version=release.get("tag_name", "unknown"),
        notes=release.get("body", "") or "",
        published_at=published,
        asset_name=installer["name"],
        asset_size=installer.get("size"),
        installer_url=installer["browser_download_url"],
        checksum_url=checksum["browser_download_url"],
        signature_url=signature["browser_download_url"],
    )


def check_for_update(
    current_version: str,
    repo: str = UPDATE_REPO,
) -> Optional[ReleaseInfo]:
    """Compara a versão local com a release mais recente.

    Retorna ``ReleaseInfo`` se houver versão mais nova; ``None`` caso
    contrário. ``UpdateError`` em falha de consulta.
    """
    latest = fetch_latest_release(repo)
    if latest is None:
        return None
    if compare_versions(latest.version, current_version) <= 0:
        return None
    return latest
