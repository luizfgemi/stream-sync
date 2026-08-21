"""Seerr client integration for request and watchlist protection."""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.schemas import SeerrProtection


class SeerrClient:
    """Client for querying Seerr requests and protected items."""

    REQUEST_FILTERS = ("pending", "approved", "available")

    def __init__(self, base_url: str, api_key: str, timeout_seconds: float = 20.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._logger = logging.getLogger("stream-sync.seerr")

    def protected_movie_tmdb_ids(self) -> set[int]:
        """Fetch set of TMDB IDs protected by Seerr requests or watchlists."""
        protected_ids = set(self.protected_movie_details().keys())
        self._logger.info("Seerr protection loaded: movies=%s", len(protected_ids))
        return protected_ids

    def protected_movie_details(self) -> dict[int, list[SeerrProtection]]:
        """Fetch details dictionary mapping protected TMDB IDs to protection records."""
        protected: dict[int, list[SeerrProtection]] = {}
        self._merge_protection(protected, self._request_movie_protections())
        self._merge_protection(protected, self._watchlist_movie_protections())
        self._logger.info("Seerr protection loaded: movies=%s", len(protected))
        return protected

    def fetch_protected_tmdb_ids(self) -> dict[int, list[SeerrProtection]]:
        """Alias for protected_movie_details for compatibility."""
        return self.protected_movie_details()

    @staticmethod
    def _merge_protection(
        target: dict[int, list[SeerrProtection]],
        source: dict[int, list[SeerrProtection]],
    ) -> None:
        """Merge protection items into target dict without duplicates."""
        for tmdb_id, protections in source.items():
            existing = target.setdefault(tmdb_id, [])
            existing_keys = {(item.source, item.user) for item in existing}
            for protection in protections:
                key = (protection.source, protection.user)
                if key in existing_keys:
                    continue
                existing.append(protection)
                existing_keys.add(key)

    def _get_json(self, path: str, params: dict[str, object] | None = None) -> dict[str, Any]:
        """Execute GET request to Seerr REST API."""
        url = f"{self._base_url}/api/v1{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "X-API-Key": self._api_key,
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                payload = response.read().decode("utf-8")
        except HTTPError as exc:
            raise RuntimeError(f"Seerr API HTTP {exc.code} for {path}") from exc
        except URLError as exc:
            raise RuntimeError(f"Seerr API request failed for {path}: {exc}") from exc

        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Seerr API returned invalid JSON for {path}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"Seerr API returned non-object JSON for {path}")
        return data

    @staticmethod
    def _results(data: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract results list from paginated Seerr API response."""
        results = data.get("results")
        if not isinstance(results, list):
            return []
        return [item for item in results if isinstance(item, dict)]

    @staticmethod
    def _page_count(data: dict[str, Any], default: int = 1) -> int:
        """Extract total page count from Seerr API response."""
        page_info = data.get("pageInfo")
        if isinstance(page_info, dict):
            pages = page_info.get("pages")
            if isinstance(pages, int) and pages > 0:
                return pages
        pages = data.get("totalPages")
        if isinstance(pages, int) and pages > 0:
            return pages
        return default

    @staticmethod
    def _tmdb_id(value: object) -> int | None:
        """Parse positive TMDB ID integer value."""
        try:
            tmdb_id = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return tmdb_id if tmdb_id > 0 else None

    @staticmethod
    def _user_label(user: object) -> str | None:
        """Extract user display label from user object dictionary."""
        if not isinstance(user, dict):
            return None
        for key in ("displayName", "username", "plexUsername", "email"):
            value = user.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _request_movie_protections(self) -> dict[int, list[SeerrProtection]]:
        """Fetch movie protections originating from Seerr requests."""
        movie_ids: dict[int, list[SeerrProtection]] = {}
        page_size = 100
        for request_filter in self.REQUEST_FILTERS:
            page = 1
            while True:
                skip = (page - 1) * page_size
                data = self._get_json(
                    "/request",
                    {
                        "mediaType": "movie",
                        "filter": request_filter,
                        "take": page_size,
                        "skip": skip,
                    },
                )
                for item in self._results(data):
                    media = item.get("media")
                    if not isinstance(media, dict):
                        continue
                    tmdb_id = self._tmdb_id(media.get("tmdbId"))
                    if tmdb_id is not None:
                        user = self._user_label(item.get("requestedBy"))
                        movie_ids.setdefault(tmdb_id, []).append(
                            SeerrProtection(source="seerr_request", user=user)
                        )

                if page >= self._page_count(data):
                    break
                page += 1
        return movie_ids

    def _users(self) -> list[dict[str, Any]]:
        """Fetch all Seerr user accounts."""
        users: list[dict[str, Any]] = []
        page_size = 100
        page = 1
        while True:
            skip = (page - 1) * page_size
            data = self._get_json("/user", {"take": page_size, "skip": skip})
            users.extend(self._results(data))
            if page >= self._page_count(data):
                break
            page += 1
        return users

    def _watchlist_movie_protections(self) -> dict[int, list[SeerrProtection]]:
        """Fetch movie protections originating from user Plex watchlists in Seerr."""
        movie_ids: dict[int, list[SeerrProtection]] = {}
        for user in self._users():
            user_id = self._tmdb_id(user.get("id"))
            if user_id is None:
                continue
            user_label = self._user_label(user)
            page = 1
            while True:
                data = self._get_json(f"/user/{user_id}/watchlist", {"page": page})
                for item in self._results(data):
                    if str(item.get("mediaType", "")).lower() != "movie":
                        continue
                    tmdb_id = self._tmdb_id(item.get("tmdbId"))
                    if tmdb_id is not None:
                        movie_ids.setdefault(tmdb_id, []).append(
                            SeerrProtection(source="plex_watchlist", user=user_label)
                        )

                if page >= self._page_count(data):
                    break
                page += 1
        return movie_ids
