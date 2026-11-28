"""Busca pública de músicas, álbuns e playlists (sem login).

Usa a API pública do Deezer, que não exige token nem conta. Os resultados
são normalizados para o formato do AudioFlow e podem ser baixados pelo mesmo
pipeline das URLs do Spotify: o engine recebe as faixas já resolvidas e só
reaproveita a conversão/assinatura existentes.

Nada aqui autentica usuário. É catálogo aberto, como a página pública do
Spotify que o app já usa para metadados.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

from .spotify_api import CollectionInfo, TrackInfo

log = logging.getLogger("audioflow.search")

API = "https://api.deezer.com"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
_TIMEOUT = (5, 10)
_CACHE_TTL = 300.0
_CACHE_MAX = 256

_URI_RE = re.compile(r"^deezer:(track|album|playlist):(\d{1,20})$")
_KINDS = ("track", "album", "playlist")


class SearchError(RuntimeError):
    """Falha ao consultar o catálogo público."""


@dataclass
class SearchResult:
    id: str
    kind: str
    title: str
    subtitle: str
    cover: str | None
    thumb: str | None
    duration_ms: int | None
    track_count: int | None
    url: str
    uri: str
    spotify_url: str

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.kind,
            "title": self.title,
            "subtitle": self.subtitle,
            "cover": self.cover,
            "thumb": self.thumb,
            "duration_ms": self.duration_ms,
            "track_count": self.track_count,
            "url": self.url,
            "uri": self.uri,
            "spotify_url": self.spotify_url,
        }


@dataclass
class ResolvedItem:
    kind: str
    collection: CollectionInfo
    tracks: list[TrackInfo]


def parse_deezer_uri(uri: str) -> tuple[str, str]:
    """Valida ``deezer:track:123`` e devolve ``(kind, id)``."""
    match = _URI_RE.match((uri or "").strip())
    if not match:
        raise SearchError("Item de busca inválido.")
    return match.group(1), match.group(2)


def is_deezer_uri(uri: str) -> bool:
    return bool(_URI_RE.match((uri or "").strip()))


# --- cliente HTTP ----------------------------------------------------------


class _Client:
    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update(
            {
                "user-agent": _UA,
                "accept": "application/json",
                "accept-language": "pt-BR,pt;q=0.9,en;q=0.8",
            }
        )
        self._lock = threading.Lock()
        self._cache: dict[tuple, tuple[float, Any]] = {}

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = API + path
        last: Exception | None = None
        for attempt in range(3):
            try:
                resp = self._session.get(url, params=params, timeout=_TIMEOUT)
                if resp.status_code == 429:
                    time.sleep(1.2 * (attempt + 1))
                    continue
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, dict) and data.get("error"):
                    err = data["error"]
                    message = err.get("message") if isinstance(err, dict) else str(err)
                    raise SearchError(message or "catálogo indisponível")
                return data if isinstance(data, dict) else {}
            except SearchError:
                raise
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < 2:
                    time.sleep(0.6 * (attempt + 1))
        raise SearchError(f"catálogo indisponível: {last}") from last

    def cached(self, key: tuple, factory):
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        value = factory()
        with self._lock:
            if len(self._cache) >= _CACHE_MAX:
                oldest = min(self._cache.items(), key=lambda kv: kv[1][0])
                self._cache.pop(oldest[0], None)
            self._cache[key] = (now + _CACHE_TTL, value)
        return value

    def search(self, query: str, kinds: tuple[str, ...], limit: int) -> list[SearchResult]:
        key = ("search", query.lower(), kinds, limit)
        return self.cached(key, lambda: self._search_uncached(query, kinds, limit))

    def _search_uncached(
        self, query: str, kinds: tuple[str, ...], limit: int
    ) -> list[SearchResult]:
        buckets: dict[str, list[SearchResult]] = {}
        per_kind = max(3, min(limit, 10))
        for kind in kinds:
            try:
                data = self._get(
                    f"/search/{kind}", {"q": query, "limit": per_kind}
                )
            except SearchError as exc:
                log.warning("busca %s falhou: %s", kind, exc)
                continue
            items = data.get("data") or []
            buckets[kind] = [
                result
                for item in items
                if isinstance(item, dict)
                for result in [self._normalize(item, kind)]
                if result is not None
            ]

        # intercala os tipos para a lista não ficar só de faixas
        merged: list[SearchResult] = []
        order = [k for k in ("track", "album", "playlist") if k in buckets]
        while len(merged) < limit and any(buckets.get(k) for k in order):
            for kind in order:
                bucket = buckets.get(kind) or []
                if bucket:
                    merged.append(bucket.pop(0))
                    if len(merged) >= limit:
                        break
        return merged

    @staticmethod
    def _normalize(item: dict, kind: str) -> SearchResult | None:
        try:
            item_id = str(item.get("id") or "").strip()
            title = str(item.get("title") or "").strip()
            if not item_id or not title:
                return None

            if kind == "track":
                artist = str((item.get("artist") or {}).get("name") or "").strip()
                album = item.get("album") or {}
                album_title = str(album.get("title") or "").strip()
                subtitle = " · ".join(part for part in (artist, album_title) if part)
                cover = album.get("cover_big") or album.get("cover_medium")
                thumb = album.get("cover_medium") or cover
                duration = item.get("duration")
                duration_ms = int(duration) * 1000 if duration else None
                count = None
                spotify_q = " ".join(part for part in (artist, title) if part)
            elif kind == "album":
                artist = str((item.get("artist") or {}).get("name") or "").strip()
                count = item.get("nb_tracks")
                subtitle = " · ".join(
                    part
                    for part in (
                        artist,
                        f"{count} faixas" if count else "",
                    )
                    if part
                )
                cover = item.get("cover_big") or item.get("cover_medium")
                thumb = item.get("cover_medium") or cover
                duration_ms = None
                spotify_q = " ".join(part for part in (artist, title) if part)
            else:
                owner = str(
                    (item.get("user") or {}).get("name")
                    or (item.get("creator") or {}).get("name")
                    or ""
                ).strip()
                count = item.get("nb_tracks")
                subtitle = " · ".join(
                    part
                    for part in (
                        owner,
                        f"{count} faixas" if count else "",
                    )
                    if part
                )
                cover = item.get("picture_big") or item.get("picture_medium")
                thumb = item.get("picture_medium") or cover
                duration_ms = None
                spotify_q = title

            return SearchResult(
                id=item_id,
                kind=kind,
                title=title,
                subtitle=subtitle,
                cover=cover,
                thumb=thumb,
                duration_ms=duration_ms,
                track_count=int(count) if count else None,
                url=str(item.get("link") or ""),
                uri=f"deezer:{kind}:{item_id}",
                spotify_url="https://open.spotify.com/search/" + quote(spotify_q),
            )
        except Exception:  # noqa: BLE001
            return None

    def meta(self, kind: str, item_id: str) -> CollectionInfo:
        key = ("meta", kind, item_id)
        return self.cached(key, lambda: self._meta_uncached(kind, item_id))

    def _meta_uncached(self, kind: str, item_id: str) -> CollectionInfo:
        if kind == "track":
            item = self._get(f"/track/{item_id}")
            artist = str((item.get("artist") or {}).get("name") or "").strip()
            title = str(item.get("title") or "").strip()
            album = item.get("album") or {}
            return CollectionInfo(
                name=" - ".join(part for part in (artist, title) if part) or title,
                owner=None,
                cover_url=album.get("cover_big") or album.get("cover_medium"),
                track_count=1,
            )
        item = self._get(f"/{kind}/{item_id}")
        title = str(item.get("title") or "").strip() or "Coleção"
        if kind == "album":
            owner = str((item.get("artist") or {}).get("name") or "").strip() or None
            cover = item.get("cover_big") or item.get("cover_medium")
        else:
            owner = str(
                (item.get("creator") or {}).get("name")
                or (item.get("user") or {}).get("name")
                or ""
            ).strip() or None
            cover = item.get("picture_big") or item.get("picture_medium")
        count = item.get("nb_tracks")
        return CollectionInfo(
            name=title,
            owner=owner,
            cover_url=cover,
            track_count=int(count) if count else None,
        )

    def resolve(self, uri: str, max_tracks: int = 50) -> ResolvedItem:
        kind, item_id = parse_deezer_uri(uri)
        if kind == "track":
            item = self._get(f"/track/{item_id}")
            track = self._track_from(item, None)
            if track is None:
                raise SearchError("faixa indisponível no catálogo.")
            meta = self.meta("track", item_id)
            return ResolvedItem(kind="track", collection=meta, tracks=[track])

        meta = self.meta(kind, item_id)
        raw_tracks: list[dict] = []
        index = 0
        page = 100
        while len(raw_tracks) < max_tracks:
            data = self._get(
                f"/{kind}/{item_id}/tracks",
                {"limit": page, "index": index},
            )
            batch = data.get("data") or []
            if not batch:
                break
            raw_tracks.extend(x for x in batch if isinstance(x, dict))
            index += len(batch)
            total = data.get("total")
            if isinstance(total, int) and index >= total:
                break
            if len(batch) < page:
                break

        tracks = [
            track
            for track in (
                self._track_from(
                    item,
                    meta,
                    position=i,
                    default_album=meta.name if kind == "album" else None,
                )
                for i, item in enumerate(raw_tracks[:max_tracks], start=1)
            )
            if track is not None
        ]
        if not tracks:
            raise SearchError("não encontrei faixas nesta coleção.")
        return ResolvedItem(kind=kind, collection=meta, tracks=tracks)

    @staticmethod
    def _track_from(
        item: dict,
        collection: CollectionInfo | None,
        position: int = 1,
        default_album: str | None = None,
    ) -> TrackInfo | None:
        try:
            track_id = str(item.get("id") or "").strip()
            title = str(item.get("title") or "").strip()
            if not track_id or not title:
                return None
            artist = str((item.get("artist") or {}).get("name") or "").strip()
            contributors = item.get("contributors") or []
            if isinstance(contributors, list) and len(contributors) > 1:
                names = [
                    str((c or {}).get("name") or "").strip()
                    for c in contributors
                    if isinstance(c, dict)
                ]
                names = [n for n in names if n]
                if names:
                    artist = ", ".join(dict.fromkeys(names))
            album = item.get("album") or {}
            album_title = (
                str(album.get("title") or "").strip() or default_album
            )
            cover = (
                album.get("cover_big")
                or album.get("cover_medium")
                or (collection.cover_url if collection else None)
            )
            duration = item.get("duration")
            return TrackInfo(
                id=track_id,
                title=title,
                artists=artist or "Artista desconhecido",
                album=album_title,
                release_date=None,
                cover_url=cover,
                duration_ms=int(duration) * 1000 if duration else None,
                position=position,
            )
        except Exception:  # noqa: BLE001
            return None


_client = _Client()


def search(query: str, kinds: tuple[str, ...] = _KINDS, limit: int = 12) -> list[dict]:
    clean = re.sub(r"\s+", " ", (query or "").strip())[:120]
    if len(clean) < 2:
        return []
    valid = tuple(k for k in kinds if k in _KINDS) or _KINDS
    limit = max(1, min(int(limit or 12), 20))
    return [r.as_dict() for r in _client.search(clean, valid, limit)]


def resolve_meta(uri: str) -> CollectionInfo:
    kind, item_id = parse_deezer_uri(uri)
    return _client.meta(kind, item_id)


def resolve(uri: str, max_tracks: int = 50) -> ResolvedItem:
    return _client.resolve(uri, max_tracks=max_tracks)


__all__ = [
    "SearchError",
    "SearchResult",
    "ResolvedItem",
    "is_deezer_uri",
    "parse_deezer_uri",
    "resolve",
    "resolve_meta",
    "search",
]
