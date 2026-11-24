"""Motor de download do AudioFlow.

Mecânica: metadados vêm do Spotify (spotify_api.py), o áudio é localizado
no YouTube via yt-dlp com correspondência estrita (título + artista +
duração, para não baixar remix/versão errada), transcode para o formato
escolhido com FFmpeg e grava tags + capa com Mutagen.

Sem dependência de GUI: o motor emite eventos por callbacks, então tanto a
interface Tkinter quanto a CLI consomem exatamente a mesma lógica.
"""

from __future__ import annotations

import concurrent.futures
import glob
import json
import logging
import os
import re
import threading
import unicodedata
from collections.abc import Callable

import requests
from yt_dlp import YoutubeDL

from .spotify_api import (
    SpotifyClient,
    SpotifyError,
    TrackInfo,
    cap_filename,
    parse_spotify_url,
    sanitize_filename,
)
from .utils import FORMATS, QUALITIES, SAMPLE_RATES, get_ffmpeg_path
from .watermark import apply_watermark

log = logging.getLogger("audioflow.core")

MANIFEST_FILENAME = ".audioflow-manifest.jsonl"

# Limite de distância de duração Spotify-vs-YouTube para considerar o mesmo
# áudio; e o limite largo para título certo mas duração muito divergente.
DURATION_TOLERANCE_S = 7
DURATION_TOLERANCE_WIDE_S = 30


class _YtdlpLog:
    """Liga o log do yt-dlp ao nosso e guarda o último erro."""

    def __init__(self) -> None:
        self.last_error: str | None = None

    def debug(self, msg):  # noqa: ANN001
        log.debug("yt-dlp: %s", msg)

    info = debug
    warning = debug

    def error(self, msg):  # noqa: ANN001
        self.last_error = msg
        log.debug("yt-dlp error: %s", msg)


class Callbacks:
    """Hooks de progresso/eventos consumidos pela GUI e pela CLI."""

    def __init__(self) -> None:
        self.on_track_start: Callable[[TrackInfo, str], None] | None = None
        self.on_track_done: Callable[[TrackInfo, str], None] | None = None
        self.on_track_skip: Callable[[TrackInfo, str], None] | None = None
        self.on_track_fail: Callable[[TrackInfo, str], None] | None = None
        self.on_warning: Callable[[str], None] | None = None
        self.on_metadata: Callable[[str], None] | None = None
        self.on_finish: Callable[[int, int, int], None] | None = None  # ok, skipped, failed


def _normalize_title(s: str | None) -> str:
    """Minúsculas, sem acentos, sem parênteses/colchetes/feat., só letras."""
    if not s:
        return ""
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"\b(feat\.?|ft\.?)\s+.*$", "", s, flags=re.IGNORECASE)
    s = s.replace("'", "").replace("’", "")
    s = "".join(ch if (ch.isspace() or unicodedata.category(ch)[0] in "LNM") else " " for ch in s)
    return re.sub(r"\s+", " ", s).strip()


def _spotify_core(s: str | None) -> str:
    """Remove o sufixo ` - Variante` que o Spotify adiciona aos títulos."""
    if not s:
        return ""
    return s.split(" - ", 1)[0]


def _title_matches(yt_title: str | None, expected: str | None) -> bool:
    yt = _normalize_title(yt_title)
    target = _normalize_title(_spotify_core(expected))
    if not target or not yt:
        return False
    if len(target) >= 4:
        return target in yt
    return target in yt.split()


class DownloadEngine:
    """Baixa playlists/álbuns/faixas do Spotify como áudio local com tags."""

    MAX_WORKERS = 4

    def __init__(
        self,
        *,
        audio_format: str = "mp3",
        audio_quality: str = "192",
        sample_rate: str = "auto",
        include_track_number: bool = False,
        artist_first: bool = False,
        loose_match: bool = False,
        max_workers: int | None = None,
        session: requests.Session | None = None,
        watermark: dict | None = None,
    ) -> None:
        self.audio_format = audio_format if audio_format in FORMATS else "mp3"
        self.audio_quality = audio_quality if audio_quality in QUALITIES else "192"
        self.sample_rate = sample_rate if sample_rate in SAMPLE_RATES else "auto"
        self.include_track_number = bool(include_track_number)
        self.artist_first = bool(artist_first)
        self.loose_match = bool(loose_match)
        self.max_workers = max_workers or self.MAX_WORKERS
        self.session = session or requests.Session()
        self.client = SpotifyClient(session=self.session)
        self.cb = Callbacks()
        self._cancel = threading.Event()
        self._failed: list[str] = []
        self._lock = threading.Lock()
        self.watermark = watermark or {}

    # --- cancelamento ------------------------------------------------------

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def _reset(self) -> None:
        self._cancel.clear()
        with self._lock:
            self._failed.clear()

    # --- nomes de arquivo --------------------------------------------------

    def _name_parts(self, title: str, artists: str) -> tuple[str, str]:
        t, a = sanitize_filename(title), sanitize_filename(artists)
        return (a, t) if self.artist_first else (t, a)

    def _build_path(self, folder: str, track: TrackInfo, track_num: int) -> str:
        first, second = self._name_parts(track.title, track.artists)
        if self.include_track_number:
            name = f"{track_num:02d}. {first} - {second}.{self.audio_format}"
        else:
            name = f"{first} - {second}.{self.audio_format}"
        return os.path.join(folder, cap_filename(name))

    # --- correspondência no YouTube ----------------------------------------

    def _search_candidates(self, query: str) -> list[dict]:
        base = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": True,
            "ignoreerrors": True,
            "retries": 5,
            "socket_timeout": 15,
        }
        # Tenta o client web padrão e, se o YouTube bloquear ("bot"), tenta os
        # clients alternativos que costumam continuar servindo.
        variants = [
            dict(base),
            {**base, "extractor_args": {"youtube": {"player_client": ["android", "ios", "tv"]}}},
        ]
        for opts in variants:
            try:
                with YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(query, download=False)
            except Exception as exc:
                log.warning("busca no youtube falhou (%s): %s", type(exc).__name__, str(exc)[:200])
                continue
            entries = [e for e in (info or {}).get("entries", []) if e and e.get("id")]
            if entries:
                return entries
        log.warning("busca no youtube retornou 0 resultados para %r (bot-block/região?)", query)
        return []

    def _pick_video(
        self, candidates: list[dict], track: TrackInfo
    ) -> str | None:
        """Escolhe o vídeo do YouTube que realmente corresponde à faixa."""
        expected_dur = (track.duration_ms or 0) / 1000 if track.duration_ms else None

        title_ok = [e for e in candidates if _title_matches(e.get("title"), track.title)]
        if not title_ok:
            if self.loose_match:
                return self._loose_pick(candidates, expected_dur)
            return None

        pool = title_ok
        if track.artists:
            tokens = re.split(r"[,&]+|\s+(?:feat\.?|ft\.?)\s+", track.artists, flags=re.IGNORECASE)
            norm_tokens = [t for t in (_normalize_title(t) for t in tokens) if t]
            if norm_tokens:
                artist_pool = [
                    e for e in title_ok
                    if any(tok in _normalize_title(e.get("title") or "") for tok in norm_tokens)
                ]
                pool = artist_pool if artist_pool else title_ok
                if not artist_pool:
                    log.info("filtro de artista rejeitou todos (artists=%r)", track.artists)

        if not pool:
            if self.loose_match:
                return self._loose_pick(title_ok, expected_dur)
            return None

        chosen = pool[0]
        timed = [e for e in pool if e.get("duration")]
        if timed:
            chosen = min(timed, key=lambda e: abs(int(e["duration"]) - expected_dur))
            off = abs(int(chosen["duration"]) - expected_dur)
            if off > DURATION_TOLERANCE_WIDE_S:
                log.info("candidato mais próximo divergiu %.0fs na duração; rejeitado", off)
                return None
        log.debug("selecionado youtube %s", chosen["id"])
        return f"https://www.youtube.com/watch?v={chosen['id']}"

    def _loose_pick(self, candidates: list[dict], expected_dur: float | None) -> str | None:
        if not candidates:
            return None
        chosen = candidates[0]
        if expected_dur:
            timed = [e for e in candidates if e.get("duration")]
            if timed:
                chosen = min(timed, key=lambda e: abs(int(e["duration"]) - expected_dur))
        log.warning("loose match: selecionado %s", chosen["id"])
        return f"https://www.youtube.com/watch?v={chosen['id']}"

    def _download_audio(self, video_url: str, dest_path: str) -> str | None:
        """Baixa o áudio do vídeo e transcode com ffmpeg.

        Retorna o caminho do arquivo gerado (str) ou None em caso de falha.
        Sem ffmpeg (Android) baixa o áudio nativo (m4a/webm/opus) sem converter.
        """
        ffmpeg = get_ffmpeg_path()
        native = ffmpeg is None

        fmt = self.audio_format
        base, _ = os.path.splitext(dest_path)

        # Cookies opcionais (JSON do Chrome/Edge exportado via extensão)
        cookies_file = os.environ.get("YTDLP_COOKIES_FILE")
        if cookies_file and os.path.exists(cookies_file):
            opts["cookiefile"] = cookies_file
        # Cookies via variável de ambiente (conteúdo do cookies.txt)
        cookies_text = os.environ.get("YTDLP_COOKIES")
        if cookies_text:
            import tempfile
            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as cf:
                cf.write(cookies_text)
                cookies_file = cf.name
                opts["cookiefile"] = cookies_file

        opts: dict = {
            "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio[ext=mp4]/bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "outtmpl": base + ".%(ext)s",
            "retries": 5,
            "socket_timeout": 30,
            "concurrent_fragment_downloads": 4,
            "ignoreerrors": True,
            "ignore_no_formats_error": True,
            "logger": _YtdlpLog(),
            # Anti-detecção: headers de navegador real
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate",
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            },
            # Reduz throttling
            "throttled_rate": "100K",
            "sleep_interval": 1,
            "max_sleep_interval": 3,
        }
        try:
            import certifi

            opts["cafile"] = certifi.where()
        except Exception:
            pass

        if ffmpeg:
            opts["ffmpeg_location"] = ffmpeg
            post = {"key": "FFmpegExtractAudio", "preferredcodec": fmt}
            if fmt in ("mp3", "m4a", "opus"):
                post["preferredquality"] = self.audio_quality
            opts["postprocessors"] = [post]
            if self.sample_rate != "auto" and fmt in ("mp3", "flac", "wav"):
                opts["postprocessor_args"] = {"extractaudio": ["-ar", self.sample_rate]}

        expected = base + "." + fmt

        # Múltiplas estratégias de player_client (ordem: mais resistentes primeiro)
        player_clients = [
            ["web"],                    # Mais resistente a bot detection
            ["android", "web"],         # Combo comum
            ["ios", "web"],             # iOS + web
            ["tv", "web"],              # Smart TV + web
            ["android", "ios", "tv"],   # Fallback original
        ]

        # Estratégias de extractor_args
        extractor_strategies = []
        for pc in player_clients:
            extractor_strategies.append({
                "youtube": {
                    "player_client": pc,
                    "player_skip": ["configs", "webpage"],  # Pula algumas verificações
                    "skip": ["hls", "dash"],  # Evita formatos problemáticos
                }
            })

        # Variantes: cada estratégia de player + fallback sem extractor_args
        variants = [opts]
        for strat in extractor_strategies:
            variants.append({**opts, "extractor_args": strat})

        # Fallback: Invidious instances (usando extractor_args correto)
        invidious_instances = [
            "https://yewtu.be",
            "https://invidious.snopyta.org",
            "https://invidious.projectsegfau.lt",
        ]
        for inst in invidious_instances:
            variants.append({
                **opts,
                "extractor_args": {
                    "youtube": {
                        "player_client": ["web"],
                        "invidious_instance": inst,
                    }
                },
            })

        # Fallback: Piped instances (extractor correto)
        piped_instances = [
            "https://piped.video",
            "https://piped.kavin.rocks",
            "https://piped.adminforge.de",
        ]
        for inst in piped_instances:
            variants.append({
                **opts,
                "extractor_args": {
                    "youtube": {
                        "player_client": ["web"],
                        "piped_instance": inst,
                    }
                },
            })

        # Geo-bypass: tenta diferentes países (apenas os principais)
        for country in ["US", "BR"]:
            variants.append({
                **opts,
                "extractor_args": {"youtube": {"player_client": ["web"]}},
                "geo_bypass": True,
                "geo_bypass_country": country,
            })

        # Estratégia: embed player (mais leve, menos verificação)
        variants.append({
            **opts,
            "extractor_args": {
                "youtube": {
                    "player_client": ["web_embedded"],
                    "player_skip": ["configs", "webpage"],
                    "skip": ["hls", "dash"],
                }
            },
        })

        # Estratégia: mweb (mobile web) - às vezes menos restrito
        variants.append({
            **opts,
            "extractor_args": {
                "youtube": {
                    "player_client": ["mweb"],
                    "player_skip": ["configs", "webpage"],
                    "skip": ["hls", "dash"],
                }
            },
        })

        # Fallback final: formato mais flexível (ignora erros de formato)
        fallback_opts = {**opts}
        fallback_opts["format"] = "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio[ext=mp4]/bestaudio"
        fallback_opts["ignore_no_formats_error"] = True
        variants.append(fallback_opts)

        def _produced() -> str | None:
            if ffmpeg:
                return expected if os.path.exists(expected) else None
            hits = [p for p in glob.glob(base + ".*")
                    if not p.endswith((".part", ".ytdl"))]
            return max(hits, key=os.path.getmtime) if hits else None

        try:
            for i, variant in enumerate(variants):
                ytlog = _YtdlpLog()
                try:
                    with YoutubeDL({**variant, "logger": ytlog}) as ydl:
                        ydl.extract_info(video_url, download=True)
                except Exception as exc:
                    log.warning("variante %d falhou para %s: %s", i, video_url, str(exc)[:150])
                    continue
                produced = _produced()
                if produced:
                    return produced
                reason = (ytlog.last_error or "sem erro relatado pelo yt-dlp").strip()
                log.warning("tentativa %d sem arquivo para %s: %s", i, video_url, reason[:200])
        except Exception as exc:
            log.warning("download falhou para %s: %s", video_url, str(exc)[:200])
        return None

    # --- faixa única -------------------------------------------------------

    def _download_one(
        self, track: TrackInfo, folder: str, track_num: int, default_cover: str | None = None
    ) -> tuple[str, str]:
        """Baixa uma faixa. Retorna ('ok'|'skip'|'fail', path_ou_motivo)."""
        if self.cancelled:
            return ("fail", "cancelado")
        path = self._build_path(folder, track, track_num)
        if os.path.exists(path):
            return ("skip", path)

        if self.cb.on_track_start:
            self.cb.on_track_start(track, path)

        # Enriquecimento de capa: playlist embed não tem capa por faixa, então
        # prioriza a capa individual (busca no embed da faixa quando faltar) e
        # só usa a capa da coleção como último recurso — toda faixa sai com
        # capa mesmo quando o enriquecimento individual falhar (rate limit,
        # rede) ou quando a coleção nem tiver capa.
        cover = track.cover_url
        release_date = track.release_date
        if not cover and track.id and not self.cancelled:
            try:
                enriched = self.client.get_track(track.id)
                cover = cover or enriched.cover_url
                release_date = release_date or enriched.release_date
            except SpotifyError:
                pass
        cover = cover or default_cover

        query = f"ytsearch5:{track.title} {track.artists} audio"
        candidates = self._search_candidates(query)
        if not candidates:
            return ("fail", "não encontrado no YouTube (bot-block/região?)")
        video = self._pick_video(candidates, track)
        if not video:
            return ("fail", "nenhuma correspondência confiável no YouTube")

        produced = self._download_audio(video, path)
        if not produced:
            return ("fail", "sem áudio jogável para esta faixa")

        if os.path.splitext(produced)[1].lower().lstrip(".") in ("mp3", "m4a", "flac"):
            self._write_tags(produced, {
                "title": track.title,
                "artists": track.artists,
                "album": track.album or "",
                "year": (release_date or "")[:4],
                "cover": cover or "",
                "trackNumber": track_num,
            })

        self._apply_watermark(produced)
        return ("ok", produced)

    def _apply_watermark(self, path: str) -> None:
        """Aplica a assinatura de áudio configurada (não faz nada se desativada)."""
        wm = self.watermark or {}
        if not wm.get("enabled"):
            return
        if self.cancelled or not os.path.exists(path):
            return
        try:
            ok = apply_watermark(
                path,
                text=str(wm.get("text") or "LJCD"),
                voice_source=str(wm.get("voice") or "tts"),
                position=str(wm.get("position") or "middle"),
                volume=float(wm.get("volume") or 0.3),
                effects=wm.get("effects") or [],
                repeat_every=float(wm.get("repeat_every") or 0.0),
                repeat_count=int(wm.get("repeat_count") or 0),
            )
            if not ok:
                log.warning("assinatura de áudio não aplicada em %s", os.path.basename(path))
        except Exception:
            log.exception("falha na assinatura de áudio de %s", os.path.basename(path))

    # --- tags + capa -------------------------------------------------------

    @staticmethod
    def _fetch_bytes(url: str) -> bytes | None:
        if not url:
            return None
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200 and resp.content:
                return resp.content
        except Exception:
            pass
        return None

    @staticmethod
    def _detect_mime(data: bytes) -> str:
        if data[:4] == b"\x89PNG":
            return "image/png"
        if data[:3] == b"GIF":
            return "image/gif"
        return "image/jpeg"

    def _write_tags(self, path: str, meta: dict) -> None:
        """Grava tags + capa com Mutagen, conforme a extensão do arquivo."""
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        if ext == "opus":
            log.info("opus sem suporte a capa/tags; pulando")
            return
        cover = self._fetch_bytes(meta.get("cover", ""))
        try:
            if ext == "mp3":
                from mutagen.easyid3 import EasyID3
                from mutagen.id3 import APIC, ID3

                try:
                    tags = EasyID3(path)
                except Exception:
                    tags = EasyID3()
                tags["title"] = meta["title"]
                tags["artist"] = meta["artists"]
                tags["album"] = meta["album"]
                tags["tracknumber"] = str(meta.get("trackNumber") or "")
                if meta.get("year"):
                    tags["date"] = meta["year"]
                tags.save(path)
                if cover:
                    audio = ID3(path)
                    audio.delall("APIC")
                    audio.add(APIC(encoding=3, mime=self._detect_mime(cover), type=3, desc="Cover", data=cover))
                    audio.save(path)
            elif ext == "m4a":
                from mutagen.mp4 import MP4, MP4Cover

                tags = MP4(path)
                tags["\xa9nam"] = [meta["title"]]
                tags["\xa9ART"] = [meta["artists"]]
                tags["\xa9alb"] = [meta["album"]]
                tags["trkn"] = [(int(meta.get("trackNumber") or 0), 0)]
                if meta.get("year"):
                    tags["\xa9day"] = [meta["year"]]
                if cover:
                    tags["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_PNG if cover[:4] == b"\x89PNG" else MP4Cover.FORMAT_JPEG)]
                tags.save(path)
            elif ext == "flac":
                from mutagen.flac import FLAC, Picture

                tags = FLAC(path)
                tags["title"] = meta["title"]
                tags["artist"] = meta["artists"]
                tags["album"] = meta["album"]
                tags["tracknumber"] = str(meta.get("trackNumber") or "")
                if meta.get("year"):
                    tags["date"] = meta["year"]
                if cover:
                    pic = Picture()
                    pic.type = 3
                    pic.mime = self._detect_mime(cover)
                    pic.data = cover
                    tags.add_picture(pic)
                tags.save(path)
            else:
                log.info("extensão %s sem suporte a tags; pulando", ext)
        except Exception:
            log.exception("falha ao gravar tags em %s", path)

    # --- orquestração ------------------------------------------------------

    def _write_manifest(self, folder: str, track: TrackInfo, path: str) -> None:
        try:
            with open(os.path.join(folder, MANIFEST_FILENAME), "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"id": track.id, "file": os.path.basename(path)}) + "\n")
        except OSError:
            pass

    def _load_manifest(self, folder: str) -> set[str]:
        done: set[str] = set()
        try:
            with open(os.path.join(folder, MANIFEST_FILENAME), encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("id") and rec.get("file") and os.path.exists(os.path.join(folder, rec["file"])):
                        done.add(rec["id"])
        except OSError:
            return set()
        return done

    def _folder_for(self, out_dir: str, meta) -> str:
        name = sanitize_filename(f"{meta.name} - {meta.owner}" if meta.owner else meta.name)
        if name in ("Unknown", ""):
            name = "AudioFlow Playlist"
        folder = os.path.join(out_dir, name)
        os.makedirs(folder, exist_ok=True)
        return folder

    def scrape(self, url: str, out_dir: str) -> int:
        """Executa o download completo. Retorna 0 (ok) / 1 (falha parcial)."""
        self._reset()
        kind, item_id = parse_spotify_url(url)

        if kind == "track":
            return self._scrape_track(item_id, out_dir)
        return self._scrape_collection(kind, item_id, out_dir)

    def _scrape_track(self, track_id: str, out_dir: str) -> int:
        os.makedirs(out_dir, exist_ok=True)
        if self.cb.on_metadata:
            self.cb.on_metadata("buscando faixa...")
        track = self.client.get_track(track_id)
        result, detail = self._download_one(track, out_dir, 1)
        self._finish_track(result, track, detail, out_dir)
        failed = 1 if result == "fail" else 0
        if self.cb.on_finish:
            self.cb.on_finish(1 if result == "ok" else 0, 1 if result == "skip" else 0, failed)
        return 1 if failed else 0

    def _scrape_collection(self, kind: str, item_id: str, out_dir: str) -> int:
        os.makedirs(out_dir, exist_ok=True)
        if self.cb.on_metadata:
            self.cb.on_metadata("buscando metadados da coleção...")
        meta = self.client.get_collection_metadata(item_id, kind)
        folder = self._folder_for(out_dir, meta)

        done = self._load_manifest(folder)
        resume_count = len(done)
        if resume_count and self.cb.on_warning:
            self.cb.on_warning(f"retomando: {resume_count} faixa(s) já baixada(s)")

        tracks: list[TrackInfo] = []
        for track in self.client.iter_collection_tracks(item_id, kind, skip_ids=done):
            if self.cancelled:
                break
            tracks.append(track)

        ok = skipped = failed = 0

        def worker(track: TrackInfo, num: int) -> tuple[str, str, TrackInfo]:
            result, detail = self._download_one(track, folder, num, meta.cover_url)
            return result, detail, track

        workers = 1 if len(tracks) < 3 else min(self.max_workers, len(tracks))
        if workers == 1:
            results = [worker(t, i) for i, t in enumerate(tracks, start=1)]
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(worker, t, i): t for i, t in enumerate(tracks, start=1)}
                results = []
                for future in concurrent.futures.as_completed(futures):
                    if self.cancelled:
                        for f in futures:
                            f.cancel()
                        break
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        log.error("erro inesperado no worker: %s", exc)

        for result, detail, track in results:
            if result == "ok":
                ok += 1
            elif result == "skip":
                skipped += 1
            else:
                failed += 1
                with self._lock:
                    self._failed.append(track.title)
            self._finish_track(result, track, detail, folder)

        if self.cb.on_finish:
            self.cb.on_finish(ok, skipped + resume_count, failed)
        return 1 if failed else 0

    def _finish_track(self, result: str, track: TrackInfo, detail: str, folder: str) -> None:
        if result == "ok":
            self._write_manifest(folder, track, detail)
            if self.cb.on_track_done:
                self.cb.on_track_done(track, detail)
        elif result == "skip":
            if self.cb.on_track_skip:
                self.cb.on_track_skip(track, detail)
        elif result == "fail":
            if self.cb.on_track_fail:
                self.cb.on_track_fail(track, detail)
            else:
                log.warning("faixa falhou: %s", detail)
