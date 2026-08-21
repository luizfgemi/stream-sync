from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


def _env_required(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ValueError(f"Missing required environment variable: {name}")
    return value.strip()


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value is not None and value.strip() else default


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value.strip())


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return float(value.strip())


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


API_CONFIG_FIELDS: dict[str, tuple[str, type]] = {
    "jwCountry": ("jw_country", str),
    "jwLanguage": ("jw_language", str),
    "removeMode": ("remove_mode", str),
    "deleteAfterDays": ("delete_after_days", int),
    "runIntervalHours": ("run_interval_hours", int),
    "jwRequestDelaySeconds": ("jw_request_delay_seconds", float),
    "jwRequestDelayJitterSeconds": ("jw_request_delay_jitter_seconds", float),
    "searchCooldownHours": ("search_cooldown_hours", int),
    "theatricalReleaseGraceMonths": ("theatrical_release_grace_months", int),
    "offersTtlOkDays": ("offers_ttl_ok_days", int),
    "offersTtlErrHours": ("offers_ttl_err_hours", int),
    "idmapTtlDays": ("idmap_ttl_days", int),
    "ttlJitterPercent": ("ttl_jitter_percent", int),
    "notifyMode": ("notify_mode", str),
    "jwOnlySubscription": ("jw_only_subscription", bool),
    "jwAllowedServices": ("jw_allowed_services", str),
    "seerrEnabled": ("seerr_enabled", bool),
    "runOnce": ("run_once", bool),
    "dryRun": ("dry_run", bool),
}

READ_ONLY_CONFIG_FIELDS = {
    "mode",
    "radarrUrl",
    "radarrApiKey",
    "seerrUrl",
    "seerrApiKey",
    "apiEnabled",
    "apiHost",
    "apiPort",
    "streamSyncApiKey",
    "cacheDbPath",
    "logDir",
    "telegramBotToken",
    "telegramChatId",
    "configFilePath",
    "secrets",
}


def config_file_path_from_env() -> str:
    return _env_str("CONFIG_FILE_PATH", "/app/data/config.json")


def _coerce_override_value(field: str, value: Any) -> Any:
    if field not in API_CONFIG_FIELDS:
        if field in READ_ONLY_CONFIG_FIELDS:
            raise ValueError(f"Config field is read-only: {field}")
        raise ValueError(f"Unknown config field: {field}")

    _attr_name, value_type = API_CONFIG_FIELDS[field]
    if value_type is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        raise ValueError(f"{field} must be a boolean")
    if value_type is int:
        if isinstance(value, bool):
            raise ValueError(f"{field} must be an integer")
        return int(value)
    if value_type is float:
        if isinstance(value, bool):
            raise ValueError(f"{field} must be a number")
        return float(value)
    if value_type is str:
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a string")
        return value.strip()
    return value


def load_config_overrides(config_file_path: str | None = None) -> dict[str, Any]:
    path = Path(config_file_path or config_file_path_from_env())
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid config override file: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Config override file must contain an object: {path}")
    overrides: dict[str, Any] = {}
    for field, value in payload.items():
        overrides[str(field)] = _coerce_override_value(str(field), value)
    return overrides


def save_config_overrides(
    overrides: dict[str, Any],
    config_file_path: str | None = None,
) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for field, value in overrides.items():
        normalized[str(field)] = _coerce_override_value(str(field), value)

    path = Path(config_file_path or config_file_path_from_env())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return normalized


def update_config_overrides(
    updates: dict[str, Any],
    config_file_path: str | None = None,
) -> dict[str, Any]:
    current = load_config_overrides(config_file_path)
    for field, value in updates.items():
        current[str(field)] = _coerce_override_value(str(field), value)
    return save_config_overrides(current, config_file_path)


def reset_config_overrides(config_file_path: str | None = None) -> None:
    path = Path(config_file_path or config_file_path_from_env())
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _apply_api_overrides(values: dict[str, Any], overrides: dict[str, Any]) -> None:
    for field, value in overrides.items():
        attr_name = API_CONFIG_FIELDS[field][0]
        values[attr_name] = value


@dataclass(frozen=True, slots=True)
class Config:
    mode: str
    radarr_url: str
    radarr_api_key: str
    jw_country: str
    jw_language: str
    remove_mode: str
    delete_after_days: int
    run_interval_hours: int
    jw_request_delay_seconds: float
    jw_request_delay_jitter_seconds: float
    search_cooldown_hours: int
    theatrical_release_grace_months: int
    radarr_init_max_retries: int
    radarr_init_retry_seconds: int
    cache_db_path: str
    offers_ttl_ok_days: int
    offers_ttl_err_hours: int
    idmap_ttl_days: int
    ttl_jitter_percent: int
    notify_mode: str
    telegram_bot_token: str
    telegram_chat_id: str
    log_dir: str
    jw_only_subscription: bool
    jw_allowed_services: str
    seerr_enabled: bool
    seerr_url: str
    seerr_api_key: str
    api_enabled: bool
    api_host: str
    api_port: int
    stream_sync_api_key: str
    run_once: bool
    dry_run: bool
    tz: str | None
    config_file_path: str
    plex_watchlist_sync_enabled: bool = False
    plex_watchlist_sync_interval_minutes: int = 15
    plex_token: str = ""
    plex_token_file: str = ""
    plex_watchlist_include_friends: bool = True
    plex_watchlist_search_on_add: bool = True
    plex_watchlist_radarr_profile_id: int = 1
    plex_watchlist_radarr_root_folder: str = "/movies"
    sonarr_url: str = ""
    sonarr_api_key: str = ""
    plex_watchlist_sonarr_profile_id: int = 1
    plex_watchlist_sonarr_root_folder: str = "/series"

    @property
    def offers_ttl_ok_seconds(self) -> int:
        return self.offers_ttl_ok_days * 24 * 60 * 60

    @property
    def offers_ttl_err_seconds(self) -> int:
        return self.offers_ttl_err_hours * 60 * 60

    @property
    def idmap_ttl_seconds(self) -> int:
        return self.idmap_ttl_days * 24 * 60 * 60

    @property
    def search_cooldown_seconds(self) -> int:
        return self.search_cooldown_hours * 60 * 60

    @property
    def delete_after_seconds(self) -> int:
        return self.delete_after_days * 24 * 60 * 60

    @property
    def run_interval_seconds(self) -> int:
        return self.run_interval_hours * 60 * 60

    def validate(self) -> None:
        if self.mode not in {"daemon", "list_services"}:
            raise ValueError("Invalid MODE. Use: daemon | list_services")
        if len(self.jw_country) != 2:
            raise ValueError("JW_COUNTRY must have 2 letters (ISO 3166-1 alpha-2)")
        if self.remove_mode not in {"report", "delete"}:
            raise ValueError("Invalid REMOVE_MODE. Use: report | delete")
        if self.run_interval_hours <= 0:
            raise ValueError("RUN_INTERVAL_HOURS must be > 0")
        if self.jw_request_delay_seconds < 0:
            raise ValueError("JW_REQUEST_DELAY_SECONDS must be >= 0")
        if self.jw_request_delay_jitter_seconds < 0:
            raise ValueError("JW_REQUEST_DELAY_JITTER_SECONDS must be >= 0")
        if self.delete_after_days <= 0:
            raise ValueError("DELETE_AFTER_DAYS must be > 0")
        if self.search_cooldown_hours <= 0:
            raise ValueError("SEARCH_COOLDOWN_HOURS must be > 0")
        if self.theatrical_release_grace_months < 0:
            raise ValueError("THEATRICAL_RELEASE_GRACE_MONTHS must be >= 0")
        if self.radarr_init_max_retries <= 0:
            raise ValueError("RADARR_INIT_MAX_RETRIES must be > 0")
        if self.radarr_init_retry_seconds <= 0:
            raise ValueError("RADARR_INIT_RETRY_SECONDS must be > 0")
        if self.offers_ttl_ok_days <= 0:
            raise ValueError("OFFERS_TTL_OK_DAYS must be > 0")
        if self.offers_ttl_err_hours <= 0:
            raise ValueError("OFFERS_TTL_ERR_HOURS must be > 0")
        if self.idmap_ttl_days <= 0:
            raise ValueError("IDMAP_TTL_DAYS must be > 0")
        if not (0 <= self.ttl_jitter_percent <= 100):
            raise ValueError("TTL_JITTER_PERCENT must be between 0 and 100")
        if self.api_port <= 0 or self.api_port > 65535:
            raise ValueError("API_PORT must be between 1 and 65535")
        if self.plex_watchlist_sync_enabled:
            if not self.plex_token and not self.plex_token_file:
                raise ValueError(
                    "PLEX_TOKEN or PLEX_TOKEN_FILE is required when "
                    "PLEX_WATCHLIST_SYNC_ENABLED=true"
                )
            if self.plex_watchlist_radarr_profile_id <= 0:
                raise ValueError("PLEX_WATCHLIST_RADARR_PROFILE_ID must be > 0")
            if self.plex_watchlist_sonarr_profile_id <= 0:
                raise ValueError("PLEX_WATCHLIST_SONARR_PROFILE_ID must be > 0")
            if self.plex_watchlist_sync_interval_minutes <= 0:
                raise ValueError(
                    "PLEX_WATCHLIST_SYNC_INTERVAL_MINUTES must be > 0"
                )

    def with_overrides(self, overrides: dict[str, Any]) -> "Config":
        values: dict[str, Any] = {}
        _apply_api_overrides(values, overrides)
        if "jw_country" in values:
            values["jw_country"] = str(values["jw_country"]).upper()
        if "remove_mode" in values:
            values["remove_mode"] = str(values["remove_mode"]).lower()
        if "notify_mode" in values:
            values["notify_mode"] = str(values["notify_mode"]).lower()
        candidate = replace(self, **values)
        candidate.validate()
        return candidate

    @classmethod
    def from_env(
        cls,
        overrides: dict[str, Any] | None = None,
        config_file_path: str | None = None,
    ) -> "Config":
        mode = _env_str("MODE", "daemon").lower()
        if mode not in {"daemon", "list_services"}:
            raise ValueError("Invalid MODE. Use: daemon | list_services")

        jw_country = _env_str("JW_COUNTRY", "BR").upper()
        if len(jw_country) != 2:
            raise ValueError("JW_COUNTRY must have 2 letters (ISO 3166-1 alpha-2)")

        remove_mode = _env_str("REMOVE_MODE", "report").lower()
        if remove_mode not in {"report", "delete"}:
            raise ValueError("Invalid REMOVE_MODE. Use: report | delete")

        delete_after_days = _env_int("DELETE_AFTER_DAYS", 30)
        run_interval_hours = _env_int("RUN_INTERVAL_HOURS", 24)
        jw_request_delay_seconds = _env_float("JW_REQUEST_DELAY_SECONDS", 2.0)
        jw_request_delay_jitter_seconds = _env_float(
            "JW_REQUEST_DELAY_JITTER_SECONDS", 1.0
        )
        search_cooldown_hours = _env_int("SEARCH_COOLDOWN_HOURS", 24)
        theatrical_release_grace_months = _env_int(
            "THEATRICAL_RELEASE_GRACE_MONTHS", 0
        )
        radarr_init_max_retries = _env_int("RADARR_INIT_MAX_RETRIES", 30)
        radarr_init_retry_seconds = _env_int("RADARR_INIT_RETRY_SECONDS", 5)
        offers_ttl_ok_days = _env_int("OFFERS_TTL_OK_DAYS", 5)
        offers_ttl_err_hours = _env_int("OFFERS_TTL_ERR_HOURS", 12)
        idmap_ttl_days = _env_int("IDMAP_TTL_DAYS", 1)
        ttl_jitter_percent = _env_int("TTL_JITTER_PERCENT", 20)
        api_port = _env_int("API_PORT", 8099)
        effective_config_file_path = config_file_path or config_file_path_from_env()

        values: dict[str, Any] = {
            "mode": mode,
            "radarr_url": _env_required("RADARR_URL")
            if mode == "daemon"
            else _env_str("RADARR_URL", ""),
            "radarr_api_key": _env_required("RADARR_API_KEY")
            if mode == "daemon"
            else _env_str("RADARR_API_KEY", ""),
            "jw_country": jw_country,
            "jw_language": _env_str("JW_LANGUAGE", "en-US"),
            "remove_mode": remove_mode,
            "delete_after_days": delete_after_days,
            "run_interval_hours": run_interval_hours,
            "jw_request_delay_seconds": jw_request_delay_seconds,
            "jw_request_delay_jitter_seconds": jw_request_delay_jitter_seconds,
            "search_cooldown_hours": search_cooldown_hours,
            "theatrical_release_grace_months": theatrical_release_grace_months,
            "radarr_init_max_retries": radarr_init_max_retries,
            "radarr_init_retry_seconds": radarr_init_retry_seconds,
            "cache_db_path": _env_str("CACHE_DB_PATH", "/app/data/cache.sqlite"),
            "offers_ttl_ok_days": offers_ttl_ok_days,
            "offers_ttl_err_hours": offers_ttl_err_hours,
            "idmap_ttl_days": idmap_ttl_days,
            "ttl_jitter_percent": ttl_jitter_percent,
            "notify_mode": _env_str("NOTIFY_MODE", "stdout").lower(),
            "telegram_bot_token": _env_str("TELEGRAM_BOT_TOKEN", ""),
            "telegram_chat_id": _env_str("TELEGRAM_CHAT_ID", ""),
            "log_dir": _env_str("LOG_DIR", "/app/data/logs"),
            "jw_only_subscription": _env_bool("JW_ONLY_SUBSCRIPTION", True),
            "jw_allowed_services": _env_str("JW_ALLOWED_SERVICES", ""),
            "seerr_enabled": _env_bool("SEERR_ENABLED", False),
            "seerr_url": _env_str("SEERR_URL", "http://seerr:5055"),
            "seerr_api_key": _env_str("SEERR_API_KEY", ""),
            "api_enabled": _env_bool("API_ENABLED", False),
            "api_host": _env_str("API_HOST", "0.0.0.0"),
            "api_port": api_port,
            "stream_sync_api_key": _env_str("STREAM_SYNC_API_KEY", ""),
            "run_once": _env_bool("RUN_ONCE", False),
            "dry_run": _env_bool("DRY_RUN", False),
            "tz": os.getenv("TZ"),
            "config_file_path": effective_config_file_path,
            "plex_watchlist_sync_enabled": _env_bool(
                "PLEX_WATCHLIST_SYNC_ENABLED", False
            ),
            "plex_watchlist_sync_interval_minutes": _env_int(
                "PLEX_WATCHLIST_SYNC_INTERVAL_MINUTES", 15
            ),
            "plex_token": _env_str("PLEX_TOKEN", ""),
            "plex_token_file": _env_str("PLEX_TOKEN_FILE", ""),
            "plex_watchlist_include_friends": _env_bool(
                "PLEX_WATCHLIST_INCLUDE_FRIENDS", True
            ),
            "plex_watchlist_search_on_add": _env_bool(
                "PLEX_WATCHLIST_SEARCH_ON_ADD", True
            ),
            "plex_watchlist_radarr_profile_id": _env_int(
                "PLEX_WATCHLIST_RADARR_PROFILE_ID", 1
            ),
            "plex_watchlist_radarr_root_folder": _env_str(
                "PLEX_WATCHLIST_RADARR_ROOT_FOLDER", "/movies"
            ),
            "sonarr_url": _env_str("SONARR_URL", ""),
            "sonarr_api_key": _env_str("SONARR_API_KEY", ""),
            "plex_watchlist_sonarr_profile_id": _env_int(
                "PLEX_WATCHLIST_SONARR_PROFILE_ID", 1
            ),
            "plex_watchlist_sonarr_root_folder": _env_str(
                "PLEX_WATCHLIST_SONARR_ROOT_FOLDER", "/series"
            ),
        }
        _apply_api_overrides(values, overrides or {})

        mode = str(values["mode"])
        jw_country = str(values["jw_country"]).upper()
        values["jw_country"] = jw_country
        remove_mode = str(values["remove_mode"]).lower()
        values["remove_mode"] = remove_mode
        values["notify_mode"] = str(values["notify_mode"]).lower()
        delete_after_days = int(values["delete_after_days"])
        run_interval_hours = int(values["run_interval_hours"])
        jw_request_delay_seconds = float(values["jw_request_delay_seconds"])
        jw_request_delay_jitter_seconds = float(
            values["jw_request_delay_jitter_seconds"]
        )
        search_cooldown_hours = int(values["search_cooldown_hours"])
        theatrical_release_grace_months = int(
            values["theatrical_release_grace_months"]
        )
        radarr_init_max_retries = int(values["radarr_init_max_retries"])
        radarr_init_retry_seconds = int(values["radarr_init_retry_seconds"])
        offers_ttl_ok_days = int(values["offers_ttl_ok_days"])
        offers_ttl_err_hours = int(values["offers_ttl_err_hours"])
        idmap_ttl_days = int(values["idmap_ttl_days"])
        ttl_jitter_percent = int(values["ttl_jitter_percent"])
        api_port = int(values["api_port"])

        if run_interval_hours <= 0:
            raise ValueError("RUN_INTERVAL_HOURS must be > 0")
        if jw_request_delay_seconds < 0:
            raise ValueError("JW_REQUEST_DELAY_SECONDS must be >= 0")
        if jw_request_delay_jitter_seconds < 0:
            raise ValueError("JW_REQUEST_DELAY_JITTER_SECONDS must be >= 0")
        if delete_after_days <= 0:
            raise ValueError("DELETE_AFTER_DAYS must be > 0")
        if search_cooldown_hours <= 0:
            raise ValueError("SEARCH_COOLDOWN_HOURS must be > 0")
        if theatrical_release_grace_months < 0:
            raise ValueError("THEATRICAL_RELEASE_GRACE_MONTHS must be >= 0")
        if radarr_init_max_retries <= 0:
            raise ValueError("RADARR_INIT_MAX_RETRIES must be > 0")
        if radarr_init_retry_seconds <= 0:
            raise ValueError("RADARR_INIT_RETRY_SECONDS must be > 0")
        if offers_ttl_ok_days <= 0:
            raise ValueError("OFFERS_TTL_OK_DAYS must be > 0")
        if offers_ttl_err_hours <= 0:
            raise ValueError("OFFERS_TTL_ERR_HOURS must be > 0")
        if idmap_ttl_days <= 0:
            raise ValueError("IDMAP_TTL_DAYS must be > 0")
        if not (0 <= ttl_jitter_percent <= 100):
            raise ValueError("TTL_JITTER_PERCENT must be between 0 and 100")
        if api_port <= 0 or api_port > 65535:
            raise ValueError("API_PORT must be between 1 and 65535")

        config = cls(**values)
        config.validate()
        return config

    @classmethod
    def from_env_file(cls, config_file_path: str | None = None) -> "Config":
        effective_path = config_file_path or config_file_path_from_env()
        return cls.from_env(
            overrides=load_config_overrides(effective_path),
            config_file_path=effective_path,
        )
