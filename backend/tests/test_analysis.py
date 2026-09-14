"""计算分析模块单元测试(03 §8,审查意见第 7 条):批量/敏感性/输出结构。

纯计算测试,不依赖 DB。覆盖:
  - apply_param 点路径改写(深拷贝/路径与单位校验);
  - summarize_sweep / summarize_batch 结构化输出(基准值/变化率/单调性/极值点);
  - rank_indicators / rank_parameters 影响排序;
  - build_analysis_payload / build_sensitivity_task_config 证据载荷;
  - check_financial 读 evidence financial 块(四维评估财务维)。

Wave 4-B 后 analysis 只消费不可变扫描点结果、不执行计算: 本文件所有
SweepResult/BatchResult 均为手工声明输出(调度侧在 application/0.8 产出);
财务块由测试经 finance 公开 API 预先构建(finance 自身覆盖见
test_finance.py),analysis 侧只做纯聚合。引擎驱动路径(run_sweep/run_batch
的 plan 自装与 engine 调用)已删除,不再覆盖。
"""

from __future__ import annotations

import json
from decimal import Decimal

import numpy as np
import pytest

from iesplan.analysis import (
    AnalysisError,
    BatchResult,
    SweepResult,
    SweepSpec,
    apply_param,
    build_analysis_payload,
    build_sensitivity_task_config,
    change_rate,
    rank_indicators,
    rank_parameters,
    summarize_batch,
    summarize_sweep,
)
from iesplan.metrics.validity import FinancialValidity
from iesplan.results import check_financial
from iesplan.finance import (
    FinanceParams,
    compute_financials,
    compute_lcoe,
    compute_payback,
    finance_params_from_config,
)

# ---------------------------------------------------------------------------
# 公共夹具
# ---------------------------------------------------------------------------


def _content(**overrides: object) -> dict:
    """最小项目内容: 新增光伏(500 kWp, 3500 CNY/kWp)+ 经济参数(基准成本 80 万元/年)。"""
    content: dict = {
        "model": {
            "devices": [
                {
                    "device_type": "ies.device.pv",
                    "name": "pv1",
                    "kind": "new",
                    "is_new": True,
                    "params": {"rated_capacity_kwp": 500.0, "unit_invest_cost": 3500.0},
                }
            ]
        },
        "calc_config": {
            "params": {
                "discount_rate": 0.08,
                "tax_rate": 0.25,
                "project_years": 20,
                "depreciation_years": 10,
                "baseline_cost": 800000.0,
            },
            "irr_floor": 0.08,
        },
    }
    content.update(overrides)
    return content


CAP_PATH = "device.pv1.params.rated_capacity_kwp"
DISCOUNT_PATH = "calc_config.params.discount_rate"


def _kpi(cap: float) -> dict:
    """手工扫描点 KPI: 与容量线性相关(便于手算变化率)。

    口径沿用旧假引擎: buy = 900 − 0.5×cap, gas = 100(元/年)。
    """
    buy, gas = 900.0 - 0.5 * cap, 100.0
    return {
        "annual_pv_kwh": 1000.0 * cap,
        "total_op_cost": Decimal(str(buy + gas)),
        "buy_cost": Decimal(str(buy)),
        "gas_cost": Decimal(str(gas)),
        "sell_revenue": Decimal("0"),
    }


def _fin_block(cap: float, discount_rate: float = 0.08):
    """测试辅助: 经 finance 公开 API 为扫描点预先构建不可变财务块。

    analysis 只消费该块、不执行计算(capex = 3500 × cap, 基准成本 80 万元/年;
    finance 自身覆盖见 test_finance.py)。
    """
    buy, gas = 900.0 - 0.5 * cap, 100.0
    n = 24
    flows = {
        "cost_buy": np.full(n, buy / n),
        "cost_gas": np.full(n, gas / n),
        "revenue_sell": np.zeros(n),
    }
    return compute_financials(
        _kpi(cap),
        flows,
        Decimal(str(3500.0 * cap)),
        Decimal("800000.0"),
        FinanceParams(discount_rate=Decimal(str(discount_rate))),
    )


def _ok_point(
    param_path: str,
    value: float,
    *,
    kpi: dict | None = None,
    financial=None,
    unit: str = "",
    solver_status: str = "optimal",
) -> SweepResult:
    """手工声明的 ok 扫描点(调度侧产出形状, analysis 只消费)。"""
    return SweepResult(
        param_path=param_path,
        param_value=value,
        unit=unit,
        status="ok",
        kpi=kpi,
        financial=financial,
        solver_status=solver_status,
    )


def _sweep_results(
    values: tuple[float, ...] = (400.0, 500.0, 600.0), *, param_path: str = CAP_PATH
) -> list[SweepResult]:
    """手工单因子扫描点序列: kpi 与容量线性相关、财务块预先构建(便于手算变化率)。"""
    return [_ok_point(param_path, v, kpi=_kpi(v), financial=_fin_block(v)) for v in values]


# ---------------------------------------------------------------------------
# apply_param 点路径改写
# ---------------------------------------------------------------------------


class TestApplyParam:
    def test_deep_copy_keeps_original(self):
        content = _content()
        new = apply_param(content, DISCOUNT_PATH, 0.10)
        assert new is not content
        assert new["calc_config"]["params"]["discount_rate"] == 0.10
        assert content["calc_config"]["params"]["discount_rate"] == 0.08  # 原值不变

    def test_set_calc_config_param(self):
        new = apply_param(_content(), "calc_config.params.discount_rate", 0.05)
        assert new["calc_config"]["params"]["discount_rate"] == 0.05

    def test_set_calc_config_direct_key(self):
        new = apply_param(_content(), "calc_config.irr_floor", 0.10)
        assert new["calc_config"]["irr_floor"] == 0.10

    def test_set_named_device_param(self):
        new = apply_param(_content(), CAP_PATH, 650.0)
        assert new["model"]["devices"][0]["params"]["rated_capacity_kwp"] == 650.0

    def test_set_indexed_device_param(self):
        new = apply_param(_content(), "model.devices.0.params.unit_invest_cost", 3200.0)
        assert new["model"]["devices"][0]["params"]["unit_invest_cost"] == 3200.0

    def test_set_data_scalar(self):
        new = apply_param(_content(data={"gas_price": 3.2}), "data.gas_price", 3.0)
        assert new["data"]["gas_price"] == 3.0

    def test_missing_path_raises(self):
        with pytest.raises(AnalysisError):
            apply_param(_content(), "calc_config.params.no_such_param", 1.0)

    def test_missing_device_raises(self):
        with pytest.raises(AnalysisError):
            apply_param(_content(), "device.nonexistent.params.rated_capacity_kwp", 1.0)

    def test_unknown_unit_raises(self):
        with pytest.raises(AnalysisError):
            apply_param(_content(), CAP_PATH, 1.0, unit="furlong")

    def test_known_unit_accepted(self):
        new = apply_param(_content(), CAP_PATH, 1.0, unit="kW")
        assert new["model"]["devices"][0]["params"]["rated_capacity_kwp"] == 1.0

    def test_non_finite_value_raises(self):
        with pytest.raises(AnalysisError):
            apply_param(_content(), CAP_PATH, float("nan"))
        with pytest.raises(AnalysisError):
            apply_param(_content(), CAP_PATH, float("inf"))

    def test_bad_prefix_raises(self):
        with pytest.raises(AnalysisError):
            apply_param(_content(), "other.path.value", 1.0)


# ---------------------------------------------------------------------------
# 引擎驱动已删除(Wave 4-B): run_sweep/run_batch 的 plan 自装(_local_plan)与
# engine 调用随"生成多个计算请求并调度"职责移交 application/0.8, analysis
# 只消费不可变扫描点结果。此处锁定执行入口已从门面移除, 聚合覆盖见下文。
# ---------------------------------------------------------------------------


class TestEngineDrivingRemoved:
    def test_sweep_batch_entrypoints_removed_from_facade(self):
        import iesplan.analysis as analysis_pkg

        assert not hasattr(analysis_pkg, "run_sweep")
        assert not hasattr(analysis_pkg, "run_batch")
        assert not hasattr(analysis_pkg, "project_financial_inputs")
        assert not hasattr(analysis_pkg, "CAPACITY_KEYS")


# ---------------------------------------------------------------------------
# summarize_sweep 结构化输出
# ---------------------------------------------------------------------------


class TestSummarizeSweep:
    def _results(self, values=(400.0, 500.0, 600.0)):
        return _sweep_results(values)

    def test_base_value_and_change_rate(self):
        summary = summarize_sweep(self._results())
        assert summary["param_path"] == CAP_PATH
        assert summary["base_value"] == 400.0
        ind = summary["indicators"]["annual_pv_kwh"]
        assert ind["base_value"] == 400000.0
        rates = {p["param_value"]: p["change_rate"] for p in ind["points"]}
        assert rates[500.0] == pytest.approx(0.25)
        assert rates[600.0] == pytest.approx(0.5)

    def test_monotonicity(self):
        summary = summarize_sweep(self._results())
        assert summary["indicators"]["annual_pv_kwh"]["monotonicity"] == "increasing"
        assert summary["indicators"]["total_op_cost"]["monotonicity"] == "decreasing"

    def test_monotonicity_flat(self):
        results = [
            _ok_point(CAP_PATH, v, kpi={"fixed": 42.0}) for v in (400.0, 500.0, 600.0)
        ]
        assert summarize_sweep(results)["indicators"]["fixed"]["monotonicity"] == "flat"

    def test_monotonicity_non_monotonic(self):
        results = [
            _ok_point(CAP_PATH, v, kpi={"wavy": (v - 500.0) ** 2})
            for v in (400.0, 500.0, 600.0)
        ]
        summary = summarize_sweep(results)
        assert summary["indicators"]["wavy"]["monotonicity"] == "non_monotonic"
        # 极值点: 谷底在 500
        assert summary["indicators"]["wavy"]["extremum"]["min"]["param_value"] == 500.0

    def test_extremum(self):
        summary = summarize_sweep(self._results())
        ext = summary["indicators"]["annual_pv_kwh"]["extremum"]
        assert ext["max"] == {"param_value": 600.0, "value": 600000.0}
        assert ext["min"] == {"param_value": 400.0, "value": 400000.0}

    def test_financial_indicator_units(self):
        summary = summarize_sweep(self._results())
        assert summary["indicators"]["npv"]["unit"] == "CNY"
        assert summary["indicators"]["irr"]["unit"] == "-"
        assert summary["indicators"]["payback_years"]["unit"] == "a"

    def test_infeasible_points_excluded_from_indicator_points(self):
        results = [
            _ok_point(CAP_PATH, 400.0, kpi=_kpi(400.0), financial=_fin_block(400.0)),
            SweepResult(
                param_path=CAP_PATH, param_value=700.0, unit="", status="infeasible",
                kpi=None, financial=None, solver_status="infeasible",
            ),
        ]
        summary = summarize_sweep(results)
        assert summary["results"][1]["status"] == "infeasible"  # 原始行保留
        points = summary["indicators"]["annual_pv_kwh"]["points"]
        assert [p["param_value"] for p in points] == [400.0]

    def test_empty_results(self):
        summary = summarize_sweep([])
        assert summary["results"] == []
        assert summary["indicators"] == {}

    def test_json_serializable(self):
        summary = summarize_sweep(self._results())
        json.dumps(summary)  # Decimal 金额键已转 float


# ---------------------------------------------------------------------------
# summarize_batch
# ---------------------------------------------------------------------------


class TestSummarizeBatch:
    def test_rows_and_extremum(self):
        # 手工批量扫描点(容量 × 贴现率组合, 调度侧产出形状, analysis 只消费)
        results = [
            BatchResult(
                scenario_index=0,
                param_values={CAP_PATH: cap, DISCOUNT_PATH: discount},
                status="ok",
                kpi=_kpi(cap),
                financial=_fin_block(cap, discount_rate=discount),
                solver_status="optimal",
            )
            for cap in (400.0, 600.0)
            for discount in (0.05, 0.10)
        ]
        summary = summarize_batch(results)
        assert summary["runs"] == 4
        assert summary["scenarios"] == 1
        assert len(summary["rows"]) == 4
        row = summary["rows"][0]
        assert row["scenario_index"] == 0
        assert row["param_values"][CAP_PATH] in (400.0, 600.0)
        assert "annual_pv_kwh" in row["indicators"]
        ind = summary["indicators"]["annual_pv_kwh"]
        assert ind["unit"] == "-"
        assert ind["max"]["value"] == 600000.0
        assert ind["max"]["scenario_index"] == 0
        assert ind["min"]["value"] == 400000.0


# ---------------------------------------------------------------------------
# 敏感性分析:变化率与影响排序
# ---------------------------------------------------------------------------


class TestSensitivity:
    def test_change_rate_zero_base_returns_none(self):
        assert change_rate(0.0, 5.0) is None
        assert change_rate(100.0, 110.0) == pytest.approx(0.1)
        assert change_rate(100.0, 90.0) == pytest.approx(-0.1)

    def test_rank_indicators_order(self):
        ranked = rank_indicators(_sweep_results((400.0, 500.0, 600.0)))
        impacts = [d["impact"] for d in ranked]
        assert impacts == sorted(impacts, reverse=True)  # 按影响度降序
        assert ranked[0]["indicator"] == "annual_pv_kwh"
        assert ranked[0]["impact"] == pytest.approx(0.5)
        assert ranked[0]["direction"] == "positive"
        names = {d["indicator"] for d in ranked}
        assert {"annual_pv_kwh", "total_op_cost", "npv", "irr", "payback_years"} <= names
        rank_of = {d["indicator"]: i for i, d in enumerate(ranked)}
        assert rank_of["annual_pv_kwh"] < rank_of["irr"] < rank_of["total_op_cost"]
        # 恒定指标(gas_cost)影响度为 0 且 direction flat
        gas = next(d for d in ranked if d["indicator"] == "gas_cost")
        assert gas["impact"] == 0.0
        assert gas["direction"] == "flat"

    def test_rank_indicators_filtered(self):
        ranked = rank_indicators(_sweep_results((400.0, 600.0)), indicator_keys=("npv", "irr"))
        assert {d["indicator"] for d in ranked} == {"npv", "irr"}

    def test_rank_parameters(self):
        cap_results = _sweep_results((400.0, 500.0, 600.0))
        # 贴现率扫描: kpi 与容量(500)无关, 仅财务块随贴现率变化(原 engine 语义)
        disc_results = [
            _ok_point(
                DISCOUNT_PATH, d, kpi=_kpi(500.0),
                financial=_fin_block(500.0, discount_rate=d),
            )
            for d in (0.075, 0.08, 0.085)
        ]
        ranked = rank_parameters({CAP_PATH: cap_results, DISCOUNT_PATH: disc_results})
        assert [d["param_path"] for d in ranked] == [CAP_PATH, DISCOUNT_PATH]  # 容量影响更大
        assert ranked[0]["top_indicator"] == "annual_pv_kwh"
        assert ranked[0]["impact"] == pytest.approx(0.5)
        assert ranked[1]["top_indicator"] == "npv"  # 贴现率只影响财务指标
        assert ranked[1]["impact"] > 0
        # 变化率相对基准 0.075:贴现率升高 → npv 降低 → 方向 negative
        assert ranked[1]["direction"] == "negative"

    def test_rank_parameters_empty(self):
        assert rank_parameters({}) == []


# ---------------------------------------------------------------------------
# 证据载荷(结构化输出)
# ---------------------------------------------------------------------------


class TestPayload:
    def test_build_analysis_payload_structure(self):
        payload = build_analysis_payload(_sweep_results((400.0, 600.0)))
        assert payload["result_kind"] == "analysis_result"
        assert len(payload["sweeps"]) == 2
        row = payload["sweeps"][0]
        assert set(row) == {
            "param_path", "param_value", "unit", "status", "kpi", "financial", "solver_status"
        }
        assert row["financial"] is not None
        assert row["financial"]["investment"] == 1400000.0  # 3500 × 400
        assert "cashflows" in row["financial"]
        assert payload["summary"]["indicators"]["annual_pv_kwh"]["base_value"] == 400000.0
        assert payload["sensitivity"]["rank_indicators"][0]["indicator"] == "annual_pv_kwh"
        assert payload["financial"] is not None  # 基准点财务块(供四维评估)
        json.dumps(payload)  # 全载荷可 JSON 序列化(evidence 落库)

    def test_payload_with_infeasible(self):
        results = [
            SweepResult(
                param_path=CAP_PATH, param_value=700.0, unit="", status="infeasible",
                kpi=None, financial=None, solver_status="infeasible",
            )
        ]
        payload = build_analysis_payload(results)
        assert payload["sweeps"][0]["status"] == "infeasible"
        assert payload["financial"] is None  # 无 ok 结果 → 财务块缺省

    def test_build_sensitivity_task_config(self):
        config = build_sensitivity_task_config(
            [SweepSpec(CAP_PATH, (400.0, 500.0), unit="kWp")], base_config={"note": "x"}
        )
        assert config["sweeps"] == [
            {"param_path": CAP_PATH, "values": [400.0, 500.0], "unit": "kWp"}
        ]
        assert config["base_config"] == {"note": "x"}
        json.dumps(config)

    def test_build_sensitivity_task_config_empty_raises(self):
        with pytest.raises(AnalysisError):
            build_sensitivity_task_config([])


# ---------------------------------------------------------------------------
# 四维评估财务维(check_financial 读 evidence financial 块)
# ---------------------------------------------------------------------------


class TestCheckFinancial:
    def test_passed_unique(self):
        level, checks = check_financial(
            {"financial": {"irr": 0.12, "irr_status": "unique", "npv": 1000.0, "cashflows": [1, 2]}}
        )
        assert level == FinancialValidity.passed
        assert checks["cashflows_len"] == 2
        assert checks["irr"] == 0.12

    def test_missing_block_insufficient(self):
        level, checks = check_financial({})
        assert level == FinancialValidity.insufficient
        assert checks["reason"] == "missing_financial"

    def test_invalid_status_insufficient(self):
        level, _ = check_financial({"financial": {"irr_status": "bogus"}})
        assert level == FinancialValidity.insufficient

    def test_multiple_restricted(self):
        level, checks = check_financial({"financial": {"irr_status": "multiple"}})
        assert level == FinancialValidity.restricted
        assert checks["irr_status"] == "multiple"

    def test_payload_financial_block_consumable(self):
        # 消费链: 调度侧随点提供的财务块经 payload 可被四维评估消费(修复"财务恒 unknown")
        payload = build_analysis_payload(_sweep_results((500.0,)))
        level, checks = check_financial({"financial": payload["financial"]})
        assert level == FinancialValidity.passed
        assert checks["cashflows_len"] == 21


# ---------------------------------------------------------------------------
# 指标门面(indicators 转发)与最小财务实现
# ---------------------------------------------------------------------------


# Wave 4-C: analysis 指标转发门面已删除,实现唯一归属 metrics 域;
# 此处直接消费 metrics 权威实现,不再经 analysis 转发。
class TestIndicatorsMetricsAuthority:
    def test_energy_balance_summary(self):
        from iesplan.metrics.engineering import energy_balance_summary

        summary = energy_balance_summary({"e_load": 1000.0, "p_grid_buy": 1000.0})
        assert summary["electric"]["residual_kwh"] == 0.0

    def test_operational_emissions(self):
        from iesplan.metrics.environmental import operational_emissions

        result = operational_emissions(
            {"grid_purchase": 100.0, "gas": 10.0},
            {"grid_purchase": 0.581, "gas": 2.0},
            boundary="scope1+scope2",
            factor_version="2024-v1.0",
        )
        assert result["total_kg"] == pytest.approx(100.0 * 0.581 + 10.0 * 2.0)
        assert result["boundary"] == "scope1+scope2"


class TestMinFinance:
    def test_compute_payback_hand_calc(self):
        flows = [Decimal("-1000"), Decimal("300"), Decimal("300"), Decimal("300"), Decimal("300")]
        assert compute_payback(flows) == pytest.approx(3.333333, abs=1e-6)

    def test_compute_payback_never(self):
        assert compute_payback([Decimal("-1000"), Decimal("-100")]) is None

    def test_compute_payback_immediate(self):
        # [-1000, +2000]: 0 年末累计 -1000,第 1 年转正 → 0 + 1000/2000 = 0.5 年
        assert compute_payback([Decimal("-1000"), Decimal("2000")]) == pytest.approx(0.5)

    def test_compute_lcoe(self):
        assert compute_lcoe(Decimal("100"), Decimal("1000")) == Decimal("0.1")

    def test_compute_lcoe_zero_energy(self):
        assert compute_lcoe(Decimal("100"), Decimal("0")) is None

    def test_finance_params_from_config(self):
        params = finance_params_from_config({"params": {"discount_rate": 0.05}, "irr_floor": 0.06})
        assert params.discount_rate == Decimal("0.05")
        assert params.irr_floor == Decimal("0.06")
        assert params.tax_rate == Decimal("0.25")  # 缺省

    def test_financial_params_defaults(self):
        params = finance_params_from_config({})
        assert params.discount_rate == Decimal("0.08")
        assert params.project_years == 20


# ---------------------------------------------------------------------------
# 真实引擎端到端冒烟已删除(Wave 4-B): analysis 不再驱动引擎; 旧 engines
# 计算模块与 test_eval_run.py 已同步删除, 批量扇出待 computation 接入。
# ---------------------------------------------------------------------------
