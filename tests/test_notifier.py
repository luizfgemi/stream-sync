from __future__ import annotations

import unittest
from urllib.parse import parse_qs
from unittest.mock import patch

from app.notifier import TelegramNotifier, service_name_from_id


class FakeResponse:
    status = 200

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class TelegramNotifierTests(unittest.TestCase):
    def test_telegram_payload_uses_html_title_and_escaped_body(self) -> None:
        captured: dict[str, bytes] = {}

        def fake_urlopen(request: object, timeout: float) -> FakeResponse:
            captured["data"] = request.data
            captured["timeout"] = str(timeout).encode("utf-8")
            return FakeResponse()

        notifier = TelegramNotifier("token", "chat", timeout_seconds=3)

        with self.assertLogs("app.notifier.telegram", level="INFO") as logs:
            with patch("app.notifier.urlopen", fake_urlopen):
                notifier.notify_action("Movie <One> & Friends")

        payload = parse_qs(captured["data"].decode("utf-8"))
        text = payload["text"][0]

        self.assertEqual(payload["parse_mode"], ["HTML"])
        self.assertTrue(text.startswith("<b>Stream Sync</b>\n"))
        self.assertIn("Movie &lt;One&gt; &amp; Friends", text)
        self.assertIn("Movie <One> & Friends", "\n".join(logs.output))
        self.assertNotIn("<b>Stream Sync</b>", "\n".join(logs.output))

    def test_amazon_prime_video_service_name_is_human(self) -> None:
        self.assertEqual(service_name_from_id("amazonprimevideo"), "Prime Video")


if __name__ == "__main__":
    unittest.main()
