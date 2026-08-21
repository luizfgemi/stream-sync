from __future__ import annotations

import logging
import os
import signal
import shutil
import threading
import time
from collections import defaultdict

from .cache_sqlite import SQLiteCache
from .config import Config
from .justwatch_provider import JustWatchProvider
from .log import setup_logging
from .notifier import Notifier, build_notifier, service_name_from_id
from .policy import evaluate_movie
from .recent_release import is_within_theatrical_release_grace
from .radarr_client import RadarrClient
from .seerr_client import SeerrClient
from .plex_watchlist import PlexWatchlistClient
from .snapshot import movie_snapshot_payload
from .types import CycleStats, LookupStatus, MovieState, SeerrProtection

def _parse_allowed_services(csv_value: str) -> set[str]:
    return {item.strip().lower() for item in csv_value.split(",") if item.strip()}


def _validate_allowed_services(
    allowed_services: set[str],
    justwatch: JustWatchProvider,
    logger: logging.Logger,
    country: str,
) -> bool:
    if not allowed_services:
        return True
    try:
        available_services = justwatch.list_country_services(country)
    except Exception as exc:
        logger.error(
            "Could not validate JW_ALLOWED_SERVICES at startup: %s. "
            "Running in safe mode (no changes will be applied).",
            exc,
        )
        return False

    available_ids = {service.service_id.lower() for service in available_services}
    invalid_services = sorted(service for service in allowed_services if service not in available_ids)
    if not invalid_services:
        return True

    logger.error(
        "Invalid JW_ALLOWED_SERVICES for country=%s: %s",
        country.upper(),
        ",".join(invalid_services),
    )
    logger.info(
        "Run MODE=list_services to discover available services, then configure JW_ALLOWED_SERVICES."
    )
    logger.info(
        "Available service IDs for country=%s: %s",
        country.upper(),
        ",".join(sorted(available_ids)),
    )
    return False


def _is_released(status: str | None) -> bool:
    return str(status or "").strip().lower() == "released"


def _install_signal_handlers(logger: logging.Logger) -> threading.Event:
    stop_event = threading.Event()

    def _handle_signal(signum: int, _frame: object) -> None:
        if stop_event.is_set():
            return
        signame = signal.Signals(signum).name
        logger.info("Received %s. Stopping stream-sync gracefully.", signame)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    return stop_event


def _delete_movie_folder(path: str, dry_run: bool) -> tuple[bool, str | None]:
    target_path = os.path.normpath(path.strip())
    if not target_path:
        return False, "empty_path"
    parent = os.path.dirname(target_path)
    if target_path in {"/", "."} or parent in {"", "/"}:
        return False, "invalid_path"
    if not os.path.exists(target_path):
        return True, "already_missing"
    if not os.path.isdir(target_path):
        return False, "not_a_directory"
    if dry_run:
        return True, None

    shutil.rmtree(target_path)
    return True, None


def _format_duration_human(seconds: float) -> str:
    if seconds <= 0:
        return "0s"
    if seconds < 60:
        return f"{int(round(seconds))}s"

    total_seconds = int(round(seconds))
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)

    parts: list[str] = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0 and not parts:
        parts.append(f"{secs}s")
    elif secs > 0 and len(parts) < 3:
        parts.append(f"{secs}s")

    return " ".join(parts) if parts else "0s"


def _format_time_until(now_ts: int, target_ts: int) -> str:
    remaining_seconds = max(0, int(target_ts) - int(now_ts))
    return _format_duration_human(remaining_seconds)


def _format_services_human(service_ids: list[str]) -> str:
    names = [service_name_from_id(service_id) for service_id in service_ids if service_id]
    unique_names = sorted(set(names), key=str.lower)
    if not unique_names:
        return "streaming"
    if len(unique_names) == 1:
        return unique_names[0]
    if len(unique_names) == 2:
        return f"{unique_names[0]} and {unique_names[1]}"
    return f"{', '.join(unique_names[:-1])}, and {unique_names[-1]}"


def _is_recent_theatrical_release(movie: MovieState, config: Config) -> bool:
    return is_within_theatrical_release_grace(
        movie.in_cinemas,
        config.theatrical_release_grace_months,
    )


def _format_bytes_human(size_bytes: int) -> str:
    if size_bytes <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    value = float(size_bytes)
    unit_idx = 0
    while value >= 1024 and unit_idx < len(units) - 1:
        value /= 1024.0
        unit_idx += 1
    if unit_idx == 0:
        return f"{int(value)} {units[unit_idx]}"
    return f"{value:.2f} {units[unit_idx]}"


def _cycle_stats_payload(stats: CycleStats) -> dict[str, int]:
    return {
        "processed": int(stats.processed),
        "favorites": int(stats.favorite_skipped),
        "seerrProtected": int(stats.seerr_protected),
        "recentProtected": int(stats.recent_protected),
        "changed": int(stats.changed),
        "searches": int(stats.search_triggered),
        "unknown": int(stats.unknown),
        "schemaErrors": int(stats.schema_errors),
        "errors": int(stats.errors),
    }


def _movie_status_payload(movie: MovieState) -> dict[str, object]:
    return {
        "radarrId": movie.movie_id,
        "tmdbId": movie.tmdb_id,
        "title": movie.title,
        "year": movie.year,
    }


def _deletion_queue_payload(
    scheduled: int = 0,
    due_now: int = 0,
    potential_savings_bytes: int = 0,
    sized_paths: int = 0,
    missing_or_invalid_paths: int = 0,
) -> dict[str, object]:
    return {
        "scheduled": int(scheduled),
        "dueNow": int(due_now),
        "potentialSavingsBytes": int(potential_savings_bytes),
        "potentialSavings": _format_bytes_human(int(potential_savings_bytes)),
        "sizedPaths": int(sized_paths),
        "missingOrInvalidPaths": int(missing_or_invalid_paths),
    }


def _finish_cycle_status(
    cache: SQLiteCache,
    stats: CycleStats,
    processed: int,
    total: int,
) -> None:
    finished_at = int(time.time())
    stats_payload = _cycle_stats_payload(stats)
    cache.set_daemon_status(
        {
            "lastCycleFinishedAt": finished_at,
            "currentMovie": None,
            "progress": {"processed": int(processed), "total": int(total)},
            "lastCycleStats": stats_payload,
        }
    )
    cache.set_runtime_state("last_cycle_finished_at", str(finished_at))
    cache.append_runtime_event(
        "cycle_finished",
        {"finishedAt": finished_at, "stats": stats_payload},
    )


def _directory_size_bytes(path: str) -> int | None:
    target_path = os.path.normpath(path.strip())
    if not target_path or not os.path.isdir(target_path):
        return None

    total = 0
    stack: list[str] = [target_path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _today_yyyymmdd() -> int:
    return int(time.strftime("%Y%m%d"))


def _notification_prefix(config: Config, allow_mutations: bool) -> str:
    if config.dry_run:
        return "[DRY_RUN]"
    if not allow_mutations:
        return "[SAFE_MODE]"
    return ""


def _wait_until_next_cycle(
    stop_event: threading.Event,
    logger: logging.Logger,
    target_wall_ts: float,
    poll_interval_seconds: float = 60.0,
) -> bool:
    """
    Wait until a wall-clock deadline using short polling intervals.

    A single long Event.wait(timeout) can drift after host suspend/resume.
    Polling in short chunks keeps schedule closer to wall clock.
    """
    poll_interval = max(1.0, float(poll_interval_seconds))
    while not stop_event.is_set():
        remaining = target_wall_ts - time.time()
        if remaining <= 0:
            return False
        if stop_event.wait(min(remaining, poll_interval)):
            logger.info("Stop requested during wait. Exiting daemon loop.")
            return True
    logger.info("Stop requested during wait. Exiting daemon loop.")
    return True


def _run_cycle(
    config: Config,
    cache: SQLiteCache,
    radarr: RadarrClient,
    justwatch: JustWatchProvider,
    notifier: Notifier,
    logger: logging.Logger,
    stop_event: threading.Event,
    seerr: SeerrClient | None = None,
    plex_watchlists: PlexWatchlistClient | None = None,
    allow_mutations: bool = True,
) -> CycleStats:
    stats = CycleStats()
    cycle_started_at = int(time.time())
    grouped_entering: dict[str, list[str]] = defaultdict(list)
    grouped_leaving: dict[str, list[str]] = defaultdict(list)
    validated_due_deletions: dict[int, MovieState] = {}
    allowed_services = _parse_allowed_services(config.jw_allowed_services)
    protected_tmdb_ids: set[int] = set()
    protected_details: dict[int, list[SeerrProtection]] = {}
    snapshot_rows: list[dict[str, object]] = []
    seerr_failed = False
    plex_watchlists_failed = False
    safe_mode_reason: str | None = None

    cache.set_daemon_status(
        {
            "state": "scanning",
            "cycleStartedAt": cycle_started_at,
            "nextCycleAt": None,
            "currentMovie": None,
            "progress": {"processed": 0, "total": 0},
            "deletionQueue": _deletion_queue_payload(),
            "safeMode": {"active": False, "reason": None},
        }
    )
    cache.append_runtime_event(
        "cycle_started",
        {"startedAt": cycle_started_at, "mode": "full_scan"},
    )

    if not allowed_services:
        safe_mode_reason = "jw_allowed_services_missing"
        cache.set_daemon_status(
            {"safeMode": {"active": True, "reason": safe_mode_reason}}
        )
        cache.append_runtime_event(
            "safe_mode_entered",
            {"reason": safe_mode_reason},
        )
        logger.warning(
            "JW_ALLOWED_SERVICES whitelist is not configured. No changes will be applied."
        )
        logger.warning(
            "Run MODE=list_services to discover available services, then configure JW_ALLOWED_SERVICES."
        )
        _finish_cycle_status(cache, stats, 0, 0)
        return stats
    if not allow_mutations:
        safe_mode_reason = "invalid_jw_allowed_services"
        cache.set_daemon_status(
            {"safeMode": {"active": True, "reason": safe_mode_reason}}
        )
        cache.append_runtime_event(
            "safe_mode_entered",
            {"reason": safe_mode_reason},
        )
        logger.warning(
            "JW_ALLOWED_SERVICES contains invalid IDs. Running in safe mode: no changes will be applied."
        )

    if config.seerr_enabled:
        if seerr is None:
            seerr_failed = True
            allow_mutations = False
            safe_mode_reason = "seerr_client_missing"
            cache.set_daemon_status(
                {"safeMode": {"active": True, "reason": safe_mode_reason}}
            )
            cache.append_runtime_event("seerr_failed", {"reason": safe_mode_reason})
            logger.error(
                "SEERR_ENABLED=true but Seerr client is not configured. "
                "Running in safe mode for this cycle."
            )
        else:
            try:
                if hasattr(seerr, "protected_movie_details"):
                    protected_details = seerr.protected_movie_details()
                    protected_tmdb_ids = set(protected_details.keys())
                else:
                    protected_tmdb_ids = seerr.protected_movie_tmdb_ids()
                    protected_details = {
                        tmdb_id: [SeerrProtection(source="seerr_protected")]
                        for tmdb_id in protected_tmdb_ids
                    }
            except Exception as exc:
                seerr_failed = True
                allow_mutations = False
                safe_mode_reason = "seerr_unavailable"
                cache.set_daemon_status(
                    {"safeMode": {"active": True, "reason": safe_mode_reason}}
                )
                cache.append_runtime_event(
                    "seerr_failed",
                    {"reason": safe_mode_reason, "error": str(exc)[:500]},
                )
                logger.exception(
                    "Could not load Seerr protected movies. "
                    "Running in safe mode for this cycle: %s",
                    exc,
                )

    if config.plex_watchlist_sync_enabled:
        if plex_watchlists is None:
            plex_watchlists_failed = True
            allow_mutations = False
            safe_mode_reason = "plex_watchlist_client_missing"
            cache.set_daemon_status(
                {"safeMode": {"active": True, "reason": safe_mode_reason}}
            )
            cache.append_runtime_event(
                "plex_watchlist_failed", {"reason": safe_mode_reason}
            )
            logger.error(
                "Direct Plex watchlist protection is enabled but the client is "
                "not configured. Running in safe mode for this cycle."
            )
        else:
            try:
                plex_details = plex_watchlists.protected_movie_details()
                for tmdb_id, protections in plex_details.items():
                    existing = protected_details.setdefault(tmdb_id, [])
                    existing_keys = {(item.source, item.user) for item in existing}
                    for protection in protections:
                        key = (protection.source, protection.user)
                        if key not in existing_keys:
                            existing.append(protection)
                            existing_keys.add(key)
                protected_tmdb_ids = set(protected_details.keys())
            except Exception as exc:
                plex_watchlists_failed = True
                allow_mutations = False
                safe_mode_reason = "plex_watchlist_unavailable"
                cache.set_daemon_status(
                    {"safeMode": {"active": True, "reason": safe_mode_reason}}
                )
                cache.append_runtime_event(
                    "plex_watchlist_failed",
                    {"reason": safe_mode_reason, "error": str(exc)[:500]},
                )
                logger.exception(
                    "Could not load direct Plex watchlist protection. "
                    "Running in safe mode for this cycle: %s",
                    exc,
                )

    cache.purge_expired()
    movies = radarr.list_movies()
    movies.sort(key=lambda movie: movie.movie_id)
    cache.set_daemon_status({"progress": {"processed": 0, "total": len(movies)}})
    prune_stats = cache.prune_orphan_movie_state({movie.movie_id for movie in movies})
    if any(prune_stats.values()):
        logger.info(
            "Pruned orphan state: deletion_state=%s search_next_allowed=%s deletion_countdown_logged_day=%s",
            prune_stats["deletion_state"],
            prune_stats["search_next_allowed"],
            prune_stats["deletion_countdown_logged_day"],
        )
    movies_by_id = {movie.movie_id: movie for movie in movies}

    if config.remove_mode == "delete":
        now_ts = int(time.time())
        scheduled_rows = cache.list_scheduled_deletions()
        due_now = 0
        potential_savings_bytes = 0
        sized_paths_count = 0
        missing_or_invalid_paths = 0

        for row in scheduled_rows:
            if int(row.delete_after_ts) <= now_ts:
                due_now += 1
            movie_info = movies_by_id.get(row.radarr_id)
            target_path = row.movie_path
            if movie_info and movie_info.path:
                target_path = movie_info.path
            size_bytes = _directory_size_bytes(target_path)
            if size_bytes is not None:
                potential_savings_bytes += int(size_bytes)
                sized_paths_count += 1
            else:
                missing_or_invalid_paths += 1

        logger.info(
            "Deletion queue: scheduled=%s due_now=%s potential_savings=%s (sized_paths=%s missing_or_invalid_paths=%s)",
            len(scheduled_rows),
            due_now,
            _format_bytes_human(potential_savings_bytes),
            sized_paths_count,
            missing_or_invalid_paths,
        )
        cache.set_daemon_status(
            {
                "deletionQueue": _deletion_queue_payload(
                    scheduled=len(scheduled_rows),
                    due_now=due_now,
                    potential_savings_bytes=potential_savings_bytes,
                    sized_paths=sized_paths_count,
                    missing_or_invalid_paths=missing_or_invalid_paths,
                )
            }
        )

    if not movies:
        logger.info("No movies found in Radarr for this cycle.")
        cache.upsert_movie_snapshots([], set())
        _finish_cycle_status(cache, stats, 0, 0)
        return stats

    logger.info("Cycle started: total_movies=%s mode=full_scan", len(movies))

    eligible_non_favorite = 0
    unknown_count = 0
    schema_error_found = False

    for movie in movies:
        if stop_event.is_set():
            logger.info("Stop requested during movie scan. Aborting cycle.")
            break
        try:
            stats.processed += 1
            snapshot_conditions: list[str] = []
            cache.set_daemon_status(
                {
                    "currentMovie": _movie_status_payload(movie),
                    "progress": {
                        "processed": stats.processed,
                        "total": len(movies),
                    },
                }
            )

            if movie.has_tag("favorite"):
                stats.favorite_skipped += 1
                deletion_state = cache.get_deletion_state(movie.movie_id)
                if (
                    deletion_state is not None
                    and deletion_state.last_status == "scheduled"
                ):
                    if config.dry_run or not allow_mutations:
                        prefix = "[DRY_RUN]" if config.dry_run else "[SAFE_MODE]"
                        logger.info(
                            "%s Deletion countdown canceled by favorite: %s",
                            prefix,
                            movie.title,
                        )
                    else:
                        cache.delete_deletion_state(movie.movie_id)
                        cache.clear_deletion_countdown_logged_day(movie.movie_id)
                        logger.info(
                            "✅ Deletion countdown canceled by favorite: %s",
                            movie.title,
                        )
                    cache.append_runtime_event(
                        "deletion_suppressed",
                        {
                            "reason": "favorite",
                            "movie": _movie_status_payload(movie),
                        },
                    )

                if config.dry_run or not allow_mutations:
                    prefix = "[DRY_RUN]" if config.dry_run else "[SAFE_MODE]"
                    if config.dry_run and (
                        not movie.monitored or bool(movie.streaming_tags)
                    ):
                        stats.changed += 1
                    logger.info(
                        "%s Favorite override for '%s' (id=%s): monitored=true and streaming tags removed",
                        prefix,
                        movie.title,
                        movie.movie_id,
                    )
                else:
                    updated = radarr.reconcile_and_update_movie(
                        movie=movie,
                        desired_streaming_labels=[],
                        monitored=True,
                    )
                    if updated:
                        stats.changed += 1

                if not movie.monitored:
                    if movie.has_file:
                        logger.info(
                            "Favorite movie '%s' (id=%s) is already downloaded (has_file=true). Search skipped.",
                            movie.title,
                            movie.movie_id,
                        )
                    else:
                        if config.dry_run or not allow_mutations:
                            prefix = "[DRY_RUN]" if config.dry_run else "[SAFE_MODE]"
                            logger.info(
                                "%s Favorite search trigger for '%s' (id=%s)",
                                prefix,
                                movie.title,
                                movie.movie_id,
                            )
                        else:
                            radarr.trigger_search(movie.movie_id)
                            stats.search_triggered += 1

                snapshot_rows.append(
                    movie_snapshot_payload(
                        movie,
                        ["favorite"],
                        int(time.time()),
                        deletion_state=cache.get_deletion_state(movie.movie_id),
                    )
                )
                continue

            if movie.tmdb_id is not None and movie.tmdb_id in protected_tmdb_ids:
                stats.seerr_protected += 1
                deletion_state = cache.get_deletion_state(movie.movie_id)
                movie_protection = protected_details.get(
                    movie.tmdb_id,
                    [SeerrProtection(source="seerr_protected")],
                )
                if (
                    deletion_state is not None
                    and deletion_state.last_status == "scheduled"
                ):
                    if config.dry_run or not allow_mutations:
                        prefix = "[DRY_RUN]" if config.dry_run else "[SAFE_MODE]"
                        logger.info(
                            "%s Deletion countdown canceled by request/watchlist protection: %s",
                            prefix,
                            movie.title,
                        )
                    else:
                        cache.delete_deletion_state(movie.movie_id)
                        cache.clear_deletion_countdown_logged_day(movie.movie_id)
                        logger.info(
                            "Deletion countdown canceled by request/watchlist protection: %s",
                            movie.title,
                        )
                    cache.append_runtime_event(
                        "deletion_suppressed",
                        {
                            "reason": "request_or_watchlist_protection",
                            "movie": _movie_status_payload(movie),
                        },
                    )

                if config.dry_run or not allow_mutations:
                    prefix = "[DRY_RUN]" if config.dry_run else "[SAFE_MODE]"
                    if config.dry_run and (
                        not movie.monitored or bool(movie.streaming_tags)
                    ):
                        stats.changed += 1
                    logger.info(
                        "%s Request/watchlist protection for '%s' (id=%s tmdb=%s): "
                        "monitored=true and streaming tags removed",
                        prefix,
                        movie.title,
                        movie.movie_id,
                        movie.tmdb_id,
                    )
                else:
                    updated = radarr.reconcile_and_update_movie(
                        movie=movie,
                        desired_streaming_labels=[],
                        monitored=True,
                    )
                    if updated:
                        stats.changed += 1

                if not movie.has_file:
                    search_reason = "request_or_watchlist_protected"
                    if not _is_released(movie.status):
                        logger.info(
                            "Search not triggered for '%s' (id=%s): status=%s (released only). reasons=%s",
                            movie.title,
                            movie.movie_id,
                            movie.status,
                            search_reason,
                        )
                    else:
                        now_ts = int(time.time())
                        next_allowed = cache.get_search_next_allowed(movie.movie_id)
                        if now_ts < next_allowed:
                            logger.info(
                                "Search on cooldown for '%s' (id=%s): remaining=%ss reasons=%s",
                                movie.title,
                                movie.movie_id,
                                next_allowed - now_ts,
                                search_reason,
                            )
                        elif config.dry_run or not allow_mutations:
                            prefix = "[DRY_RUN]" if config.dry_run else "[SAFE_MODE]"
                            logger.info(
                                "%s Simulated search for '%s' (id=%s) reasons=%s",
                                prefix,
                                movie.title,
                                movie.movie_id,
                                search_reason,
                            )
                        else:
                            radarr.trigger_search(movie.movie_id)
                            cache.set_search_next_allowed(
                                movie.movie_id,
                                now_ts + config.search_cooldown_seconds,
                            )
                            stats.search_triggered += 1

                snapshot_rows.append(
                    movie_snapshot_payload(
                        movie,
                        [item.source for item in movie_protection],
                        int(time.time()),
                        deletion_state=cache.get_deletion_state(movie.movie_id),
                        protection=movie_protection,
                    )
                )
                continue

            if _is_recent_theatrical_release(movie, config):
                stats.recent_protected += 1
                deletion_state = cache.get_deletion_state(movie.movie_id)
                if (
                    deletion_state is not None
                    and deletion_state.last_status == "scheduled"
                ):
                    if config.dry_run or not allow_mutations:
                        prefix = "[DRY_RUN]" if config.dry_run else "[SAFE_MODE]"
                        logger.info(
                            "%s Deletion countdown canceled by recent theatrical release: %s",
                            prefix,
                            movie.title,
                        )
                    else:
                        cache.delete_deletion_state(movie.movie_id)
                        cache.clear_deletion_countdown_logged_day(movie.movie_id)
                        logger.info(
                            "Deletion countdown canceled by recent theatrical release: %s",
                            movie.title,
                        )
                    cache.append_runtime_event(
                        "deletion_suppressed",
                        {
                            "reason": "recent_theatrical",
                            "movie": _movie_status_payload(movie),
                        },
                    )

                if config.dry_run or not allow_mutations:
                    prefix = "[DRY_RUN]" if config.dry_run else "[SAFE_MODE]"
                    if config.dry_run and (
                        not movie.monitored or bool(movie.streaming_tags)
                    ):
                        stats.changed += 1
                    logger.info(
                        "%s Recent theatrical release protection for '%s' "
                        "(id=%s in_cinemas=%s): monitored=true and streaming tags removed",
                        prefix,
                        movie.title,
                        movie.movie_id,
                        movie.in_cinemas,
                    )
                else:
                    updated = radarr.reconcile_and_update_movie(
                        movie=movie,
                        desired_streaming_labels=[],
                        monitored=True,
                    )
                    if updated:
                        stats.changed += 1

                if not movie.has_file:
                    search_reason = "recent_theatrical_release"
                    if not _is_released(movie.status):
                        logger.info(
                            "Search not triggered for '%s' (id=%s): status=%s (released only). reasons=%s",
                            movie.title,
                            movie.movie_id,
                            movie.status,
                            search_reason,
                        )
                    else:
                        now_ts = int(time.time())
                        next_allowed = cache.get_search_next_allowed(movie.movie_id)
                        if now_ts < next_allowed:
                            logger.info(
                                "Search on cooldown for '%s' (id=%s): remaining=%ss reasons=%s",
                                movie.title,
                                movie.movie_id,
                                next_allowed - now_ts,
                                search_reason,
                            )
                        elif config.dry_run or not allow_mutations:
                            prefix = "[DRY_RUN]" if config.dry_run else "[SAFE_MODE]"
                            logger.info(
                                "%s Simulated search for '%s' (id=%s) reasons=%s",
                                prefix,
                                movie.title,
                                movie.movie_id,
                                search_reason,
                            )
                        else:
                            radarr.trigger_search(movie.movie_id)
                            cache.set_search_next_allowed(
                                movie.movie_id,
                                now_ts + config.search_cooldown_seconds,
                            )
                            stats.search_triggered += 1

                snapshot_rows.append(
                    movie_snapshot_payload(
                        movie,
                        ["recent_theatrical"],
                        int(time.time()),
                        deletion_state=cache.get_deletion_state(movie.movie_id),
                    )
                )
                continue

            eligible_non_favorite += 1
            lookup = justwatch.lookup_movie(movie, enabled_services=allowed_services)

            if lookup.status == LookupStatus.SCHEMA_ERROR:
                stats.schema_errors += 1
                schema_error_found = True
                cache.append_runtime_event(
                    "justwatch_schema_error",
                    {
                        "movie": _movie_status_payload(movie),
                        "error": (lookup.error_message or "")[:500],
                    },
                )
                logger.error(
                    "JustWatch parsing/schema error for '%s' (id=%s): %s. "
                    "Aborting cycle.",
                    movie.title,
                    movie.movie_id,
                    lookup.error_message,
                )
                break

            decision = evaluate_movie(movie, lookup)

            if lookup.status == LookupStatus.UNKNOWN or decision.reason == "unknown":
                stats.unknown += 1
                unknown_count += 1
                logger.warning(
                    "JustWatch unknown/failure for '%s' (id=%s). No change applied.",
                    movie.title,
                    movie.movie_id,
                )
                snapshot_rows.append(
                    movie_snapshot_payload(
                        movie,
                        ["unknown"],
                        int(time.time()),
                        deletion_state=cache.get_deletion_state(movie.movie_id),
                    )
                )
                continue

            if decision.should_update and decision.target_monitored is not None:
                if config.dry_run or not allow_mutations:
                    prefix = "[DRY_RUN]" if config.dry_run else "[SAFE_MODE]"
                    if config.dry_run:
                        stats.changed += 1
                    logger.info(
                        "%s Simulated change for '%s' (id=%s): monitored=%s streaming_tags=%s",
                        prefix,
                        movie.title,
                        movie.movie_id,
                        decision.target_monitored,
                        decision.desired_streaming_labels,
                    )
                else:
                    updated = radarr.reconcile_and_update_movie(
                        movie=movie,
                        desired_streaming_labels=decision.desired_streaming_labels,
                        monitored=decision.target_monitored,
                    )
                    if updated:
                        stats.changed += 1

            deletion_state = cache.get_deletion_state(movie.movie_id)
            now_ts = int(time.time())

            if lookup.status == LookupStatus.AVAILABLE:
                snapshot_conditions = ["streaming_allowed"]
                primary_service = (
                    sorted(service.service_id for service in lookup.services)[0]
                    if lookup.services
                    else "unknown"
                )
                primary_service_name = service_name_from_id(primary_service)
                if not movie.has_file:
                    if deletion_state is not None and deletion_state.last_status == "scheduled":
                        if config.dry_run or not allow_mutations:
                            prefix = "[DRY_RUN]" if config.dry_run else "[SAFE_MODE]"
                            logger.info(
                                "%s Scheduled deletion cleared (movie has no file): %s",
                                prefix,
                                movie.title,
                            )
                        else:
                            cache.delete_deletion_state(movie.movie_id)
                            cache.clear_deletion_countdown_logged_day(movie.movie_id)
                            logger.info(
                                "Scheduled deletion cleared (movie has no file): %s",
                                movie.title,
                            )
                        cache.append_runtime_event(
                            "deletion_suppressed",
                            {
                                "reason": "movie_has_no_file",
                                "movie": _movie_status_payload(movie),
                            },
                        )
                    elif config.remove_mode == "delete":
                        logger.debug(
                            "Deletion schedule skipped for '%s' (id=%s): has_file=false.",
                            movie.title,
                            movie.movie_id,
                        )
                    snapshot_rows.append(
                        movie_snapshot_payload(
                            movie,
                            snapshot_conditions,
                            int(time.time()),
                            streaming_services=lookup.services,
                            deletion_state=cache.get_deletion_state(movie.movie_id),
                        )
                    )
                    continue

                if deletion_state is not None and deletion_state.last_status == "scheduled":
                    snapshot_conditions.append("scheduled_deletion")
                    effective_delete_after_ts = int(deletion_state.delete_after_ts)
                    remaining_text = _format_time_until(now_ts, effective_delete_after_ts)
                    if (
                        config.remove_mode == "delete"
                        and effective_delete_after_ts <= now_ts
                    ):
                        if not movie.path:
                            logger.warning(
                                "Scheduled deletion not validated for movie without path: %s (id=%s)",
                                movie.title,
                                movie.movie_id,
                            )
                        elif _directory_size_bytes(movie.path) is None:
                            logger.warning(
                                "Scheduled deletion not validated for movie with invalid "
                                "path: %s (id=%s path=%s)",
                                movie.title,
                                movie.movie_id,
                                movie.path,
                            )
                        else:
                            validated_due_deletions[movie.movie_id] = movie
                            snapshot_conditions.append("eligible_for_deletion")
                            logger.info(
                                "Scheduled deletion validated this cycle: %s (id=%s) | service: %s",
                                movie.title,
                                movie.movie_id,
                                primary_service,
                            )
                        snapshot_rows.append(
                            movie_snapshot_payload(
                                movie,
                                snapshot_conditions,
                                int(time.time()),
                                streaming_services=lookup.services,
                                deletion_state=cache.get_deletion_state(movie.movie_id),
                            )
                        )
                        del snapshot_conditions
                        continue
                    today_yyyymmdd = _today_yyyymmdd()
                    last_logged_day = cache.get_deletion_countdown_logged_day(movie.movie_id)
                    if last_logged_day != today_yyyymmdd:
                        logger.info(
                            "⏳ Deletion countdown: %s (deletes in %s) | service: %s",
                            movie.title,
                            remaining_text,
                            primary_service,
                        )
                        if not config.dry_run and allow_mutations:
                            cache.set_deletion_countdown_logged_day(
                                movie.movie_id, today_yyyymmdd
                            )
                            notifier.notify_action(
                                f"Deletion reminder: {movie.title}. Available on "
                                f"{primary_service_name}. ETA: {remaining_text}."
                            )
                elif config.remove_mode == "delete":
                    if not movie.path:
                        logger.warning(
                            "Cannot schedule deletion for movie without path: %s (id=%s)",
                            movie.title,
                            movie.movie_id,
                        )
                    else:
                        delete_after_ts = now_ts + config.delete_after_seconds
                        remaining_text = _format_time_until(now_ts, delete_after_ts)
                        if config.dry_run or not allow_mutations:
                            prefix = "[DRY_RUN]" if config.dry_run else "[SAFE_MODE]"
                            logger.info(
                                "%s 🕒 Deletion scheduled: %s (deletes in %s) | service: %s",
                                prefix,
                                movie.title,
                                remaining_text,
                                primary_service,
                            )
                        else:
                            cache.upsert_deletion_state(
                                radarr_id=movie.movie_id,
                                movie_path=movie.path,
                                scheduled_at=now_ts,
                                delete_after_ts=delete_after_ts,
                                last_status="scheduled",
                                updated_at=now_ts,
                            )
                            cache.set_deletion_countdown_logged_day(
                                movie.movie_id, _today_yyyymmdd()
                            )
                            logger.info(
                                "🕒 Deletion scheduled: %s (deletes in %s) | service: %s",
                                movie.title,
                                remaining_text,
                                primary_service,
                            )
                            notifier.notify_action(
                                f"Deletion scheduled: {movie.title}. Available on {primary_service_name}. ETA: {remaining_text}."
                            )
                        cache.append_runtime_event(
                            "deletion_scheduled",
                            {
                                "movie": _movie_status_payload(movie),
                                "service": primary_service,
                                "deleteAfterTs": delete_after_ts,
                                "dryRun": bool(config.dry_run),
                                "safeMode": not allow_mutations,
                            },
                        )
                        snapshot_conditions.append("scheduled_deletion")
            elif lookup.status == LookupStatus.UNAVAILABLE:
                if deletion_state is not None and deletion_state.last_status == "scheduled":
                    if config.dry_run or not allow_mutations:
                        prefix = "[DRY_RUN]" if config.dry_run else "[SAFE_MODE]"
                        logger.info(
                            "%s Scheduled deletion canceled (no longer in allowed streaming): %s",
                            prefix,
                            movie.title,
                        )
                    else:
                        cache.delete_deletion_state(movie.movie_id)
                        cache.clear_deletion_countdown_logged_day(movie.movie_id)
                        logger.info(
                            "Scheduled deletion canceled (no longer in allowed streaming): %s",
                            movie.title,
                        )
                    cache.append_runtime_event(
                        "deletion_suppressed",
                        {
                            "reason": "left_allowed_streaming",
                            "movie": _movie_status_payload(movie),
                        },
                    )

            search_reasons: list[str] = []
            if decision.trigger_search:
                if movie.has_file:
                    logger.info(
                        "Policy search skipped for '%s' (id=%s): has_file=true.",
                        movie.title,
                        movie.movie_id,
                    )
                else:
                    search_reasons.append(decision.search_reason or "policy")
            effective_monitored = (
                decision.target_monitored
                if decision.target_monitored is not None
                else movie.monitored
            )
            if effective_monitored and not movie.has_file:
                search_reasons.append("monitored_without_file")

            if search_reasons:
                unique_search_reasons = sorted(set(search_reasons))
                reasons_joined = ",".join(unique_search_reasons)
                if not _is_released(movie.status):
                    logger.info(
                        "Search not triggered for '%s' (id=%s): status=%s (released only). reasons=%s",
                        movie.title,
                        movie.movie_id,
                        movie.status,
                        reasons_joined,
                    )
                else:
                    now_ts = int(time.time())
                    next_allowed = cache.get_search_next_allowed(movie.movie_id)
                    if now_ts < next_allowed:
                        logger.info(
                            "Search on cooldown for '%s' (id=%s): remaining=%ss reasons=%s",
                            movie.title,
                            movie.movie_id,
                            next_allowed - now_ts,
                            reasons_joined,
                        )
                    else:
                        if config.dry_run or not allow_mutations:
                            prefix = "[DRY_RUN]" if config.dry_run else "[SAFE_MODE]"
                            logger.info(
                                "%s Simulated search for '%s' (id=%s) reasons=%s",
                                prefix,
                                movie.title,
                                movie.movie_id,
                                reasons_joined,
                            )
                        else:
                            radarr.trigger_search(movie.movie_id)
                            cache.set_search_next_allowed(
                                movie.movie_id,
                                now_ts + config.search_cooldown_seconds,
                            )
                            stats.search_triggered += 1
                            if "left_streaming" in unique_search_reasons:
                                left_services = _format_services_human(decision.leaving_service_ids)
                                notifier.notify_action(
                                    f"Re-queued in Radarr: {movie.title}. Left {left_services}."
                                )

            for service in decision.entering_services:
                grouped_entering[service.service_name].append(movie.title)

            for service_id in decision.leaving_service_ids:
                grouped_leaving[service_name_from_id(service_id)].append(movie.title)

            snapshot_rows.append(
                movie_snapshot_payload(
                    movie,
                    snapshot_conditions,
                    int(time.time()),
                    streaming_services=lookup.services
                    if lookup.status == LookupStatus.AVAILABLE
                    else [],
                    deletion_state=cache.get_deletion_state(movie.movie_id),
                )
            )

        except InterruptedError:
            logger.info(
                "Stop requested while processing movie '%s' (id=%s). Aborting cycle.",
                movie.title,
                movie.movie_id,
            )
            break
        except Exception as exc:
            stats.errors += 1
            logger.exception(
                "Error while processing movie '%s' (id=%s): %s",
                movie.title,
                movie.movie_id,
                exc,
            )

    if grouped_entering:
        if config.dry_run or not allow_mutations:
            logger.info(
                "%s Streaming entered notification suppressed: %s",
                _notification_prefix(config, allow_mutations),
                dict(grouped_entering),
            )
        else:
            notifier.notify_entering(dict(grouped_entering))

    if grouped_leaving:
        if config.dry_run or not allow_mutations:
            logger.info(
                "%s Streaming left notification suppressed: %s",
                _notification_prefix(config, allow_mutations),
                dict(grouped_leaving),
            )
        else:
            notifier.notify_leaving(dict(grouped_leaving))

    if config.remove_mode == "delete" and validated_due_deletions:
        deleted_count = 0
        deleted_bytes_total = 0

        if seerr_failed or plex_watchlists_failed:
            logger.warning(
                "Scheduled deletions suppressed: request/watchlist protection "
                "could not be loaded."
            )
            cache.append_runtime_event(
                "deletion_suppressed",
                {
                    "reason": (
                        "plex_watchlist_failed"
                        if plex_watchlists_failed
                        else "seerr_failed"
                    ),
                    "count": len(validated_due_deletions),
                },
            )
        elif schema_error_found:
            logger.warning(
                "Scheduled deletions suppressed: cycle had a JustWatch schema error."
            )
            cache.append_runtime_event(
                "deletion_suppressed",
                {"reason": "justwatch_schema_error", "count": len(validated_due_deletions)},
            )
        elif stats.errors > 0:
            logger.warning(
                "Scheduled deletions suppressed: cycle had movie processing errors."
            )
            cache.append_runtime_event(
                "deletion_suppressed",
                {"reason": "movie_processing_errors", "count": len(validated_due_deletions)},
            )
        elif stop_event.is_set():
            logger.info("Scheduled deletions suppressed: stop requested during cycle.")
            cache.append_runtime_event(
                "deletion_suppressed",
                {"reason": "stop_requested", "count": len(validated_due_deletions)},
            )
        else:
            for movie in validated_due_deletions.values():
                if stop_event.is_set():
                    logger.info(
                        "Stop requested during scheduled deletion pass. Aborting deletion pass."
                    )
                    break

                movie_name = (
                    f"{movie.title} ({movie.year})" if movie.year else movie.title
                )
                target_path = movie.path or ""

                if not allow_mutations:
                    logger.info(
                        "Safe mode: scheduled deletion suppressed for %s (id=%s)",
                        movie_name,
                        movie.movie_id,
                    )
                    cache.append_runtime_event(
                        "deletion_suppressed",
                        {
                            "reason": "safe_mode",
                            "movie": _movie_status_payload(movie),
                        },
                    )
                    continue

                reclaimed_bytes = _directory_size_bytes(target_path)
                if reclaimed_bytes is None:
                    logger.warning(
                        "Failed scheduled deletion: %s path=%s reason=invalid_path",
                        movie_name,
                        target_path,
                    )
                    cache.append_runtime_event(
                        "deletion_suppressed",
                        {
                            "reason": "invalid_path",
                            "movie": _movie_status_payload(movie),
                        },
                    )
                    continue

                deleted, reason = _delete_movie_folder(target_path, config.dry_run)
                if not deleted:
                    logger.warning(
                        "Failed scheduled deletion: %s path=%s reason=%s",
                        movie_name,
                        target_path,
                        reason,
                    )
                    continue

                if config.dry_run:
                    logger.info(
                        "[DRY_RUN] 🗑️ Deleted from library (scheduled): %s",
                        movie_name,
                    )
                else:
                    deleted_count += 1
                    deleted_bytes_total += reclaimed_bytes
                    cache.mark_state(
                        movie.movie_id,
                        "deleted",
                        int(time.time()),
                        delete_after_ts=0,
                    )
                    cache.clear_deletion_countdown_logged_day(movie.movie_id)
                    logger.info("🗑️ Deleted from library (scheduled): %s", movie_name)
                    notifier.notify_action(
                        f"Deleted from library: {movie_name}. Scheduled time reached."
                    )
                    radarr.trigger_rescan(movie.movie_id)

            if deleted_count > 0 and not config.dry_run and allow_mutations:
                noun = "movie" if deleted_count == 1 else "movies"
                notifier.notify_action(
                    f"Deletion batch completed: {deleted_count} {noun} deleted. Freed {_format_bytes_human(deleted_bytes_total)} total."
                )

    if schema_error_found:
        notifier.notify_error(
            "Cycle warning: JustWatch schema error."
        )
    elif (
        eligible_non_favorite > 0
        and unknown_count == eligible_non_favorite
        and not grouped_entering
        and not grouped_leaving
        and stats.changed == 0
    ):
        notifier.notify_error(
            "Cycle warning: JustWatch unavailable."
        )

    logger.info(
        "Cycle finished: processed=%s favorite_skip=%s seerr_protected=%s recent_protected=%s changed=%s searches=%s unknown=%s schema=%s errors=%s",
        stats.processed,
        stats.favorite_skipped,
        stats.seerr_protected,
        stats.recent_protected,
        stats.changed,
        stats.search_triggered,
        stats.unknown,
        stats.schema_errors,
        stats.errors,
    )
    if not stop_event.is_set():
        cache.upsert_movie_snapshots(
            snapshot_rows,
            {movie.movie_id for movie in movies},
        )
        _finish_cycle_status(cache, stats, stats.processed, len(movies))
    return stats


def _run_list_services_mode(config: Config, justwatch: JustWatchProvider) -> None:
    services = justwatch.list_country_services(config.jw_country)
    for service in sorted(services, key=lambda item: item.service_name.lower()):
        print(f"{service.service_name} | {service.service_id}")


def _build_radarr_client_with_retry(config: Config, logger: logging.Logger) -> RadarrClient:
    last_error: Exception | None = None
    for attempt in range(1, config.radarr_init_max_retries + 1):
        try:
            return RadarrClient(config.radarr_url, config.radarr_api_key)
        except Exception as exc:
            last_error = exc
            if attempt >= config.radarr_init_max_retries:
                break
            logger.warning(
                "Radarr unavailable on startup (attempt %s/%s): %s. Retrying in %ss.",
                attempt,
                config.radarr_init_max_retries,
                exc,
                config.radarr_init_retry_seconds,
            )
            time.sleep(config.radarr_init_retry_seconds)

    raise RuntimeError(
        "Could not connect to Radarr during initialization"
    ) from last_error


def _justwatch_provider_settings(config: Config) -> tuple[object, ...]:
    return (
        config.jw_country,
        config.jw_language,
        config.jw_only_subscription,
        config.jw_request_delay_seconds,
        config.jw_request_delay_jitter_seconds,
    )


def _build_justwatch_provider(
    config: Config,
    cache: SQLiteCache | None,
    stop_event: threading.Event,
) -> JustWatchProvider:
    return JustWatchProvider(
        cache=cache,
        country=config.jw_country,
        language=config.jw_language,
        only_subscription=config.jw_only_subscription,
        request_delay_seconds=config.jw_request_delay_seconds,
        request_delay_jitter_seconds=config.jw_request_delay_jitter_seconds,
        stop_event=stop_event,
    )


def _build_seerr_client(config: Config, logger: logging.Logger) -> SeerrClient | None:
    if not config.seerr_enabled:
        return None
    if not config.seerr_api_key:
        logger.error("SEERR_ENABLED=true but SEERR_API_KEY is empty.")
        return None
    return SeerrClient(config.seerr_url, config.seerr_api_key)


def _build_plex_watchlist_client(
    config: Config,
) -> PlexWatchlistClient | None:
    if not config.plex_watchlist_sync_enabled:
        return None
    return PlexWatchlistClient(
        token=config.plex_token,
        token_file=config.plex_token_file,
        include_friends=config.plex_watchlist_include_friends,
    )


def _start_api_server(
    config: Config,
    cache: SQLiteCache,
    radarr: RadarrClient,
    logger: logging.Logger,
) -> threading.Thread | None:
    if not config.api_enabled:
        return None
    if not config.stream_sync_api_key:
        logger.error("API_ENABLED=true but STREAM_SYNC_API_KEY is empty. API not started.")
        return None

    from .api import create_app
    import uvicorn

    app = create_app(config, cache, radarr)

    def _run() -> None:
        uvicorn.run(
            app,
            host=config.api_host,
            port=config.api_port,
            log_level="info",
        )

    thread = threading.Thread(target=_run, name="stream-sync-api", daemon=True)
    thread.start()
    logger.info("Stream Sync API started on %s:%s", config.api_host, config.api_port)
    return thread


def _start_plex_watchlist_sync(
    config: Config,
    cache: SQLiteCache,
    logger: logging.Logger,
    stop_event: threading.Event,
) -> threading.Thread | None:
    if not config.plex_watchlist_sync_enabled:
        return None

    from .watchlist_sync import sync_plex_watchlists

    def _run() -> None:
        current = config
        while not stop_event.is_set():
            try:
                current = Config.from_env_file(current.config_file_path)
                if current.plex_watchlist_sync_enabled:
                    stats = sync_plex_watchlists(current)
                    cache.append_runtime_event(
                        "plex_watchlist_sync_finished",
                        {
                            "users": stats.users,
                            "moviesAdded": stats.movies_added,
                            "seriesAdded": stats.series_added,
                            "existing": stats.existing,
                            "skipped": stats.skipped,
                            "errors": stats.errors,
                        },
                    )
            except Exception as exc:
                logger.exception("Plex watchlist sync failed: %s", exc)
                cache.append_runtime_event(
                    "plex_watchlist_sync_failed",
                    {"error": str(exc)[:500]},
                )
            wait_seconds = max(
                60, current.plex_watchlist_sync_interval_minutes * 60
            )
            if stop_event.wait(wait_seconds):
                break

    thread = threading.Thread(
        target=_run,
        name="plex-watchlist-sync",
        daemon=True,
    )
    thread.start()
    logger.info(
        "Plex watchlist sync started with interval=%sm",
        config.plex_watchlist_sync_interval_minutes,
    )
    return thread


def main() -> None:
    config = Config.from_env_file()
    logger = setup_logging(config.tz, config.log_dir)
    stop_event = _install_signal_handlers(logger)
    logger.info(
        "Starting stream-sync (mode=%s remove_mode=%s delete_after=%sd run_interval=%sh jw_delay=%ss jitter=%ss cooldown=%sh theatrical_release_grace=%smo seerr_enabled=%s api_enabled=%s startup_retry=%sx%ss run_once=%s dry_run=%s)",
        config.mode,
        config.remove_mode,
        config.delete_after_days,
        config.run_interval_hours,
        config.jw_request_delay_seconds,
        config.jw_request_delay_jitter_seconds,
        config.search_cooldown_hours,
        config.theatrical_release_grace_months,
        config.seerr_enabled,
        config.api_enabled,
        config.radarr_init_max_retries,
        config.radarr_init_retry_seconds,
        config.run_once,
        config.dry_run,
    )

    if config.mode == "list_services":
        justwatch = _build_justwatch_provider(config, None, stop_event)
        _run_list_services_mode(config, justwatch)
        return

    cache = SQLiteCache(
        db_path=config.cache_db_path,
        idmap_ttl_seconds=config.idmap_ttl_seconds,
        offers_ttl_ok_seconds=config.offers_ttl_ok_seconds,
        offers_ttl_err_seconds=config.offers_ttl_err_seconds,
        ttl_jitter_percent=config.ttl_jitter_percent,
    )
    cache.set_daemon_status(
        {
            "state": "starting",
            "nextCycleAt": None,
            "currentMovie": None,
            "safeMode": {"active": False, "reason": None},
        }
    )
    cache.append_runtime_event("daemon_started", {"startedAt": int(time.time())})
    justwatch = _build_justwatch_provider(config, cache, stop_event)
    justwatch_settings = _justwatch_provider_settings(config)

    if config.mode != "daemon":
        cache.close()
        raise ValueError(f"Invalid MODE: {config.mode}")

    radarr = _build_radarr_client_with_retry(config, logger)
    seerr = _build_seerr_client(config, logger)
    plex_watchlists = _build_plex_watchlist_client(config)
    _start_api_server(config, cache, radarr, logger)
    _start_plex_watchlist_sync(config, cache, logger, stop_event)

    try:
        while not stop_event.is_set():
            try:
                config = Config.from_env_file(config.config_file_path)
            except ValueError as exc:
                logger.error(
                    "Could not load config overrides. Keeping previous effective config: %s",
                    exc,
                )
                cache.append_runtime_event(
                    "config_load_failed",
                    {"error": str(exc)[:500]},
                )

            current_justwatch_settings = _justwatch_provider_settings(config)
            if current_justwatch_settings != justwatch_settings:
                justwatch = _build_justwatch_provider(config, cache, stop_event)
                justwatch_settings = current_justwatch_settings
                logger.info("JustWatch provider rebuilt after config change.")

            seerr = _build_seerr_client(config, logger)
            plex_watchlists = _build_plex_watchlist_client(config)
            notifier = build_notifier(
                config.notify_mode,
                config.telegram_bot_token,
                config.telegram_chat_id,
            )
            whitelist_valid = _validate_allowed_services(
                _parse_allowed_services(config.jw_allowed_services),
                justwatch,
                logger,
                config.jw_country,
            )
            run_interval_seconds = config.run_interval_seconds
            cycle_started_at = time.time()
            try:
                _run_cycle(
                    config,
                    cache,
                    radarr,
                    justwatch,
                    notifier,
                    logger,
                    stop_event,
                    seerr=seerr,
                    plex_watchlists=plex_watchlists,
                    allow_mutations=whitelist_valid,
                )
            except Exception as exc:
                logger.exception("Unexpected cycle failure: %s", exc)
                cache.set_daemon_status(
                    {
                        "state": "error",
                        "currentMovie": None,
                        "safeMode": {
                            "active": True,
                            "reason": "unexpected_cycle_failure",
                        },
                    }
                )
                cache.append_runtime_event(
                    "cycle_error",
                    {"error": str(exc)[:500]},
                )

            if config.run_once:
                cache.set_daemon_status({"state": "stopping", "currentMovie": None})
                cache.append_runtime_event(
                    "stopping",
                    {"reason": "run_once"},
                )
                logger.info("RUN_ONCE=true: exiting after one cycle.")
                break
            if stop_event.is_set():
                cache.set_daemon_status({"state": "stopping", "currentMovie": None})
                cache.append_runtime_event(
                    "stopping",
                    {"reason": "stop_requested"},
                )
                logger.info("Stop requested. Exiting daemon loop.")
                break

            elapsed = time.time() - cycle_started_at
            sleep_for = max(0.0, run_interval_seconds - elapsed)
            logger.info(
                "Waiting %s for next cycle.",
                _format_duration_human(sleep_for),
            )
            next_cycle_deadline = time.time() + sleep_for
            cache.set_daemon_status(
                {
                    "state": "waiting",
                    "nextCycleAt": int(next_cycle_deadline),
                    "currentMovie": None,
                    "progress": {"processed": 0, "total": 0},
                }
            )
            cache.append_runtime_event(
                "waiting_next_cycle",
                {
                    "nextCycleAt": int(next_cycle_deadline),
                    "waitSeconds": int(round(sleep_for)),
                },
            )
            if sleep_for > 0 and _wait_until_next_cycle(
                stop_event, logger, next_cycle_deadline
            ):
                cache.set_daemon_status({"state": "stopping", "currentMovie": None})
                cache.append_runtime_event(
                    "stopping",
                    {"reason": "stop_requested"},
                )
                break
    except KeyboardInterrupt:
        logger.info("Stopping stream-sync due to manual interruption.")
        cache.set_daemon_status({"state": "stopping", "currentMovie": None})
        cache.append_runtime_event("stopping", {"reason": "keyboard_interrupt"})
    finally:
        cache.set_daemon_status({"state": "stopping", "currentMovie": None})
        cache.close()


if __name__ == "__main__":
    main()
