from __future__ import annotations

import logging
import os
import unittest
from dataclasses import dataclass
from unittest.mock import MagicMock

from app.database import SQLiteCache
from app.log import setup_logging
from app.notifier import StdoutNotifier, TelegramNotifier
from app.schemas import JwService, MovieState
from app.seerr_client import SeerrClient
from app.snapshot import movie_snapshot_payload


@dataclass
class DummyDeletionState:
    radarr_id: int = 10
    movie_path: str = "/movies/test"
    scheduled_at: int = 100
    delete_after_ts: int = 200
    last_status: str = "scheduled"
    updated_at: int = 150


class FindingsFixesTests(unittest.TestCase):
    def test_setup_logging_accepts_tz_and_log_dir(self) -> None:
        logger = setup_logging(tz="UTC", log_dir="/tmp/test_stream_sync_logs")
        self.assertIsNotNone(logger)
        self.assertEqual(os.environ.get("TZ"), "UTC")

    def test_snapshot_payload_json_serializable(self) -> None:
        movie = MovieState(movie_id=1, title="Test", path="/path", monitored=True, has_file=True)
        deletion_state = DummyDeletionState()
        services = [JwService(service_id="netflix", service_name="Netflix")]
        payload = movie_snapshot_payload(
            movie,
            conditions=["test"],
            last_evaluated_at=123,
            deletion_state=deletion_state,
            streaming_services=services,
        )
        cache = SQLiteCache(
            db_path=":memory:",
            idmap_ttl_seconds=3600,
            offers_ttl_ok_seconds=3600,
            offers_ttl_err_seconds=300,
            ttl_jitter_percent=0.1,
        )
        cache.upsert_movie_snapshots([payload])
        snapshot = cache.get_movie_snapshot(1)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.payload["deletionState"]["status"], "scheduled")
        self.assertEqual(snapshot.payload["deletionState"]["remainingSeconds"], 77)
        self.assertEqual(snapshot.payload["streamingServices"][0]["id"], "netflix")
        self.assertEqual(snapshot.payload["streamingServices"][0]["name"], "Netflix")

    def test_seerr_client_methods_exist(self) -> None:
        client = SeerrClient("http://localhost:5055", "api-key")
        self.assertTrue(hasattr(client, "protected_movie_details"))
        self.assertTrue(hasattr(client, "protected_movie_tmdb_ids"))

    def test_notifier_entering_and_leaving(self) -> None:
        stdout_notifier = StdoutNotifier()
        with self.assertLogs("app.notifier", level="INFO") as logs:
            stdout_notifier.notify_entering({"Netflix": ["Movie A", "Movie B"]})
            stdout_notifier.notify_leaving({"Prime Video": ["Movie C"]})
        self.assertTrue(any("entered Netflix" in line for line in logs.output))
        self.assertTrue(any("left Prime Video" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()


    def test_never_in_streaming_unmonitored_outside_whitelist_search_reason(self) -> None:
        movie = MovieState(movie_id=1, title="Test", path="/path", monitored=False, has_file=False, tags=[])
        jw_result = JwLookupResult(status=LookupStatus.UNAVAILABLE, services=[])
        decision = evaluate_movie_policy(movie, jw_result)
        self.assertTrue(decision.trigger_search)
        self.assertEqual(decision.search_reason, "unmonitored_outside_whitelist")

    def test_canonical_service_id_leaving_service_name(self) -> None:
        movie = MovieState(
            movie_id=1,
            title="Test",
            path="/path",
            monitored=False,
            has_file=False,
            tags=[TagState(id=1, label="streaming-primevideo-withads")],
        )
        jw_result = JwLookupResult(status=LookupStatus.UNAVAILABLE, services=[])
        decision = evaluate_movie_policy(movie, jw_result)
        self.assertEqual(decision.leaving_service_ids, ["primevideo_withads"])
        self.assertEqual(decision.search_reason, "left_streaming")
