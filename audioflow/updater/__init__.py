"""Sistema de atualização automática do AudioFlow.

O app consulta as Releases do GitHub (``UPDATE_REPO`` em ``audioflow/version.py``)
e compara com a versão local. Quando há novidade, baixa o instalador com
barra de progresso e verifica a autenticidade (assinatura Ed25519 + SHA-256)
antes de aplicar.

Fluxo:
    1. ``check_for_update()``  -> ReleaseInfo | None
    2. ``download_update()``   -> caminho do instalador verificado
    3. ``apply_update()``      -> substitui o executável e reinicia
    4. ``apply_pending_update()`` -> aplica no próximo boot, se pendente

Qualquer falha de rede, checksum ou assinatura levanta ``UpdateError`` com
mensagem legível; o app apenas ignora e segue a vida normal.
"""

from __future__ import annotations

from .apply import apply_pending_update, apply_update, pending_exe, update_dir
from .download import download_update
from .keys import PUBLIC_KEY_PEM
from .manifest import ReleaseInfo, check_for_update
from .verify import UpdateError, verify_installer

__all__ = [
    "PUBLIC_KEY_PEM",
    "ReleaseInfo",
    "UpdateError",
    "apply_pending_update",
    "apply_update",
    "check_for_update",
    "download_update",
    "pending_exe",
    "update_dir",
    "verify_installer",
]
