from __future__ import annotations

from typing import Any

from ..database import SQLiteCache
from ..config import (
    API_CONFIG_FIELDS,
    READ_ONLY_CONFIG_FIELDS,
    Config,
    load_config_overrides,
    reset_config_overrides,
    update_config_overrides,
)


class ConfigService:
    def __init__(self, base_config: Config, cache: SQLiteCache) -> None:
        self._base_config = base_config
        self._cache = cache

    def get_effective_config(self) -> dict[str, object]:
        overrides = load_config_overrides(self._base_config.config_file_path)
        effective_config = self._base_config.with_overrides(overrides)
        return self._redacted_config(effective_config, overrides)

    def update_overrides(self, updates: dict[str, Any]) -> dict[str, object]:
        if not isinstance(updates, dict):
            raise TypeError("Request body must be a JSON object")

        overrides = update_config_overrides(
            updates,
            self._base_config.config_file_path,
        )
        effective_config = self._base_config.with_overrides(overrides)
        self._cache.append_runtime_event(
            "config_updated",
            {"fields": sorted(str(field) for field in updates.keys())},
        )
        return self._redacted_config(effective_config, overrides)

    def reset_overrides(self) -> dict[str, object]:
        reset_config_overrides(self._base_config.config_file_path)
        self._cache.append_runtime_event("config_reset", {})
        return self.get_effective_config()

    @staticmethod
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
