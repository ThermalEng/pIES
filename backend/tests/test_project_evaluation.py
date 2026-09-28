"""项目投资口径纯评价切片测试(0.10-D)。

手算小样例 + 边界:固定年份净现金流与用户折现率 → 逐年明细行与
NPV/IRR/静态/动态回收期同源一致。不读取项目、不拼单年为多年。
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from iesplan.finance import (
    IRREvaluationStatus,
    PaybackStatus,
    evaluate_project_investment,
)

CNY = {"currency": "CNY", "price_basis": "含税", "base_year": 2024}


def test_known_case_rows_and_indicators_agree():
    """手算样例 [-100, 60, 60], r=0.1。

    d1 = 60/1.1 = 54.545454…;d2 = 60/1.21 = 49.586776…;
    NPV = 4.132231…;静态 = 1 + 40/60 = 1.6666…;
    动态 = 1 + (500/11)/(6000/121) = 1 + 121/132 = 1.9166…;
    IRR:60x^2 + 60x − 100 = 0,x = 1/(1+r) → r ≈ 0.13066,唯一根。
    """
    result = evaluate_project_investment(
        [Decimal("-100"), Decimal("60"), Decimal("60")],
        Decimal("0.1"),
        **CNY,
    )
    assert result.evaluation_years == 2
    assert [r.year for r in result.rows] == [0, 1, 2]
    assert [r.net for r in result.rows] == [Decimal("-100"), Decimal("60"), Decimal("60")]
    assert float(result.rows[1].discounted) == pytest.approx(54.54545454545455)
    assert float(result.rows[2].discounted) == pytest.approx(49.58677685950413)
    assert [r.cumulative for r in result.rows] == [
        Decimal("-100"), Decimal("-40"), Decimal("20"),
    ]
    # 指标与明细同源:NPV 即末年贴现累计
    assert result.npv == result.rows[-1].cumulative_discounted
    assert float(result.npv) == pytest.approx(4.132231404958678)
    assert result.static_payback == pytest.approx(1.6666666666666667)
    assert result.static_status == PaybackStatus.recovered
    assert result.dynamic_payback == pytest.approx(1.9166666666666667)
    assert result.dynamic_status == PaybackStatus.recovered
    assert result.irr == pytest.approx(0.130662, rel=1e-5)
    assert result.irr_status == IRREvaluationStatus.normal


def test_zero_rate_dynamic_equals_static():
    """r=0 时贴现流与原始流完全一致,动态回收期与静态相等。"""
    result = evaluate_project_investment([-200, 80, 80, 80], 0, **CNY)
    assert result.npv == Decimal("40")
    assert result.static_payback == pytest.approx(2.5)
    assert result.dynamic_payback == result.static_payback
    assert result.dynamic_status == PaybackStatus.recovered


def test_unrecovered_has_explicit_status():
    """[-100, 10, 10], r=0.1:两种回收期均为无值 + 明确未回收状态。"""
    result = evaluate_project_investment(
        [Decimal("-100"), Decimal("10"), Decimal("10")], Decimal("0.1"), **CNY,
    )
    assert result.static_payback is None
    assert result.static_status == PaybackStatus.not_recovered
    assert result.dynamic_payback is None
    assert result.dynamic_status == PaybackStatus.not_recovered
    assert result.npv < 0
    assert result.irr_status == IRREvaluationStatus.out_of_domain


def test_irr_abnormal_statuses():
    """IRR 非正常状态:全正无解、全零退化、两次变号多解取最小正根。"""
    no_solution = evaluate_project_investment([100, 10], Decimal("0.1"), **CNY)
    assert no_solution.irr is None
    assert no_solution.irr_status == IRREvaluationStatus.no_solution

    degenerate = evaluate_project_investment([0, 0, 0], Decimal("0.1"), **CNY)
    assert degenerate.irr is None
    assert degenerate.irr_status == IRREvaluationStatus.degenerate

    # -100 + 250x − 150x^2 = 0 在 x = 1 与 x = 2/3 处为零 → r ∈ {0, 0.5}
    multiple = evaluate_project_investment([-100, 250, -150], Decimal("0.1"), **CNY)
    assert multiple.irr == pytest.approx(0.0)
    assert multiple.irr_status == IRREvaluationStatus.multiple


def test_user_input_boundary_rejected():
    """用户输入边界:空序列、不足两年、负折现率、非有限值、空币种均大声失败。"""
    with pytest.raises(ValueError):
        evaluate_project_investment([], Decimal("0.1"), **CNY)
    with pytest.raises(ValueError):
        evaluate_project_investment([Decimal("-100")], Decimal("0.1"), **CNY)
    with pytest.raises(ValueError):
        evaluate_project_investment([-100, 60], Decimal("-0.1"), **CNY)
    with pytest.raises(ValueError):
        evaluate_project_investment([-100, float("nan")], Decimal("0.1"), **CNY)
    with pytest.raises(ValueError):
        evaluate_project_investment(
            [-100, 60], Decimal("0.1"),
            currency="", price_basis="含税", base_year=2024,
        )


def test_contract_schema_units_and_serialization():
    """版本化 contract:口径回显、单位标注、可 JSON 序列化。"""
    result = evaluate_project_investment([-100, 60, 60], Decimal("0.1"), **CNY)
    assert result.schema == "ies.project-evaluation"
    assert result.schema_version == "1.0.0"
    assert result.currency == "CNY"
    assert result.price_basis == "含税"
    assert result.base_year == 2024
    assert result.discount_rate == Decimal("0.1")
    payload = result.to_dict()
    assert json.dumps(payload)
    assert payload["npv_unit"] == "CNY"
    assert payload["static_payback_unit"] == "year"
    assert payload["dynamic_payback_unit"] == "year"
    assert payload["evaluation_years"] == 2
