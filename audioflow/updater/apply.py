"""Aplicação da atualização: substituição segura do executável + reinício.

O Windows bloqueia mover um EXE em execução, então usamos um script em lote
pequeno (``apply_update.bat``) disparado em segundo plano: ele aguarda o app
fechar, tenta substituir o executável (com limite de tentativas), opcionalmente
relança o novo app e se remove. Falhas deixam o arquivo pendente para o
próximo boot (``apply_pending_update`` re-tenta).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from .verify import CHECKSUM_FILE, SIGNATURE_FILE, UpdateError, sha256_file, verify_installer

_CREATE_NO_WINDOW = 0x08000000
_WAIT_TRIES = 90          # ~90s aguardando o processo atual encerrar
_MOVE_TRIES = 20          # ~20s para a troca do arquivo

_BAT_TEMPLATE = r"""@echo off
setlocal
set "SRC=%~1"
set "DST=%~2"
set "RESTART=%~3"
set "LOG=%~dp0apply.log"
echo [%date% %time%] Aplicando atualizacao > "%LOG%"
set /a n=0
:wait
tasklist /FI "IMAGENAME eq %~nx2" 2>NUL | find /I "%~nx2" >NUL
if %errorlevel% equ 0 (
    set /a n+=1
    if %n% GEQ %_WAIT_TRIES% (
        echo [%date% %time%] Abortado: processo ainda em execucao >> "%LOG%"
        exit /b 1
    )
    ping -n 2 127.0.0.1 >NUL
    goto wait
)
set /a m=0
:retry
move /Y "%SRC%" "%DST%" >> "%LOG%" 2>&1
if %errorlevel% neq 0 (
    set /a m+=1
    if %m% GEQ %_MOVE_TRIES% (
        echo [%date% %time%] Falha ao substituir %~nx2 >> "%LOG%"
        exit /b 2
    )
    ping -n 2 127.0.0.1 >NUL
    goto retry
)
echo [%date% %time%] Substituicao concluida >> "%LOG%"
del /Q "%~dp0AudioFlow.sha256" "%~dp0AudioFlow.sig" >> "%LOG%" 2>&1
if /I "%RESTART%"=="1" (
    start "" "%DST%"
)
del /Q "%~f0" >> "%LOG%" 2>&1
exit /b 0
"""


def update_dir() -> Path:
    """Diretório de atualizações do app (%LOCALAPPDATA%\\AudioFlow\\update)."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "AudioFlow" / "update"


def pending_exe() -> Path:
    return update_dir() / "AudioFlow.exe"


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def apply_update(installer: Path, restart: bool = True) -> None:
    """Dispara a substituição do executável em segundo plano.

    Deve ser chamado com o app prestes a fechar. Em modo de desenvolvimento
    (``python gui.py``) levanta ``UpdateError``.
    """
    if not _is_frozen():
        raise UpdateError(
            "Executando em modo de desenvolvimento: aplique a atualização "
            "manualmente."
        )
    installer = Path(installer).resolve()
    dst = Path(sys.executable).resolve()

    if installer == dst:
        # Já somos a versão nova: basta relançar.
        if restart:
            subprocess.Popen(f'cmd /c start "" "{dst}"', creationflags=_CREATE_NO_WINDOW)
        return

    target_dir = installer.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    bat = target_dir / "apply_update.bat"

    body = _BAT_TEMPLATE.replace("%_WAIT_TRIES%", str(_WAIT_TRIES)).replace(
        "%_MOVE_TRIES%", str(_MOVE_TRIES)
    )
    bat.write_text(body, encoding="ascii", errors="replace")

    flag = "1" if restart else "0"
    subprocess.Popen(
        f'cmd /c ""{bat}" "{installer}" "{dst}" "{flag}""',
        creationflags=_CREATE_NO_WINDOW,
    )


def apply_pending_update(restart: bool = True) -> bool:
    """Aplica uma atualização já baixada no boot anterior, se houver.

    Re-verifica assinatura e checksum antes de aplicar; arquivos inválidos
    são removidos para evitar loops. Retorna True quando há atualização
    pendente (o app deve fechar logo em seguida).
    """
    if not _is_frozen():
        return False

    target_dir = update_dir()
    installer = target_dir / "AudioFlow.exe"
    checksum = target_dir / CHECKSUM_FILE
    signature = target_dir / SIGNATURE_FILE
    if not (installer.exists() and checksum.exists() and signature.exists()):
        return False

    try:
        verify_installer(installer, checksum, signature)
    except UpdateError:
        for path in (installer, checksum, signature):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return False

    running = Path(sys.executable).resolve()
    if running.exists() and sha256_file(installer) == sha256_file(running):
        # Já estamos executando a versão nova: apenas limpa os pendentes.
        for path in (installer, checksum, signature):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return False

    apply_update(installer, restart=restart)
    return True


def wait_for_pending_install(timeout: float = 5.0) -> bool:
    """Aguarda o instalador pendente ser aplicado (helper de teste/CLI)."""
    pending = pending_exe()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pending.exists():
            return True
        time.sleep(0.2)
    return False
