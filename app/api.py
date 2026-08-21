from __future__ import annotations

import time
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, status as http_status

from .database import SQLiteCache
from .config import (
    API_CONFIG_FIELDS,
    READ_ONLY_CONFIG_FIELDS,
    Config,
    load_config_overrides,
    reset_config_overrides,
    update_config_overrides,
)
from .radarr import RadarrClient
from .snapshot import movie_snapshot_payload


def _redacted_config(
    config: Config,
    overrides: dict[str, Any] | None = None,
) -> dict[str, object]:
    return {
        "mode": config.mode,
        "jwCountry": config.jw_country,
        "jwLanguage": config.jw_language,
        "removeMode": config.remove_mode,
        "deleteAfterDays": config.delete_after_days,
        "runIntervalHours": config.run_interval_hours,
        "searchCooldownHours": config.search_cooldown_hours,
        "theatricalReleaseGraceMonths": config.theatrical_release_grace_months,
        "jwAllowedServices": config.jw_allowed_services,
        "jwOnlySubscription": config.jw_only_subscription,
        "seerrEnabled": config.seerr_enabled,
        "seerrUrl": config.seerr_url,
        "apiEnabled": config.api_enabled,
        "apiHost": config.api_host,
        "apiPort": config.api_port,
        "dryRun": config.dry_run,
        "notifyMode": config.notify_mode,
        "cacheDbPath": config.cache_db_path,
        "logDir": config.log_dir,
        "configFilePath": config.config_file_path,
        "overrides": overrides or {},
        "mutableFields": sorted(API_CONFIG_FIELDS.keys()),
        "readOnlyFields": sorted(READ_ONLY_CONFIG_FIELDS),
        "applies": "next_cycle",
        "secrets": {
            "radarrApiKey": bool(config.radarr_api_key),
            "seerrApiKey": bool(config.seerr_api_key),
            "streamSyncApiKey": bool(config.stream_sync_api_key),
            "telegramBotToken": bool(config.telegram_bot_token),
        },
    }


def _movie_page_response(
    cache: SQLiteCache,
    page: int,
    page_size: int,
    search: str = "",
    condition: str = "",
    sort: str = "title",
) -> dict[str, object]:
    rows, total = cache.list_movie_snapshots(
        page=page,
        page_size=page_size,
        search=search,
        condition=condition,
        sort=sort,
    )
    return {
        "page": page,
        "pageSize": page_size,
        "total": total,
        "pages": (total + page_size - 1) // page_size,
        "results": [row.payload for row in rows],
    }


def create_app(config: Config, cache: SQLiteCache, radarr: RadarrClient) -> FastAPI:
    app = FastAPI(title="stream-sync", version="1.0")
    started_at = int(time.time())

    def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
        if not config.stream_sync_api_key:
            raise HTTPException(
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="API key is not configured",
            )
        if x_api_key != config.stream_sync_api_key:
            raise HTTPException(
                status_code=http_status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
            )

    auth_dependency = Depends(require_api_key)

    def _effective_config_response() -> dict[str, object]:
        try:
            overrides = load_config_overrides(config.config_file_path)
            effective_config = config.with_overrides(overrides)
        except ValueError as exc:
            raise HTTPException(
                status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(exc),
            ) from exc
        return _redacted_config(effective_config, overrides)

    def _update_config_response(updates: dict[str, Any]) -> dict[str, object]:
        if not isinstance(updates, dict):
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail="Request body must be a JSON object",
            )
        try:
            overrides = update_config_overrides(updates, config.config_file_path)
            effective_config = config.with_overrides(overrides)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        cache.append_runtime_event(
            "config_updated",
            {"fields": sorted(str(field) for field in updates.keys())},
        )
        return _redacted_config(effective_config, overrides)

    @app.get("/api/v1/health", dependencies=[auth_dependency])
    def health() -> dict[str, object]:
        status_payload = cache.get_daemon_status()
        last_cycle = status_payload.get("lastCycleFinishedAt")
        if last_cycle is None:
            last_cycle = cache.get_runtime_state("last_cycle_finished_at")
        return {
            "status": "ok",
            "startedAt": started_at,
            "lastCycleFinishedAt": int(last_cycle) if last_cycle else None,
        }

    @app.get("/api/v1/status", dependencies=[auth_dependency])
    def get_status() -> dict[str, object]:
        return cache.get_daemon_status()

    @app.get("/api/v1/config", dependencies=[auth_dependency])
    def get_config() -> dict[str, object]:
        return _effective_config_response()

    @app.patch("/api/v1/config", dependencies=[auth_dependency])
    def patch_config(updates: dict[str, Any] = Body(...)) -> dict[str, object]:
        return _update_config_response(updates)

    @app.post("/api/v1/config", dependencies=[auth_dependency])
    def post_config(updates: dict[str, Any] = Body(...)) -> dict[str, object]:
        return _update_config_response(updates)

    @app.delete("/api/v1/config", dependencies=[auth_dependency])
    def reset_config() -> dict[str, object]:
        reset_config_overrides(config.config_file_path)
        cache.append_runtime_event("config_reset", {})
        return _effective_config_response()

    @app.get("/api/v1/movies", dependencies=[auth_dependency])
    def list_movies(
        page: int = Query(default=1, ge=1),
        pageSize: int = Query(default=50, ge=1, le=200),
        search: str = "",
        condition: str = "",
        sort: str = "title",
    ) -> dict[str, object]:
        return _movie_page_response(
            cache,
            page=page,
            page_size=pageSize,
            search=search,
            condition=condition,
            sort=sort,
        )

    @app.get("/api/v1/search", dependencies=[auth_dependency])
    def search_movies(
        q: str = Query(min_length=1),
        page: int = Query(default=1, ge=1),
        pageSize: int = Query(default=50, ge=1, le=200),
        condition: str = "",
        sort: str = "title",
    ) -> dict[str, object]:
        return _movie_page_response(
            cache,
            page=page,
            page_size=pageSize,
            search=q,
            condition=condition,
            sort=sort,
        )

    @app.get("/api/v1/movies/{radarr_id}", dependencies=[auth_dependency])
    def get_movie(radarr_id: int) -> dict[str, object]:
        snapshot = cache.get_movie_snapshot(radarr_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Movie snapshot not found")
        return snapshot.payload

    @app.post("/api/v1/movies/{radarr_id}/favorite", dependencies=[auth_dependency])
    def add_favorite(radarr_id: int) -> dict[str, object]:
        movie = radarr.set_favorite(radarr_id, True)
        payload = movie_snapshot_payload(movie, ["favorite"], int(time.time()))
        cache.upsert_movie_snapshots([payload])
        return payload

    @app.delete("/api/v1/movies/{radarr_id}/favorite", dependencies=[auth_dependency])
    def remove_favorite(radarr_id: int) -> dict[str, object]:
        movie = radarr.set_favorite(radarr_id, False)
        payload = movie_snapshot_payload(movie, [], int(time.time()))
        cache.upsert_movie_snapshots([payload])
        return payload

    return app
