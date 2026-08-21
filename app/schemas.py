"""Domain and API Pydantic schemas for stream-sync.

Provides strongly-typed contracts and DTOs for external API requests,
responses, movie state snapshots, and decision structures.
"""

from __future__ import annotations

from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class LookupStatus(str, Enum):
    """Status enum for JustWatch lookup evaluations."""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"
    SCHEMA_ERROR = "SCHEMA_ERROR"


class TagState(BaseModel):
    """Tag metadata state from Radarr."""

    model_config = ConfigDict(frozen=True)

    id: int = Field(description="Radarr tag unique identifier")
    label: str = Field(description="Radarr tag text label")


class MovieState(BaseModel):
    """Radarr movie internal state for processing."""

    movie_id: int = Field(description="Radarr movie ID")
    tmdb_id: int | None = Field(default=None, description="TMDB media ID")
    title: str = Field(description="Movie title")
    year: int | None = Field(default=None, description="Release year")
    path: str | None = Field(default=None, description="Disk directory path")
    monitored: bool = Field(description="Monitoring status in Radarr")
    has_file: bool = Field(description="File presence status on disk")
    status: str | None = Field(default=None, description="Radarr release status")
    in_cinemas: str | None = Field(default=None, description="Theatrical release date string")
    tags: list[TagState] = Field(default_factory=list, description="Associated Radarr tags")

    def copy(self, **overrides: Any) -> MovieState:
        """Return a new MovieState with updated attributes."""
        return self.model_copy(update=overrides)

    @property
    def tag_labels(self) -> list[str]:

        """Return list of lowercase/normalized tag label strings."""
        return [tag.label for tag in self.tags]

    @property
    def streaming_tags(self) -> list[TagState]:
        """Return list of tags starting with streaming prefix."""
        return [
            tag
            for tag in self.tags
            if tag.label.lower().startswith("streaming_")
            or tag.label.lower().startswith("streaming-")
        ]

    def has_tag(self, label: str) -> bool:
        """Check if movie has a specific tag label (case-insensitive)."""
        target = label.lower()
        return any(tag.label.lower() == target for tag in self.tags)


class JwService(BaseModel):
    """Streaming service metadata."""

    model_config = ConfigDict(frozen=True)

    service_id: str = Field(description="JustWatch short service code (e.g. nfx)")
    service_name: str = Field(description="Human-readable service name (e.g. Netflix)")


class JwLookupResult(BaseModel):
    """Result of a JustWatch availability lookup."""

    status: LookupStatus = Field(description="Lookup status code")
    services: list[JwService] = Field(default_factory=list, description="Available streaming providers")
    error_message: str | None = Field(default=None, description="Error details if lookup failed")


class MovieDecision(BaseModel):
    """Decision structure for processing a movie in a sync cycle."""

    skip: bool = Field(default=False, description="Whether to skip Radarr updates for this movie")
    reason: str = Field(default="", description="Human-readable decision explanation")
    should_update: bool = Field(default=False, description="Whether Radarr metadata needs update")
    desired_streaming_labels: list[str] = Field(default_factory=list, description="Streaming tag labels to apply")
    target_monitored: bool | None = Field(default=None, description="Target monitored status if changing")
    trigger_search: bool = Field(default=False, description="Whether to trigger Radarr search")
    search_reason: str | None = Field(default=None, description="Reason for triggering search")
    entering_services: list[JwService] = Field(default_factory=list, description="Newly joined streaming services")
    leaving_service_ids: list[str] = Field(default_factory=list, description="Left streaming service IDs")


class CycleStats(BaseModel):
    """Counters and statistics for a single synchronization cycle."""

    processed: int = Field(default=0, description="Total movies processed")
    favorite_skipped: int = Field(default=0, description="Movies protected by favorite tag")
    seerr_protected: int = Field(default=0, description="Movies protected by Seerr/Watchlist")
    recent_protected: int = Field(default=0, description="Movies protected by theatrical grace period")
    changed: int = Field(default=0, description="Movies mutated in Radarr")
    search_triggered: int = Field(default=0, description="Radarr searches executed")
    unknown: int = Field(default=0, description="JustWatch unknown/error lookups")
    schema_errors: int = Field(default=0, description="JustWatch schema error lookups")
    errors: int = Field(default=0, description="General cycle processing errors")


class CachedOffersEntry(BaseModel):
    """Cached JustWatch offers entry in SQLite."""

    model_config = ConfigDict(frozen=True)

    is_error: bool = Field(description="Whether the entry represents a cached error")
    payload: dict[str, Any] | None = Field(default=None, description="Raw JustWatch offers API payload")
    error_message: str | None = Field(default=None, description="Cached error message if present")


class DeletionStateRow(BaseModel):
    """Scheduled movie deletion record."""

    model_config = ConfigDict(frozen=True)

    radarr_id: int = Field(description="Radarr movie ID")
    movie_path: str = Field(description="Library directory path")
    scheduled_at: int = Field(description="Timestamp when deletion was scheduled")
    delete_after_ts: int = Field(description="Timestamp deadline for file deletion")
    last_status: str = Field(description="Last recorded status (scheduled/reminder)")
    updated_at: int = Field(description="Timestamp of last update")


class SeerrProtection(BaseModel):
    """Protection source record from Seerr or Plex Watchlist."""

    model_config = ConfigDict(frozen=True)

    source: str = Field(description="Protection source (e.g. seerr_request, plex_watchlist)")
    user: str | None = Field(default=None, description="Requestor or watchlist owner name")


class MovieSnapshot(BaseModel):
    """Snapshot record of a movie stored in SQLite cache."""

    model_config = ConfigDict(frozen=True)

    radarr_id: int = Field(description="Radarr movie ID")
    tmdb_id: int | None = Field(default=None, description="TMDB media ID")
    title: str = Field(description="Movie title")
    year: int | None = Field(default=None, description="Release year")
    payload: dict[str, Any] = Field(description="Full API presentation payload")
    conditions: list[str] = Field(description="Active movie status conditions")
    last_evaluated_at: int = Field(description="Timestamp of evaluation")


# --- API Response Models ---

class HealthResponse(BaseModel):
    """Response DTO for GET /health endpoint."""

    status: str = Field(default="ok", description="Service health status")
    startedAt: int = Field(description="Service start timestamp in seconds")
    lastCycleFinishedAt: int | None = Field(default=None, description="Timestamp of last completed sync cycle")


class MoviePageResponse(BaseModel):
    """Response DTO for paginated movie queries."""

    page: int = Field(description="Current page number")
    pageSize: int = Field(description="Items per page")
    total: int = Field(description="Total count matching query")
    pages: int = Field(description="Total page count")
    results: list[dict[str, Any]] = Field(description="List of movie snapshot payloads")


class RedactedConfigResponse(BaseModel):
    """Response DTO for GET /api/v1/config endpoint."""

    mode: str
    jwCountry: str
    jwLanguage: str
    removeMode: str
    deleteAfterDays: int
    runIntervalHours: int
    searchCooldownHours: int
    theatricalReleaseGraceMonths: int
    jwAllowedServices: list[str]
    jwOnlySubscription: bool
    seerrEnabled: bool
    seerrUrl: str
    apiEnabled: bool
    apiHost: str
    apiPort: int
    dryRun: bool
    notifyMode: str
    cacheDbPath: str
    logDir: str
    configFilePath: str
    overrides: dict[str, Any]
    mutableFields: list[str]
    readOnlyFields: list[str]
    applies: str
    secrets: dict[str, bool]
