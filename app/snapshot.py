from __future__ import annotations

from .types import DeletionStateRow, JwService, MovieState, SeerrProtection


def deletion_state_payload(
    deletion_state: DeletionStateRow | None,
    now_ts: int,
) -> dict[str, object] | None:
    if deletion_state is None:
        return None
    return {
        "status": deletion_state.last_status,
        "scheduledAt": deletion_state.scheduled_at,
        "deleteAfterTs": deletion_state.delete_after_ts,
        "updatedAt": deletion_state.updated_at,
        "remainingSeconds": max(0, deletion_state.delete_after_ts - now_ts)
        if deletion_state.last_status == "scheduled"
        else 0,
    }


def movie_snapshot_payload(
    movie: MovieState,
    conditions: list[str],
    last_evaluated_at: int,
    streaming_services: list[JwService] | None = None,
    deletion_state: DeletionStateRow | None = None,
    protection: list[SeerrProtection] | None = None,
) -> dict[str, object]:
    return {
        "radarrId": movie.movie_id,
        "tmdbId": movie.tmdb_id,
        "title": movie.title,
        "year": movie.year,
        "hasFile": movie.has_file,
        "monitored": movie.monitored,
        "path": movie.path,
        "tags": movie.tag_labels,
        "streamingServices": [
            {"id": service.service_id, "name": service.service_name}
            for service in (streaming_services or [])
        ],
        "conditions": sorted(set(conditions)),
        "deletionState": deletion_state_payload(deletion_state, last_evaluated_at),
        "protection": [
            {"source": item.source, "user": item.user}
            for item in (protection or [])
        ],
        "lastEvaluatedAt": last_evaluated_at,
    }
