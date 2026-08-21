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
    deletion_payload: dict[str, Any] | None = None
    if deletion_state is not None:
        if hasattr(deletion_state, "model_dump"):
            deletion_payload = deletion_state.model_dump()
        elif hasattr(deletion_state, "_asdict"):
            deletion_payload = deletion_state._asdict()
        elif isinstance(deletion_state, dict):
            deletion_payload = deletion_state
        else:
            deletion_payload = {
                "radarrId": getattr(deletion_state, "radarr_id", getattr(deletion_state, "radarrId", None)),
                "moviePath": getattr(deletion_state, "movie_path", getattr(deletion_state, "moviePath", None)),
                "scheduledAt": getattr(deletion_state, "scheduled_at", getattr(deletion_state, "scheduledAt", None)),
                "deleteAfterTs": getattr(deletion_state, "delete_after_ts", getattr(deletion_state, "deleteAfterTs", None)),
                "lastStatus": getattr(deletion_state, "last_status", getattr(deletion_state, "lastStatus", None)),
                "updatedAt": getattr(deletion_state, "updated_at", getattr(deletion_state, "updatedAt", None)),
            }

    formatted_streaming: list[dict[str, Any]] = []
    for svc in (streaming_services or []):
        if hasattr(svc, "model_dump"):
            formatted_streaming.append(svc.model_dump())
        elif hasattr(svc, "_asdict"):
            formatted_streaming.append(svc._asdict())
        elif isinstance(svc, dict):
            formatted_streaming.append(svc)
        else:
            formatted_streaming.append({
                "service_id": getattr(svc, "service_id", getattr(svc, "serviceId", "")),
                "service_name": getattr(svc, "service_name", getattr(svc, "serviceName", "")),
            })

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
        "deletionState": deletion_payload,
        "streamingServices": formatted_streaming,
        "protection": [
            item.model_dump() if hasattr(item, "model_dump")
            else (item._asdict() if hasattr(item, "_asdict")
            else {"source": getattr(item, "source", ""), "user": getattr(item, "user", None)})
            for item in (protection or [])
        ],
    }
