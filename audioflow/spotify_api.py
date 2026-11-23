"""Cliente de metadados do Spotify para o AudioFlow.

Extrai metadados de playlists, álbuns e faixas únicas SEM autenticação,
lendo a página pública de embed (bloco __NEXT_DATA__) que o Spotify serve
a qualquer um. Para playlists grandes (>100 faixas) usa a API spclient com
o token anônimo embutido na página e busca individual de faixas.

O nome do álbum de faixas avulsas vem do og:description da página social
do track (requer user-agent de crawler), pois o JSON de embed não carrega
esse campo.
"""

from __future__ import annotations

import functools
import html as _html
import json
import logging
import re
import time
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger("audioflow.spotify")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
_SOCIAL_UA = "facebookexternalhit/1.1"


class SpotifyError(RuntimeError):
    pass


class NetworkFailure(SpotifyError):
    pass


class ExtractionFailure(SpotifyError):
    pass


class RateLimited(SpotifyError):
    pass


def _retry(max_attempts=3, backoff=1.0):
    """Retry uma chamada de rede com backoff exponencial."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            last = None
            for attempt in range(max_attempts):
                try:
                    return fn(*args, **kwargs)
                except (NetworkFailure, RateLimited, requests.Timeout, requests.ConnectionError) as e:
                    last = e
                    if attempt < max_attempts - 1:
                        time.sleep(backoff * (2**attempt))
            raise last  # type: ignore[misc]

        return wrapped

    return deco


@dataclass
class TrackInfo:
    id: str
    title: str
    artists: str
    album: str | None = None
    release_date: str | None = None
    cover_url: str | None = None
    duration_ms: int | None = None
    position: int | None = None


@dataclass
class CollectionInfo:
    name: str
    owner: str | None = None
    cover_url: str | None = None
    track_count: int | None = None


class SpotifyClient:
    """Busca metadados nas páginas públicas do Spotify, sem login."""

    EMBED_PLAYLIST = "https://open.spotify.com/embed/playlist/{id}"
    EMBED_ALBUM = "https://open.spotify.com/embed/album/{id}"
    EMBED_TRACK = "https://open.spotify.com/embed/track/{id}"
    TRACK_PAGE = "https://open.spotify.com/track/{id}"
    SPCLIENT = "https://spclient.wg.spotify.com/playlist/v2/playlist/{id}"

    _NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
    _OG_DESC = re.compile(
        r'<meta\s+(?=[^>]*\bproperty="og:description")[^>]*\bcontent="([^"]*)"',
        re.IGNORECASE,
    )

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()
        self._token: str | None = None
        self._token_expiry = 0.0
        self._album_cache: dict[str, str | None] = {}

    def _headers(self, social: bool = False) -> dict[str, str]:
        return {
            "user-agent": _SOCIAL_UA if social else _UA,
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
        }

    @_retry(max_attempts=4, backoff=1.5)
    def _get_embed(self, url: str) -> dict:
        try:
            resp = self._session.get(url, headers=self._headers(), timeout=30)
        except requests.Timeout as exc:
            raise NetworkFailure(f"timeout ao buscar {url}") from exc
        except requests.ConnectionError as exc:
            raise NetworkFailure(f"sem conexão ao buscar {url}") from exc

        if resp.status_code == 429:
            raise RateLimited("Spotify limitou a taxa de requisições")
        if resp.status_code in (401, 403):
            raise ExtractionFailure("acesso negado (playlist pode ser privada)")
        if resp.status_code != 200:
            raise NetworkFailure(f"HTTP {resp.status_code} em {url}")

        match = self._NEXT_DATA.search(resp.text)
        if not match:
            raise ExtractionFailure("bloco __NEXT_DATA__ não encontrado")
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise ExtractionFailure("JSON inválido em __NEXT_DATA__") from exc

        # Guarda o token anônimo da sessão (expira junto da página).
        for path in (
            ("props", "pageProps", "state", "settings", "session"),
            ("props", "pageProps", "settings", "session"),
            ("props", "pageProps", "session"),
        ):
            node = data
            for key in path:
                node = node.get(key) if isinstance(node, dict) else None
            if isinstance(node, dict) and node.get("accessToken"):
                self._token = node["accessToken"]
                exp = node.get("accessTokenExpirationTimestampMs", 0)
                self._token_expiry = exp / 1000 if exp else 0
                break
        return data

    @staticmethod
    def _find_entity(data: Any) -> dict:
        """Acha o dict de entidade (playlist/track) dentro do __NEXT_DATA__."""
        def walk(node: Any, depth: int = 0) -> dict | None:
            if depth > 8 or not isinstance(node, dict):
                return None
            uri = node.get("uri", "")
            if isinstance(uri, str) and uri.startswith("spotify:") and (
                "trackList" in node or node.get("type") in ("track", "playlist", "album")
            ):
                return node
            for value in node.values():
                found = walk(value, depth + 1)
                if found:
                    return found
            return None

        entity = walk(data)
        if entity is None:
            raise ExtractionFailure("não foi possível localizar a entidade na página")
        return entity

    @staticmethod
    def _cover_from(entity: dict) -> str | None:
        sources = entity.get("coverArt", {}).get("sources", [])
        if sources:
            return sources[-1].get("url")
        images = entity.get("visualIdentity", {}).get("image", [])
        if images and isinstance(images[-1], dict):
            return images[-1].get("url")
        return None

    def _embed_url(self, item_id: str, kind: str) -> str:
        if kind == "album":
            return self.EMBED_ALBUM.format(id=item_id)
        if kind == "track":
            return self.EMBED_TRACK.format(id=item_id)
        return self.EMBED_PLAYLIST.format(id=item_id)

    def get_collection_metadata(self, item_id: str, kind: str = "playlist") -> CollectionInfo:
        """Metadados de uma playlist ou álbum."""
        data = self._get_embed(self._embed_url(item_id, kind))
        entity = self._find_entity(data)
        name = entity.get("name") or entity.get("title") or "Playlist sem nome"
        owner = entity.get("subtitle")
        count = None
        if kind == "playlist":
            tracks = entity.get("trackList") or entity.get("trackListV2", {}).get("items", [])
            count = len(tracks)
            # Ajusta a contagem real via spclient quando o token estiver fresco.
            if self._token and time.time() < self._token_expiry - 30:
                try:
                    resp = self._session.get(
                        self.SPCLIENT.format(id=item_id),
                        headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
                        timeout=10,
                    )
                    if resp.status_code == 200:
                        count = resp.json().get("length", count)
                except Exception:
                    pass
        else:
            tracks = entity.get("trackList") or entity.get("trackListV2", {}).get("items", [])
            count = len(tracks)
        return CollectionInfo(
            name=str(name),
            owner=str(owner) if owner else None,
            cover_url=self._cover_from(entity),
            track_count=count,
        )

    @staticmethod
    def _parse_track(raw: dict, track_id: str, position: int | None = None) -> TrackInfo:
        title = raw.get("title") or raw.get("name") or "Faixa desconhecida"
        artists = raw.get("subtitle") or raw.get("artists") or ""
        if isinstance(artists, list):
            artists = ", ".join(
                a.get("name", "") for a in artists if isinstance(a, dict) and a.get("name")
            )
        album = None
        if isinstance(raw.get("album"), dict):
            album = raw["album"].get("name")
        return TrackInfo(
            id=track_id,
            title=str(title),
            artists=str(artists),
            album=album,
            release_date=raw.get("releaseDate"),
            duration_ms=int(raw["duration"]) if raw.get("duration") else None,
            position=position,
        )

    def iter_collection_tracks(
        self, item_id: str, kind: str = "playlist", skip_ids: set[str] | None = None
    ) -> Iterator[TrackInfo]:
        """Itera pelas faixas de uma playlist ou álbum.

        Playlists <=100 faixas vêm completas da página de embed. Acima disso,
        a lista completa de URIs vem da spclient e o metadado de cada faixa
        extra é buscado individualmente (paralelo, 4 workers). `skip_ids`
        omite faixas já baixadas em execuções anteriores (retomada).
        """
        skip = skip_ids or set()
        data = self._get_embed(self._embed_url(item_id, kind))
        entity = self._find_entity(data)
        album_name = entity.get("name") if kind == "album" else None

        if "trackListV2" in entity:
            raw_items = [t.get("track", {}) for t in entity["trackListV2"].get("items", [])]
        else:
            raw_items = entity.get("trackList", [])

        embed_ids: set[str] = set()
        for pos, raw in enumerate(raw_items, start=1):
            uri = raw.get("uri", "")
            if not uri.startswith("spotify:track:"):
                continue
            track_id = uri.split(":")[-1]
            embed_ids.add(track_id)
            if track_id in skip:
                continue
            info = self._parse_track(raw, track_id, position=pos)
            if album_name and not info.album:
                info.album = album_name
            yield info

        if kind != "playlist" or not self._token:
            return

        try:
            resp = self._session.get(
                self.SPCLIENT.format(id=item_id),
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
                timeout=30,
            )
            if resp.status_code != 200:
                return
            payload = resp.json()
            if payload.get("length", 0) <= len(embed_ids):
                return
            items = payload.get("contents", {}).get("items", [])
        except Exception:
            return

        pending: list[tuple[str, int]] = []
        positions: dict[str, int] = {}
        for pos, item in enumerate(items, start=1):
            uri = item.get("uri", "")
            if not uri.startswith("spotify:track:"):
                continue
            track_id = uri.split(":")[-1]
            positions[track_id] = pos
            if track_id not in embed_ids and track_id not in skip:
                pending.append((track_id, pos))

        if not pending:
            return

        import concurrent.futures

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        try:
            futures = {pool.submit(self.get_track, tid): (tid, pos) for tid, pos in pending}
            for future in concurrent.futures.as_completed(futures):
                tid, pos = futures[future]
                try:
                    info = future.result()
                except Exception:
                    info = TrackInfo(id=tid, title=f"Faixa {tid}", artists="Artista desconhecido", position=positions.get(tid, pos))
                if album_name and not info.album:
                    info.album = album_name
                yield info
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def get_track(self, track_id: str) -> TrackInfo:
        """Metadados de uma faixa única (com busca de álbum no og:description)."""
        data = self._get_embed(self.EMBED_TRACK.format(id=track_id))
        entity = self._find_entity(data)
        title = entity.get("name") or entity.get("title") or "Faixa desconhecida"
        artists = entity.get("artists") or entity.get("subtitle") or ""
        if isinstance(artists, list):
            artists = ", ".join(a.get("name", "") for a in artists if isinstance(a, dict))
        cover = self._cover_from(entity)
        release_date = None
        rd = entity.get("releaseDate")
        if isinstance(rd, dict):
            release_date = str(rd.get("isoString", ""))[:10]
        elif isinstance(rd, str):
            release_date = rd
        duration = entity.get("duration")
        return TrackInfo(
            id=track_id,
            title=str(title),
            artists=str(artists),
            album=self._fetch_album_name(track_id),
            release_date=release_date,
            cover_url=cover,
            duration_ms=int(duration) if duration else None,
        )

    def _fetch_album_name(self, track_id: str) -> str | None:
        """Nome do álbum via og:description da página social do track."""
        if track_id in self._album_cache:
            return self._album_cache[track_id]

        @_retry(max_attempts=3, backoff=1.0)
        def _go() -> str | None:
            resp = self._session.get(
                self.TRACK_PAGE.format(id=track_id), headers=self._headers(social=True), timeout=15
            )
            if resp.status_code != 200:
                return None
            match = self._OG_DESC.search(resp.text)
            if not match:
                return None
            parts = _html.unescape(match.group(1)).split(" · ")
            if len(parts) < 2:
                return None
            return parts[1].strip() or None

        try:
            album = _go()
        except NetworkFailure:
            return None
        if len(self._album_cache) >= 256:
            self._album_cache.pop(next(iter(self._album_cache)))
        self._album_cache[track_id] = album
        return album


# --- Utilidades de parsing de URL -------------------------------------------------

_URL_RE = re.compile(
    r"(?:https?://open\.spotify\.com/(?:intl-[a-z]{2,}/)?|spotify:)"
    r"(?P<kind>playlist|track|album)[/:](?P<id>[a-zA-Z0-9]+)"
)


def parse_spotify_url(url: str) -> tuple[str, str]:
    """Retorna (kind, id) para qualquer URL/URI do Spotify suportada."""
    if not url:
        raise ValueError("URL vazia")
    match = _URL_RE.search(url.strip())
    if not match:
        raise ValueError("URL do Spotify inválida (playlist, álbum ou faixa)")
    return match.group("kind"), match.group("id")


def sanitize_filename(name: str, allow_spaces: bool = True) -> str:
    """Remove caracteres inseguros para nome de arquivo (Windows/macOS/Linux).

    Mantém acentos e letras não-latinas; só tira o que realmente quebra o
    filesystem: caracteres reservados do Windows, controles, e escapa nomes
    de device (CON, NUL, COM1...).
    """
    reserved = '<>:"/\\|?*'
    devices = {"CON", "PRN", "AUX", "NUL"} | {
        f"COM{i}" for i in range(1, 10)
    } | {f"LPT{i}" for i in range(1, 10)}
    normalized = unicodedata.normalize("NFC", name)
    out = "".join(
        ch for ch in normalized
        if ch not in reserved and unicodedata.category(ch) != "Cc"
    )
    if not allow_spaces:
        out = out.replace(" ", "")
    out = " ".join(out.split()) if allow_spaces else out.strip()
    out = out.strip(" .")
    if not out:
        return "Unknown"
    if out.split(".")[0].upper() in devices:
        out = f"_{out}"
    return out


def cap_filename(name: str, max_bytes: int = 250) -> str:
    """Limita o nome completo (com extensão) a um tamanho válido em todo lugar."""
    if len(name.encode("utf-8")) <= max_bytes:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or not ext or len(ext) > 10 or " " in ext:
        stem, ext = name, ""
    suffix = f".{ext}" if ext else ""
    budget = max(1, max_bytes - len(suffix.encode("utf-8")))
    stem = stem.encode("utf-8")[:budget].decode("utf-8", "ignore").rstrip(" .")
    return f"{stem}{suffix}" if stem else f"file{suffix}"


__all__ = [
    "CollectionInfo",
    "ExtractionFailure",
    "NetworkFailure",
    "RateLimited",
    "SpotifyClient",
    "SpotifyError",
    "TrackInfo",
    "cap_filename",
    "parse_spotify_url",
    "sanitize_filename",
]
