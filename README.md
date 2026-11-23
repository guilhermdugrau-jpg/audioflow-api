# AudioFlow

Baixe playlists, álbuns e faixas do **Spotify** como áudio local (MP3, M4A,
Opus, FLAC ou WAV), com tags, capa embutida e interface premium em
WebView2. Entregue como um único executável para Windows.

> **Uso responsável:** baixe apenas conteúdo que você tem direito de possuir.

> **Versão web:** a API para o site (Cloudflare Pages + Render) está em
> [`server/README.md`](server/README.md).

## Recursos

- **Mecânica de download**: metadados do Spotify via embed público, matching
  estrito (título + artista + duração) com yt-dlp, transcodificação via ffmpeg,
  tags e capa com Mutagen.
- **Filas em paralelo**: 4 workers com manifesto de retomada
  (`.audioflow-manifest.jsonl`) — um download interrompido não começa do zero.
- **Duas faces num binário**: GUI (`AudioFlow.exe`) ou CLI
  (`AudioFlow.exe download <url>`).
- **Auto Update**: verifica no boot se há release nova no GitHub, baixa com
  barra de progresso e velocidade, valida **assinatura Ed25519 + SHA-256** e
  substitui o executável com reinício automático.

## Estrutura

```
audioflow/
  core.py          # motor de download (filas, retomada, transcodificação)
  spotify_api.py   # cliente dos metadados do Spotify + normalização de nome
  cli.py           # interface de terminal
  gui.py           # host pywebview + servidor HTTP local do frontend
  updater/         # verificação, download, assinatura e aplicação de updates
  web/             # frontend HTML/CSS/JS
  version.py       # versão central e repositório de updates
scripts/
  gen_keys.py          # gera o par de chaves de assinatura
  publish_release.py   # build + assinatura + release no GitHub
.github/workflows/     # release automatizada via GitHub Actions
```

## Desenvolvimento

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
# ffmpeg: coloque ffmpeg.exe/ffprobe.exe em bin/  (ou: winget install ffmpeg)
.\.venv\Scripts\python AudioFlow.py                # GUI
.\.venv\Scripts\python -m audioflow.cli doctor     # diagnóstico
```

## Build do executável

```powershell
.\.venv\Scripts\python -m PyInstaller --noconfirm AudioFlow.spec
# -> dist/AudioFlow.exe
```

## Publicar uma nova versão

Veja [`RELEASE.md`](RELEASE.md) — cobre chave de assinatura, release manual,
release via GitHub Actions e o fluxo de atualização pelo app.

## Telemetria de execução (beacon, opcional)

O app pode informar discretamente, sempre que for aberto, que foi executado e
quais as infos básicas do PC (hostname, usuário, SO, versão). Nada é enviado
por padrão: é preciso configurar a URL e rebuildar.

1. **Servidor** — duas opções:

   **No seu PC/VPS:**
   ```powershell
   python server/beacon_server.py --port 8000
   ```
   Dashboard em `http://localhost:8000` (quem abriu o app, quando e de qual IP);
   dados ficam em `beacons.jsonl`. Use `--token SEGREDO` se quiser exigir
   autenticação no `/beacon`.

   **No Railway (recomendado, fica online 24/7):**
   1. No painel do Railway, crie um projeto a partir deste repositório (o
      `Procfile` detecta e inicia o servidor sozinho).
   2. Monte um **Volume** em `/data` (senão os beacons se perdem a cada
      redeploy).
   3. Opcional: adicione a variável `BEACON_TOKEN` com um segredo.
   4. Use a URL pública gerada (ex: `https://audioflow-beacon.up.railway.app`).
      O servidor lê a porta `PORT` que o Railway injeta automaticamente.

2. **App**: defina `BEACON_URL` em `audioflow/version.py` com o endereço do
   servidor (ex: `https://audioflow-beacon.up.railway.app/beacon`) e, se o
   servidor exigir token, `BEACON_TOKEN` com o mesmo segredo. Rebuilda o EXE.
   O envio é silencioso, em thread própria, com timeout — o app nunca trava por
   causa disso e ignora falhas de rede.

## Versão Android (APK)

O APK reaproveita a mesma UI HTML/CSS/JS num WebView nativo. No celular o
frontend fala com o Python por HTTP local (`/api/call` + `/api/events`) em vez
da ponte pywebview, e o download usa m4a/opus de forma nativa (sem ffmpeg).

Como o Buildozer não roda no Windows, o APK é gerado no GitHub Actions:

1. Empurre uma **tag** `v*` (ex: `v2.0.1`) ou rode o workflow manualmente em
   **Actions → Android APK → Run workflow**.
2. Baixe o `audioflow-apk` (artifact) em **Actions → Android APK → build**.
3. Instale no celular (Android 8+, arm64). Os arquivos vão para o diretório do
   app e são registrados no MediaStore (aparecem em "Músicas").

Para buildar localmente (Linux/Mac): instale Buildozer e rode `buildozer -v
android debug`. As especificações estão em `buildozer.spec` e a entrada em
`android/main.py`.

> Diferenças para o celular: sem atualização automática (o updater é do EXE do
> Windows) e sem seletor de pasta (a pasta é fixa no diretório do app).

