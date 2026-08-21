"""Logging configuration and setup for stream-sync."""

from __future__ import annotations

import logging
import os
import sys
import time

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


class DailyDateFileHandler(logging.Handler):
    """Logging handler that rotates log files daily by date string."""

    def __init__(self, log_dir: str) -> None:
        super().__init__()
        self._log_dir = os.path.abspath(log_dir)
        self._current_date: str | None = None
        self._delegate: logging.FileHandler | None = None

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    def _switch_if_needed(self) -> None:
        day = self._today()
        if self._delegate is not None and self._current_date == day:
            return

        path = os.path.join(self._log_dir, f"stream-sync.{day}.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        new_delegate = logging.FileHandler(path, encoding="utf-8")
        if self.formatter is not None:
            new_delegate.setFormatter(self.formatter)

        old_delegate = self._delegate
        self._delegate = new_delegate
        self._current_date = day
        if old_delegate is not None:
            old_delegate.close()

    def emit(self, record: logging.LogRecord) -> None:
        self.acquire()
        try:
            self._switch_if_needed()
            if self._delegate is not None:
                self._delegate.emit(record)
        except Exception:
            self.handleError(record)
        finally:
            self.release()

    def setFormatter(self, fmt: logging.Formatter) -> None:
        super().setFormatter(fmt)
        if self._delegate is not None:
            self._delegate.setFormatter(fmt)

    def close(self) -> None:
        try:
            if self._delegate is not None:
                self._delegate.close()
                self._delegate = None
                self._current_date = None
        finally:
            super().close()


class HealthCheckFilter(logging.Filter):
    """Filter out GET /api/v1/health logs with 200 OK from uvicorn.access logger."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "uvicorn.access":
            return True
        message = record.getMessage()
        return not ("GET /api/v1/health" in message and " 200" in message)


def setup_logging(
    tz: str | None = None,
    log_dir: str | None = None,
    level: int = logging.INFO,
) -> logging.Logger:
    """Configure root logger with console, timezone, and daily date file handlers.

    Args:
        tz: Optional timezone name (e.g. America/Sao_Paulo).
        log_dir: Optional log output directory path.
        level: Logging level (default INFO).

    Returns:
        Configured Logger instance.
    """
    if tz:
        os.environ["TZ"] = tz
        if hasattr(time, "tzset"):
            time.tzset()

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()

    formatter = logging.Formatter(_LOG_FORMAT)
    health_filter = HealthCheckFilter()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.addFilter(health_filter)
    root_logger.addHandler(console)

    if log_dir:
        try:
            file_handler = DailyDateFileHandler(log_dir)
            file_handler.setFormatter(formatter)
            file_handler.addFilter(health_filter)
            root_logger.addHandler(file_handler)
        except Exception as exc:
            logging.getLogger("app").warning("Could not initialize file logging in %s: %s", log_dir, exc)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("arrapi").setLevel(logging.WARNING)

    return logging.getLogger("stream-sync")
