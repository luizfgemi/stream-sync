"""Movie snapshot serialization helpers."""

from __future__ import annotations

import calendar
from typing import Any
from app.schemas import MovieState


def movie_snapshot_payload(
    movie: MovieState,
    conditions: list[str],
    last_evaluated_at: int,
    deletion_state: dict[str, Any] | None = None,
    streaming_services: list[dict[str, Any]] | None = None,
    protection: list[dict[str, Any]] | None = None,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Format movie state and active conditions into snapshot payload dictionary."""
    return {
        "radarrId": movie.movie_id,
        "tmdbId": movie.tmdb_id,
        "title": movie.title,
        "year": movie.year,
        "path": movie.path,
        "monitored": movie.monitored,
        "hasFile": movie.has_file,
        "status": movie.status,
        "inCinemas": movie.in_cinemas,
        "tags": [tag.label if hasattr(tag, "label") else str(tag) for tag in movie.tags],
        "conditions": conditions,
        "lastEvaluatedAt": last_evaluated_at,
        "deletionState": deletion_state,
        "streamingServices": streaming_services or [],
        "protection": [
            item.model_dump() if hasattr(item, "model_dump")
            else (item._asdict() if hasattr(item, "_asdict")
            else {"source": getattr(item, "source", ""), "user": getattr(item, "user", None)})
            for item in (protection or [])
        ],
    }
