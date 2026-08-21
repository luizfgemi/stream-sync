"""Telegram and logging notification service for stream-sync events."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from html import escape
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("stream-sync.notifier")
TELEGRAM_TITLE = "Stream Sync"
TELEGRAM_MAX_LEN = 4000


def service_name_from_id(service_id: str) -> str:
    """Format streaming service technical ID into human-friendly name."""
    known = {
        "amazonprimevideo": "Prime Video",
        "primevideo": "Prime Video",
        "primevideo_withads": "Prime Video (with ads)",
        "disneyplus": "Disney+",
        "appletvplus": "Apple TV+",
        "hbomax": "HBO Max",
        "max": "Max",
        "paramountplus": "Paramount+",
        "globoplay": "Globoplay",
    }
    key = service_id.strip().lower()
    if key in known:
        return known[key]
    return key.replace("_", " ").title()


def _normalize_message(message: str) -> str:
    return " ".join(str(message).split())


def _escape_and_clip_html(text: str, max_len: int) -> str:
    output: list[str] = []
    current_len = 0
    for char in text:
        escaped = escape(char, quote=False)
        if current_len + len(escaped) > max_len:
            suffix = "..."
            while output and current_len + len(suffix) > max_len:
                current_len -= len(output.pop())
            if len(suffix) <= max_len:
                output.append(suffix)
            break
        output.append(escaped)
        current_len += len(escaped)
    return "".join(output)


def _format_telegram_html(message: str) -> str:
    normalized = _normalize_message(message)
    if not normalized:
        return ""
    title = escape(TELEGRAM_TITLE, quote=False)
    prefix = f"<b>{title}</b>\n"
    body_max_len = max(0, TELEGRAM_MAX_LEN - len(prefix))
    return f"{prefix}{_escape_and_clip_html(normalized, body_max_len)}"


class Notifier(ABC):
    """Abstract base class for notification handlers."""

    @abstractmethod
    def notify_entering(self, grouped_movies_by_service: dict[str, list[str]]) -> None:
        pass

    @abstractmethod
    def notify_leaving(self, grouped_movies_by_service: dict[str, list[str]]) -> None:
        pass

    @abstractmethod
    def notify_error(self, message: str) -> None:
        pass

    @abstractmethod
    def notify_action(self, message: str) -> None:
        pass


class StdoutNotifier(Notifier):
    """Fallback notifier logging to standard output."""

    def __init__(self) -> None:
        self._logger = logging.getLogger("app.notifier")

    def notify_entering(self, grouped_movies_by_service: dict[str, list[str]]) -> None:
        pass

    def notify_leaving(self, grouped_movies_by_service: dict[str, list[str]]) -> None:
        pass

    def notify_error(self, message: str) -> None:
        self._logger.warning(_normalize_message(message))

    def notify_action(self, message: str) -> None:
        self._logger.info(_normalize_message(message))


class TelegramNotifier(Notifier):
    """Notifier for dispatching cycle events to Telegram chat."""

    def __init__(
        self,
        bot_token: str | None = "",
        chat_id: str | None = "",
        notify_mode: str = "all",
        timeout_seconds: float = 10.0,
        **_kwargs: Any,
    ) -> None:
        self._logger = logging.getLogger("app.notifier.telegram")
        self._bot_token = (bot_token or "").strip()
        self._chat_id = (chat_id or "").strip()
        self._notify_mode = (notify_mode or "all").lower()
        self._timeout_seconds = float(timeout_seconds)

    @property
    def enabled(self) -> bool:
        """Return True if bot token and chat ID are configured."""
        return bool(self._bot_token and self._chat_id and self._notify_mode != "disabled")

    def _send(self, message: str) -> None:
        text = _format_telegram_html(message)
        if not text:
            return

        payload = urlencode(
            {
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        request = Request(
            f"https://api.telegram.org/bot{self._bot_token}/sendMessage",
            data=payload,
            method="POST",
        )

        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                if getattr(response, "status", 200) >= 400:
                    self._logger.warning(
                        "Telegram notification request returned status=%s",
                        response.status,
                    )
        except Exception as exc:
            self._logger.warning("Failed to send Telegram notification: %s", exc)

    def _emit(self, message: str) -> None:
        line = _normalize_message(message)
        if not line:
            return
        self._logger.info(line)
        self._send(line)

    def notify_entering(self, grouped_movies_by_service: dict[str, list[str]]) -> None:
        pass

    def notify_leaving(self, grouped_movies_by_service: dict[str, list[str]]) -> None:
        pass

    def notify_error(self, message: str) -> None:
        self._emit(message)

    def notify_action(self, message: str) -> None:
        self._emit(message)


def build_notifier(
    mode: str = "stdout",
    telegram_bot_token: str = "",
    telegram_chat_id: str = "",
) -> Notifier:
    """Build Notifier implementation based on mode."""
    mode_normalized = (mode or "stdout").strip().lower()
    if mode_normalized == "telegram" and telegram_bot_token and telegram_chat_id:
        return TelegramNotifier(
            bot_token=telegram_bot_token,
            chat_id=telegram_chat_id,
        )
    return StdoutNotifier()
