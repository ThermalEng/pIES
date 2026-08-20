"""财务计算模块测试:逐时 → 财务指标计算与边界(审查意见第 6 条,03 §7)。

纯 pytest 单元测试(无数据库):覆盖 metrics(NPV/IRR 全分类/税后现金流/资本金现金流)、
hourly(逐时费用列口径/LCOE/回收期/交叉校验/回退)、params(配置来源/默认值)与边界。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import numpy as np
import pytest

from iesplan.core.errors import AppError
from iesplan.finance import (
    FinanceParams,
    FinancialResult,
    IRRStatus,
    build_equity_cashflows,
    build_project_cashflows,
    cashflow_irr,
    compute_financials,
    compute_lcoe,
    compute_payback,
    equity_irr,
    finance_params_from_config,
    npv,
    project_irr,
    project_npv,
)

# ---------------------------------------------------------------------------
# metrics:NPV
# ---------------------------------------------------------------------------


def test_npv_zero_rate_sums_cashflows():
    """贴现率为 0 时 NPV 等于现金流代数和。"""
    assert npv(Decimal("0"), [Decimal("100"), Decimal("50")]) == Decimal("150")


def test_npv_discount_exact():
    """已知解析解:NPV(0.1, [-100, 110]) == 0。"""
    assert npv(Decimal("0.1"), [Decimal("-100"), Decimal("110")]) == Decimal("0")


def test_npv_mixed_types():
    """float/Decimal 混合输入、float 利率均可用。"""
    result = npv(0.1, [-100.0, Decimal("110")])
    assert abs(result) < Decimal("1e-20")


# ---------------------------------------------------------------------------
# metrics:IRR 求根分类
# ---------------------------------------------------------------------------


def test_cashflow_irr_unique():
    """标准符号型:唯一正实根。[-100, 50, 60] → r ≈ 0.06394。"""
    rate, status, message = cashflow_irr([-100, 50, 60])
    assert status == IRRStatus.unique
    assert rate is not None and abs(rate - 0.06394) < 1e-3
    assert message


def test_cashflow_irr_root_at_zero():
    """NPV(0) 恰为 0 时返回根 r=0(unique)。"""
    rate, status, _ = cashflow_irr([-100, 100, 0])
    assert status == IRRStatus.unique
    assert rate == 0.0


def test_cashflow_irr_none_all_positive():
    """全部同号(无投资):r>=0 上无根。"""
    rate, status, _ = cashflow_irr([1, 2, 3])
    assert status == IRRStatus.none
    assert rate is None


def test_cashflow_irr_degenerate_zero_and_constant():
    """退化:全零 / 常数现金流。"""
    assert cashflow_irr([0, 0, 0])[1] == IRRStatus.degenerate
    assert cashflow_irr([5, 5, 5])[1] == IRRStatus.degenerate
    assert cashflow_irr([5, 5, 5])[0] is None


def test_cashflow_irr_out_of_domain():
    """存在符号变化但 r>=0 上无正实根(投资不可回收)。"""
    rate, status, _ = cashflow_irr([-100, 10, 5])
    assert status == IRRStatus.out_of_domain
    assert rate is None


def test_cashflow_irr_multiple_roots():
    """多次变号:取最小正根。[-100, 250, -154] 根为 r=0.1 与 r=0.4。"""
    rate, status, message = cashflow_irr([-100, 250, -154])
    assert status == IRRStatus.multiple
    assert rate is not None and abs(rate - 0.1) < 1e-6
    assert "2" in message


def test_cashflow_irr_numerical_failure():
    """空输入 / 非有限值 → numerical_failure。"""
    rate, status, _ = cashflow_irr([])
    assert status == IRRStatus.numerical_failure and rate is None
    rate, status, _ = cashflow_irr([float("nan"), 1.0])
    assert status == IRRStatus.numerical_failure and rate is None


# ---------------------------------------------------------------------------
# metrics:税后项目现金流
# ---------------------------------------------------------------------------


def test_build_project_cashflows_basic():
    """现金流口径:CF0=-投资;CF[y]=(1-t)(NB-OM-DEP)+DEP。"""
    flows = build_project_cashflows(
        1000, 0, 200, 0, 0.25, 10, project_years=5
    )
    assert flows[0] == Decimal("-1000")
    assert len(flows) == 6
    # 折旧 = 1000/10 = 100;应纳税所得 = 200 - 100 = 100;ATCF = 75 + 100 = 175
    assert flows[1:] == [Decimal("175")] * 5


def test_build_project_cashflows_baseline_increment():
    """增量语义:baseline_annual_cost=300、年成本=100 → 节省=200,与直接口径一致。"""
    flows = build_project_cashflows(
        1000, 0, 100, 0, 0.25, 10, project_years=5, baseline_annual_cost=300
    )
    assert flows == build_project_cashflows(1000, 0, 200, 0, 0.25, 10, project_years=5)


def test_build_project_cashflows_salvage_and_dep_end():
    """期末残值计入末年;折旧期满后不再计提。"""
    flows = build_project_cashflows(
        1000, 0, 200, 0, 0.25, 3, project_years=5, salvage=50
    )
    # 第 1-3 年(折旧期):dep=333.33…,ATCF=0.75*(200-333.33…)+333.33…=233.33…
    assert flows[1] == pytest.approx(Decimal("233.3333333333333333"), abs=Decimal("0.01"))
    assert flows[2] == flows[1] and flows[3] == flows[1]
    # 第 4 年折旧期满(dep=0):ATCF=0.75*200=150
    assert flows[4] == Decimal("150")
    # 第 5 年 = 第 4 年 ATCF + 残值 50 = 200
    assert flows[-1] == flows[4] + Decimal("50") == Decimal("200")


def test_build_project_cashflows_invalid_input():
    """税率越界 / 期数非法 → ValueError。"""
    with pytest.raises(ValueError):
        build_project_cashflows(1000, 0, 200, 0, 1.5, 10, project_years=5)
    with pytest.raises(ValueError):
        build_project_cashflows(1000, 0, 200, 0, 0.25, 10, project_years=0)


def test_project_npv_consistency():
    """project_npv 与 npv(build_project_cashflows(...)) 完全一致。"""
    kw = dict(investment=1000, annual_om=0, annual_energy_saving=200, tax_rate=0.25,
              depreciation_years=10, project_years=5)
    assert project_npv(Decimal("0.08"), **kw) == npv(
        Decimal("0.08"), build_project_cashflows(1000, 0, 200, 0, 0.25, 10, project_years=5)
    )


def test_project_irr_equals_cashflow_irr_of_flows():
    """project_irr 与对同口径现金流的 cashflow_irr 一致。"""
    rate, status, _ = project_irr(1000, 0, 200, tax_rate=0.25, depreciation_years=10, project_years=5)
    flows = build_project_cashflows(1000, 0, 200, 0, 0.25, 10, project_years=5)
    assert (rate, status) == cashflow_irr(flows)[:2]


# ---------------------------------------------------------------------------
# metrics:资本金现金流
# ---------------------------------------------------------------------------


def test_build_equity_cashflows_structure():
    """资本金现金流:CF0=-(1-ratio)*投资;贷款期外与项目现金流一致。"""
    project_flows = build_project_cashflows(
        1000, 0, 200, 0, 0.25, 10, project_years=15
    )
    equity_flows = build_equity_cashflows(
        1000, 0, 200, 0, 0.25, 10, 15, loan_ratio=0.7, loan_rate=0.05, loan_years=10
    )
    assert equity_flows[0] == Decimal("-300")  # 只出资本金 30%
    assert len(equity_flows) == 16
    # 贷款 10 年还清后(第 11-15 年,索引 11..15)无债务服务 → 与项目现金流完全一致
    for i in range(11, 16):
        assert equity_flows[i] == project_flows[i]


def test_equity_irr_valid_status():
    """资本金 IRR 返回合法分类且无异常。"""
    _, status, message = equity_irr(1000, 0, 200, tax_rate=0.25, depreciation_years=10,
                                    project_years=15, loan_ratio=0.7)
    assert status in (IRRStatus.unique, IRRStatus.multiple, IRRStatus.none, IRRStatus.out_of_domain)
    assert isinstance(message, str)


# ---------------------------------------------------------------------------
# hourly:LCOE 与静态回收期
# ---------------------------------------------------------------------------


def test_compute_lcoe_basic():
    """LCOE = 总成本/总发电量;发电量非正 → None。"""
    assert compute_lcoe(Decimal("1000"), Decimal("5000")) == Decimal("0.2")
    assert compute_lcoe(1000, 0) is None
    assert compute_lcoe(1000, -5) is None
    assert compute_lcoe(Decimal("0"), Decimal("1")) == Decimal("0")


def test_compute_payback_basic():
    """静态回收期:累计现金流首次转正的年数(线性插值)。"""
    assert compute_payback([-100, 50, 60]) == pytest.approx(1 + 50 / 60)
    assert compute_payback([-100, 90, 20]) == pytest.approx(1.5)
    assert compute_payback([-100, 40, 60]) == 2.0  # 恰在第 2 年末回收
    assert compute_payback([-100, 100]) == 1.0


def test_compute_payback_boundaries():
    """边界:未回收 / 首期即回收 / 空序列。"""
    assert compute_payback([-100, 30, 30]) is None
    assert compute_payback([100, 5]) == 0.0
    assert compute_payback([]) is None


# ---------------------------------------------------------------------------
# hourly:compute_financials 逐时 → 财务指标
# ---------------------------------------------------------------------------


def _basic_params() -> FinanceParams:
    return FinanceParams(
        discount_rate=Decimal("0.08"),
        tax_rate=Decimal("0.25"),
        depreciation_years=5,
        project_years=5,
        irr_floor=Decimal("0.08"),
    )


def _basic_kpi(energy_kwh: float = 1000.0) -> dict:
    return {
        "total_op_cost": 7.0,
        "buy_cost": 6.0,
        "gas_cost": 2.0,
        "sell_revenue": 1.0,
        "annual_pv_kwh": energy_kwh,
    }


def _basic_flows() -> dict[str, np.ndarray]:
    return {
        "cost_buy": np.array([3.0, 3.0]),
        "cost_gas": np.array([1.0, 1.0]),
        "revenue_sell": np.array([0.5, 0.5]),
    }


def test_compute_financials_basic():
    """逐时费用列 → 年运营费 = Σ(buy+gas)-Σ(sell)=7;现金流/IRR/NPV/LCOE/回收期齐备。"""
    result = compute_financials(
        _basic_kpi(), _basic_flows(), Decimal("100"), Decimal("50"), _basic_params()
    )
    assert isinstance(result, FinancialResult)
    assert result.annual_op_cost == Decimal("7")
    assert result.annual_revenue == Decimal("1")
    assert result.capex == Decimal("100")
    assert result.baseline_cost == Decimal("50")
    # 现金流:节省=50-7=43,折旧=100/5=20,ATCF=0.75*(43-20)+20=37.25
    assert result.cashflows == [Decimal("-100")] + [Decimal("37.25")] * 5
    # IRR:100 = 37.25 * 年金因子(5,r) → r≈0.25
    assert result.irr_status == IRRStatus.unique
    assert result.irr is not None and 0.24 < result.irr < 0.26
    # NPV 与独立计算一致
    assert result.npv == npv(Decimal("0.08"), result.cashflows)
    # 回收期:累计 -100/-62.75/-25.5/+11.75 → 2 + 25.5/37.25
    assert result.payback_years == pytest.approx(2 + 25.5 / 37.25)
    # LCOE = (100 + Σ7/1.08^y) / (Σ1000/1.08^y)
    af = sum(Decimal("1") / Decimal("1.08") ** y for y in range(1, 6))
    expected_lcoe = (Decimal("100") + Decimal("7") * af) / (Decimal("1000") * af)
    assert result.lcoe is not None and abs(result.lcoe - expected_lcoe) < Decimal("1e-10")
    # 交叉校验一致 → 无诊断
    assert result.detail["diagnostics"] == []
    assert result.detail["irr_floor"] == "0.08"
    # Decimal("50") - Decimal("7") 字符串为 "43.0"(Decimal 保持小数位), 数值比较
    assert float(result.detail["annual_saving"]) == pytest.approx(43.0)


def test_compute_financials_flows_authoritative_and_cross_check():
    """kpi 与逐时口径偏差 >1% 时记诊断,年运营费以逐时费用列为准。"""
    kpi = _basic_kpi()
    kpi["total_op_cost"] = 10.0  # 与 flows 的 7 偏差 30%
    result = compute_financials(kpi, _basic_flows(), 100, 50, _basic_params())
    assert result.annual_op_cost == Decimal("7")  # 逐时费用列权威
    assert any("偏差" in d for d in result.detail["diagnostics"])
    assert float(result.detail["cross_check"]["deviation"]) > 0.01


def test_compute_financials_kpi_fallback():
    """flows 无费用列 → 回退 kpi.total_op_cost 并给出诊断。"""
    kpi = _basic_kpi()
    for flows in (None, {}):
        result = compute_financials(kpi, flows, 100, 50, _basic_params())
        assert result.annual_op_cost == Decimal("7")
        assert any("回退" in d for d in result.detail["diagnostics"])


def test_compute_financials_missing_all_cost_sources():
    """flows 与 kpi 均无成本 → ValueError。"""
    with pytest.raises(ValueError):
        compute_financials({}, None, 100, 50, _basic_params())


def test_compute_financials_missing_capex():
    """capex/baseline 为空 → ValueError。"""
    with pytest.raises(ValueError):
        compute_financials(_basic_kpi(), _basic_flows(), None, 50, _basic_params())
    with pytest.raises(ValueError):
        compute_financials(_basic_kpi(), _basic_flows(), 100, None, _basic_params())


def test_compute_financials_lcoe_missing_or_zero_energy():
    """kpi 无发电量键 / 发电量为 0 → LCOE=None 且附说明。"""
    kpi_no_energy = {k: v for k, v in _basic_kpi().items() if k != "annual_pv_kwh"}
    result = compute_financials(kpi_no_energy, _basic_flows(), 100, 50, _basic_params())
    assert result.lcoe is None
    assert "无年度发电量键" in result.detail["lcoe_note"]

    kpi_zero_energy = _basic_kpi(energy_kwh=0.0)
    result = compute_financials(kpi_zero_energy, _basic_flows(), 100, 50, _basic_params())
    assert result.lcoe is None
    assert "为 0" in result.detail["lcoe_note"]


def test_compute_financials_si_energy_via_units():
    """SI 发电量键(J)经 core.units.convert 换算 kWh,与 kWh 键结果一致。"""
    kpi_si = _basic_kpi()
    kpi_si.pop("annual_pv_kwh")
    kpi_si["annual_pv_gen_j"] = 3.6e9  # = 1000 kWh
    result_si = compute_financials(kpi_si, _basic_flows(), 100, 50, _basic_params())
    result_kwh = compute_financials(_basic_kpi(), _basic_flows(), 100, 50, _basic_params())
    assert result_si.lcoe == result_kwh.lcoe


def test_compute_financials_list_flows_and_empty_arrays():
    """flows 为纯 list / 空数组时仍可计算(不崩溃)。"""
    flows_list = {"cost_buy": [3.0, 3.0], "cost_gas": [1.0, 1.0], "revenue_sell": [0.5, 0.5]}
    result = compute_financials(_basic_kpi(), flows_list, 100, 50, _basic_params())
    assert result.annual_op_cost == Decimal("7")

    flows_empty = {"cost_buy": np.array([]), "cost_gas": np.array([]), "revenue_sell": np.array([])}
    kpi = _basic_kpi()
    kpi["total_op_cost"] = 0.0
    result = compute_financials(kpi, flows_empty, 100, 50, _basic_params())
    assert result.annual_op_cost == Decimal("0")
    assert result.lcoe is not None  # 发电量仍在


def test_compute_financials_default_params():
    """params=None 时使用 FinanceParams() 默认值。"""
    result = compute_financials(_basic_kpi(), _basic_flows(), 100, 50)
    assert result.detail["discount_rate"] == "0.08"
    assert result.detail["tax_rate"] == "0.25"
    assert result.detail["project_years"] == 20


# ---------------------------------------------------------------------------
# params:配置来源
# ---------------------------------------------------------------------------


def test_finance_params_defaults():
    """空配置 / None → 全部默认值。"""
    for cfg in ({}, None, {"parameters": {}}):
        params = finance_params_from_config(cfg)
        assert params.discount_rate == Decimal("0.08")
        assert params.tax_rate == Decimal("0.25")
        assert params.depreciation_years == 10
        assert params.project_years == 20
        assert params.currency == "CNY"
        assert params.irr_floor == Decimal("0.08")


def test_finance_params_from_config():
    """calc_config parameters.economic + 顶层 irr_floor → FinanceParams。"""
    cfg = {
        "irr_floor": 0.1,
        "parameters": {
            "economic": {
                "discount_rate": 0.05,
                "tax_rate": 0.2,
                "project_years": 15,
                "depreciation_years": 5,
                "currency": "USD",
            }
        },
    }
    params = finance_params_from_config(cfg)
    assert params.discount_rate == Decimal("0.05")
    assert params.tax_rate == Decimal("0.2")
    assert params.project_years == 15
    assert params.depreciation_years == 5
    assert params.currency == "USD"
    assert params.irr_floor == Decimal("0.1")


def test_finance_params_from_config_params_alias():
    """文档别名路径 calc_config.params.economic 兼容。"""
    params = finance_params_from_config({"params": {"economic": {"discount_rate": 0.06}}, "irr_floor": 0.09})
    assert params.discount_rate == Decimal("0.06")
    assert params.irr_floor == Decimal("0.09")
    assert params.tax_rate == Decimal("0.25")  # 其余默认


def test_finance_params_frozen():
    """FinanceParams 不可变。"""
    with pytest.raises(FrozenInstanceError):
        FinanceParams().discount_rate = Decimal("0.1")  # type: ignore[misc]


def test_price_fact_source_error_not_swallowed(monkeypatch):
    """价格事实源存在但加载失败 → 上抛, 不静默回退内置默认(P2 审查意见)。

    兼容回退只允许"模块缺失"(ImportError); 文件缺失/语法错误等加载失败
    必须抛 SYS-CFG-001, 避免财务参数来源静默漂移。
    """
    import iesplan.devices.pricing as pricing
    import iesplan.finance.params as params_mod

    def boom() -> None:
        raise pricing._err("价格文件读取失败", file="prices.yaml")

    monkeypatch.setattr(pricing, "load_price_book", boom)
    # 事实源路径(已实现)加载失败 → 上抛, 不得回退
    with pytest.raises(AppError) as excinfo:
        params_mod._price_finance_defaults()
    assert excinfo.value.code == "SYS-CFG-001"


def test_price_fact_source_missing_module_falls_back(monkeypatch):
    """事实源模块缺失(兼容场景) → 回退内置默认, 不报错。"""
    import importlib

    import iesplan.finance.params as params_mod

    def missing(name):
        exc = ModuleNotFoundError(f"No module named {name!r}")
        exc.name = name
        raise exc

    monkeypatch.setattr(importlib, "import_module", missing)
    defaults = params_mod._price_finance_defaults()
    assert defaults == params_mod.FALLBACK_PRICE_FINANCE


def test_price_fact_source_internal_error_raised(monkeypatch):
    """模块存在但其内部依赖导入失败(ModuleNotFoundError 指向子依赖)→ 上抛,
    不得误判为兼容缺失(codex 二次审核 Medium-6)。"""
    import importlib

    import iesplan.finance.params as params_mod

    def internal_error(name):
        exc = ModuleNotFoundError("No module named 'iesplan.devices.pricing'")
        exc.name = "iesplan.devices.pricing._impl"  # 子依赖缺失, 非模块本身
        raise exc

    monkeypatch.setattr(importlib, "import_module", internal_error)
    with pytest.raises(ModuleNotFoundError):
        params_mod._price_finance_defaults()


# ---------------------------------------------------------------------------
# 门面与结果类型
# ---------------------------------------------------------------------------


def test_facade_exports():
    """finance 门面导出全部公共入口(03 §7.2)。"""
    from iesplan.finance import (
        IRRStatus as S,
        build_equity_cashflows as b,
        compute_financials as c,
        compute_lcoe as l,
        compute_payback as p,
        equity_irr as e,
        finance_params_from_config as f,
        project_npv as n,
    )

    assert S.unique == "unique"
    assert callable(b) and callable(c) and callable(l) and callable(p) and callable(e) and callable(f)
    assert callable(n)


def test_financial_result_dataclass_defaults():
    """FinancialResult.detail 默认空字典(evidence 块可序列化)。"""
    result = FinancialResult(
        irr=None,
        irr_status=IRRStatus.none,
        npv=Decimal("0"),
        payback_years=None,
        lcoe=None,
        capex=Decimal("0"),
        baseline_cost=Decimal("0"),
        annual_op_cost=Decimal("0"),
        annual_revenue=Decimal("0"),
        cashflows=[],
    )
    assert result.detail == {}
    assert result.npv == Decimal("0")
    assert result.irr_status == IRRStatus.none
