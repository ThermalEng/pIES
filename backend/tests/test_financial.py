"""财务指标单元测试:IRR 分类、NPV 手算、项目/资本金现金流构建。

纯计算测试,不依赖 DB(依据 02 §5.2/§5.4)。
"""

from decimal import Decimal

import pytest

from iesplan.metrics.financial import (
    IRRStatus,
    build_equity_cashflows,
    build_project_cashflows,
    cashflow_irr,
    equity_irr,
    npv,
    project_irr,
)


class TestCashflowIrr:
    """IRR 求根与分类。"""

    def test_known_root_10_percent(self):
        # [-1000, +1100] 的 IRR 应约等于 10%
        rate, status, msg = cashflow_irr([Decimal("-1000"), Decimal("1100")])
        assert status == IRRStatus.unique
        assert rate is not None
        assert rate == pytest.approx(0.1, abs=1e-9)

    def test_known_root_20_percent(self):
        rate, status, _ = cashflow_irr([-1000, 1200])
        assert status == IRRStatus.unique
        assert rate == pytest.approx(0.2, abs=1e-9)

    def test_float_input_accepted(self):
        rate, status, _ = cashflow_irr([-1000.0, 1100.0])
        assert status == IRRStatus.unique
        assert rate == pytest.approx(0.1, abs=1e-9)

    def test_no_sign_change_all_positive(self):
        rate, status, _ = cashflow_irr([100, 200, 300])
        assert status == IRRStatus.none
        assert rate is None

    def test_no_sign_change_all_negative(self):
        rate, status, _ = cashflow_irr([-100, -50, -30])
        assert status == IRRStatus.none
        assert rate is None

    def test_multiple_roots(self):
        # -1000 + 2500x - 1540x^2 = 0 => x=1/1.1 或 x=1/1.4 => r=10% 与 r=40%
        rate, status, msg = cashflow_irr([-1000, 2500, -1540])
        assert status == IRRStatus.multiple
        assert "多" in msg or "符号变化" in msg
        # 取最小正根 10%
        assert rate == pytest.approx(0.1, abs=1e-6)

    def test_three_period_multiple(self):
        # 3 个正根现金流:(-1, +3, -2, +0.6)? 校验至少返回 multiple 且根为正
        rate, status, _ = cashflow_irr([-1000, 3000, -2500, 660])
        assert status == IRRStatus.multiple
        assert rate is not None and rate > 0

    def test_degenerate_all_zero(self):
        rate, status, _ = cashflow_irr([0, 0, 0])
        assert status == IRRStatus.degenerate
        assert rate is None

    def test_degenerate_constant(self):
        rate, status, _ = cashflow_irr([5, 5, 5])
        assert status == IRRStatus.degenerate
        assert rate is None

    def test_out_of_domain_no_positive_root(self):
        # 单次变号但 NPV(0) < 0:无正实根(投资不可回收)
        rate, status, _ = cashflow_irr([-1000, 100])
        assert status == IRRStatus.out_of_domain
        assert rate is None

    def test_out_of_domain_multi_sign_change_no_root(self):
        rate, status, _ = cashflow_irr([-1000, 500, -200])
        assert status == IRRStatus.out_of_domain
        assert rate is None

    def test_numerical_failure_non_finite(self):
        rate, status, _ = cashflow_irr([float("nan"), 1100])
        assert status == IRRStatus.numerical_failure
        assert rate is None

    def test_numerical_failure_empty(self):
        rate, status, _ = cashflow_irr([])
        assert status == IRRStatus.numerical_failure
        assert rate is None

    def test_root_at_zero(self):
        # [-1000, +1000] 的根恰在 r=0
        rate, status, _ = cashflow_irr([-1000, 1000])
        assert status == IRRStatus.unique
        assert rate == pytest.approx(0.0, abs=1e-9)


class TestNpv:
    """NPV 手算验证。"""

    def test_hand_calculation(self):
        # NPV(10%, [-1000, 1100]) = -1000 + 1000 = 0
        assert npv(Decimal("0.1"), [Decimal("-1000"), Decimal("1100")]) == Decimal("0")

    def test_zero_rate_is_sum(self):
        assert npv(0, [100, 200, 300]) == Decimal("600")

    def test_two_period(self):
        # NPV(5%, [1000, 100]) = 1000 + 100/1.05 ≈ 1095.238
        v = npv(Decimal("0.05"), [Decimal("1000"), Decimal("100")])
        assert v == pytest.approx(Decimal("1095.2380952380952380952380952"), abs=Decimal("1e-10"))

    def test_returns_decimal(self):
        assert isinstance(npv(Decimal("0.1"), [1, 1]), Decimal)


class TestBuildProjectCashflows:
    """项目投资现金流构建(02 §5.4)。"""

    INV = 1_000_000
    OM = 50_000
    SAVING = 200_000

    def _flows(self, **kw):
        return build_project_cashflows(
            self.INV,
            self.OM,
            self.SAVING,
            0,
            Decimal("0.25"),
            10,
            project_years=20,
            **kw,
        )

    def test_periods_and_sign(self):
        flows = self._flows()
        assert len(flows) == 21  # 第 0 期 + 20 年
        assert all(isinstance(c, Decimal) for c in flows)
        assert flows[0] == Decimal(-1_000_000)  # 初始投资为负
        # 折旧期内:ATCF = 0.75*(200000-50000-100000) + 100000 = 137500
        assert flows[1] == Decimal("137500")
        # 折旧期外:0.75*(200000-50000) = 112500
        assert flows[11] == Decimal("112500")

    def test_tax_shield_shape(self):
        flows = self._flows()
        # 折旧期内现金流高于折旧期外(折旧税盾)
        assert all(flows[y] == Decimal("137500") for y in range(1, 11))
        assert all(flows[y] == Decimal("112500") for y in range(11, 21))

    def test_salvage_at_end(self):
        flows = self._flows(salvage=100_000)
        assert flows[20] == Decimal("112500") + Decimal("100000")
        assert flows[19] == Decimal("112500")

    def test_incremental_baseline_semantics(self):
        # 增量语义:baseline_annual_cost=300000,annual_energy_saving 解释为方案年运行成本
        # 节省 = 300000 - 180000 = 120000
        flows = build_project_cashflows(
            1_000_000,
            50_000,
            180_000,  # 新方案年运行成本
            0,
            Decimal("0.25"),
            10,
            project_years=20,
            baseline_annual_cost=300_000,
        )
        # ATCF = 0.75*(120000-50000-100000) + 100000 = 0.75*(-30000)+100000 = 77500
        assert flows[1] == Decimal("77500")

    def test_invalid_tax_rate(self):
        with pytest.raises(ValueError):
            build_project_cashflows(1, 0, 0, 0, Decimal("1.5"), 10, project_years=5)


class TestProjectAndEquityIrr:
    """税后项目投资 IRR 与税后资本金 IRR(分离口径)。"""

    def test_project_irr_consistent_with_cashflow_irr(self):
        rate, status, _ = project_irr(1_000_000, 50_000, 200_000, 0, Decimal("0.25"), 10, project_years=20)
        assert status == IRRStatus.unique
        flows = build_project_cashflows(1_000_000, 50_000, 200_000, 0, Decimal("0.25"), 10, project_years=20)
        rate2, status2, _ = cashflow_irr(flows)
        assert status2 == IRRStatus.unique
        assert rate == pytest.approx(rate2, abs=1e-9)

    def test_equity_irr_no_debt_equals_project_irr(self):
        # 无杠杆时资本金 IRR 应等于项目 IRR
        p_rate, p_status, _ = project_irr(
            1_000_000, 50_000, 200_000, 0, Decimal("0.25"), 10, project_years=20
        )
        e_rate, e_status, _ = equity_irr(
            1_000_000,
            50_000,
            200_000,
            0,
            Decimal("0.25"),
            10,
            project_years=20,
            loan_ratio=0,
        )
        assert p_status == IRRStatus.unique
        assert e_status == IRRStatus.unique
        assert e_rate == pytest.approx(p_rate, abs=1e-9)

    def test_equity_flow_shape(self):
        flows = build_equity_cashflows(
            1_000_000,
            50_000,
            200_000,
            0,
            Decimal("0.25"),
            10,
            20,
            loan_ratio=Decimal("0.7"),
            loan_rate=Decimal("0.05"),
            loan_years=10,
        )
        assert len(flows) == 21
        # 资本金投入 = -(1-0.7)*100万 = -30万
        assert flows[0] == Decimal("-300000")
        # 杠杆下资本金 IRR 高于项目 IRR(财务杠杆放大)
        e_rate, e_status, _ = equity_irr(
            1_000_000,
            50_000,
            200_000,
            0,
            Decimal("0.25"),
            10,
            20,
            loan_ratio=Decimal("0.7"),
        )
        p_rate, _, _ = project_irr(1_000_000, 50_000, 200_000, 0, Decimal("0.25"), 10, 20)
        assert e_status == IRRStatus.unique
        assert e_rate > p_rate
