from __future__ import annotations

import os
import unittest
from datetime import date
from unittest.mock import patch

from app.config import Config
from app.recent_release import (
    is_within_theatrical_release_grace,
    parse_radarr_date,
    subtract_calendar_months,
)


class RecentReleaseTests(unittest.TestCase):
    def test_parse_radarr_date_accepts_date_and_timestamp(self) -> None:
        self.assertEqual(parse_radarr_date("2026-04-19"), date(2026, 4, 19))
        self.assertEqual(
            parse_radarr_date("2026-04-19T03:00:00Z"),
            date(2026, 4, 19),
        )
        self.assertIsNone(parse_radarr_date(""))
        self.assertIsNone(parse_radarr_date("not-a-date"))

    def test_subtract_calendar_months_clamps_day(self) -> None:
        self.assertEqual(subtract_calendar_months(date(2025, 3, 31), 1), date(2025, 2, 28))
        self.assertEqual(subtract_calendar_months(date(2024, 3, 31), 1), date(2024, 2, 29))

    def test_theatrical_release_grace_window(self) -> None:
        today = date(2026, 4, 19)
        self.assertTrue(is_within_theatrical_release_grace("2025-04-19", 12, today))
        self.assertTrue(is_within_theatrical_release_grace("2026-04-19", 12, today))
        self.assertFalse(is_within_theatrical_release_grace("2025-04-18", 12, today))
        self.assertFalse(is_within_theatrical_release_grace("2026-04-20", 12, today))
        self.assertFalse(is_within_theatrical_release_grace(None, 12, today))
        self.assertFalse(is_within_theatrical_release_grace("2026-04-19", 0, today))


class ConfigTests(unittest.TestCase):
    def _config_env(self, **overrides: str) -> dict[str, str]:
        env = {
            "MODE": "list_services",
        }
        env.update(overrides)
        return env

    def test_theatrical_release_grace_months_default_is_disabled(self) -> None:
        with patch.dict(os.environ, self._config_env(), clear=True):
            config = Config.from_env()

        self.assertEqual(config.theatrical_release_grace_months, 0)

    def test_theatrical_release_grace_months_accepts_positive_value(self) -> None:
        with patch.dict(
            os.environ,
            self._config_env(THEATRICAL_RELEASE_GRACE_MONTHS="12"),
            clear=True,
        ):
            config = Config.from_env()

        self.assertEqual(config.theatrical_release_grace_months, 12)

    def test_theatrical_release_grace_months_rejects_negative_value(self) -> None:
        with patch.dict(
            os.environ,
            self._config_env(THEATRICAL_RELEASE_GRACE_MONTHS="-1"),
            clear=True,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "THEATRICAL_RELEASE_GRACE_MONTHS must be >= 0",
            ):
                Config.from_env()


if __name__ == "__main__":
    unittest.main()
