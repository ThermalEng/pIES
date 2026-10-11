"""0.12-T2 定向测试: 评价时间轴纯财务算法(手算, 无求解)。

覆盖 ``finance.timeline`` 的时间口径(``finance.md``“项目建设安排与
全周期现金流”): K1N2E3 付款 0.6/0.4 落款 ``CF=[-.6C,-.4C,R1,R2]``;
维护只扣发生年; K0 全款 t0; 跨闰年边界; 购/售符号; 可变 O&M 接口
按年真实归集(标量存量组合在装配边界阻断, 见装配定向测试); 总额
十进制精确; 形状错误大声失败。真实求解接通见
``test_evaluation_cashflow_chain.py``。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from iesplan.finance import evaluate_project_investment
from iesplan.finance.timeline import (
    InterfaceDriverFacts,
    TariffYearFacts,
    TimelineInputs,
    annual_boundaries,
    annual_energy_nets,
    annual_fixed_om_total,
    annual_variable_om,
    assemble_cashflows,
    combine_annual_nets,
    construction_total,
)

H = 3600.0


def _purchase(binding="g", y1=48.0, y2=96.0):
    return TariffYearFacts(
        binding_id=binding,
        direction="purchase",
        aggregation="integrate_positive",
        energy_values=(y1,) * 8760 + (y2,) * 8784,
        energy_unit="kW",
        price_values=(0.7,) * 17544,
        price_unit="CNY/kWh",
    )


def test_boundaries_follow_calendar_not_construction():
    """2023(平)+2024(闰): 年界与 K 无关, 只由运行起始年推导。"""
    assert annual_boundaries("1h", 2023, 2) == ((0, 8760), (8760, 17544))
    assert annual_boundaries("1h", 2024, 2) == ((0, 8784), (8784, 17544))


def test_purchase_energy_nets_per_year():
    """购电成本按年归集(48×8760×0.7, 96×8784×0.7), 两年不等。"""
    nets = annual_energy_nets(
        step_seconds=H,
        boundaries=((0, 8760), (8760, 17544)),
        tariffs=[_purchase()],
        currency="CNY",
    )
    assert float(nets[0]) == pytest.approx(float(Decimal("48") * 8760 * Decimal("0.7")), rel=1e-9)
    assert float(nets[1]) == pytest.approx(float(Decimal("96") * 8784 * Decimal("0.7")), rel=1e-9)
    assert float(nets[0]) != pytest.approx(float(nets[1]), rel=1e-3)


def test_sale_and_time_series_price_signs():
    """售电为负(收益向); 时变价格逐点相乘, 非总量先聚合。"""
    energy = (10.0,) * 4
    nets = annual_energy_nets(
        step_seconds=H,
        boundaries=((0, 2), (2, 4)),
        tariffs=[
            TariffYearFacts(
                "s",
                "sale",
                "integrate_positive",
                energy,
                "kW",
                (0.4, 0.5, 0.4, 0.5),
                "CNY/kWh",
            )
        ],
        currency="CNY",
    )
    assert float(nets[0]) == pytest.approx(
        float(Decimal("-10") * (Decimal("0.4") + Decimal("0.5"))), rel=1e-9
    )
    assert float(nets[1]) == pytest.approx(
        float(Decimal("-10") * (Decimal("0.4") + Decimal("0.5"))), rel=1e-9
    )
    nets2 = annual_energy_nets(
        step_seconds=H,
        boundaries=((0, 2), (2, 4)),
        tariffs=[
            TariffYearFacts(
                "s2",
                "sale",
                "integrate_positive",
                energy,
                "kW",
                (0.4, 0.4, 0.9, 0.9),
                "CNY/kWh",
            )
        ],
        currency="CNY",
    )
    assert float(nets2[0]) != pytest.approx(float(nets2[1]), rel=1e-9)


def test_volume_variable_om_yearly_both_aggregations():
    """体积使用量两年手算: integrate(m³/h 流率×步长)与 sum(m³ 直接求和)
    均按年直接归集, 两年不等, 不均摊、不转能量。"""
    bounds = ((0, 2), (2, 4))
    integrate = annual_variable_om(
        step_seconds=H,
        boundaries=bounds,
        interface_drivers=[
            InterfaceDriverFacts(
                "gb",
                "gas",
                (10.0, 10.0, 30.0, 30.0),
                "m3/h",
                "integrate_positive",
                Decimal("2.5"),
                "CNY/m3",
            )
        ],
        currency="CNY",
    )
    # 1h 步: 10 m³/h×1h=10 m³/step, 年1共20 m³×2.5=50; 年2为150。
    assert float(integrate[0]) == pytest.approx(2.5 * 20, rel=1e-9)
    assert float(integrate[1]) == pytest.approx(2.5 * 60, rel=1e-9)
    summed = annual_variable_om(
        step_seconds=H,
        boundaries=bounds,
        interface_drivers=[
            InterfaceDriverFacts(
                "gb",
                "gas",
                (100.0, 100.0, 300.0, 300.0),
                "m3",
                "sum_positive",
                Decimal("2.5"),
                "CNY/m3",
            )
        ],
        currency="CNY",
    )
    assert float(summed[0]) == pytest.approx(2.5 * 200, rel=1e-9)
    assert float(summed[1]) == pytest.approx(2.5 * 600, rel=1e-9)


def test_volume_tariff_purchase_yearly():
    """体积计费两年手算: 购气按年归集(年1: 20 m³×2.5=50, 年2: 60 m³×2.5=150)。"""
    nets = annual_energy_nets(
        step_seconds=H,
        boundaries=((0, 2), (2, 4)),
        tariffs=[
            TariffYearFacts(
                "g",
                "purchase",
                "integrate_positive",
                (10.0, 10.0, 30.0, 30.0),
                "m3/h",
                (2.5, 2.5, 2.5, 2.5),
                "CNY/m3",
            )
        ],
        currency="CNY",
    )
    assert float(nets[0]) == pytest.approx(2.5 * 20, rel=1e-9)
    assert float(nets[1]) == pytest.approx(2.5 * 60, rel=1e-9)


def test_variable_om_interface_yearly():
    """接口 driver 按两年不同实际量产生不同年度费用, 直接归集不均摊。

    标量存量组合(无年份全期金额)在装配公开输入边界阻断
    (``period_variable_om_scalar_unsupported``), 不再进入财务时间轴,
    此处只保留接口型归集证据。"""
    bounds = ((0, 2), (2, 4))
    by_interface = annual_variable_om(
        step_seconds=H,
        boundaries=bounds,
        interface_drivers=[
            InterfaceDriverFacts(
                "hp",
                "consumed",
                (100.0, 100.0, 300.0, 300.0),
                "kW",
                "integrate_positive",
                Decimal("0.008"),
                "CNY/kWh",
            )
        ],
        currency="CNY",
    )
    assert float(by_interface[0]) == pytest.approx(float(Decimal("0.008") * 200), rel=1e-9)
    assert float(by_interface[1]) == pytest.approx(float(Decimal("0.008") * 600), rel=1e-9)
    assert float(by_interface[0]) != pytest.approx(float(by_interface[1]), rel=1e-9)


def test_totals_are_decimal_exact():
    """C 与年固定 O&M 十进制精确累加(140000、1400)。"""
    assert construction_total([100000.0, 40000.0]) == Decimal("140000")
    assert annual_fixed_om_total([1400.0]) == Decimal("1400")


def test_k1n2_cashflow_shape_and_maintenance():
    """K=1、N=2、E=3: CF=[-.6C,-.4C,R1,R2]; 维护只扣发生年(t=3)。"""
    energy = [Decimal("294336"), Decimal("590284.8")]
    annual = combine_annual_nets(
        energy, [Decimal("0"), Decimal("0")], Decimal("1400"), {3: Decimal("1000.00")}, 1
    )
    assert annual[0].net == Decimal("294336") * -1 - Decimal("1400")
    assert annual[1].net == Decimal("590284.8") * -1 - Decimal("1400") - Decimal("1000.00")
    flows = assemble_cashflows(
        TimelineInputs(1, 2, 3, ((0, Decimal("0.6")), (1, Decimal("0.4"))), tuple(annual)),
        Decimal("140000"),
    )
    assert flows == [
        Decimal("-84000.00"),
        Decimal("-56000.00"),
        annual[0].net,
        annual[1].net,
    ]
    assert len(flows) == 3 + 1


def test_k0_full_payment_at_t0():
    """显式 K=0: 全部建设款 t=0, 运营净额进 CF[1..2]。"""
    annual = combine_annual_nets(
        [Decimal("100"), Decimal("200")], [Decimal("0"), Decimal("0")], Decimal("0"), {}, 0
    )
    flows = assemble_cashflows(
        TimelineInputs(0, 2, 2, ((0, Decimal("1")),), tuple(annual)), Decimal("1150000")
    )
    assert flows == [Decimal("-1150000"), Decimal("-100"), Decimal("-200")]


def test_same_year_maintenance_sums_once():
    """同年多项维护累加一次扣除(调用方已按年累加, 此处只记合并语义)。"""
    annual = combine_annual_nets(
        [Decimal("100"), Decimal("200")],
        [Decimal("0"), Decimal("0")],
        Decimal("0"),
        {2: Decimal("1000.00") + Decimal("2500.50")},
        0,
    )
    assert annual[0].net == Decimal("-100")
    assert annual[1].net == Decimal("-200") - Decimal("3500.50")


def test_evaluation_echoes_frozen_years_and_matches_rows():
    """评价回显冻结 E=3; NPV 与明细同源(末年贴现累计)。"""
    flows = [Decimal("-84000"), Decimal("-56000"), Decimal("-295736"), Decimal("-591884.8")]
    assessed = evaluate_project_investment(
        flows, 0.08, 3, currency="CNY", price_basis="tax_inclusive", base_year=2025
    )
    assert assessed.evaluation_years == 3
    assert len(assessed.rows) == 4
    expected = sum(f / (Decimal("1.08") ** t) for t, f in enumerate(flows))
    assert float(assessed.npv) == pytest.approx(float(expected), rel=1e-9)
    assert assessed.rows[-1].cumulative_discounted == assessed.npv
