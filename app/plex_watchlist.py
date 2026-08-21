from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

from .types import SeerrProtection


PLEX_DISCOVER_URL = "https://discover.provider.plex.tv"
PLEX_COMMUNITY_URL = "https://community.plex.tv/api"
PLEX_ACCOUNT_URL = "https://plex.tv/users/account.json"


@dataclass(frozen=True, slots=True)
class PlexWatchlistItem:
    plex_id: str
    title: str
    media_type: str
    tmdb_id: int | None
    tvdb_id: int | None
    users: tuple[str, ...]


def _safe_tag_part(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", value.strip().lower())
    return normalized.strip("-")[:80] or "unknown"


class PlexWatchlistClient:
    def __init__(
        self,
        token: str = "",
        token_file: str = "",
        include_friends: bool = True,
        timeout_seconds: int = 30,
    ) -> None:
        self._configured_token = token.strip()
        self._token_file = token_file.strip()
        self._include_friends = include_friends
        self._timeout_seconds = timeout_seconds
        self._logger = logging.getLogger("app.plex_watchlist")

    def _token(self) -> str:
        if self._configured_token:
            return self._configured_token
        if not self._token_file:
            raise RuntimeError("Plex token is not configured")
        try:
            root = ET.parse(self._token_file).getroot()
        except (OSError, ET.ParseError) as exc:
            raise RuntimeError(
                f"Could not read Plex token file: {self._token_file}"
            ) from exc
        token = str(root.attrib.get("PlexOnlineToken", "")).strip()
        if not token:
            raise RuntimeError(
                f"PlexOnlineToken is missing from token file: {self._token_file}"
            )
        return token

    def _request_json(
        self,
        url: str,
        token: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "stream-sync/1.0",
            "X-Plex-Token": token,
        }
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(
                request, timeout=self._timeout_seconds
            ) as response:
                parsed = json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Plex API HTTP {exc.code} for {url}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"Plex API request failed for {url}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Plex API returned invalid JSON for {url}") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError(f"Plex API returned non-object JSON for {url}")
        if parsed.get("errors"):
            raise RuntimeError(f"Plex GraphQL error: {parsed['errors']}")
        return parsed

    def _account_name(self, token: str) -> str:
        data = self._request_json(PLEX_ACCOUNT_URL, token)
        user = data.get("user")
        if isinstance(user, dict):
            return str(
                user.get("username") or user.get("title") or user.get("email") or "owner"
            )
        return "owner"

    def _self_watchlist(self, token: str) -> list[dict[str, str]]:
        output: list[dict[str, str]] = []
        start = 0
        while True:
            query = urllib.parse.urlencode(
                {
                    "X-Plex-Container-Start": start,
                    "X-Plex-Container-Size": 100,
                }
            )
            data = self._request_json(
                f"{PLEX_DISCOVER_URL}/library/sections/watchlist/all?{query}",
                token,
            )
            container = data.get("MediaContainer")
            if not isinstance(container, dict):
                break
            metadata = container.get("Metadata")
            rows = metadata if isinstance(metadata, list) else []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                key = str(row.get("key") or "")
                plex_id = key.replace("/library/metadata/", "").replace(
                    "/children", ""
                )
                if plex_id:
                    output.append(
                        {
                            "id": plex_id,
                            "title": str(row.get("title") or "Unknown"),
                            "type": str(row.get("type") or ""),
                        }
                    )
            total = int(container.get("totalSize") or len(rows))
            start += len(rows)
            if not rows or start >= total:
                break
            time.sleep(0.2)
        return output

    def _friends(self, token: str) -> list[dict[str, str]]:
        data = self._request_json(
            PLEX_COMMUNITY_URL,
            token,
            {
                "query": (
                    "query GetAllFriends { allFriendsV2 { "
                    "user { id username displayName } } }"
                )
            },
        )
        rows = data.get("data", {}).get("allFriendsV2", [])
        output: list[dict[str, str]] = []
        if not isinstance(rows, list):
            return output
        for row in rows:
            user = row.get("user") if isinstance(row, dict) else None
            if not isinstance(user, dict) or not user.get("id"):
                continue
            output.append(
                {
                    "id": str(user["id"]),
                    "name": str(
                        user.get("username")
                        or user.get("displayName")
                        or user["id"]
                    ),
                }
            )
        return output

    def _friend_watchlist(
        self, token: str, user_id: str
    ) -> list[dict[str, str]]:
        output: list[dict[str, str]] = []
        cursor: str | None = None
        while True:
            data = self._request_json(
                PLEX_COMMUNITY_URL,
                token,
                {
                    "query": (
                        "query GetWatchlistHub($user: UserInput!, "
                        "$first: PaginationInt!, $after: String) { "
                        "userV2(user: $user) { ... on User { "
                        "watchlist(first: $first, after: $after) { "
                        "nodes { id title type } "
                        "pageInfo { hasNextPage endCursor } } } } }"
                    ),
                    "variables": {
                        "user": {"id": user_id},
                        "first": 100,
                        "after": cursor,
                    },
                },
            )
            watchlist = data.get("data", {}).get("userV2", {}).get("watchlist")
            if not isinstance(watchlist, dict):
                break
            nodes = watchlist.get("nodes")
            for node in nodes if isinstance(nodes, list) else []:
                if isinstance(node, dict) and node.get("id"):
                    output.append(
                        {
                            "id": str(node["id"]),
                            "title": str(node.get("title") or "Unknown"),
                            "type": str(node.get("type") or ""),
                        }
                    )
            page_info = watchlist.get("pageInfo")
            if not isinstance(page_info, dict) or not page_info.get("hasNextPage"):
                break
            cursor = str(page_info.get("endCursor") or "")
            if not cursor:
                break
            time.sleep(0.2)
        return output

    def _metadata_ids(
        self, token: str, plex_id: str
    ) -> tuple[int | None, int | None]:
        data = self._request_json(
            f"{PLEX_DISCOVER_URL}/library/metadata/{urllib.parse.quote(plex_id)}",
            token,
        )
        metadata = data.get("MediaContainer", {}).get("Metadata", [])
        row = metadata[0] if isinstance(metadata, list) and metadata else {}
        guids = row.get("Guid", []) if isinstance(row, dict) else []
        tmdb_id: int | None = None
        tvdb_id: int | None = None
        for guid in guids if isinstance(guids, list) else []:
            value = str(guid.get("id") or "") if isinstance(guid, dict) else ""
            try:
                if value.startswith("tmdb://"):
                    tmdb_id = int(value.removeprefix("tmdb://"))
                elif value.startswith("tvdb://"):
                    tvdb_id = int(value.removeprefix("tvdb://"))
            except ValueError:
                continue
        return tmdb_id, tvdb_id

    def fetch_all(self) -> list[PlexWatchlistItem]:
        token = self._token()
        users_and_rows: list[tuple[str, list[dict[str, str]]]] = [
            (self._account_name(token), self._self_watchlist(token))
        ]
        if self._include_friends:
            for friend in self._friends(token):
                users_and_rows.append(
                    (friend["name"], self._friend_watchlist(token, friend["id"]))
                )

        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for username, rows in users_and_rows:
            for row in rows:
                media_type = "show" if row["type"] == "show" else "movie"
                key = (media_type, row["id"])
                entry = grouped.setdefault(
                    key,
                    {
                        "plex_id": row["id"],
                        "title": row["title"],
                        "media_type": media_type,
                        "users": set(),
                    },
                )
                entry["users"].add(_safe_tag_part(username))

        metadata_cache: dict[str, tuple[int | None, int | None]] = {}
        output: list[PlexWatchlistItem] = []
        for entry in grouped.values():
            plex_id = str(entry["plex_id"])
            if plex_id not in metadata_cache:
                metadata_cache[plex_id] = self._metadata_ids(token, plex_id)
            tmdb_id, tvdb_id = metadata_cache[plex_id]
            output.append(
                PlexWatchlistItem(
                    plex_id=plex_id,
                    title=str(entry["title"]),
                    media_type=str(entry["media_type"]),
                    tmdb_id=tmdb_id,
                    tvdb_id=tvdb_id,
                    users=tuple(sorted(entry["users"])),
                )
            )
        self._logger.info(
            "Plex watchlists loaded: users=%s unique_items=%s",
            len(users_and_rows),
            len(output),
        )
        return output

    def protected_movie_details(self) -> dict[int, list[SeerrProtection]]:
        protected: dict[int, list[SeerrProtection]] = {}
        for item in self.fetch_all():
            if item.media_type != "movie" or item.tmdb_id is None:
                continue
            protected[item.tmdb_id] = [
                SeerrProtection(source="plex_watchlist", user=user)
                for user in item.users
            ]
        self._logger.info(
            "Direct Plex watchlist protection loaded: movies=%s",
            len(protected),
        )
        return protected
