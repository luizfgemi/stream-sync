from __future__ import annotations

import json
import os
import random
import sqlite3
import threading
import time
from typing import Any

from .types import CachedOffersEntry, DeletionStateRow, MovieSnapshot

RUNTIME_EVENT_LIMIT = 100


def _daemon_status_defaults() -> dict[str, Any]:
    return {
        "state": "starting",
        "cycleStartedAt": None,
        "lastCycleFinishedAt": None,
        "nextCycleAt": None,
        "progress": {"processed": 0, "total": 0},
        "currentMovie": None,
        "lastCycleStats": {
            "processed": 0,
            "favorites": 0,
            "seerrProtected": 0,
            "recentProtected": 0,
            "changed": 0,
            "searches": 0,
            "unknown": 0,
            "schemaErrors": 0,
            "errors": 0,
        },
        "deletionQueue": {
            "scheduled": 0,
            "dueNow": 0,
            "potentialSavingsBytes": 0,
            "potentialSavings": "0 B",
            "sizedPaths": 0,
            "missingOrInvalidPaths": 0,
        },
        "safeMode": {"active": False, "reason": None},
    }


class SQLiteCache:
    def __init__(
        self,
        db_path: str,
        idmap_ttl_seconds: int,
        offers_ttl_ok_seconds: int,
        offers_ttl_err_seconds: int,
        ttl_jitter_percent: int,
    ) -> None:
        self._db_path = db_path
        self._idmap_ttl_seconds = idmap_ttl_seconds
        self._offers_ttl_ok_seconds = offers_ttl_ok_seconds
        self._offers_ttl_err_seconds = offers_ttl_err_seconds
        self._ttl_jitter_percent = ttl_jitter_percent
        self._lock = threading.Lock()

        parent = os.path.dirname(self._db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._create_tables()

    def _create_tables(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jw_id_map (
                    tmdb_id INTEGER PRIMARY KEY,
                    jw_node_id TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jw_offers_cache (
                    jw_node_id TEXT NOT NULL,
                    country TEXT NOT NULL,
                    payload_json TEXT,
                    is_error INTEGER NOT NULL,
                    error_message TEXT,
                    updated_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    PRIMARY KEY (jw_node_id, country)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cursor_state (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS deletion_state (
                    radarr_id INTEGER PRIMARY KEY,
                    movie_path TEXT NOT NULL,
                    scheduled_at INTEGER NOT NULL,
                    delete_after_ts INTEGER NOT NULL,
                    last_status TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS movie_snapshot (
                    radarr_id INTEGER PRIMARY KEY,
                    tmdb_id INTEGER,
                    title TEXT NOT NULL,
                    year INTEGER,
                    payload_json TEXT NOT NULL,
                    conditions_json TEXT NOT NULL,
                    last_evaluated_at INTEGER NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_event (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jw_id_map_expires ON jw_id_map(expires_at)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jw_offers_expires ON jw_offers_cache(expires_at)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_deletion_delete_after ON deletion_state(delete_after_ts)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_movie_snapshot_title ON movie_snapshot(title)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_movie_snapshot_tmdb ON movie_snapshot(tmdb_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runtime_event_created ON runtime_event(created_at)"
            )

    @staticmethod
    def _now_ts() -> int:
        return int(time.time())

    def _with_jitter(self, base_seconds: int) -> int:
        if self._ttl_jitter_percent <= 0:
            return max(1, base_seconds)
        pct = self._ttl_jitter_percent / 100.0
        jitter = base_seconds * pct
        value = base_seconds + random.uniform(-jitter, jitter)
        return max(1, int(value))

    def get_jw_node_id(self, tmdb_id: int) -> str | None:
        now = self._now_ts()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT jw_node_id, expires_at FROM jw_id_map WHERE tmdb_id = ?",
                (tmdb_id,),
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] <= now:
                self._conn.execute("DELETE FROM jw_id_map WHERE tmdb_id = ?", (tmdb_id,))
                return None
            return str(row["jw_node_id"])

    def set_jw_node_id(self, tmdb_id: int, jw_node_id: str) -> None:
        now = self._now_ts()
        expires_at = now + self._with_jitter(self._idmap_ttl_seconds)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO jw_id_map (tmdb_id, jw_node_id, updated_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(tmdb_id) DO UPDATE SET
                    jw_node_id = excluded.jw_node_id,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at
                """,
                (tmdb_id, jw_node_id, now, expires_at),
            )

    def delete_jw_node_id(self, tmdb_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM jw_id_map WHERE tmdb_id = ?", (int(tmdb_id),))

    def get_offers(self, jw_node_id: str, country: str) -> CachedOffersEntry | None:
        now = self._now_ts()
        country = country.upper()
        with self._lock, self._conn:
            row = self._conn.execute(
                """
                SELECT payload_json, is_error, error_message, expires_at
                FROM jw_offers_cache
                WHERE jw_node_id = ? AND country = ?
                """,
                (jw_node_id, country),
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] <= now:
                self._conn.execute(
                    "DELETE FROM jw_offers_cache WHERE jw_node_id = ? AND country = ?",
                    (jw_node_id, country),
                )
                return None

            is_error = bool(row["is_error"])
            if is_error:
                return CachedOffersEntry(is_error=True, error_message=row["error_message"])

            payload_json = row["payload_json"]
            if payload_json is None:
                raise ValueError(
                    f"Inconsistent cache: missing payload for jw_node_id={jw_node_id}"
                )
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Corrupted cache: invalid payload for jw_node_id={jw_node_id}"
                ) from exc
            if not isinstance(payload, dict):
                raise ValueError(
                    f"Invalid cache: payload is not an object for jw_node_id={jw_node_id}"
                )
            return CachedOffersEntry(is_error=False, payload=payload)

    def set_offers_ok(self, jw_node_id: str, country: str, payload: dict[str, Any]) -> None:
        now = self._now_ts()
        country = country.upper()
        expires_at = now + self._with_jitter(self._offers_ttl_ok_seconds)
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO jw_offers_cache (
                    jw_node_id, country, payload_json, is_error, error_message, updated_at, expires_at
                )
                VALUES (?, ?, ?, 0, NULL, ?, ?)
                ON CONFLICT(jw_node_id, country) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    is_error = excluded.is_error,
                    error_message = excluded.error_message,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at
                """,
                (jw_node_id, country, payload_json, now, expires_at),
            )

    def set_offers_error(self, jw_node_id: str, country: str, error_message: str) -> None:
        now = self._now_ts()
        country = country.upper()
        expires_at = now + self._with_jitter(self._offers_ttl_err_seconds)
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO jw_offers_cache (
                    jw_node_id, country, payload_json, is_error, error_message, updated_at, expires_at
                )
                VALUES (?, ?, NULL, 1, ?, ?, ?)
                ON CONFLICT(jw_node_id, country) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    is_error = excluded.is_error,
                    error_message = excluded.error_message,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at
                """,
                (jw_node_id, country, error_message[:1000], now, expires_at),
            )

    def get_cursor(self, key: str, default: int = 0) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM cursor_state WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return default
            return int(row["value"])

    def set_cursor(self, key: str, value: int) -> None:
        now = self._now_ts()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO cursor_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, int(value), now),
            )

    def delete_cursor(self, key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM cursor_state WHERE key = ?", (key,))

    @staticmethod
    def _search_key(movie_id: int) -> str:
        return f"search_next_allowed_{int(movie_id)}"

    def get_search_next_allowed(self, movie_id: int) -> int:
        return self.get_cursor(self._search_key(movie_id), default=0)

    def set_search_next_allowed(self, movie_id: int, epoch_seconds: int) -> None:
        self.set_cursor(self._search_key(movie_id), int(epoch_seconds))

    @staticmethod
    def _deletion_countdown_day_key(movie_id: int) -> str:
        return f"deletion_countdown_logged_day_{int(movie_id)}"

    def get_deletion_countdown_logged_day(self, movie_id: int) -> int:
        return self.get_cursor(self._deletion_countdown_day_key(movie_id), default=0)

    def set_deletion_countdown_logged_day(self, movie_id: int, yyyymmdd: int) -> None:
        self.set_cursor(self._deletion_countdown_day_key(movie_id), int(yyyymmdd))

    def clear_deletion_countdown_logged_day(self, movie_id: int) -> None:
        self.delete_cursor(self._deletion_countdown_day_key(movie_id))

    @staticmethod
    def _row_to_deletion_state(row: sqlite3.Row) -> DeletionStateRow:
        return DeletionStateRow(
            radarr_id=int(row["radarr_id"]),
            movie_path=str(row["movie_path"]),
            scheduled_at=int(row["scheduled_at"]),
            delete_after_ts=int(row["delete_after_ts"]),
            last_status=str(row["last_status"]),
            updated_at=int(row["updated_at"]),
        )

    def get_deletion_state(self, radarr_id: int) -> DeletionStateRow | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT radarr_id, movie_path, scheduled_at,
                       delete_after_ts, last_status, updated_at
                FROM deletion_state
                WHERE radarr_id = ?
                """,
                (int(radarr_id),),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_deletion_state(row)

    def upsert_deletion_state(
        self,
        radarr_id: int,
        movie_path: str,
        scheduled_at: int,
        delete_after_ts: int,
        last_status: str,
        updated_at: int,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO deletion_state (
                    radarr_id, movie_path, scheduled_at,
                    delete_after_ts, last_status, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(radarr_id) DO UPDATE SET
                    movie_path = excluded.movie_path,
                    scheduled_at = excluded.scheduled_at,
                    delete_after_ts = excluded.delete_after_ts,
                    last_status = excluded.last_status,
                    updated_at = excluded.updated_at
                """,
                (
                    int(radarr_id),
                    movie_path,
                    int(scheduled_at),
                    int(delete_after_ts),
                    last_status,
                    int(updated_at),
                ),
            )

    def delete_deletion_state(self, radarr_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM deletion_state WHERE radarr_id = ?",
                (int(radarr_id),),
            )

    def list_scheduled_deletions(self) -> list[DeletionStateRow]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT radarr_id, movie_path, scheduled_at,
                       delete_after_ts, last_status, updated_at
                FROM deletion_state
                WHERE last_status = 'scheduled'
                ORDER BY scheduled_at ASC
                """,
            ).fetchall()
            return [self._row_to_deletion_state(row) for row in rows]

    def prune_orphan_movie_state(self, valid_movie_ids: set[int]) -> dict[str, int]:
        valid_ids = {int(movie_id) for movie_id in valid_movie_ids}
        removed_deletions = 0
        removed_search_cursors = 0
        removed_countdown_cursors = 0

        with self._lock, self._conn:
            deletion_rows = self._conn.execute(
                "SELECT radarr_id FROM deletion_state"
            ).fetchall()
            orphan_deletion_ids = [
                int(row["radarr_id"])
                for row in deletion_rows
                if int(row["radarr_id"]) not in valid_ids
            ]
            for movie_id in orphan_deletion_ids:
                self._conn.execute(
                    "DELETE FROM deletion_state WHERE radarr_id = ?",
                    (movie_id,),
                )
            removed_deletions = len(orphan_deletion_ids)

            cursor_rows = self._conn.execute(
                """
                SELECT key FROM cursor_state
                WHERE key LIKE 'search_next_allowed_%'
                   OR key LIKE 'deletion_countdown_logged_day_%'
                """
            ).fetchall()
            orphan_search_keys: list[str] = []
            orphan_countdown_keys: list[str] = []
            for row in cursor_rows:
                key = str(row["key"])
                movie_id_text = key.rsplit("_", 1)[-1]
                try:
                    movie_id = int(movie_id_text)
                except ValueError:
                    continue
                if movie_id in valid_ids:
                    continue
                if key.startswith("search_next_allowed_"):
                    orphan_search_keys.append(key)
                elif key.startswith("deletion_countdown_logged_day_"):
                    orphan_countdown_keys.append(key)

            for key in orphan_search_keys:
                self._conn.execute("DELETE FROM cursor_state WHERE key = ?", (key,))
            for key in orphan_countdown_keys:
                self._conn.execute("DELETE FROM cursor_state WHERE key = ?", (key,))

            removed_search_cursors = len(orphan_search_keys)
            removed_countdown_cursors = len(orphan_countdown_keys)

        return {
            "deletion_state": removed_deletions,
            "search_next_allowed": removed_search_cursors,
            "deletion_countdown_logged_day": removed_countdown_cursors,
        }

    def mark_state(
        self,
        radarr_id: int,
        status: str,
        updated_at: int,
        delete_after_ts: int | None = None,
    ) -> None:
        with self._lock, self._conn:
            if delete_after_ts is None:
                self._conn.execute(
                    """
                    UPDATE deletion_state
                    SET last_status = ?, updated_at = ?
                    WHERE radarr_id = ?
                    """,
                    (status, int(updated_at), int(radarr_id)),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE deletion_state
                    SET last_status = ?, updated_at = ?, delete_after_ts = ?
                    WHERE radarr_id = ?
                    """,
                    (status, int(updated_at), int(delete_after_ts), int(radarr_id)),
                )

    @staticmethod
    def _row_to_movie_snapshot(row: sqlite3.Row) -> MovieSnapshot:
        payload = json.loads(str(row["payload_json"]))
        conditions = json.loads(str(row["conditions_json"]))
        if not isinstance(payload, dict):
            payload = {}
        if not isinstance(conditions, list):
            conditions = []
        return MovieSnapshot(
            radarr_id=int(row["radarr_id"]),
            tmdb_id=int(row["tmdb_id"]) if row["tmdb_id"] is not None else None,
            title=str(row["title"]),
            year=int(row["year"]) if row["year"] is not None else None,
            payload=payload,
            conditions=[str(item) for item in conditions],
            last_evaluated_at=int(row["last_evaluated_at"]),
        )

    def upsert_movie_snapshots(
        self,
        snapshots: list[dict[str, Any]],
        valid_movie_ids: set[int] | None = None,
    ) -> None:
        with self._lock, self._conn:
            for snapshot in snapshots:
                radarr_id = int(snapshot["radarrId"])
                conditions = [str(item) for item in snapshot.get("conditions", [])]
                payload_json = json.dumps(
                    snapshot,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                conditions_json = json.dumps(
                    conditions,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                self._conn.execute(
                    """
                    INSERT INTO movie_snapshot (
                        radarr_id, tmdb_id, title, year, payload_json,
                        conditions_json, last_evaluated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(radarr_id) DO UPDATE SET
                        tmdb_id = excluded.tmdb_id,
                        title = excluded.title,
                        year = excluded.year,
                        payload_json = excluded.payload_json,
                        conditions_json = excluded.conditions_json,
                        last_evaluated_at = excluded.last_evaluated_at
                    """,
                    (
                        radarr_id,
                        snapshot.get("tmdbId"),
                        str(snapshot.get("title") or ""),
                        snapshot.get("year"),
                        payload_json,
                        conditions_json,
                        int(snapshot.get("lastEvaluatedAt") or self._now_ts()),
                    ),
                )

            if valid_movie_ids is not None:
                valid_ids = {int(movie_id) for movie_id in valid_movie_ids}
                rows = self._conn.execute("SELECT radarr_id FROM movie_snapshot").fetchall()
                for row in rows:
                    movie_id = int(row["radarr_id"])
                    if movie_id not in valid_ids:
                        self._conn.execute(
                            "DELETE FROM movie_snapshot WHERE radarr_id = ?",
                            (movie_id,),
                        )

    def list_movie_snapshots(
        self,
        page: int,
        page_size: int,
        search: str = "",
        condition: str = "",
        sort: str = "title",
    ) -> tuple[list[MovieSnapshot], int]:
        page = max(1, int(page))
        page_size = min(200, max(1, int(page_size)))
        clauses: list[str] = []
        params: list[object] = []

        if search.strip():
            clauses.append("LOWER(title) LIKE ?")
            params.append(f"%{search.strip().lower()}%")
        if condition.strip():
            clauses.append("conditions_json LIKE ?")
            params.append(f'%"{condition.strip()}"%')

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sort_sql = {
            "title": "LOWER(title) ASC, year ASC",
            "year": "year DESC, LOWER(title) ASC",
            "updated": "last_evaluated_at DESC, LOWER(title) ASC",
            "radarr_id": "radarr_id ASC",
        }.get(sort, "LOWER(title) ASC, year ASC")
        offset = (page - 1) * page_size

        with self._lock:
            total_row = self._conn.execute(
                f"SELECT COUNT(*) AS count FROM movie_snapshot {where_sql}",
                tuple(params),
            ).fetchone()
            rows = self._conn.execute(
                f"""
                SELECT radarr_id, tmdb_id, title, year, payload_json,
                       conditions_json, last_evaluated_at
                FROM movie_snapshot
                {where_sql}
                ORDER BY {sort_sql}
                LIMIT ? OFFSET ?
                """,
                tuple(params + [page_size, offset]),
            ).fetchall()
            total = int(total_row["count"]) if total_row is not None else 0
            return [self._row_to_movie_snapshot(row) for row in rows], total

    def get_movie_snapshot(self, radarr_id: int) -> MovieSnapshot | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT radarr_id, tmdb_id, title, year, payload_json,
                       conditions_json, last_evaluated_at
                FROM movie_snapshot
                WHERE radarr_id = ?
                """,
                (int(radarr_id),),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_movie_snapshot(row)

    def set_runtime_state(self, key: str, value: str) -> None:
        now = self._now_ts()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO runtime_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, now),
            )

    def get_runtime_state(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM runtime_state WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            return str(row["value"])

    def _load_daemon_status_locked(self) -> dict[str, Any]:
        status = _daemon_status_defaults()
        row = self._conn.execute(
            "SELECT value FROM runtime_state WHERE key = ?",
            ("daemon_status",),
        ).fetchone()
        if row is None:
            return status
        try:
            stored = json.loads(str(row["value"]))
        except json.JSONDecodeError:
            return status
        if isinstance(stored, dict):
            status.update(stored)
        return status

    def set_daemon_status(self, updates: dict[str, Any]) -> None:
        status = _daemon_status_defaults()
        now = self._now_ts()
        with self._lock, self._conn:
            status.update(self._load_daemon_status_locked())
            status.update(updates)
            self._conn.execute(
                """
                INSERT INTO runtime_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (
                    "daemon_status",
                    json.dumps(status, ensure_ascii=False, separators=(",", ":")),
                    now,
                ),
            )

    def append_runtime_event(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        limit: int = RUNTIME_EVENT_LIMIT,
    ) -> None:
        now = self._now_ts()
        payload_json = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO runtime_event (event_type, payload_json, created_at)
                VALUES (?, ?, ?)
                """,
                (event_type, payload_json, now),
            )
            self._conn.execute(
                """
                DELETE FROM runtime_event
                WHERE id NOT IN (
                    SELECT id FROM runtime_event ORDER BY id DESC LIMIT ?
                )
                """,
                (max(1, int(limit)),),
            )

    def list_runtime_events(self, limit: int = RUNTIME_EVENT_LIMIT) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, event_type, payload_json, created_at
                FROM runtime_event
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            events.append(
                {
                    "id": int(row["id"]),
                    "type": str(row["event_type"]),
                    "createdAt": int(row["created_at"]),
                    "payload": payload,
                }
            )
        return events

    def get_daemon_status(self) -> dict[str, Any]:
        now = self._now_ts()
        with self._lock:
            status = self._load_daemon_status_locked()

        next_cycle_at = status.get("nextCycleAt")
        try:
            seconds_until_next_cycle = (
                max(0, int(next_cycle_at) - now)
                if next_cycle_at is not None
                else None
            )
        except (TypeError, ValueError):
            seconds_until_next_cycle = None

        status["now"] = now
        status["secondsUntilNextCycle"] = seconds_until_next_cycle
        status["recentEvents"] = self.list_runtime_events(RUNTIME_EVENT_LIMIT)
        return status

    def purge_expired(self) -> None:
        now = self._now_ts()
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM jw_id_map WHERE expires_at <= ?", (now,))
            self._conn.execute(
                "DELETE FROM jw_offers_cache WHERE expires_at <= ?",
                (now,),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
