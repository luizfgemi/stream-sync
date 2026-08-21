"""Backward compatibility adapter re-exporting theatrical release helpers."""

from app.policy import (
    is_recent_theatrical_release,
    is_within_theatrical_release_grace,
    parse_in_cinemas_date,
    parse_radarr_date,
    subtract_calendar_months,
)

__all__ = [
    "is_recent_theatrical_release",
    "is_within_theatrical_release_grace",
    "parse_in_cinemas_date",
    "parse_radarr_date",
    "subtract_calendar_months",
]
