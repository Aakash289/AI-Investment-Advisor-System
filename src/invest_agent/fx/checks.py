"""
fx/checks.py: the two quality rules for exchange rates (design 14.8).

What this file does:
    stale rate         reference rates are published on business days only, so on a
                       Monday the latest one may be Friday's, which is normal. A rate more
                       than three business days old is flagged as stale.
    disagreement       Frankfurter's rate and FRED's rate for the same date should match
                       closely, since both are official. A gap of more than 1% is flagged,
                       because it means one of them is wrong.

    Business days here are Monday to Friday. Indian and US holidays are not counted
    separately; a holiday only makes a rate look one day older, well inside the limit.
"""

from __future__ import annotations

from datetime import date, timedelta

STALE_AFTER_BUSINESS_DAYS = 3
DISAGREEMENT_TOLERANCE = 0.01          # 1%


def to_date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def business_days_between(start: str | date, end: str | date) -> int:
    """Weekdays after start, up to and including end (0 if end is not after start)."""
    start, end = to_date(start), to_date(end)
    days, current = 0, start
    while current < end:
        current += timedelta(days=1)
        if current.weekday() < 5:          # weekday(): Monday 0 ... Friday 4, Saturday 5, Sunday 6
            days += 1
    return days


def is_stale(rate_date: str | date, today: str | date, limit: int = STALE_AFTER_BUSINESS_DAYS) -> bool:
    return business_days_between(rate_date, today) > limit


def relative_gap(a: float, b: float) -> float:
    """How far apart two rates are, as a share of the second (0.01 is 1%)."""
    return abs(a - b) / abs(b)


def disagrees(a: float, b: float, tolerance: float = DISAGREEMENT_TOLERANCE) -> bool:
    return relative_gap(a, b) > tolerance
