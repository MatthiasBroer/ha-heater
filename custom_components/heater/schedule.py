"""Weekly schedule calculations for Heat Learner."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any


def _parse_time(value: str) -> time:
    """Parse a local wall-clock time."""
    return time.fromisoformat(value)


def active_period(
    now: datetime, entries: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Return the comfort period active at local datetime ``now``."""
    weekday = now.weekday()
    current = now.time().replace(tzinfo=None)

    for entry in entries:
        if weekday not in entry["days"]:
            continue

        start = _parse_time(entry["start"])
        end = _parse_time(entry["end"])

        if start <= current < end:
            return entry

    return None


def next_period(
    now: datetime, entries: list[dict[str, Any]]
) -> tuple[datetime, dict[str, Any]] | None:
    """Return the next future period start and its entry."""
    candidates: list[tuple[datetime, dict[str, Any]]] = []

    for offset in range(8):
        day: date = (now + timedelta(days=offset)).date()

        for entry in entries:
            if day.weekday() not in entry["days"]:
                continue

            start = _parse_time(entry["start"])
            candidate = datetime.combine(day, start, tzinfo=now.tzinfo)

            if candidate > now:
                candidates.append((candidate, entry))

    return min(candidates, key=lambda item: item[0]) if candidates else None
