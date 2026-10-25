"""Pure calendar-month daily-price expansion for ISSUE-01.

This module deliberately does not define a public finance DTO or schema version.
The public contract and its snapshot identity require review before wiring the
submit/worker/UI paths. Input is an already selected set of complete day curves.
No timezone, weekday, interpolation, resampling or annual array cycling.
"""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Mapping, Sequence
from typing import TypeVar


_Points = TypeVar("_Points")
DAY_POINTS = {"15min": 96, "30min": 48, "1h": 24}
MONTHS = frozenset(range(1, 13))


def check_monthly_daily_curves(
    curves: Mapping[int, Sequence[_Points]], *, resolution: str
) -> None:
    """Validate only month coverage and daily length at an input boundary.

    Source object availability, metadata resolution/price unit and signed Decimal
    validation belong to the existing price CSV and task submission boundaries.
    Reusing one source for several months is legal; keys refer to months, not IDs.
    """
    if resolution not in DAY_POINTS:
        raise ValueError(f"unsupported project resolution: {resolution!r}")
    missing = MONTHS - curves.keys()
    extra = curves.keys() - MONTHS
    if missing or extra:
        raise ValueError(
            f"monthly daily price requires exactly months 1..12 "
            f"(missing={sorted(missing)}, extra={sorted(extra)})"
        )
    expected = DAY_POINTS[resolution]
    for month in range(1, 13):
        actual = len(curves[month])
        if actual != expected:
            raise ValueError(
                f"month {month} daily price requires {expected} steps "
                f"at {resolution}, got {actual}"
            )


def expand_monthly_daily_prices(
    curves: Mapping[int, Sequence[_Points]], *,
    resolution: str, start_year: int, operation_years: int
) -> tuple[_Points, ...]:
    """Expand trusted 12-month/day curves over *operating* calendar years.

    The first step is January 1 of start_year. Every day selects its natural
    month and repeats that month's 24h curve. February 29 is another February
    day; the next calendar month switches immediately at midnight.

    Inputs must already have passed check_monthly_daily_curves and source-object
    validation at the public submit boundary. Like existing Worker materializers,
    this function does not revalidate the signed input or calculate a hash.
    """
    values: list[_Points] = []
    for year in range(start_year, start_year + operation_years):
        for month in range(1, 13):
            day_curve = tuple(curves[month])
            values.extend(day_curve * monthrange(year, month)[1])
    return tuple(values)
