"""Business policy decision rules for stream-sync.

Evaluates streaming availability against Radarr tags, favorite protection,
Seerr protection, and theatrical release grace periods.
"""

from __future__ import annotations

import calendar
import logging
from datetime import date, datetime
from typing import Any

from app.schemas import JwLookupResult, JwService, LookupStatus, MovieDecision, MovieState, SeerrProtection

LOGGER = logging.getLogger("stream-sync.policy")


def parse_in_cinemas_date(in_cinemas: str | None) -> date | None:
    """Parse theatrical release date string (YYYY-MM-DD or ISO timestamp) into date object."""
    if not in_cinemas:
        return None
    raw = in_cinemas.strip()
    if not raw:
        return None
    if len(raw) >= 10:
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def is_recent_theatrical_release(
    in_cinemas: str | None,
    grace_months: int,
    reference_date: date | None = None,
) -> bool:
    """Check if movie was released in cinemas within grace_months relative to reference_date."""
    if grace_months <= 0:
        return False
    cinema_date = parse_in_cinemas_date(in_cinemas)
    if cinema_date is None:
        return False
    ref = reference_date or date.today()
    if cinema_date > ref:
        return False
    cutoff_date = subtract_calendar_months(ref, grace_months)
    return cinema_date >= cutoff_date


def evaluate_movie_policy(
    movie: MovieState,
    lookup_result: JwLookupResult,
    grace_months: int = 0,
    seerr_protection: list[SeerrProtection] | None = None,
    search_cooldown_passed: bool = True,
) -> MovieDecision:
    """Evaluate business policy rules for a movie and return decision structure."""
    if movie.has_tag("favorite"):
        return MovieDecision(
            skip=True,
            reason="Favorite tag active",
            target_monitored=True,
            desired_streaming_labels=[],
            trigger_search=not movie.monitored and not movie.has_file and search_cooldown_passed,
            search_reason="favorite_unmonitored" if not movie.monitored and not movie.has_file else None,
        )

    if seerr_protection:
        source_labels = sorted(list({p.source for p in seerr_protection}))
        return MovieDecision(
            skip=True,
            reason=f"Protected by Seerr/Watchlist ({', '.join(source_labels)})",
            target_monitored=True,
            desired_streaming_labels=[],
            trigger_search=not movie.monitored and not movie.has_file and search_cooldown_passed,
            search_reason="seerr_protected_unmonitored" if not movie.monitored and not movie.has_file else None,
        )

    if is_recent_theatrical_release(movie.in_cinemas, grace_months):
        return MovieDecision(
            skip=True,
            reason=f"Recent theatrical release (released {movie.in_cinemas})",
            target_monitored=True,
            desired_streaming_labels=[],
            trigger_search=not movie.monitored and not movie.has_file and search_cooldown_passed,
            search_reason="recent_release_unmonitored" if not movie.monitored and not movie.has_file else None,
        )

    if lookup_result.status in (LookupStatus.UNKNOWN, LookupStatus.SCHEMA_ERROR):
        return MovieDecision(
            skip=True,
            reason=f"JustWatch lookup error: {lookup_result.error_message}",
        )

STREAMING_TAG_PREFIX = "streaming_"
STREAMING_TAG_PREFIX_SAFE = "streaming-"


def _current_streaming_ids(movie: MovieState) -> list[str]:
    """Extract canonical streaming service IDs (with underscores) from movie streaming tags."""
    output: list[str] = []
    for tag in movie.streaming_tags:
        label = tag.label.lower()
        if label.startswith(STREAMING_TAG_PREFIX):
            output.append(label[len(STREAMING_TAG_PREFIX) :])
        elif label.startswith(STREAMING_TAG_PREFIX_SAFE):
            raw = label[len(STREAMING_TAG_PREFIX_SAFE) :]
            output.append(raw.replace("-", "_"))
    return output


def evaluate_movie_policy(
    movie: MovieState,
    lookup_result: JwLookupResult,
    grace_months: int = 0,
    seerr_protection: list[SeerrProtection] | None = None,
    search_cooldown_passed: bool = True,
) -> MovieDecision:
    """Evaluate business policy rules for a movie and return decision structure."""
    if movie.has_tag("favorite"):
        return MovieDecision(
            skip=True,
            reason="Favorite tag active",
            target_monitored=True,
            desired_streaming_labels=[],
            trigger_search=not movie.monitored and not movie.has_file and search_cooldown_passed,
            search_reason="favorite_unmonitored" if not movie.monitored and not movie.has_file else None,
        )

    if seerr_protection:
        source_labels = sorted(list({p.source for p in seerr_protection}))
        return MovieDecision(
            skip=True,
            reason=f"Protected by Seerr/Watchlist ({', '.join(source_labels)})",
            target_monitored=True,
            desired_streaming_labels=[],
            trigger_search=not movie.monitored and not movie.has_file and search_cooldown_passed,
            search_reason="seerr_protected_unmonitored" if not movie.monitored and not movie.has_file else None,
        )

    if is_recent_theatrical_release(movie.in_cinemas, grace_months):
        return MovieDecision(
            skip=True,
            reason=f"Recent theatrical release (released {movie.in_cinemas})",
            target_monitored=True,
            desired_streaming_labels=[],
            trigger_search=not movie.monitored and not movie.has_file and search_cooldown_passed,
            search_reason="recent_release_unmonitored" if not movie.monitored and not movie.has_file else None,
        )

    if lookup_result.status in (LookupStatus.UNKNOWN, LookupStatus.SCHEMA_ERROR):
        return MovieDecision(
            skip=True,
            reason=f"JustWatch lookup error: {lookup_result.error_message}",
        )

    current_ids = _current_streaming_ids(movie)
    current_set = set(current_ids)
    had_streaming = bool(current_set)

    is_available = lookup_result.status == LookupStatus.AVAILABLE

    if is_available:
        desired_ids = sorted({service.service_id for service in lookup_result.services})
        desired_set = set(desired_ids)
        desired_labels = [
            f"{STREAMING_TAG_PREFIX_SAFE}{svc_id.replace('_', '-')}"
            for svc_id in desired_ids
        ]

        by_id = {svc.service_id: svc for svc in lookup_result.services}
        entering_services = [
            by_id[svc_id] for svc_id in desired_ids
            if svc_id not in current_set and svc_id in by_id
        ]
        leaving_svc_ids = sorted(list(current_set - desired_set))

        should_update = (current_set != desired_set) or movie.monitored
        return MovieDecision(
            skip=False,
            reason=f"Available on allowed streaming: {', '.join(svc.service_name for svc in lookup_result.services)}",
            should_update=should_update,
            desired_streaming_labels=desired_labels,
            target_monitored=False,
            entering_services=entering_services,
            leaving_service_ids=leaving_svc_ids,
        )

    # Unavailable on allowed streaming
    should_update = had_streaming or not movie.monitored
    trigger_search = (had_streaming or not movie.monitored) and not movie.has_file and search_cooldown_passed
    search_reason = "left_streaming" if had_streaming else "unmonitored_outside_whitelist"

    return MovieDecision(
        skip=False,
        reason="Not available on allowed streaming services",
        should_update=should_update,
        desired_streaming_labels=[],
        target_monitored=True,
        trigger_search=trigger_search,
        search_reason=search_reason if trigger_search else None,
        leaving_service_ids=sorted(current_ids),
    )

# Backward compatibility alias
evaluate_movie = evaluate_movie_policy
is_within_theatrical_release_grace = is_recent_theatrical_release

# Backward compatibility alias
parse_radarr_date = parse_in_cinemas_date


def subtract_calendar_months(ref_date: date, months: int) -> date:
    """Subtract calendar months from a date object."""
    year_offset = (ref_date.month - 1 - months) // 12
    new_month = (ref_date.month - 1 - months) % 12 + 1
    new_year = ref_date.year + year_offset
    max_day = calendar.monthrange(new_year, new_month)[1]
    return date(new_year, new_month, min(ref_date.day, max_day))
