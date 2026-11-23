"""Verificação de integridade e autenticidade dos instaladores.

O instalador publicado é acompanhado de dois assets:

* ``AudioFlow.sha256`` — ``<sha256 hex>  AudioFlow.exe``
* ``AudioFlow.sig``    — assinatura Ed25519 (raw) sobre o conteúdo de ``AudioFlow.sha256``

O app baixa os três, confere a assinatura com a chave pública embutida e então
valida o SHA-256 do executável antes de qualquer aplicação.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .keys import PUBLIC_KEY_PEM

# Nomes locais usados durante o download/instalação (veja apply.py).
CHECKSUM_FILE = "AudioFlow.sha256"
SIGNATURE_FILE = "AudioFlow.sig"


class UpdateError(RuntimeError):
    """Erro de atualização com mensagem amigável para o usuário."""


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    """SHA-256 hex-minúsculo do conteúdo do arquivo."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_checksum_file(text: str) -> str:
    """Extrai o hex do SHA-256 de um arquivo estilo ``sha256sum``."""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if not parts:
            continue
        digest = parts[0].lower()
        if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
            return digest
    raise UpdateError("Arquivo de checksum inválido ou vazio.")


def _public_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey,
    )

    raw = serialization.load_pem_public_key(
        PUBLIC_KEY_PEM.encode("ascii"),
        backend=None,  # type: ignore[arg-type]
    )
    if not isinstance(raw, Ed25519PublicKey):
        raise UpdateError("Chave pública de assinatura inesperada.")
    return raw


def verify_signature(signature_bytes: bytes, data_bytes: bytes) -> None:
    """Levanta ``UpdateError`` se a assinatura não confere com os dados."""
    try:
        _public_key().verify(signature_bytes, data_bytes)
    except Exception as exc:  # BadSignature ou payload inválido
        raise UpdateError("Assinatura da atualização não confere.") from exc


def verify_installer(
    installer: Path,
    checksum_path: Path,
    signature_path: Path,
) -> None:
    """Valida assinatura + SHA-256 de um instalador baixado.

    Arquivos de verificação podem ser os assets baixados (``.sha256``/``.sig``)
    ou os arquivos pendentes (``.new.sha256``/``.new.sig``).
    """
    checksum_text = checksum_path.read_text(encoding="utf-8", errors="strict")
    signature = signature_path.read_bytes()

    verify_signature(signature, checksum_text.encode("utf-8"))

    expected = _parse_checksum_file(checksum_text)
    actual = sha256_file(installer)
    if actual != expected:
        raise UpdateError(
            "Checksum do instalador não confere (arquivo corrompido ou "
            "adulterado)."
        )
