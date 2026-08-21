from __future__ import annotations

import time

from ..cache_sqlite import SQLiteCache
from ..radarr_client import RadarrClient
from ..snapshot import movie_snapshot_payload


class MovieService:
    def __init__(self, cache: SQLiteCache, radarr: RadarrClient) -> None:
        self._cache = cache
        self._radarr = radarr

    def list_movies(
        self,
        page: int,
        page_size: int,
        search: str = "",
        condition: str = "",
        sort: str = "title",
    ) -> dict[str, object]:
        rows, total = self._cache.list_movie_snapshots(
            page=page,
            page_size=page_size,
            search=search,
            condition=condition,
            sort=sort,
        )
        return {
            "page": page,
            "pageSize": page_size,
            "total": total,
            "pages": (total + page_size - 1) // page_size,
            "results": [row.payload for row in rows],
        }

    def search_movies(
        self,
        query: str,
        page: int,
        page_size: int,
        condition: str = "",
        sort: str = "title",
    ) -> dict[str, object]:
        return self.list_movies(
            page=page,
            page_size=page_size,
            search=query,
            condition=condition,
            sort=sort,
        )

    def get_movie(self, radarr_id: int) -> dict[str, object] | None:
        snapshot = self._cache.get_movie_snapshot(radarr_id)
        if snapshot is None:
            return None
        return snapshot.payload

    def set_favorite(self, radarr_id: int, favorite: bool) -> dict[str, object]:
        movie = self._radarr.set_favorite(radarr_id, favorite)
        conditions = ["favorite"] if favorite else []
        payload = movie_snapshot_payload(movie, conditions, int(time.time()))
        self._cache.upsert_movie_snapshots([payload])
        return payload
