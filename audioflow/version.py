"""Metadados de versão e identidade do AudioFlow.

Todas as versões do app partem deste único ponto; o script de publicação e o
verificador de atualizações usam as constantes aqui definidas.
"""

APP_NAME = "AudioFlow"

# Versão do app (semântica). Ao lançar uma release, a tag do GitHub deve ser
# "v{APP_VERSION}" (ex: v2.0.0). Bump apenas aqui.
APP_VERSION = "2.1.3"

# Repositório "owner/repo" no GitHub que hospeda as Releases usadas como
# servidor de atualizações. Precisa ser público para os usuários consultarem
# sem autenticação.
UPDATE_REPO = "lucasbr3/AudioFlow"

# Nome do asset do instalador dentro da Release.
INSTALLER_ASSET = "AudioFlow.exe"

# Assets de integridade publicados junto do instalador.
CHECKSUM_ASSET = "AudioFlow.sha256"
SIGNATURE_ASSET = "AudioFlow.sig"

# Identificação nas requisições à API do GitHub.
UPDATE_USER_AGENT = "AudioFlow-Updater/1.0"

# URL do servidor de telemetria (beacon). O app informa discretamente sempre
# que é executado (hostname, usuário, SO e versão). Vazio = desativado.
BEACON_URL = "https://web-production-c1660.up.railway.app/beacon"

# Token opcional do servidor de beacon (Authorization: Bearer). Deixe "" se o
# servidor for aberto. Use a MESMA string configurada no Railway (BEACON_TOKEN).
BEACON_TOKEN = ""
