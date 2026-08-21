from __future__ import annotations

import logging
import unittest

from app.servarr_client import ServarrClient


class FakeServarrClient(ServarrClient):
    def __init__(self, responses: dict[tuple[str, str], object]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str, object]] = []
        self._service = "radarr"
        self._logger = logging.getLogger("test.servarr")

    def _request(self, method: str, path: str, payload=None):
        self.calls.append((method, path, payload))
        return self._responses.get((method, path))


class ServarrClientTests(unittest.TestCase):
    def test_existing_movie_is_not_added(self) -> None:
        client = FakeServarrClient(
            {("GET", "/movie"): [{"id": 1, "tmdbId": 123}]}
        )
        result = client.add_movie(123, ("alice",), 7, "/movies", True)
        self.assertEqual(result, "exists")
        self.assertEqual(len(client.calls), 1)

    def test_movie_is_added_with_user_tag(self) -> None:
        lookup_path = "/movie/lookup/tmdb?tmdbId=123"
        client = FakeServarrClient(
            {
                ("GET", "/movie"): [],
                ("GET", lookup_path): {"title": "Movie", "tmdbId": 123},
                ("GET", "/tag"): [],
                ("POST", "/tag"): {"id": 9, "label": "plex-watchlist-alice"},
                ("POST", "/movie"): {"id": 10},
            }
        )
        result = client.add_movie(123, ("alice",), 7, "/movies", True)
        self.assertEqual(result, "added")
        payload = next(call[2] for call in client.calls if call[:2] == ("POST", "/movie"))
        self.assertEqual(payload["qualityProfileId"], 7)
        self.assertEqual(payload["rootFolderPath"], "/movies")
        self.assertEqual(payload["tags"], [9])
        self.assertTrue(payload["addOptions"]["searchForMovie"])


if __name__ == "__main__":
    unittest.main()
