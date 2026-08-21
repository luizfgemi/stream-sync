from __future__ import annotations

import calendar
from datetime import date, datetime
from typing import Any


def parse_radarr_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    if not text:
        return None

    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        normalized = text.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        return None


def subtract_calendar_months(source: date, months: int) -> date:
    if months < 0:
        raise ValueError("months must be >= 0")

    month_index = source.year * 12 + source.month - 1 - months
    year, month_zero = divmod(month_index, 12)
    if year < 1:
        return date.min

    month = month_zero + 1
    day = min(source.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def is_within_theatrical_release_grace(
    in_cinemas: Any,
    grace_months: int,
    today: date | None = None,
) -> bool:
    if grace_months <= 0:
        return False

    release_date = parse_radarr_date(in_cinemas)
    if release_date is None:
        return False

    current_date = today or date.today()
    cutoff = subtract_calendar_months(current_date, grace_months)
    return cutoff <= release_date <= current_date
