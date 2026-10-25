"""ISSUE-01: independent synthetic checks for natural-month daily pricing.

Pure calendar core only. Public DTO, upload references, frozen-task recovery,
browser authoring and actual Generator integration remain gated on contract review.
"""

from decimal import Decimal

import pytest

from iesplan.core.contracts import horizon_point_count
from iesplan.finance.monthly_daily import (
    DAY_POINTS,
    check_monthly_daily_curves,
    expand_monthly_daily_prices,
)


def curves(n: int):
    return {month: tuple([Decimal(month)] * n) for month in range(1, 13)}


@pytest.mark.parametrize("resolution", ["1h", "30min", "15min"])
def test_all_months_match_real_horizon_and_feb_29(resolution):
    count = DAY_POINTS[resolution]
    month = curves(count)
    check_monthly_daily_curves(month, resolution=resolution)
    out = expand_monthly_daily_prices(
        month, resolution=resolution, start_year=2023, operation_years=3
    )
    assert len(out) == horizon_point_count(resolution, 2023, 3)
    # 2023/2024/2025: January, Feb, March each use their own daily curve.
    for year in (2023, 2024, 2025):
        prefix = sum(
            horizon_point_count(resolution, 2023, 1)
            if x == 2023 else horizon_point_count(resolution, x, 1)
            for x in range(2023, year)
        )
        for month_index, expected in ((0, 1), (31, 2), (59 if year != 2024 else 60, 3)):
            assert out[prefix + month_index * count] == Decimal(expected)
    # The leap day and both sides of the Feb->Mar midnight boundary.
    year_2024 = horizon_point_count(resolution, 2023, 1)
    leap_day = year_2024 + (31 + 28) * count
    assert out[leap_day:leap_day + count] == (Decimal(2),) * count
    assert out[leap_day - 1] == Decimal(2)
    assert out[leap_day + count] == Decimal(3)


def test_reuses_same_daily_curve_for_several_months_without_reupload():
    daily = tuple(Decimal(x) / 10 for x in range(24))
    entries = curves(24)
    entries[1] = daily
    entries[7] = daily
    check_monthly_daily_curves(entries, resolution="1h")
    result = expand_monthly_daily_prices(
        entries, resolution="1h", start_year=2024, operation_years=1
    )
    assert result[:24] == daily
    july = (31 + 29 + 31 + 30 + 31 + 30) * 24
    assert result[july:july + 24] == daily
    assert result[july + 24:july + 48] == daily


def test_day_hour_boundary_month_change_and_cross_year():
    daily = tuple(Decimal(f"{x}.25") for x in range(24))
    entries = curves(24)
    entries[12] = daily
    out = expand_monthly_daily_prices(
        entries, resolution="1h", start_year=2023, operation_years=2
    )
    assert out[23] == Decimal(1)
    assert out[24] == Decimal(1)
    assert out[365 * 24 - 24:365 * 24] == daily
    assert out[365 * 24] == Decimal(1)
    assert out[-24:] == daily


def test_missing_month_extra_month_and_incomplete_day_rejected():
    entries = curves(24)
    del entries[12]
    with pytest.raises(ValueError, match="missing=\\[12\\]"):
        check_monthly_daily_curves(entries, resolution="1h")
    entries[12] = (Decimal(0),) * 24
    entries[13] = (Decimal(0),) * 24
    with pytest.raises(ValueError, match="extra=\\[13\\]"):
        check_monthly_daily_curves(entries, resolution="1h")
    del entries[13]
    entries[2] = entries[2][:-1]
    with pytest.raises(ValueError, match="month 2 daily price requires 24"):
        check_monthly_daily_curves(entries, resolution="1h")
    with pytest.raises(ValueError, match="unsupported"):
        check_monthly_daily_curves(entries, resolution="2h")


def test_signed_prices_and_independent_manual_step_charge():
    entries = curves(24)
    entries[1] = (Decimal("0.100"), Decimal("0"), Decimal("-0.200")) + (
        Decimal("0.75"),
    ) * 21
    check_monthly_daily_curves(entries, resolution="1h")
    out = expand_monthly_daily_prices(
        entries, resolution="1h", start_year=2025, operation_years=1
    )
    assert out[:3] == (Decimal("0.100"), Decimal("0"), Decimal("-0.200"))
    # Synthetic 1 kWh at each of the first three hours:
    manual = Decimal("0.100") + Decimal("0") - Decimal("0.200")
    assert sum(out[i] * Decimal("1") for i in range(3)) == manual
    assert manual == Decimal("-0.100")
    # Existing sale-direction sign is -raw_charge, not a mutation of price.
    assert -sum(out[i] * Decimal("1") for i in range(3)) == Decimal("0.100")
