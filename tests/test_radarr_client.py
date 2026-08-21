from __future__ import annotations

import logging
import sys
import types
import unittest

arrapi_stub = types.ModuleType("arrapi")
arrapi_stub.ArrException = Exception
arrapi_stub.Invalid = Exception
arrapi_stub.RadarrAPI = object
sys.modules.setdefault("arrapi", arrapi_stub)

from app.radarr_client import RadarrClient
from app.types import MovieState, TagState


class FakeTag:
    def __init__(self, tag_id: int, label: str) -> None:
        self.id = tag_id
        self.label = label


class FakeMovieObj:
    def __init__(self) -> None:
        self.tagsIds = [1, 2, 3]
        self.monitored = True
        self.id = 10
        self.tmdbId = 123
        self.title = "Movie"
        self.year = 2026
        self.path = "/movies/Movie"
        self.hasFile = True
        self.status = "released"
        self.inCinemas = None
        self.edited: dict[str, object] | None = None

    def edit(self, **kwargs: object) -> None:
        self.edited = kwargs
        if "tags" in kwargs:
            self.tagsIds = list(kwargs["tags"])
        if "monitored" in kwargs:
            self.monitored = bool(kwargs["monitored"])


class FakeRadarrApi:
    def __init__(self) -> None:
        self.movie_obj = FakeMovieObj()

    def all_tags(self) -> list[FakeTag]:
        return [
            FakeTag(1, "favorite"),
            FakeTag(2, "streaming-netflix"),
            FakeTag(3, "manual-tag"),
            FakeTag(4, "streaming-max"),
        ]

    def get_movie(self, movie_id: int) -> FakeMovieObj:
        self.requested_movie_id = movie_id
        return self.movie_obj


class RadarrClientTests(unittest.TestCase):
    def test_reconcile_preserves_fresh_non_streaming_tags(self) -> None:
        fake_api = FakeRadarrApi()
        client = RadarrClient.__new__(RadarrClient)
        client._logger = logging.getLogger("app.radarr.test")
        client._api = fake_api
        stale_movie = MovieState(
            movie_id=10,
            tmdb_id=123,
            title="Movie",
            year=2026,
            path="/movies/Movie",
            monitored=True,
            has_file=True,
            status="released",
            in_cinemas=None,
            tags=[
                TagState(id=1, label="favorite"),
                TagState(id=2, label="streaming-netflix"),
            ],
        )

        updated = client.reconcile_and_update_movie(
            movie=stale_movie,
            desired_streaming_labels=["streaming-max"],
            monitored=False,
        )

        self.assertTrue(updated)
        self.assertEqual(fake_api.movie_obj.edited["tags"], [1, 3, 4])
        self.assertEqual(fake_api.movie_obj.edited["apply_tags"], "replace")
        self.assertEqual(fake_api.movie_obj.edited["monitored"], False)

    def test_set_favorite_adds_favorite_and_removes_streaming_tags(self) -> None:
        fake_api = FakeRadarrApi()
        fake_api.movie_obj.tagsIds = [2, 3]
        client = RadarrClient.__new__(RadarrClient)
        client._logger = logging.getLogger("app.radarr.test")
        client._api = fake_api

        movie = client.set_favorite(10, True)

        self.assertEqual(fake_api.movie_obj.edited["tags"], [3, 1])
        self.assertEqual(movie.tag_labels, ["manual-tag", "favorite"])

    def test_set_favorite_removes_only_favorite_and_streaming_tags(self) -> None:
        fake_api = FakeRadarrApi()
        fake_api.movie_obj.tagsIds = [1, 2, 3]
        client = RadarrClient.__new__(RadarrClient)
        client._logger = logging.getLogger("app.radarr.test")
        client._api = fake_api

        movie = client.set_favorite(10, False)

        self.assertEqual(fake_api.movie_obj.edited["tags"], [3])
        self.assertEqual(movie.tag_labels, ["manual-tag"])


if __name__ == "__main__":
    unittest.main()
