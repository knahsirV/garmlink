"""Build date-range results from Garmin endpoints that only accept one day.

Several Garmin metrics (max metrics / VO2 max, HRV) have no range endpoint in
garminconnect — only a per-day call. These helpers fan out one call per day,
bounded on both range length and concurrency so a single wide request cannot
exhaust the client's thread pool or trip Garmin's rate limiter.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

__all__ = ["MAX_RANGE_DAYS", "MAX_RANGE_CONCURRENCY", "build_date_list", "fetch_per_day"]

# A wide range is almost always a mistake by the caller, and each day is a full
# Garmin payload held in cache. Cap it rather than melting a 256 MB machine.
MAX_RANGE_DAYS = 90
MAX_RANGE_CONCURRENCY = 4


def build_date_list(start_date: str, end_date: str) -> list[str]:
    """Return every ISO date from start to end inclusive.

    Raises ValueError on malformed dates, an inverted range, or a range longer
    than MAX_RANGE_DAYS.
    """
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise ValueError(
            f"Dates must be YYYY-MM-DD (got {start_date!r}, {end_date!r})"
        ) from exc

    if end < start:
        raise ValueError(f"end_date {end_date} is before start_date {start_date}")

    days = (end - start).days + 1
    if days > MAX_RANGE_DAYS:
        raise ValueError(
            f"Range of {days} days exceeds the {MAX_RANGE_DAYS}-day maximum; "
            "request a shorter window."
        )
    return [(start + timedelta(days=i)).isoformat() for i in range(days)]


async def fetch_per_day(
    client: Any,
    method_name: str,
    start_date: str,
    end_date: str,
    *,
    ttl: float,
) -> dict:
    """Call a single-date Garmin method once per day in the range.

    Days that fail individually are dropped rather than failing the whole
    request, and the count of both is reported back so the caller can tell a
    sparse range from a broken one.
    """
    try:
        dates = build_date_list(start_date, end_date)
    except ValueError as exc:
        return {"error": str(exc)}

    semaphore = asyncio.Semaphore(MAX_RANGE_CONCURRENCY)

    async def one(day: str) -> Any:
        async with semaphore:
            return await client.call(method_name, day, ttl=ttl)

    results = await asyncio.gather(
        *[one(d) for d in dates], return_exceptions=True
    )

    days = [
        {"date": d, "data": r}
        for d, r in zip(dates, results)
        if not isinstance(r, BaseException) and r is not None
    ]

    return {
        "start_date": start_date,
        "end_date": end_date,
        "days": days,
        "total_days": len(dates),
        "successful_days": len(days),
    }
