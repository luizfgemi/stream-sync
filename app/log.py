"""Logging configuration and setup for stream-sync."""

from __future__ import annotations

import logging
import os
import sys

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging(log_dir: str | None = None, level: int = logging.INFO) -> logging.Logger:
    """Configure root logger with console and optional file handlers."""
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    formatter = logging.Formatter(_LOG_FORMAT)

    if not root_logger.handlers:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        root_logger.addHandler(console)

    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.FileHandler(os.path.join(log_dir, "stream-sync.log"), encoding="utf-8")
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    return logging.getLogger("stream-sync")
