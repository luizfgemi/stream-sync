"""Backward compatibility adapter re-exporting Pydantic DTOs from app.schemas.

Ensures existing imports continue working seamlessly during refactoring phases.
"""

from app.schemas import (
    CachedOffersEntry,
    CycleStats,
    DeletionStateRow,
    JwLookupResult,
    JwService,
    LookupStatus,
    MovieDecision,
    MovieSnapshot,
    MovieState,
    SeerrProtection,
    TagState,
)

__all__ = [
    "LookupStatus",
    "TagState",
    "MovieState",
    "JwService",
    "JwLookupResult",
    "MovieDecision",
    "CycleStats",
    "CachedOffersEntry",
    "DeletionStateRow",
    "SeerrProtection",
    "MovieSnapshot",
]
