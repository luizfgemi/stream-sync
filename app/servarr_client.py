"""Servarr API client helper for Plex Watchlist synchronization into Radarr/Sonarr.

Handles direct REST queries and payload submission to Radarr and Sonarr for Watchlist items.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class ServarrClient:
    """Client for generic Radarr/Sonarr API operations required by Watchlist sync.

    Args:
        url: Servarr application URL.
        api_key: Servarr API authentication key.
        service: Service identifier ('radarr' or 'sonarr').
    """

    def __init__(self, url: str, api_key: str, service: str) -> None:
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._service = service
        self._logger = logging.getLogger(f"stream-sync.{service}.watchlist")

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        """Execute HTTP request against Servarr API endpoint."""
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self._url}/api/v3{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Api-Key": self._api_key,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(
                f"{self._service} API HTTP {exc.code} for {path}: {detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"{self._service} API request failed for {path}: {exc}"
            ) from exc
        if not raw:
            return None
        return json.loads(raw)

    def _tag_ids(self, users: tuple[str, ...]) -> list[int]:
        """Fetch or create tag IDs for Watchlist users."""
        tags = self._request("GET", "/tag")
        by_label = {
            str(tag.get("label", "")).lower(): int(tag["id"])
            for tag in tags
            if isinstance(tag, dict) and tag.get("id")
        }
        output: list[int] = []
        for user in users:
            label = f"plex-watchlist-{user}"
            tag_id = by_label.get(label)
            if tag_id is None:
                created = self._request("POST", "/tag", {"label": label})
                tag_id = int(created["id"])
                by_label[label] = tag_id
            output.append(tag_id)
        return output

    def add_movie(
        self,
        tmdb_id: int,
        users: tuple[str, ...],
        quality_profile_id: int,
        root_folder: str,
        search: bool,
    ) -> str:
        """Add movie to Radarr from TMDB ID with user tags."""
        movies = self._request("GET", "/movie")
        if any(int(movie.get("tmdbId") or 0) == tmdb_id for movie in movies):
            return "exists"
        query = urllib.parse.urlencode({"tmdbId": tmdb_id})
        movie = self._request("GET", f"/movie/lookup/tmdb?{query}")
        if not isinstance(movie, dict):
            raise RuntimeError(f"Radarr lookup returned no movie for TMDB {tmdb_id}")
        movie.update(
            {
                "qualityProfileId": quality_profile_id,
                "rootFolderPath": root_folder,
                "monitored": True,
                "minimumAvailability": "released",
                "tags": self._tag_ids(users),
                "addOptions": {"searchForMovie": search},
            }
        )
        self._request("POST", "/movie", movie)
        self._logger.info(
            "Added Plex watchlist movie: %s (tmdb=%s users=%s)",
            movie.get("title"),
            tmdb_id,
            ",".join(users),
        )
        return "added"

    def add_series(
        self,
        tvdb_id: int,
        users: tuple[str, ...],
        quality_profile_id: int,
        root_folder: str,
        search: bool,
    ) -> str:
        """Add TV series to Sonarr from TVDB ID with user tags."""
        series_rows = self._request("GET", "/series")
        if any(int(series.get("tvdbId") or 0) == tvdb_id for series in series_rows):
            return "exists"
        query = urllib.parse.urlencode({"term": f"tvdb:{tvdb_id}"})
        matches = self._request("GET", f"/series/lookup?{query}")
        series = matches[0] if isinstance(matches, list) and matches else None
        if not isinstance(series, dict):
            raise RuntimeError(f"Sonarr lookup returned no series for TVDB {tvdb_id}")
        series.update(
            {
                "qualityProfileId": quality_profile_id,
                "rootFolderPath": root_folder,
                "monitored": True,
                "seasonFolder": True,
                "tags": self._tag_ids(users),
                "addOptions": {
                    "monitor": "all",
                    "searchForMissingEpisodes": search,
                },
            }
        )
        self._request("POST", "/series", series)
        self._logger.info(
            "Added Plex watchlist series: %s (tvdb=%s users=%s)",
            series.get("title"),
            tvdb_id,
            ",".join(users),
        )
        return "added"
