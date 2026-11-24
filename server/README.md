# AudioFlow Web — API (Render) + site (Cloudflare Pages)

Backend que roda o mesmo motor do app desktop (`audioflow/core.py`) atrás de
uma API HTTP, feito para o plano **free do Render** e para um frontend
estático no **Cloudflare Pages**.

```
Cloudflare Pages  ──/api/call, /api/events──▶  Render (FastAPI + yt-dlp)
      (site)                                      │
                                                  └─ /tmp/audioflow/<sessão>/<job>
                                                     arquivos expiram em 3 min
```

## Por que sem ffmpeg?

O plano free do Render tem **512 MB de RAM e 0.1 CPU**. Transcodificar MP3
nessa máquina é lento e arrisca estourar a memória. Então a imagem Docker
**não instala ffmpeg** e o motor entrega o áudio nativo do YouTube (M4A na
maioria dos casos, às vezes WebM/Opus). Isso também deixa o download bem mais
rápido.

Quer MP3/FLAC/WAV? Só num plano pago (ou num VPS), instalando ffmpeg e
removendo `AUDIOFLOW_DISABLE_FFMPEG`.

## Rodar local

```powershell
python -m venv .venv-web
.\.venv-web\Scripts\pip install -r server\api\requirements.txt
.\.venv-web\Scripts\python -m uvicorn server.api.app:app --reload --port 8000
```

Abra `http://127.0.0.1:8000` — o próprio FastAPI serve o frontend de
`audioflow/web`, então dá para testar tudo sem Cloudflare.

## Deploy no Render (free)

### Opção A — Blueprint (recomendado)

1. Suba estas mudanças no GitHub.
2. No Render: **New → Blueprint** → selecione o repositório.
3. O `render.yaml` da raiz cria o serviço `audioflow-api` já em Docker.
4. Espere o build e copie a URL (ex.: `https://audioflow-api.onrender.com`).
5. Teste: `https://SUA-URL.onrender.com/api/health`.

### Opção B — manual

1. **New → Web Service** → repositório.
2. Runtime: **Docker**.
3. Dockerfile: `server/Dockerfile`.
4. Docker context: `.` (raiz do repo).
5. Health check: `/api/health`.
6. Plano: **Free**.

> Se o Render detectar Python sozinho, ele vai usar o `Procfile` antigo (beacon)
> e não a API. Nesse caso force o runtime **Docker** ou use o Blueprint.

### Variáveis de ambiente

| Variável | Padrão | Para que serve |
|---|---|---|
| `AUDIOFLOW_DISABLE_FFMPEG` | `1` no `render.yaml` | entrega áudio nativo, sem transcode |
| `AUDIOFLOW_MAX_TRACKS` | `50` | limite de faixas por link |
| `AUDIOFLOW_MAX_QUEUE` | `10` | downloads esperando + rodando |
| `AUDIOFLOW_JOBS_PER_HOUR` | `5` | cota por IP (`0` = sem limite) |
| `AUDIOFLOW_MAX_CONCURRENT` | `1` | downloads simultâneos |
| `AUDIOFLOW_MAX_SESSIONS` | `500` | teto de sessões em memória |
| `AUDIOFLOW_FILE_TTL_MIN` | `3` | tempo até apagar os arquivos |
| `AUDIOFLOW_ALLOWED_ORIGINS` | `*` | origem(ns) do site, separadas por vírgula |
| `AUDIOFLOW_API_TOKEN` | vazio | se definido, exige `Authorization: Bearer ...` |

## Site no Cloudflare Pages

1. **Workers & Pages → Create → Pages → Connect to Git**.
2. Build command: **vazio**.
3. Build output directory: `audioflow/web`.
4. Deploy.
5. Edite `audioflow/web/config.js` antes do deploy:

```js
window.AUDIOFLOW_API_BASE = "https://audioflow-api.onrender.com";
window.AUDIOFLOW_API_TOKEN = ""; // só se você definiu AUDIOFLOW_API_TOKEN
```

6. No Render, troque `AUDIOFLOW_ALLOWED_ORIGINS` para a URL do Pages
   (ex.: `https://audioflow.pages.dev`). Deixar `*` funciona, mas é mais aberto.

Também dá para publicar por linha de comando:

```powershell
npx wrangler pages deploy audioflow/web --project-name audioflow
```

## O que o Render free faz com você

- **Dorme após 15 min sem acesso.** A primeira visita depois disso leva
  ~30–60 s. Para manter acordado, use um monitor gratuito (UptimeRobot,
  cron-job.org) batendo em `/api/health` a cada 10 min. O plano free dá
  ~750 h/mês, então um serviço 24/7 cabe (744 h), mas sem margem para outro.
- **Sem disco persistente.** Os arquivos vivem em `/tmp` e somem no restart.
  É por isso que o link expira em 30 min.
- **IP de datacenter.** O YouTube bloqueia muito IP de Render com
  `Sign in to confirm you're not a bot`. O código já tenta variantes de
  `player_client`, mas não tem garantia. Isso não é bug do site: é a limitação
  do plano gratuito.
- **512 MB / 0.1 CPU.** Por isso 1 download por vez e limite de 50 faixas.

## Aviso

Site público de download de música tem dois riscos reais: **direitos autorais**
(DMCA) e **termos de uso** do Render/YouTube. O Render pode derrubar o serviço
por reclamação. Para uso pessoal/entrega de trabalho, mantenha o acesso
restrito (Cloudflare Access ou `AUDIOFLOW_API_TOKEN`) em vez de aberto ao
público.

## Endpoints

| Método | Rota | Descrição |
|---|---|---|
| `GET` | `/api/health` | status e fila |
| `POST` | `/api/call` | `{method, args}` — mesma interface do app desktop |
| `GET` | `/api/events?since=N` | eventos da sessão (polling) |
| `GET` | `/api/files/{sid}/{job}/{caminho}` | baixa um arquivo |
| `GET` | `/api/zip/{sid}/{job}` | baixa tudo em `.zip` |

A sessão vai no header `X-AudioFlow-Session` (UUID gerado no navegador). Não
há cookie, então o Pages pode chamar o Render sem configuração de credenciais.
