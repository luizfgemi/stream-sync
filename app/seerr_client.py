"""Seerr client integration for request protection."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from app.schemas import SeerrProtection


class SeerrClient:
    """Client for querying Seerr requests and protected items."""

    def __init__(self, url: str, api_key: str) -> None:
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._logger = logging.getLogger("stream-sync.seerr")

    def _request(self, path: str) -> Any:
        url = f"{self._url}/api/v1{path}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "X-Api-Key": self._api_key,
            },
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    def fetch_protected_tmdb_ids(self) -> dict[int, list[SeerrProtection]]:
        """Fetch dictionary of TMDB IDs protected by Seerr requests or watchlists."""
        output: dict[int, list[SeerrProtection]] = {}
        for filter_name in ("pending", "approved", "available"):
            try:
                data = self._request(f"/request?take=10000&filter={filter_name}")
                results = data.get("results") if isinstance(data, dict) else []
                for item in results or []:
                    if not isinstance(item, dict):
                        continue
                    media = item.get("media")
                    if not isinstance(media, dict):
                        continue
                    tmdb_id = media.get("tmdbId")
                    if not tmdb_id:
                        continue
                    requested_by = item.get("requestedBy")
                    user_name = requested_by.get("displayName") if isinstance(requested_by, dict) else None
                    protection = SeerrProtection(source="seerr_request", user=user_name)
                    output.setdefault(int(tmdb_id), []).append(protection)
            except Exception as exc:
                self._logger.warning("Failed to fetch Seerr requests for filter %s: %s", filter_name, exc)
                raise
        return output
