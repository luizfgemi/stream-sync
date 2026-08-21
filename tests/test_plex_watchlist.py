from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.plex_watchlist import PlexWatchlistClient


class PlexWatchlistClientTests(unittest.TestCase):
    def test_reads_token_from_preferences_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Preferences.xml"
            path.write_text(
                '<Preferences PlexOnlineToken="secret-token" />',
                encoding="utf-8",
            )
            client = PlexWatchlistClient(token_file=str(path))
            self.assertEqual(client._token(), "secret-token")

    def test_configured_token_wins_over_file(self) -> None:
        client = PlexWatchlistClient(
            token="configured-token",
            token_file="/does/not/exist",
        )
        self.assertEqual(client._token(), "configured-token")


if __name__ == "__main__":
    unittest.main()
