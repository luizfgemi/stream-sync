"""Movie snapshot serialization helpers."""

from __future__ import annotations

import calendar
from typing import Any
from app.schemas import MovieState


def deletion_state_payload(
    deletion_state: Any | None,
    now_ts: int,
) -> dict[str, Any] | None:
    """Format DeletionStateRow into API-compliant payload dictionary."""
    if deletion_state is None:
        return None

    status = (
        getattr(deletion_state, "last_status", None)
        or getattr(deletion_state, "status", None)
        or (deletion_state.get("last_status") if isinstance(deletion_state, dict) else None)
        or (deletion_state.get("status") if isinstance(deletion_state, dict) else "scheduled")
    )
    scheduled_at = (
        getattr(deletion_state, "scheduled_at", None)
        or getattr(deletion_state, "scheduledAt", None)
        or (deletion_state.get("scheduled_at") if isinstance(deletion_state, dict) else None)
        or (deletion_state.get("scheduledAt") if isinstance(deletion_state, dict) else 0)
    )
    delete_after_ts = (
        getattr(deletion_state, "delete_after_ts", None)
        or getattr(deletion_state, "deleteAfterTs", None)
        or (deletion_state.get("delete_after_ts") if isinstance(deletion_state, dict) else None)
        or (deletion_state.get("deleteAfterTs") if isinstance(deletion_state, dict) else 0)
    )
    updated_at = (
        getattr(deletion_state, "updated_at", None)
        or getattr(deletion_state, "updatedAt", None)
        or (deletion_state.get("updated_at") if isinstance(deletion_state, dict) else None)
        or (deletion_state.get("updatedAt") if isinstance(deletion_state, dict) else 0)
    )

    remaining_seconds = max(0, delete_after_ts - now_ts) if status == "scheduled" else 0

    return {
        "status": status,
        "scheduledAt": scheduled_at,
        "deleteAfterTs": delete_after_ts,
        "updatedAt": updated_at,
        "remainingSeconds": remaining_seconds,
    }


def movie_snapshot_payload(
    movie: MovieState,
    conditions: list[str],
    last_evaluated_at: int,
    deletion_state: Any | None = None,
    streaming_services: list[Any] | None = None,
    protection: list[Any] | None = None,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Format movie state and active conditions into API-compliant snapshot payload dictionary."""
    formatted_streaming: list[dict[str, Any]] = []
    for svc in (streaming_services or []):
        service_id = (
            getattr(svc, "service_id", None)
            or getattr(svc, "id", None)
            or (svc.get("service_id") if isinstance(svc, dict) else None)
            or (svc.get("id") if isinstance(svc, dict) else "")
        )
        service_name = (
            getattr(svc, "service_name", None)
            or getattr(svc, "name", None)
            or (svc.get("service_name") if isinstance(svc, dict) else None)
            or (svc.get("name") if isinstance(svc, dict) else "")
        )
        formatted_streaming.append({"id": service_id, "name": service_name})

    return {
        "radarrId": movie.movie_id,
        "tmdbId": movie.tmdb_id,
        "title": movie.title,
        "year": movie.year,
        "hasFile": movie.has_file,
        "monitored": movie.monitored,
        "path": movie.path,
        "tags": [tag.label if hasattr(tag, "label") else str(tag) for tag in movie.tags],
        "conditions": sorted(set(conditions)),
        "lastEvaluatedAt": last_evaluated_at,
        "deletionState": deletion_state_payload(deletion_state, last_evaluated_at),
        "streamingServices": formatted_streaming,
        "protection": [
            item.model_dump() if hasattr(item, "model_dump")
            else (item._asdict() if hasattr(item, "_asdict")
            else {"source": getattr(item, "source", ""), "user": getattr(item, "user", None)})
            for item in (protection or [])
        ],
    }
