from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class LookupStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"
    SCHEMA_ERROR = "SCHEMA_ERROR"


@dataclass(frozen=True, slots=True)
class TagState:
    id: int
    label: str


@dataclass(slots=True)
class MovieState:
    movie_id: int
    tmdb_id: int | None
    title: str
    year: int | None
    path: str | None
    monitored: bool
    has_file: bool
    status: str | None
    in_cinemas: str | None
    tags: list[TagState]

    @property
    def tag_labels(self) -> list[str]:
        return [tag.label for tag in self.tags]

    @property
    def streaming_tags(self) -> list[TagState]:
        return [
            tag
            for tag in self.tags
            if tag.label.lower().startswith("streaming_")
            or tag.label.lower().startswith("streaming-")
        ]

    def has_tag(self, label: str) -> bool:
        target = label.lower()
        return any(tag.label.lower() == target for tag in self.tags)


@dataclass(frozen=True, slots=True)
class JwService:
    service_id: str
    service_name: str


@dataclass(slots=True)
class JwLookupResult:
    status: LookupStatus
    services: list[JwService] = field(default_factory=list)
    error_message: str | None = None


@dataclass(slots=True)
class MovieDecision:
    skip: bool = False
    reason: str = ""
    should_update: bool = False
    desired_streaming_labels: list[str] = field(default_factory=list)
    target_monitored: bool | None = None
    trigger_search: bool = False
    search_reason: str | None = None
    entering_services: list[JwService] = field(default_factory=list)
    leaving_service_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CycleStats:
    processed: int = 0
    favorite_skipped: int = 0
    seerr_protected: int = 0
    recent_protected: int = 0
    changed: int = 0
    search_triggered: int = 0
    unknown: int = 0
    schema_errors: int = 0
    errors: int = 0


@dataclass(frozen=True, slots=True)
class CachedOffersEntry:
    is_error: bool
    payload: dict[str, Any] | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class DeletionStateRow:
    radarr_id: int
    movie_path: str
    scheduled_at: int
    delete_after_ts: int
    last_status: str
    updated_at: int


@dataclass(frozen=True, slots=True)
class SeerrProtection:
    source: str
    user: str | None = None


@dataclass(frozen=True, slots=True)
class MovieSnapshot:
    radarr_id: int
    tmdb_id: int | None
    title: str
    year: int | None
    payload: dict[str, Any]
    conditions: list[str]
    last_evaluated_at: int
