from __future__ import annotations

from .types import JwService, LookupStatus, MovieDecision, MovieState, JwLookupResult

STREAMING_TAG_PREFIX = "streaming_"
STREAMING_TAG_PREFIX_SAFE = "streaming-"


def _current_streaming_ids(movie: MovieState) -> list[str]:
    output: list[str] = []
    for tag in movie.streaming_tags:
        label = tag.label.lower()
        if label.startswith(STREAMING_TAG_PREFIX):
            output.append(label[len(STREAMING_TAG_PREFIX) :])
        elif label.startswith(STREAMING_TAG_PREFIX_SAFE):
            raw = label[len(STREAMING_TAG_PREFIX_SAFE) :]
            output.append(raw.replace("-", "_"))
    return output


def evaluate_movie(movie: MovieState, jw_result: JwLookupResult) -> MovieDecision:
    if movie.has_tag("favorite"):
        return MovieDecision(skip=True, reason="favorite")

    current_ids = _current_streaming_ids(movie)
    current_set = set(current_ids)

    if jw_result.status == LookupStatus.UNKNOWN:
        return MovieDecision(skip=True, reason="unknown")
    if jw_result.status == LookupStatus.SCHEMA_ERROR:
        return MovieDecision(skip=True, reason="schema_error")

    if jw_result.status == LookupStatus.AVAILABLE:
        desired_ids = sorted({service.service_id for service in jw_result.services})
        desired_set = set(desired_ids)
        desired_labels = [
            f"{STREAMING_TAG_PREFIX_SAFE}{service_id.replace('_', '-')}"
            for service_id in desired_ids
        ]
        should_update = bool(movie.monitored) or current_set != desired_set

        entering_services: list[JwService] = []
        if not current_set and desired_set:
            by_id = {service.service_id: service for service in jw_result.services}
            entering_services = [by_id[service_id] for service_id in desired_ids if service_id in by_id]

        return MovieDecision(
            should_update=should_update,
            desired_streaming_labels=desired_labels,
            target_monitored=False,
            entering_services=entering_services,
        )

    had_streaming = bool(current_set)
    should_update = had_streaming or (not movie.monitored)
    should_trigger_search = had_streaming or (not movie.monitored)
    search_reason = "left_streaming" if had_streaming else "unmonitored_outside_whitelist"
    return MovieDecision(
        should_update=should_update,
        desired_streaming_labels=[],
        target_monitored=True,
        trigger_search=should_trigger_search,
        search_reason=search_reason if should_trigger_search else None,
        leaving_service_ids=sorted(current_set),
    )
