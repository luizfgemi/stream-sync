from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from html import escape
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

TELEGRAM_TITLE = "Stream Sync"
TELEGRAM_MAX_LEN = 3900


def service_name_from_id(service_id: str) -> str:
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
    if service_id in known:
        return known[service_id]
    return service_id.replace("_", " ").title()


def _normalize_message(message: str) -> str:
    return " ".join(str(message).split())


def _clip(text: str, max_len: int = 3900) -> str:
    normalized = _normalize_message(text)
    if len(normalized) > max_len:
        return f"{normalized[: max_len - 3]}..."
    return normalized


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


def _join_titles(titles: list[str], max_len: int = 256) -> str:
    joined = ", ".join(titles)
    if len(joined) > max_len:
        return f"{joined[: max_len - 3]}..."
    return joined


def _iter_grouped_lines(
    event: str, grouped_movies_by_service: dict[str, list[str]]
) -> list[str]:
    lines: list[str] = []
    for service in sorted(grouped_movies_by_service.keys(), key=str.lower):
        movie_titles = sorted(set(grouped_movies_by_service[service]), key=str.lower)
        count = len(movie_titles)
        titles_text = _join_titles(movie_titles)
        noun = "movie" if count == 1 else "movies"
        if event == "streaming_entered":
            lines.append(
                f"Streaming update: {count} {noun} entered {service}: {titles_text}."
            )
        elif event == "streaming_left":
            lines.append(
                f"Streaming update: {count} {noun} left {service}: {titles_text}."
            )
        else:
            lines.append(
                f"Streaming update: {count} {noun} changed on {service}: {titles_text}."
            )
    return lines


class Notifier(ABC):
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
    def __init__(self) -> None:
        self._logger = logging.getLogger("app.notifier")

    def notify_entering(self, grouped_movies_by_service: dict[str, list[str]]) -> None:
        if not grouped_movies_by_service:
            return
        for line in _iter_grouped_lines("streaming_entered", grouped_movies_by_service):
            self._logger.info(line)

    def notify_leaving(self, grouped_movies_by_service: dict[str, list[str]]) -> None:
        if not grouped_movies_by_service:
            return
        for line in _iter_grouped_lines("streaming_left", grouped_movies_by_service):
            self._logger.info(line)

    def notify_error(self, message: str) -> None:
        self._logger.warning(_normalize_message(message))

    def notify_action(self, message: str) -> None:
        self._logger.info(_normalize_message(message))


class DiscordWebhookNotifier(Notifier):
    def __init__(self) -> None:
        self._logger = logging.getLogger("app.notifier.discord")
        self._logger.warning(
            "DiscordWebhookNotifier configured, but webhook sending is not implemented in MVP."
        )

    def notify_entering(self, grouped_movies_by_service: dict[str, list[str]]) -> None:
        if grouped_movies_by_service:
            self._logger.info("Stub Discord (entering): %s", grouped_movies_by_service)

    def notify_leaving(self, grouped_movies_by_service: dict[str, list[str]]) -> None:
        if grouped_movies_by_service:
            self._logger.info("Stub Discord (leaving): %s", grouped_movies_by_service)

    def notify_error(self, message: str) -> None:
        self._logger.warning("Discord stub (error): %s", message)

    def notify_action(self, message: str) -> None:
        self._logger.info("Discord stub (action): %s", message)


class TelegramNotifier(Notifier):
    def __init__(self, bot_token: str, chat_id: str, timeout_seconds: float = 10.0) -> None:
        self._logger = logging.getLogger("app.notifier.telegram")
        self._bot_token = bot_token.strip()
        self._chat_id = chat_id.strip()
        self._timeout_seconds = float(timeout_seconds)

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
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            self._logger.warning("Failed to send Telegram notification: %s", exc)

    def _emit(self, message: str) -> None:
        line = _normalize_message(message)
        if not line:
            return
        # Always mirror notification lines to stdout logs.
        self._logger.info(line)
        self._send(line)

    def notify_entering(self, grouped_movies_by_service: dict[str, list[str]]) -> None:
        if not grouped_movies_by_service:
            return
        for line in _iter_grouped_lines("streaming_entered", grouped_movies_by_service):
            self._emit(line)

    def notify_leaving(self, grouped_movies_by_service: dict[str, list[str]]) -> None:
        if not grouped_movies_by_service:
            return
        for line in _iter_grouped_lines("streaming_left", grouped_movies_by_service):
            self._emit(line)

    def notify_error(self, message: str) -> None:
        self._emit(message)

    def notify_action(self, message: str) -> None:
        self._emit(message)


def build_notifier(
    mode: str,
    telegram_bot_token: str = "",
    telegram_chat_id: str = "",
) -> Notifier:
    mode_normalized = (mode or "stdout").strip().lower()
    if mode_normalized == "stdout":
        return StdoutNotifier()
    if mode_normalized == "discord":
        return DiscordWebhookNotifier()
    if mode_normalized == "telegram":
        if not telegram_bot_token or not telegram_chat_id:
            logger = logging.getLogger("app.notifier")
            logger.warning(
                "NOTIFY_MODE=telegram, but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are missing. Using stdout."
            )
            return StdoutNotifier()
        return TelegramNotifier(
            bot_token=telegram_bot_token,
            chat_id=telegram_chat_id,
        )

    logger = logging.getLogger("app.notifier")
    logger.warning("Unknown NOTIFY_MODE: %s. Using stdout.", mode)
    return StdoutNotifier()
