from __future__ import annotations

import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

arrapi_stub = types.ModuleType("arrapi")
arrapi_stub.ArrException = Exception
arrapi_stub.Invalid = Exception
arrapi_stub.RadarrAPI = object
sys.modules.setdefault("arrapi", arrapi_stub)

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:
    raise unittest.SkipTest("fastapi is not installed in the local Python environment")

from app.api import create_app
from app.database import SQLiteCache
from app.config import Config
from app.types import MovieState, TagState


def make_config(**overrides: object) -> Config:
    values = {
        "mode": "daemon",
        "radarr_url": "http://radarr:7878",
        "radarr_api_key": "radarr-key",
        "jw_country": "BR",
        "jw_language": "en-US",
        "remove_mode": "delete",
        "delete_after_days": 5,
        "run_interval_hours": 2,
        "jw_request_delay_seconds": 0,
        "jw_request_delay_jitter_seconds": 0,
        "search_cooldown_hours": 24,
        "theatrical_release_grace_months": 2,
        "radarr_init_max_retries": 1,
        "radarr_init_retry_seconds": 1,
        "cache_db_path": ":memory:",
        "offers_ttl_ok_days": 5,
        "offers_ttl_err_hours": 12,
        "idmap_ttl_days": 1,
        "ttl_jitter_percent": 0,
        "notify_mode": "stdout",
        "telegram_bot_token": "telegram-secret",
        "telegram_chat_id": "chat",
        "log_dir": "/app/data/logs",
        "jw_only_subscription": True,
        "jw_allowed_services": "netflix,max",
        "seerr_enabled": True,
        "seerr_url": "http://seerr:5055",
        "seerr_api_key": "seerr-secret",
        "api_enabled": True,
        "api_host": "0.0.0.0",
        "api_port": 8099,
        "stream_sync_api_key": "api-secret",
        "run_once": False,
        "dry_run": False,
        "tz": None,
        "config_file_path": ":memory:",
    }
    values.update(overrides)
    return Config(**values)


class FakeRadarr:
    def __init__(self) -> None:
        self.favorite_value: bool | None = None

    def set_favorite(self, radarr_id: int, favorite: bool) -> MovieState:
        self.favorite_value = favorite
        tags = [TagState(id=1, label="manual-tag")]
        if favorite:
            tags.append(TagState(id=2, label="favorite"))
        return MovieState(
            movie_id=radarr_id,
            tmdb_id=123,
            title="Movie",
            year=2026,
            path="/movies/Movie",
            monitored=True,
            has_file=True,
            status="released",
            in_cinemas=None,
            tags=tags,
        )


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.config_file_path = str(Path(self.tempdir.name) / "config.json")
        self.cache = SQLiteCache(":memory:", 1, 1, 1, 0)
        self.radarr = FakeRadarr()
        self.client = TestClient(
            create_app(
                make_config(config_file_path=self.config_file_path),
                self.cache,
                self.radarr,
            )
        )

    def tearDown(self) -> None:
        self.cache.close()
        self.tempdir.cleanup()

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": "api-secret"}

    def test_auth_required(self) -> None:
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 401)

    def test_status_auth_required(self) -> None:
        response = self.client.get("/api/v1/status")
        self.assertEqual(response.status_code, 401)

    def test_health_with_auth(self) -> None:
        response = self.client.get("/api/v1/health", headers=self._headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_status_defaults_before_first_cycle(self) -> None:
        response = self.client.get("/api/v1/status", headers=self._headers())

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["state"], "starting")
        self.assertEqual(body["progress"], {"processed": 0, "total": 0})
        self.assertEqual(body["safeMode"], {"active": False, "reason": None})
        self.assertEqual(body["recentEvents"], [])

    def test_status_reads_runtime_state_and_recent_events(self) -> None:
        next_cycle_at = int(time.time()) + 60
        self.cache.set_daemon_status(
            {
                "state": "waiting",
                "nextCycleAt": next_cycle_at,
                "progress": {"processed": 7, "total": 10},
            }
        )
        self.cache.append_runtime_event(
            "waiting_next_cycle",
            {"nextCycleAt": next_cycle_at},
        )

        response = self.client.get("/api/v1/status", headers=self._headers())

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["state"], "waiting")
        self.assertGreaterEqual(body["secondsUntilNextCycle"], 0)
        self.assertEqual(body["progress"], {"processed": 7, "total": 10})
        self.assertEqual(body["recentEvents"][0]["type"], "waiting_next_cycle")

    def test_runtime_events_keep_latest_100(self) -> None:
        for index in range(105):
            self.cache.append_runtime_event("event", {"index": index})

        events = self.cache.list_runtime_events()

        self.assertEqual(len(events), 100)
        self.assertEqual(events[0]["payload"]["index"], 104)
        self.assertEqual(events[-1]["payload"]["index"], 5)

    def test_movies_pagination_search_and_condition(self) -> None:
        self._seed_snapshots()

        response = self.client.get(
            "/api/v1/movies?search=alp&condition=streaming_allowed",
            headers=self._headers(),
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["results"][0]["title"], "Alpha")

    def test_search_endpoint_uses_snapshot_search(self) -> None:
        self._seed_snapshots()

        response = self.client.get(
            "/api/v1/search?q=bet",
            headers=self._headers(),
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["results"][0]["title"], "Beta")

    def _seed_snapshots(self) -> None:
        self.cache.upsert_movie_snapshots(
            [
                {
                    "radarrId": 1,
                    "tmdbId": 11,
                    "title": "Alpha",
                    "year": 2024,
                    "hasFile": True,
                    "monitored": False,
                    "path": "/movies/Alpha",
                    "tags": ["streaming-netflix"],
                    "streamingServices": [{"id": "netflix", "name": "Netflix"}],
                    "conditions": ["streaming_allowed", "scheduled_deletion"],
                    "deletionState": {"status": "scheduled"},
                    "protection": [],
                    "lastEvaluatedAt": 10,
                },
                {
                    "radarrId": 2,
                    "tmdbId": 22,
                    "title": "Beta",
                    "year": 2025,
                    "hasFile": True,
                    "monitored": True,
                    "path": "/movies/Beta",
                    "tags": ["favorite"],
                    "streamingServices": [],
                    "conditions": ["favorite"],
                    "deletionState": None,
                    "protection": [],
                    "lastEvaluatedAt": 11,
                },
            ],
            {1, 2},
        )

    def test_favorite_post_and_delete(self) -> None:
        add_response = self.client.post(
            "/api/v1/movies/10/favorite",
            headers=self._headers(),
        )
        self.assertEqual(add_response.status_code, 200)
        self.assertTrue(self.radarr.favorite_value)
        self.assertIn("favorite", add_response.json()["tags"])

        delete_response = self.client.delete(
            "/api/v1/movies/10/favorite",
            headers=self._headers(),
        )
        self.assertEqual(delete_response.status_code, 200)
        self.assertFalse(self.radarr.favorite_value)
        self.assertNotIn("favorite", delete_response.json()["tags"])

    def test_config_redacts_secrets(self) -> None:
        response = self.client.get("/api/v1/config", headers=self._headers())

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertNotIn("radarr-key", str(body))
        self.assertTrue(body["secrets"]["radarrApiKey"])

    def test_config_patch_persists_allowed_overrides(self) -> None:
        response = self.client.patch(
            "/api/v1/config",
            headers=self._headers(),
            json={"runIntervalHours": 6, "dryRun": True},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["runIntervalHours"], 6)
        self.assertTrue(body["dryRun"])
        self.assertEqual(
            body["overrides"],
            {"dryRun": True, "runIntervalHours": 6},
        )
        self.assertTrue(Path(self.config_file_path).exists())

    def test_config_patch_rejects_read_only_secret(self) -> None:
        response = self.client.patch(
            "/api/v1/config",
            headers=self._headers(),
            json={"radarrApiKey": "new-secret"},
        )

        self.assertEqual(response.status_code, 400)

    def test_config_delete_resets_overrides(self) -> None:
        self.client.patch(
            "/api/v1/config",
            headers=self._headers(),
            json={"runIntervalHours": 6},
        )

        response = self.client.delete("/api/v1/config", headers=self._headers())

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["runIntervalHours"], 2)
        self.assertEqual(body["overrides"], {})
        self.assertFalse(Path(self.config_file_path).exists())


if __name__ == "__main__":
    unittest.main()
