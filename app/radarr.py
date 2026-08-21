"""Radarr API integration client for stream-sync.

Handles movie queries, tag creation, monitoring status toggles, automatic searches,
and favorite tag management via Radarr API.
"""

from __future__ import annotations

import logging
from typing import Iterable

from arrapi import ArrException, Invalid, RadarrAPI
from app.schemas import MovieState, TagState

LOGGER = logging.getLogger("stream-sync.radarr")


def _dedupe_keep_order(values: Iterable[int]) -> list[int]:
    """Deduplicate an iterable of integers while preserving original order."""
    output: list[int] = []
    seen: set[int] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def _optional_str_attr(obj: object, *names: str) -> str | None:
    """Extract first non-None string attribute from an object among candidate names."""
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return str(value)
    return None


def _tag_ids_from_movie_obj(movie_obj: object) -> list[int]:
    """Extract deduplicated tag ID list from a Radarr movie object."""
    return _dedupe_keep_order(getattr(movie_obj, "tagsIds", []) or [])


class RadarrClient:
    """Client wrapper for Radarr REST API interaction.

    Args:
        url: Radarr instance base URL.
        api_key: Radarr API authentication key.
    """

    def __init__(self, url: str, api_key: str) -> None:
        self._logger = LOGGER
        self._api = RadarrAPI(url, api_key)

    def _get_tag_maps(self) -> tuple[dict[int, str], dict[str, int]]:
        """Fetch all Radarr tags and build ID-to-label and label-to-ID mappings."""
        id_to_label: dict[int, str] = {}
        label_to_id: dict[str, int] = {}
        for tag in self._api.all_tags():
            id_to_label[int(tag.id)] = str(tag.label)
            label_to_id[str(tag.label).lower()] = int(tag.id)
        return id_to_label, label_to_id

    def list_movies(self) -> list[MovieState]:
        """Fetch all movies from Radarr and map them to MovieState DTOs."""
        id_to_label, _ = self._get_tag_maps()
        output: list[MovieState] = []

        for movie in self._api.all_movies():
            tag_ids = _dedupe_keep_order(getattr(movie, "tagsIds", []) or [])
            tags: list[TagState] = []
            for tag_id in tag_ids:
                label = id_to_label.get(int(tag_id), f"tag_{tag_id}")
                tags.append(TagState(id=int(tag_id), label=label))

            output.append(
                MovieState(
                    movie_id=int(movie.id),
                    tmdb_id=int(movie.tmdbId) if movie.tmdbId is not None else None,
                    title=str(movie.title),
                    year=int(movie.year) if movie.year is not None else None,
                    path=(
                        str(getattr(movie, "path"))
                        if getattr(movie, "path", None) is not None
                        else None
                    ),
                    monitored=bool(movie.monitored),
                    has_file=bool(getattr(movie, "hasFile", False)),
                    status=(
                        str(getattr(movie, "status"))
                        if getattr(movie, "status", None) is not None
                        else None
                    ),
                    in_cinemas=_optional_str_attr(movie, "inCinemas", "in_cinemas"),
                    tags=tags,
                )
            )
        return output

    def _ensure_tag_ids(self, labels: list[str]) -> list[int]:
        """Get tag IDs for specified labels, creating missing tags in Radarr as needed."""
        _, label_to_id = self._get_tag_maps()
        desired_ids: list[int] = []
        for label in labels:
            key = label.lower()
            tag_id = label_to_id.get(key)
            if tag_id is None:
                created = self._api.create_tag(key)
                tag_id = int(created.id)
                label_to_id[key] = tag_id
                self._logger.info("Tag created in Radarr: %s (id=%s)", key, tag_id)
            desired_ids.append(tag_id)
        return _dedupe_keep_order(desired_ids)

    def set_monitored(self, movie_id: int, monitored: bool) -> bool:
        """Update monitored status for a movie in Radarr if it differs."""
        movie_obj = self._api.get_movie(movie_id=movie_id)
        current_monitored = bool(getattr(movie_obj, "monitored", False))
        if current_monitored == monitored:
            return False

        movie_obj.edit(monitored=monitored)
        self._logger.info(
            "Movie monitored status updated in Radarr: movie_id=%s monitored=%s",
            movie_id,
            monitored,
        )
        return True

    def get_movie_state(self, movie_id: int) -> MovieState:
        """Fetch current MovieState DTO for a specific Radarr movie ID."""
        id_to_label, _ = self._get_tag_maps()
        movie = self._api.get_movie(movie_id=movie_id)
        tag_ids = _dedupe_keep_order(getattr(movie, "tagsIds", []) or [])
        tags = [
            TagState(id=int(tag_id), label=id_to_label.get(int(tag_id), f"tag_{tag_id}"))
            for tag_id in tag_ids
        ]
        return MovieState(
            movie_id=int(movie.id),
            tmdb_id=int(movie.tmdbId) if movie.tmdbId is not None else None,
            title=str(movie.title),
            year=int(movie.year) if movie.year is not None else None,
            path=(
                str(getattr(movie, "path"))
                if getattr(movie, "path", None) is not None
                else None
            ),
            monitored=bool(movie.monitored),
            has_file=bool(getattr(movie, "hasFile", False)),
            status=(
                str(getattr(movie, "status"))
                if getattr(movie, "status", None) is not None
                else None
            ),
            in_cinemas=_optional_str_attr(movie, "inCinemas", "in_cinemas"),
            tags=tags,
        )

    def set_favorite(self, movie_id: int, favorite: bool) -> MovieState:
        """Add or remove favorite tag on a movie in Radarr."""
        favorite_ids = self._ensure_tag_ids(["favorite"])
        favorite_id = favorite_ids[0]
        movie_obj = self._api.get_movie(movie_id=movie_id)
        id_to_label, _ = self._get_tag_maps()
        current_tag_ids = _tag_ids_from_movie_obj(movie_obj)
        final_tag_ids: list[int] = []
        for tag_id in current_tag_ids:
            label = id_to_label.get(tag_id, f"tag_{tag_id}").lower()
            if label.startswith("streaming_") or label.startswith("streaming-"):
                continue
            if label == "favorite" and not favorite:
                continue
            final_tag_ids.append(tag_id)
        if favorite and favorite_id not in final_tag_ids:
            final_tag_ids.append(favorite_id)
        final_tag_ids = _dedupe_keep_order(final_tag_ids)

        if current_tag_ids != final_tag_ids:
            movie_obj.edit(tags=final_tag_ids, apply_tags="replace")
            self._logger.info(
                "Movie favorite tag updated in Radarr: movie_id=%s favorite=%s tags=%s",
                movie_id,
                favorite,
                final_tag_ids,
            )
        return self.get_movie_state(movie_id)

    def reconcile_and_update_movie(
        self,
        movie: MovieState,
        desired_streaming_labels: list[str],
        monitored: bool,
    ) -> bool:
        """Reconcile streaming tags and monitored state for a movie in Radarr."""
        desired_streaming_ids = self._ensure_tag_ids(desired_streaming_labels)
        movie_obj = self._api.get_movie(movie_id=movie.movie_id)
        id_to_label, _ = self._get_tag_maps()
        current_tag_ids = _tag_ids_from_movie_obj(movie_obj)
        user_tag_ids = []
        for tag_id in current_tag_ids:
            label = id_to_label.get(tag_id, f"tag_{tag_id}").lower()
            if label.startswith("streaming_") or label.startswith("streaming-"):
                continue
            user_tag_ids.append(tag_id)
        final_tag_ids = _dedupe_keep_order(user_tag_ids + desired_streaming_ids)
        current_monitored = bool(getattr(movie_obj, "monitored", movie.monitored))

        if current_tag_ids == final_tag_ids and current_monitored == monitored:
            return False

        movie_obj.edit(tags=final_tag_ids, apply_tags="replace", monitored=monitored)
        self._logger.info(
            "Movie updated in Radarr: %s (id=%s) monitored=%s tags=%s",
            movie.title,
            movie.movie_id,
            monitored,
            final_tag_ids,
        )
        return True

    def trigger_search(self, movie_id: int) -> str:
        """Trigger an automated movie search command in Radarr."""
        attempts = [
            ("MoviesSearch", {"movieIds": [movie_id]}),
            ("MovieSearch", {"movieIds": [movie_id]}),
            ("MovieSearch", {"movieId": movie_id}),
        ]

        last_error: Exception | None = None
        for command_name, payload in attempts:
            try:
                self._api.send_command(command_name, **payload)
                self._logger.info(
                    "Search triggered in Radarr for movie_id=%s using command=%s",
                    movie_id,
                    command_name,
                )
                return command_name
            except (ArrException, Invalid, ValueError) as exc:
                last_error = exc
                self._logger.warning(
                    "Failed to send command %s for movie_id=%s: %s",
                    command_name,
                    movie_id,
                    exc,
                )

        raise RuntimeError(
            f"Could not trigger search in Radarr for movie_id={movie_id}"
        ) from last_error

    def trigger_rescan(self, movie_id: int) -> str | None:
        """Trigger a rescan/refresh command in Radarr for a specific movie ID."""
        attempts = [
            ("RescanMovie", {"movieId": movie_id}),
            ("RescanMovies", {"movieIds": [movie_id]}),
            ("RefreshMovie", {"movieId": movie_id}),
        ]

        for command_name, payload in attempts:
            try:
                self._api.send_command(command_name, **payload)
                self._logger.info(
                    "Rescan/refresh triggered in Radarr for movie_id=%s using command=%s",
                    movie_id,
                    command_name,
                )
                return command_name
            except (ArrException, Invalid, ValueError) as exc:
                self._logger.warning(
                    "Failed to send rescan command %s for movie_id=%s: %s",
                    command_name,
                    movie_id,
                    exc,
                )

        self._logger.warning(
            "No rescan command succeeded for movie_id=%s. Continuing without rescan.",
            movie_id,
        )
        return None
