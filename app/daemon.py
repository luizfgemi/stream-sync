"""Background daemon loop and sync cycle orchestrator for stream-sync."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from app.config import Config
from app.database import SQLiteCache
from app.justwatch import JustWatchProvider
from app.notifier import TelegramNotifier, build_notifier
from app.radarr import RadarrClient

LOGGER = logging.getLogger("stream-sync.daemon")


def format_duration_human(seconds: float) -> str:
    """Format seconds into human-friendly duration string."""
    seconds_int = max(0, int(seconds))
    hours, remainder = divmod(seconds_int, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def wait_until_next_cycle(
    stop_event: threading.Event,
    logger: logging.Logger,
    target_wall_ts: float,
    poll_interval_seconds: float = 60.0,
) -> bool:
    """Sleep in poll intervals until target epoch timestamp or stop event set."""
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
