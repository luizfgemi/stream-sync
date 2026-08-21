from __future__ import annotations

import sys
import types
import unittest

justwatch_package_stub = types.ModuleType("simplejustwatchapi")
justwatch_stub = types.ModuleType("simplejustwatchapi.justwatch")
justwatch_stub.offers_for_countries = lambda *args, **kwargs: {}
justwatch_stub.search = lambda *args, **kwargs: []
sys.modules.setdefault("simplejustwatchapi", justwatch_package_stub)
sys.modules.setdefault("simplejustwatchapi.justwatch", justwatch_stub)

from app.justwatch_provider import JustWatchProvider
from app.types import LookupStatus, MovieState


class FakeCache:
    def delete_jw_node_id(self, _tmdb_id: int) -> None:
        return None

    def set_jw_node_id(self, _tmdb_id: int, _jw_node_id: str) -> None:
        return None

    def set_offers_error(
        self,
        _jw_node_id: str,
        _country: str,
        _error_message: str,
    ) -> None:
        return None

    def set_offers_ok(
        self,
        _jw_node_id: str,
        _country: str,
        _payload: dict[str, object],
    ) -> None:
        return None


def make_movie() -> MovieState:
    return MovieState(
        movie_id=1,
        tmdb_id=123,
        title="Movie",
        year=2026,
        path="/movies/Movie",
        monitored=True,
        has_file=True,
        status="released",
        in_cinemas=None,
        tags=[],
    )


class JustWatchProviderTests(unittest.TestCase):
    def make_provider(self) -> JustWatchProvider:
        return JustWatchProvider(
            cache=FakeCache(),
            country="BR",
            language="en-US",
            request_delay_seconds=0,
            request_delay_jitter_seconds=0,
        )

    def test_schema_error_after_idmap_refresh_stays_schema_error(self) -> None:
        provider = self.make_provider()
        provider._resolve_node_id = lambda _movie: "node-2"
        provider._fetch_payload_for_node = lambda _node_id: ({"offers": "bad"}, None)

        result = provider._retry_after_idmap_refresh(
            movie=make_movie(),
            enabled_services={"netflix"},
            previous_node_id="node-1",
            previous_error="previous schema error",
        )

        self.assertEqual(result.status, LookupStatus.SCHEMA_ERROR)
        self.assertIn("schema error", result.error_message or "")

    def test_resolve_node_id_propagates_interrupted_error(self) -> None:
        provider = self.make_provider()
        provider._wait_before_request = lambda: (_ for _ in ()).throw(
            InterruptedError("stop")
        )

        with self.assertRaises(InterruptedError):
            provider._resolve_node_id(make_movie())


if __name__ == "__main__":
    unittest.main()
