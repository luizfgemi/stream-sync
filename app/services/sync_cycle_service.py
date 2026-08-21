from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Protocol

from ..database import SQLiteCache
from ..config import Config
from ..justwatch_provider import JustWatchProvider
from ..notifier import Notifier
from ..radarr_client import RadarrClient
from ..seerr_client import SeerrClient
from ..types import CycleStats, MovieState
from .deletion_service import DeletionService


class StopSignal(Protocol):
    def is_set(self) -> bool:
        ...


@dataclass(frozen=True, slots=True)
class SyncCycleDependencies:
    cache: SQLiteCache
    radarr: RadarrClient
    justwatch: JustWatchProvider
    notifier: Notifier
    logger: logging.Logger
    deletion: DeletionService
    stop_signal: StopSignal
    seerr: SeerrClient | None = None


def parse_allowed_services(csv_value: str) -> set[str]:
    return {item.strip().lower() for item in csv_value.split(",") if item.strip()}


def is_released(status: str | None) -> bool:
    return str(status or "").strip().lower() == "released"


def format_duration_human(seconds: float) -> str:
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


def format_time_until(now_ts: int, target_ts: int) -> str:
    return format_duration_human(max(0, int(target_ts) - int(now_ts)))


def cycle_stats_payload(stats: CycleStats) -> dict[str, int]:
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


def movie_status_payload(movie: MovieState) -> dict[str, object]:
    return {
        "radarrId": movie.movie_id,
        "tmdbId": movie.tmdb_id,
        "title": movie.title,
        "year": movie.year,
    }


class SyncCycleService:
    def __init__(self, dependencies: SyncCycleDependencies) -> None:
        self._deps = dependencies

    def finish_cycle_status(
        self,
        stats: CycleStats,
        processed: int,
        total: int,
    ) -> None:
        finished_at = int(time.time())
        stats_payload = cycle_stats_payload(stats)
        self._deps.cache.set_daemon_status(
            {
                "lastCycleFinishedAt": finished_at,
                "currentMovie": None,
                "progress": {"processed": int(processed), "total": int(total)},
                "lastCycleStats": stats_payload,
            }
        )
        self._deps.cache.set_runtime_state("last_cycle_finished_at", str(finished_at))
        self._deps.cache.append_runtime_event(
            "cycle_finished",
            {"finishedAt": finished_at, "stats": stats_payload},
        )

    def run_cycle(self, config: Config, allow_mutations: bool = True) -> CycleStats:
        raise NotImplementedError(
            "SyncCycleService.run_cycle is the next migration step. "
            "The current implementation still lives in app.main._run_cycle."
        )
