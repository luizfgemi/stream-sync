from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from dataclasses import replace
from datetime import date

arrapi_stub = types.ModuleType("arrapi")
arrapi_stub.ArrException = Exception
arrapi_stub.Invalid = Exception
arrapi_stub.RadarrAPI = object
sys.modules.setdefault("arrapi", arrapi_stub)

justwatch_package_stub = types.ModuleType("simplejustwatchapi")
justwatch_stub = types.ModuleType("simplejustwatchapi.justwatch")
justwatch_stub.offers_for_countries = lambda *args, **kwargs: {}
justwatch_stub.search = lambda *args, **kwargs: []
sys.modules.setdefault("simplejustwatchapi", justwatch_package_stub)
sys.modules.setdefault("simplejustwatchapi.justwatch", justwatch_stub)

from app.config import Config
from app.main import _run_cycle
from app.types import (
    CycleStats,
    DeletionStateRow,
    JwLookupResult,
    JwService,
    LookupStatus,
    MovieState,
    SeerrProtection,
    TagState,
)


class FakeCache:
    def __init__(self, deletion_state: DeletionStateRow | None = None) -> None:
        self.deletion_state = deletion_state
        self.deleted_deletion_state = False
        self.cleared_countdown_day = False
        self.marked_states: list[tuple[int, str, int]] = []
        self.next_allowed = 0
        self.search_next_allowed_set: tuple[int, int] | None = None
        self.snapshots: list[dict[str, object]] = []
        self.runtime_state: dict[str, str] = {}
        self.daemon_status: dict[str, object] = {}
        self.runtime_events: list[tuple[str, dict[str, object]]] = []

    def purge_expired(self) -> None:
        return None

    def prune_orphan_movie_state(self, _movie_ids: set[int]) -> dict[str, int]:
        return {
            "deletion_state": 0,
            "search_next_allowed": 0,
            "deletion_countdown_logged_day": 0,
        }

    def get_deletion_state(self, _movie_id: int) -> DeletionStateRow | None:
        return self.deletion_state

    def list_scheduled_deletions(self) -> list[DeletionStateRow]:
        if (
            self.deletion_state is not None
            and self.deletion_state.last_status == "scheduled"
        ):
            return [self.deletion_state]
        return []

    def delete_deletion_state(self, _movie_id: int) -> None:
        self.deleted_deletion_state = True
        self.deletion_state = None

    def clear_deletion_countdown_logged_day(self, _movie_id: int) -> None:
        self.cleared_countdown_day = True

    def get_search_next_allowed(self, _movie_id: int) -> int:
        return self.next_allowed

    def set_search_next_allowed(self, movie_id: int, next_allowed: int) -> None:
        self.search_next_allowed_set = (movie_id, next_allowed)

    def mark_state(
        self,
        radarr_id: int,
        status: str,
        updated_at: int,
        delete_after_ts: int | None = None,
    ) -> None:
        self.marked_states.append((radarr_id, status, int(delete_after_ts or 0)))
        if self.deletion_state is not None:
            if hasattr(self.deletion_state, "model_copy"):
                self.deletion_state = self.deletion_state.model_copy(
                    update={
                        "last_status": status,
                        "updated_at": updated_at,
                        "delete_after_ts": int(delete_after_ts or self.deletion_state.delete_after_ts),
                    }
                )
            else:
                self.deletion_state = replace(
                    self.deletion_state,
                    last_status=status,
                    updated_at=updated_at,
                    delete_after_ts=int(delete_after_ts or self.deletion_state.delete_after_ts),
                )

    def upsert_movie_snapshots(
        self,
        snapshots: list[dict[str, object]],
        _valid_movie_ids: set[int] | None = None,
    ) -> None:
        self.snapshots = snapshots

    def set_runtime_state(self, key: str, value: str) -> None:
        self.runtime_state[key] = value

    def set_daemon_status(self, updates: dict[str, object]) -> None:
        self.daemon_status.update(updates)

    def append_runtime_event(
        self,
        event_type: str,
        payload: dict[str, object] | None = None,
        limit: int = 100,
    ) -> None:
        self.runtime_events.append((event_type, payload or {}))
        self.runtime_events = self.runtime_events[-limit:]


class FakeRadarr:
    def __init__(self, movies: list[MovieState]) -> None:
        self.movies = movies
        self.updates: list[tuple[int, list[str], bool]] = []
        self.searches: list[int] = []
        self.rescans: list[int] = []

    def list_movies(self) -> list[MovieState]:
        return self.movies

    def reconcile_and_update_movie(
        self,
        movie: MovieState,
        desired_streaming_labels: list[str],
        monitored: bool,
    ) -> bool:
        self.updates.append((movie.movie_id, desired_streaming_labels, monitored))
        return True

    def trigger_search(self, movie_id: int) -> str:
        self.searches.append(movie_id)
        return "MoviesSearch"

    def trigger_rescan(self, movie_id: int) -> str:
        self.rescans.append(movie_id)
        return "RescanMovie"


class FakeJustWatch:
    def __init__(self, result: JwLookupResult | None = None) -> None:
        self.result = result
        self.calls = 0

    def lookup_movie(self, *_args: object, **_kwargs: object) -> JwLookupResult:
        self.calls += 1
        if self.result is None:
            raise AssertionError("movie should skip JustWatch")
        return self.result


class FakeJustWatchSequence:
    def __init__(self, results: list[JwLookupResult]) -> None:
        self.results = results
        self.calls = 0

    def lookup_movie(self, *_args: object, **_kwargs: object) -> JwLookupResult:
        result = self.results[self.calls]
        self.calls += 1
        return result


class FakeSeerr:
    def __init__(self, protected_ids: set[int] | None = None, fail: bool = False) -> None:
        self.protected_ids = protected_ids or set()
        self.fail = fail
        self.calls = 0

    def protected_movie_tmdb_ids(self) -> set[int]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("seerr unavailable")
        return set(self.protected_ids)


class FakeSeerrDetails:
    def __init__(self, details: dict[int, list[SeerrProtection]]) -> None:
        self.details = details

    def protected_movie_details(self) -> dict[int, list[SeerrProtection]]:
        return self.details


class FakeNotifier:
    def __init__(self) -> None:
        self.actions: list[str] = []
        self.errors: list[str] = []
        self.entering: list[dict[str, list[str]]] = []
        self.leaving: list[dict[str, list[str]]] = []

    def notify_action(self, _message: str) -> None:
        self.actions.append(_message)

    def notify_entering(self, _grouped: dict[str, list[str]]) -> None:
        self.entering.append(_grouped)

    def notify_leaving(self, _grouped: dict[str, list[str]]) -> None:
        self.leaving.append(_grouped)

    def notify_error(self, _message: str) -> None:
        self.errors.append(_message)


def make_config(**overrides: object) -> Config:
    values = {
        "mode": "daemon",
        "radarr_url": "http://radarr:7878",
        "radarr_api_key": "key",
        "jw_country": "BR",
        "jw_language": "en-US",
        "remove_mode": "report",
        "delete_after_days": 30,
        "run_interval_hours": 24,
        "jw_request_delay_seconds": 0,
        "jw_request_delay_jitter_seconds": 0,
        "search_cooldown_hours": 24,
        "theatrical_release_grace_months": 12,
        "radarr_init_max_retries": 1,
        "radarr_init_retry_seconds": 1,
        "cache_db_path": ":memory:",
        "offers_ttl_ok_days": 5,
        "offers_ttl_err_hours": 12,
        "idmap_ttl_days": 1,
        "ttl_jitter_percent": 0,
        "notify_mode": "stdout",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "log_dir": "",
        "jw_only_subscription": True,
        "jw_allowed_services": "netflix",
        "seerr_enabled": False,
        "seerr_url": "http://seerr:5055",
        "seerr_api_key": "",
        "api_enabled": False,
        "api_host": "0.0.0.0",
        "api_port": 8099,
        "stream_sync_api_key": "",
        "run_once": True,
        "dry_run": False,
        "tz": None,
        "config_file_path": ":memory:",
    }
    values.update(overrides)
    return Config(**values)


def make_recent_movie(**overrides: object) -> MovieState:
    movie = MovieState(
        movie_id=10,
        tmdb_id=123,
        title="Recent Movie",
        year=date.today().year,
        path="/movies/recent",
        monitored=False,
        has_file=True,
        status="released",
        in_cinemas=date.today().isoformat(),
        tags=[TagState(id=1, label="streaming-netflix")],
    )
    if hasattr(movie, "model_copy"):
        return movie.model_copy(update=overrides)
    return replace(movie, **overrides)


class RecentCycleTests(unittest.TestCase):
    def run_cycle(
        self,
        movie: MovieState,
        cache: FakeCache | None = None,
        justwatch: FakeJustWatch | None = None,
        config: Config | None = None,
        seerr: FakeSeerr | None = None,
        plex_watchlists: FakeSeerr | None = None,
    ) -> tuple[CycleStats, FakeCache, FakeRadarr, FakeJustWatch]:
        fake_cache = cache or FakeCache()
        fake_radarr = FakeRadarr([movie])
        fake_justwatch = justwatch or FakeJustWatch()
        fake_notifier = FakeNotifier()
        self.last_notifier = fake_notifier
        logger = logging.getLogger("app.test")
        logger.addHandler(logging.NullHandler())

        stats = _run_cycle(
            config or make_config(),
            fake_cache,
            fake_radarr,
            fake_justwatch,
            fake_notifier,
            logger,
            threading.Event(),
            seerr=seerr,
            plex_watchlists=plex_watchlists,
        )
        return stats, fake_cache, fake_radarr, fake_justwatch

    def test_recent_theatrical_release_protects_and_skips_justwatch(self) -> None:
        deletion_state = DeletionStateRow(
            radarr_id=10,
            movie_path="/movies/recent",
            scheduled_at=1,
            delete_after_ts=2,
            last_status="scheduled",
            updated_at=1,
        )
        stats, fake_cache, fake_radarr, fake_justwatch = self.run_cycle(
            make_recent_movie(),
            FakeCache(deletion_state=deletion_state),
        )

        self.assertEqual(stats.recent_protected, 1)
        self.assertEqual(stats.changed, 1)
        self.assertEqual(fake_radarr.updates, [(10, [], True)])
        self.assertTrue(fake_cache.deleted_deletion_state)
        self.assertTrue(fake_cache.cleared_countdown_day)
        self.assertEqual(fake_justwatch.calls, 0)

    def test_recent_theatrical_release_without_file_searches_when_released(self) -> None:
        stats, fake_cache, fake_radarr, _fake_justwatch = self.run_cycle(
            make_recent_movie(has_file=False, monitored=True, tags=[]),
        )

        self.assertEqual(stats.search_triggered, 1)
        self.assertEqual(fake_radarr.searches, [10])
        self.assertEqual(fake_cache.search_next_allowed_set[0], 10)

    def test_favorite_due_deletion_is_canceled_before_delete(self) -> None:
        with tempfile.TemporaryDirectory() as movie_path:
            movie = make_recent_movie(
                path=movie_path,
                in_cinemas=None,
                tags=[TagState(id=2, label="favorite")],
            )
            cache = FakeCache(deletion_state=due_deletion(movie, movie_path))

            stats, fake_cache, fake_radarr, fake_justwatch = self.run_cycle(
                movie,
                cache,
                config=make_config(remove_mode="delete"),
            )

            self.assertEqual(stats.favorite_skipped, 1)
            self.assertTrue(fake_cache.deleted_deletion_state)
            self.assertFalse(fake_cache.marked_states)
            self.assertFalse(fake_radarr.rescans)
            self.assertEqual(fake_justwatch.calls, 0)

    def test_recent_due_deletion_is_canceled_before_delete(self) -> None:
        with tempfile.TemporaryDirectory() as movie_path:
            movie = make_recent_movie(path=movie_path)
            cache = FakeCache(deletion_state=due_deletion(movie, movie_path))

            stats, fake_cache, fake_radarr, fake_justwatch = self.run_cycle(
                movie,
                cache,
                config=make_config(remove_mode="delete"),
            )

            self.assertEqual(stats.recent_protected, 1)
            self.assertTrue(fake_cache.deleted_deletion_state)
            self.assertFalse(fake_cache.marked_states)
            self.assertFalse(fake_radarr.rescans)
            self.assertEqual(fake_justwatch.calls, 0)

    def test_seerr_protected_due_deletion_is_canceled_and_skips_justwatch(self) -> None:
        with tempfile.TemporaryDirectory() as movie_path:
            movie = make_recent_movie(
                path=movie_path,
                in_cinemas=None,
                tags=[TagState(id=3, label="streaming-netflix")],
            )
            cache = FakeCache(deletion_state=due_deletion(movie, movie_path))

            stats, fake_cache, fake_radarr, fake_justwatch = self.run_cycle(
                movie,
                cache,
                config=make_config(remove_mode="delete", seerr_enabled=True),
                seerr=FakeSeerr({123}),
            )

            self.assertEqual(stats.seerr_protected, 1)
            self.assertEqual(stats.changed, 1)
            self.assertEqual(fake_radarr.updates, [(10, [], True)])
            self.assertTrue(fake_cache.deleted_deletion_state)
            self.assertTrue(fake_cache.cleared_countdown_day)
            self.assertFalse(fake_cache.marked_states)
            self.assertFalse(fake_radarr.rescans)
            self.assertEqual(fake_justwatch.calls, 0)

    def test_seerr_protected_without_file_searches_when_released(self) -> None:
        stats, fake_cache, fake_radarr, fake_justwatch = self.run_cycle(
            make_recent_movie(
                in_cinemas=None,
                has_file=False,
                monitored=False,
                tags=[],
            ),
            config=make_config(seerr_enabled=True),
            seerr=FakeSeerr({123}),
        )

        self.assertEqual(stats.seerr_protected, 1)
        self.assertEqual(stats.search_triggered, 1)
        self.assertEqual(fake_radarr.searches, [10])
        self.assertEqual(fake_cache.search_next_allowed_set[0], 10)
        self.assertEqual(fake_justwatch.calls, 0)

    def test_snapshot_records_seerr_sources(self) -> None:
        stats, fake_cache, _fake_radarr, _fake_justwatch = self.run_cycle(
            make_recent_movie(in_cinemas=None, tags=[]),
            config=make_config(seerr_enabled=True),
            seerr=FakeSeerrDetails(
                {
                    123: [
                        SeerrProtection(source="seerr_request", user="alice"),
                        SeerrProtection(source="plex_watchlist", user="bob"),
                    ]
                }
            ),
        )

        self.assertEqual(stats.seerr_protected, 1)
        self.assertEqual(
            fake_cache.snapshots[0]["conditions"],
            ["plex_watchlist", "seerr_request"],
        )
        self.assertEqual(
            fake_cache.snapshots[0]["protection"],
            [
                {"source": "seerr_request", "user": "alice"},
                {"source": "plex_watchlist", "user": "bob"},
            ],
        )

    def test_movie_removed_from_seerr_protection_returns_to_streaming_policy(self) -> None:
        movie = make_recent_movie(
            in_cinemas=None,
            monitored=True,
            has_file=True,
            tags=[],
        )

        stats, _fake_cache, fake_radarr, fake_justwatch = self.run_cycle(
            movie,
            justwatch=FakeJustWatch(available_on_netflix()),
            config=make_config(seerr_enabled=True),
            seerr=FakeSeerr(set()),
        )

        self.assertEqual(stats.seerr_protected, 0)
        self.assertEqual(fake_justwatch.calls, 1)
        self.assertEqual(fake_radarr.updates, [(10, ["streaming-netflix"], False)])

    def test_direct_plex_watchlist_protects_movie(self) -> None:
        movie = make_recent_movie(
            monitored=False,
            tags=[TagState(id=1, label="streaming-netflix")],
            in_cinemas=None,
        )
        stats, _cache, radarr, _justwatch = self.run_cycle(
            movie,
            config=make_config(plex_watchlist_sync_enabled=True),
            plex_watchlists=FakeSeerrDetails(
                {
                    123: [
                        SeerrProtection(
                            source="plex_watchlist",
                            user="alice",
                        )
                    ]
                }
            ),
        )

        self.assertEqual(stats.seerr_protected, 1)
        self.assertEqual(radarr.updates[-1][1], [])
        self.assertTrue(radarr.updates[-1][2])

    def test_movie_removed_from_direct_watchlist_returns_to_streaming_policy(
        self,
    ) -> None:
        movie = make_recent_movie(monitored=True, tags=[], in_cinemas=None)
        stats, _cache, radarr, _justwatch = self.run_cycle(
            movie,
            config=make_config(plex_watchlist_sync_enabled=True),
            plex_watchlists=FakeSeerrDetails({}),
            justwatch=FakeJustWatch(available_on_netflix()),
        )

        self.assertEqual(stats.seerr_protected, 0)
        self.assertEqual(radarr.updates[-1][1], ["streaming-netflix"])
        self.assertFalse(radarr.updates[-1][2])

    def test_seerr_failure_suppresses_due_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as movie_path:
            movie = make_recent_movie(path=movie_path, in_cinemas=None, tags=[])
            cache = FakeCache(deletion_state=due_deletion(movie, movie_path))

            _stats, fake_cache, fake_radarr, fake_justwatch = self.run_cycle(
                movie,
                cache,
                justwatch=FakeJustWatch(available_on_netflix()),
                config=make_config(remove_mode="delete", seerr_enabled=True),
                seerr=FakeSeerr(fail=True),
            )

            self.assertEqual(fake_justwatch.calls, 1)
            self.assertFalse(fake_cache.marked_states)
            self.assertFalse(fake_radarr.rescans)
            self.assertTrue(os.path.exists(movie_path))

    def test_movie_that_left_streaming_is_not_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as movie_path:
            movie = make_recent_movie(
                path=movie_path,
                in_cinemas=None,
                tags=[TagState(id=3, label="streaming-netflix")],
            )
            cache = FakeCache(deletion_state=due_deletion(movie, movie_path))

            _stats, fake_cache, fake_radarr, fake_justwatch = self.run_cycle(
                movie,
                cache,
                justwatch=FakeJustWatch(JwLookupResult(status=LookupStatus.UNAVAILABLE)),
                config=make_config(remove_mode="delete"),
            )

            self.assertEqual(fake_justwatch.calls, 1)
            self.assertTrue(fake_cache.deleted_deletion_state)
            self.assertFalse(fake_cache.marked_states)
            self.assertFalse(fake_radarr.rescans)

    def test_unknown_justwatch_due_deletion_is_not_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as movie_path:
            movie = make_recent_movie(path=movie_path, in_cinemas=None, tags=[])
            cache = FakeCache(deletion_state=due_deletion(movie, movie_path))

            stats, fake_cache, fake_radarr, fake_justwatch = self.run_cycle(
                movie,
                cache,
                justwatch=FakeJustWatch(JwLookupResult(status=LookupStatus.UNKNOWN)),
                config=make_config(remove_mode="delete"),
            )

            self.assertEqual(stats.unknown, 1)
            self.assertEqual(fake_justwatch.calls, 1)
            self.assertFalse(fake_cache.deleted_deletion_state)
            self.assertFalse(fake_cache.marked_states)
            self.assertFalse(fake_radarr.rescans)

    def test_available_due_deletion_is_deleted_after_current_validation(self) -> None:
        movie_path_holder = tempfile.TemporaryDirectory()
        movie_path = movie_path_holder.name
        movie = make_recent_movie(path=movie_path, in_cinemas=None, tags=[])
        cache = FakeCache(deletion_state=due_deletion(movie, movie_path))

        _stats, fake_cache, fake_radarr, fake_justwatch = self.run_cycle(
            movie,
            cache,
            justwatch=FakeJustWatch(available_on_netflix()),
            config=make_config(remove_mode="delete"),
        )

        self.assertEqual(fake_justwatch.calls, 1)
        self.assertEqual(fake_cache.marked_states, [(10, "deleted", 0)])
        self.assertEqual(fake_radarr.rescans, [10])
        self.assertFalse(os.path.exists(movie_path))
        movie_path_holder.cleanup()

    def test_due_deletion_uses_stored_delete_after_ts_not_current_config(self) -> None:
        movie_path_holder = tempfile.TemporaryDirectory()
        movie_path = movie_path_holder.name
        movie = make_recent_movie(path=movie_path, in_cinemas=None, tags=[])
        cache = FakeCache(
            deletion_state=DeletionStateRow(
                radarr_id=movie.movie_id,
                movie_path=movie_path,
                scheduled_at=int(time.time()),
                delete_after_ts=1,
                last_status="scheduled",
                updated_at=1,
            )
        )

        _stats, fake_cache, fake_radarr, _fake_justwatch = self.run_cycle(
            movie,
            cache,
            justwatch=FakeJustWatch(available_on_netflix()),
            config=make_config(remove_mode="delete", delete_after_days=365),
        )

        self.assertEqual(fake_cache.marked_states, [(10, "deleted", 0)])
        self.assertEqual(fake_radarr.rescans, [10])
        self.assertFalse(os.path.exists(movie_path))
        movie_path_holder.cleanup()

    def test_dry_run_available_due_deletion_does_not_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as movie_path:
            movie = make_recent_movie(path=movie_path, in_cinemas=None, tags=[])
            cache = FakeCache(deletion_state=due_deletion(movie, movie_path))

            _stats, fake_cache, fake_radarr, _fake_justwatch = self.run_cycle(
                movie,
                cache,
                justwatch=FakeJustWatch(available_on_netflix()),
                config=make_config(remove_mode="delete", dry_run=True),
            )

            self.assertFalse(fake_cache.marked_states)
            self.assertFalse(fake_radarr.rescans)
            self.assertTrue(os.path.exists(movie_path))

    def test_dry_run_counts_favorite_and_recent_changes(self) -> None:
        favorite = make_recent_movie(
            in_cinemas=None,
            monitored=False,
            tags=[
                TagState(id=4, label="favorite"),
                TagState(id=5, label="streaming-netflix"),
            ],
        )
        favorite_stats, _cache, _radarr, _jw = self.run_cycle(
            favorite,
            config=make_config(dry_run=True),
        )

        recent = make_recent_movie(
            monitored=False,
            tags=[TagState(id=5, label="streaming-netflix")],
        )
        recent_stats, _cache, _radarr, _jw = self.run_cycle(
            recent,
            config=make_config(dry_run=True),
        )

        self.assertEqual(favorite_stats.changed, 1)
        self.assertEqual(recent_stats.changed, 1)

    def test_dry_run_does_not_emit_grouped_streaming_notifications(self) -> None:
        movie = make_recent_movie(
            in_cinemas=None,
            monitored=True,
            has_file=True,
            tags=[],
        )

        self.run_cycle(
            movie,
            justwatch=FakeJustWatch(available_on_netflix()),
            config=make_config(dry_run=True),
        )

        self.assertEqual(self.last_notifier.entering, [])
        self.assertEqual(self.last_notifier.leaving, [])

    def test_cycle_writes_runtime_status_and_events(self) -> None:
        stats, fake_cache, _fake_radarr, _fake_justwatch = self.run_cycle(
            make_recent_movie(),
        )

        self.assertEqual(stats.processed, 1)
        self.assertEqual(fake_cache.daemon_status["currentMovie"], None)
        self.assertEqual(
            fake_cache.daemon_status["progress"],
            {"processed": 1, "total": 1},
        )
        self.assertEqual(
            fake_cache.daemon_status["lastCycleStats"]["recentProtected"],
            1,
        )
        self.assertIn("cycle_started", [event[0] for event in fake_cache.runtime_events])
        self.assertIn("cycle_finished", [event[0] for event in fake_cache.runtime_events])

    def test_snapshot_conditions_do_not_leak_between_movies(self) -> None:
        first = make_recent_movie(
            movie_id=10,
            tmdb_id=1010,
            title="Available Without File",
            in_cinemas=None,
            has_file=False,
            monitored=True,
            tags=[],
        )
        second = make_recent_movie(
            movie_id=11,
            tmdb_id=1111,
            title="Unavailable After Available",
            in_cinemas=None,
            has_file=True,
            monitored=True,
            tags=[],
        )
        fake_cache = FakeCache()
        fake_radarr = FakeRadarr([first, second])
        fake_justwatch = FakeJustWatchSequence(
            [
                available_on_netflix(),
                JwLookupResult(status=LookupStatus.UNAVAILABLE),
            ]
        )
        logger = logging.getLogger("app.test")
        logger.addHandler(logging.NullHandler())

        _run_cycle(
            make_config(),
            fake_cache,
            fake_radarr,
            fake_justwatch,
            FakeNotifier(),
            logger,
            threading.Event(),
        )

        self.assertEqual(fake_cache.snapshots[0]["conditions"], ["streaming_allowed"])
        self.assertEqual(fake_cache.snapshots[1]["conditions"], [])


def due_deletion(movie: MovieState, movie_path: str) -> DeletionStateRow:
    return DeletionStateRow(
        radarr_id=movie.movie_id,
        movie_path=movie_path,
        scheduled_at=1,
        delete_after_ts=2,
        last_status="scheduled",
        updated_at=1,
    )


def available_on_netflix() -> JwLookupResult:
    return JwLookupResult(
        status=LookupStatus.AVAILABLE,
        services=[JwService(service_id="netflix", service_name="Netflix")],
    )


if __name__ == "__main__":
    unittest.main()
