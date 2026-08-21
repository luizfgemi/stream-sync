from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from ..cache_sqlite import SQLiteCache
from ..config import Config
from ..justwatch_provider import JustWatchProvider
from ..notifier import build_notifier
from ..radarr_client import RadarrClient
from ..seerr_client import SeerrClient
from .deletion_service import DeletionService
from .sync_cycle_service import (
    SyncCycleDependencies,
    SyncCycleService,
    format_duration_human,
    parse_allowed_services,
)


@dataclass(frozen=True, slots=True)
class DaemonDependencies:
    cache: SQLiteCache
    radarr: RadarrClient
    justwatch: JustWatchProvider
    logger: logging.Logger
    stop_event: threading.Event
    seerr: SeerrClient | None = None


def justwatch_provider_settings(config: Config) -> tuple[object, ...]:
    return (
        config.jw_country,
        config.jw_language,
        config.jw_only_subscription,
        config.jw_request_delay_seconds,
        config.jw_request_delay_jitter_seconds,
    )


def wait_until_next_cycle(
    stop_event: threading.Event,
    logger: logging.Logger,
    target_wall_ts: float,
    poll_interval_seconds: float = 60.0,
) -> bool:
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


class DaemonService:
    def __init__(self, dependencies: DaemonDependencies) -> None:
        self._deps = dependencies

    def build_cycle_service(self, config: Config) -> SyncCycleService:
        return SyncCycleService(
            SyncCycleDependencies(
                cache=self._deps.cache,
                radarr=self._deps.radarr,
                justwatch=self._deps.justwatch,
                notifier=build_notifier(
                    config.notify_mode,
                    config.telegram_bot_token,
                    config.telegram_chat_id,
                ),
                logger=self._deps.logger,
                deletion=DeletionService(),
                stop_signal=self._deps.stop_event,
                seerr=self._deps.seerr,
            )
        )

    def run_forever(self, config: Config) -> None:
        raise NotImplementedError(
            "DaemonService.run_forever is the next migration step. "
            "The current scheduler still lives in app.main.main."
        )

    def log_next_cycle_wait(self, sleep_for: float) -> None:
        self._deps.logger.info(
            "Waiting %s for next cycle.",
            format_duration_human(sleep_for),
        )

    def allowed_services(self, config: Config) -> set[str]:
        return parse_allowed_services(config.jw_allowed_services)
