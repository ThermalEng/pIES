"""Indicator-only zero semantics; cash-flow facts must retain their precision."""

from decimal import Decimal

import pytest

from iesplan.finance import IRRStatus, cashflow_irr, compute_payback, evaluate_project_investment

CNY = {"currency": "CNY", "price_basis": "tax_inclusive", "base_year": 2026}


@pytest.mark.parametrize("flows", [
    [0, 0, 0],
    [-1e-11, 2e-11, -1e-12],
    [-1e-6, 1e-6, 0],
])
def test_zero_and_solver_residue_have_no_irr_or_payback(flows):
    original = [Decimal(str(value)) for value in flows]
    result = evaluate_project_investment(original, Decimal("0.08"), 2, **CNY)
    assert result.irr is None
    assert result.irr_status == "degenerate"
    assert result.static_payback is None
    assert result.dynamic_payback is None
    assert result.static_status == result.dynamic_status == "not_applicable"
    assert [row.net for row in result.rows] == original
    assert result.npv == sum((value / Decimal("1.08") ** year
                              for year, value in enumerate(original)), Decimal(0))
    payload = result.to_dict()
    assert payload["static_payback_unit"] is None
    assert payload["dynamic_payback_unit"] is None


@pytest.mark.parametrize("flows", [[0, 20, 30], [100, 5, 10], [-1e-11, 20, 30]])
def test_no_funding_deficit_is_not_an_immediate_payback(flows):
    result = evaluate_project_investment(flows, 0, 2, **CNY)
    assert result.irr is None
    assert result.irr_status == "no_solution"
    assert result.static_payback is result.dynamic_payback is None
    assert result.static_status == result.dynamic_status == "not_applicable"


def test_delayed_investment_keeps_the_original_year_axis():
    flows = [0, -100, 60, 60]
    result = evaluate_project_investment(flows, 0, 3, **CNY)
    assert result.irr == pytest.approx(0.1306623863)
    assert result.irr_status == "normal"
    assert result.static_payback == pytest.approx(2 + 40 / 60)
    assert result.dynamic_payback == result.static_payback
    assert result.static_status == result.dynamic_status == "recovered"
    assert compute_payback([0, 0, -100, 110]) == pytest.approx(2 + 100 / 110)


def test_real_small_investment_is_not_discarded():
    flows = [Decimal("-0.00001"), Decimal("0.000006"), Decimal("0.000006")]
    rate, status, _ = cashflow_irr(flows)
    assert rate == pytest.approx(0.1306623863)
    assert status == IRRStatus.unique
    assert compute_payback(flows) == pytest.approx(1 + 4 / 6)


def test_relative_residue_does_not_introduce_an_extra_sign_change():
    rate, status, _ = cashflow_irr([-1e9, 1.2e9, -1e-4])
    assert rate == pytest.approx(0.2)
    assert status == IRRStatus.unique


def test_recovery_within_zero_tolerance_stays_inside_the_period():
    assert compute_payback([-100, Decimal("99.9999995")]) == 1.0


def test_unrecovered_investment_is_distinct_from_not_applicable():
    result = evaluate_project_investment([0, -100, 20], 0, 2, **CNY)
    assert result.static_payback is result.dynamic_payback is None
    assert result.static_status == result.dynamic_status == "not_recovered"


def test_nonconventional_reinvestment_is_still_classified():
    result = evaluate_project_investment([0, -100, 250, -150], 0, 3, **CNY)
    assert result.irr_status == "multiple"
    assert result.irr == pytest.approx(0)
    assert result.static_payback == pytest.approx(1.4)
