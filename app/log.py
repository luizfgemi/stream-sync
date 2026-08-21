from __future__ import annotations

import logging
import os
import time
from pathlib import Path


class DailyDateFileHandler(logging.Handler):
    def __init__(self, log_dir: str) -> None:
        super().__init__()
        self._log_dir = Path(log_dir)
        self._current_date: str | None = None
        self._delegate: logging.FileHandler | None = None

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    def _build_path(self, day: str) -> Path:
        return self._log_dir / f"stream-sync.{day}.log"

    def _switch_if_needed(self) -> None:
        day = self._today()
        if self._delegate is not None and self._current_date == day:
            return

        path = self._build_path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
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


def setup_logging(tz: str | None = None, log_dir: str | None = None) -> logging.Logger:
    if tz:
        os.environ["TZ"] = tz
        if hasattr(time, "tzset"):
            time.tzset()

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    if log_dir:
        try:
            file_handler = DailyDateFileHandler(log_dir)
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except Exception as exc:
            logging.getLogger("app").warning(
                "Could not initialize file logging in %s: %s",
                log_dir,
                exc,
            )

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("arrapi").setLevel(logging.WARNING)
    return logging.getLogger("app")
