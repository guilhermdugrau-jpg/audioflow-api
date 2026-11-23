"""Beacon discreto de execução do AudioFlow.

A cada abertura, o app envia (de forma silenciosa e não bloqueante) um POST
com informações básicas do PC para o servidor configurado em ``BEACON_URL``.
Qualquer falha é engolida — o app funciona normalmente sem rede.

As informações coletadas são básicas (hostname, usuário do sistema, versão do
SO e do app). O IP de origem é visto pelo servidor. Nada aqui lê arquivos,
teclado, câmera ou outros dados do usuário.
"""

from __future__ import annotations

import getpass
import platform
import socket
import time

from .version import APP_VERSION, BEACON_TOKEN, BEACON_URL


def collect_info(window_size: tuple[int, int] | None = None) -> dict:
    """Reúne informações básicas do ambiente para o beacon."""
    return {
        "app_version": APP_VERSION,
        "hostname": socket.gethostname(),
        "username": getpass.getuser(),
        "os": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "machine": platform.machine(),
        "arch": platform.architecture()[0],
        "python": platform.python_version(),
        "tz": next(iter(time.tzname), "") if time.tzname else "",
        "window": list(window_size) if window_size else None,
    }


def send_beacon(
    url: str = BEACON_URL,
    window_size: tuple[int, int] | None = None,
    timeout: float = 4.0,
    token: str = BEACON_TOKEN,
) -> None:
    """Envia o beacon em fire-and-forget. Nunca levanta exceção."""
    if not url:
        return
    try:
        import requests

        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        requests.post(url, json=collect_info(window_size), headers=headers, timeout=timeout)
    except Exception:
        pass  # silencioso por design
