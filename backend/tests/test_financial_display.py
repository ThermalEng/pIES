"""0.12-T3 定向测试: 封存财务评价的展示消费(纯逻辑, 无 DB)。

覆盖: Excel 展平器对封存块必需字段直接消费、金额字符串原样展示
(不重算、不舍入); 缺块原因 ``_financial_absence`` 按可观察事实区分,
不把所有缺块猜为未请求 solution。DB 端到端(证据种子 → Excel/结果视图)见
``test_wave2_c_packages.py`` / ``test_wave2_c_results.py``。
"""

from __future__ import annotations

from iesplan.application.packages.transfers import _sealed_evaluation_rows
from iesplan.application.results.writes import _financial_absence


def _sealed() -> dict:
    """K1N2 封存评价块(当前 3.0.0 形态, 无需量绑定)。"""
    return {
        "schema": "ies.financial-evaluation",
        "schema_version": "1.0.0",
        "status": "evaluated",
        "evaluation_years": 3,
        "construction_period": 1,
        "operation_years": 2,
        "currency": "CNY",
        "price_basis": "tax_inclusive",
        "base_year": 2025,
        "discount_rate": "0.08",
        "construction_total": "140000",
        "construction_total_unit": "CNY",
        "annual_fixed_om": "1400",
        "payments_applied": [
            {"t": 0, "ratio": "0.6", "amount": "84000.0"},
            {"t": 1, "ratio": "0.4", "amount": "56000.0"},
        ],
        "net_cashflows": [],
        "annual_operation": [
            {
                "financial_year": 2, "energy_net": "10000", "variable_om": "100",
                "fixed_om": "1400", "maintenance": "0", "demand_charge": "0", "net": "8500",
            },
            {
                "financial_year": 3, "energy_net": "12000", "variable_om": "120",
                "fixed_om": "1400", "maintenance": "3500.50", "demand_charge": "0", "net": "6979.50",
            },
        ],
        "monthly_demand": [],
        "annual_net_flows": ["-84000.0", "-56000.0", "8500", "6979.50"],
        # 无税展示夹具: 五个数组显式为空(合法"不适用"), 不用缺字段伪装。
        "tax_cash": {
            "vat": [],
            "refunds": [],
            "income": [],
            "depreciation": [],
            "loss_balances": [],
        },
        "evaluation": {
            "currency": "CNY", "evaluation_years": 3,
            "rows": [
                {"year": 0, "net": "-84000.0", "discounted": "-84000.0",
                 "cumulative": "-84000.0", "cumulative_discounted": "-84000.0"},
                {"year": 1, "net": "-56000.0", "discounted": "-51851.85",
                 "cumulative": "-140000.0", "cumulative_discounted": "-135851.85"},
                {"year": 2, "net": "8500", "discounted": "7288.07",
                 "cumulative": "-131500.0", "cumulative_discounted": "-128563.78"},
                {"year": 3, "net": "6979.50", "discounted": "5543.47",
                 "cumulative": "-124520.50", "cumulative_discounted": "-123020.31"},
            ],
            "npv": "-123020.31", "npv_unit": "CNY",
            "irr": None, "irr_status": "no_solution",
            "static_payback": None, "static_status": "not_recovered",
            "dynamic_payback": None, "dynamic_status": "not_recovered",
        },
    }


def test_financial_rows_render_sealed_block_verbatim():
    """封存块展平: K/N/E、付款时点、运营年分量、CF、NPV/IRR 状态齐全,
    金额为封存字符串原样。"""
    rows = _sealed_evaluation_rows(_sealed())
    text = "\n".join(f"{k}\n{v}" for k, v in rows)
    assert "评价年限 E" in text and "建设期 K" in text and "运营年限 N" in text
    assert "140000" in text and "84000.0" in text and "56000.0" in text
    assert "运营年第 1 年(t=2)" in text and "3500.50" in text
    assert "CF[t=0]" in text and "CF[t=3]" in text
    assert "-123020.31" in text and "no_solution" in text and "not_recovered" in text


def test_financial_absence_reasons(monkeypatch):
    """有块→None; 非 normal 结局→outcome_not_normal; 正常结局下以快照
    outputs 是否请求 solution 区分, 不把所有缺块猜为未请求。"""
    assert _financial_absence("x", {"status": "evaluated"}, 1, None) is None
    assert _financial_absence("no_recommendation", None, 1, None) == "outcome_not_normal"
    assert _financial_absence("insufficient_evidence", None, 1, None) == "outcome_not_normal"

    class _Snap:
        calc_config_snapshot = {"outputs": ["metrics"]}

    monkeypatch.setattr("iesplan.tasks.get_snapshot", lambda db, sid: _Snap())
    assert _financial_absence("normal_completion", None, 1, None) == "solution_not_requested"

    class _Snap2:
        calc_config_snapshot = {"outputs": ["solution", "metrics"]}

    monkeypatch.setattr("iesplan.tasks.get_snapshot", lambda db, sid: _Snap2())
    assert _financial_absence("normal_completion", None, 1, None) == "missing_unknown"


def test_financial_absence_frozen_tuple_outputs(monkeypatch):
    """冻结快照 outputs 为 tuple(与 protocols 一致的合法冻结形态)时,
    同样识别未请求 solution, 不得误报 missing_unknown。"""
    frozen = {"outputs": ("metrics",)}
    assert not isinstance(frozen["outputs"], list)

    class _SnapFrozen:
        calc_config_snapshot = frozen

    monkeypatch.setattr("iesplan.tasks.get_snapshot", lambda db, sid: _SnapFrozen())
    assert _financial_absence("normal_completion", None, 1, None) == "solution_not_requested"
