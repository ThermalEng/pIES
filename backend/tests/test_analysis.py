"""计算分析模块单元测试(03 §8,审查意见第 7 条):批量/敏感性/输出结构。

纯计算测试,不依赖 DB。覆盖:
  - apply_param 点路径改写(深拷贝/路径与单位校验);
  - run_sweep 单因子扫描(取值顺序/状态映射/异常容错/财务计算/参数覆盖);
  - run_batch 批量分析(多场景 × 多参数组合笛卡尔积);
  - summarize_sweep / summarize_batch 结构化输出(基准值/变化率/单调性/极值点);
  - rank_indicators / rank_parameters 影响排序;
  - build_analysis_payload / build_sensitivity_task_config 证据载荷;
  - check_financial 读 evidence financial 块(四维评估财务维);
  - 真实引擎端到端冒烟(evaluate_plan,4 步迷你算例)。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pytest

from iesplan.analysis import (
    AnalysisError,
    SweepSpec,
    apply_param,
    build_analysis_payload,
    build_sensitivity_task_config,
    change_rate,
    check_financial,
    rank_indicators,
    rank_parameters,
    run_batch,
    run_sweep,
    summarize_batch,
    summarize_sweep,
)
from iesplan.finance import (
    FinanceParams,
    compute_lcoe,
    compute_payback,
    finance_params_from_config,
)
from iesplan.analysis.assessment import FinancialValidity
from iesplan.core.timeaxis import TimeAxis, build_axis
from iesplan.engines.eval_run import evaluate_plan

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


def _axis() -> TimeAxis:
    """1h 标准年时间轴(8760 步)。"""
    return build_axis("1h")


def _make_engine(**behavior: object):
    """可控假引擎: kpi 与 plan 中 pv 容量线性相关(便于手算变化率)。

    behavior: {'fail_at': 容量阈值 → 该点 infeasible; 'raise_at': 容量阈值 → 抛异常}。
    口径: buy = 900 − 0.5×cap, gas = 100(元/年); flows 与 kpi 一致(交叉校验 0 偏差)。
    """

    def engine(plan: dict, data: dict, axis: TimeAxis, options: dict | None = None):
        cap = sum(
            float(dev.get("params", {}).get("rated_capacity_kwp", 0.0) or 0.0)
            for dev in plan.get("devices", [])
        )
        fail_at = behavior.get("fail_at")
        if fail_at is not None and cap >= float(fail_at):
            return SimpleNamespace(
                status="infeasible", kpi=None, flows=None, diagnostics=[], stop_reason="infeasible"
            )
        raise_at = behavior.get("raise_at")
        if raise_at is not None and cap >= float(raise_at):
            raise RuntimeError("engine boom")
        buy = 900.0 - 0.5 * cap
        gas = 100.0
        n = int(axis.n)
        kpi = {
            "annual_pv_kwh": 1000.0 * cap,
            "total_op_cost": Decimal(str(buy + gas)),
            "buy_cost": Decimal(str(buy)),
            "gas_cost": Decimal(str(gas)),
            "sell_revenue": Decimal("0"),
        }
        flows = {
            "cost_buy": np.full(n, buy / n),
            "cost_gas": np.full(n, gas / n),
            "revenue_sell": np.zeros(n),
        }
        return SimpleNamespace(
            status="ok", kpi=kpi, flows=flows, diagnostics=[], stop_reason="optimal"
        )

    return engine


def _engine_kpi(kpi_fn):
    """kpi 由容量自定义的假引擎(单调性/极值专项)。"""

    def engine(plan: dict, data: dict, axis: TimeAxis, options: dict | None = None):
        cap = sum(
            float(dev.get("params", {}).get("rated_capacity_kwp", 0.0) or 0.0)
            for dev in plan.get("devices", [])
        )
        return SimpleNamespace(
            status="ok", kpi=kpi_fn(cap), flows={}, diagnostics=[], stop_reason="optimal"
        )

    return engine


CAP_PATH = "device.pv1.params.rated_capacity_kwp"
DISCOUNT_PATH = "calc_config.params.discount_rate"


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
# run_sweep 单因子扫描
# ---------------------------------------------------------------------------


class TestRunSweep:
    def test_values_applied_in_order(self):
        spec = SweepSpec(CAP_PATH, (400.0, 500.0, 600.0))
        results = run_sweep(_content(), {}, _axis(), spec, engine=_make_engine())
        assert [r.param_value for r in results] == [400.0, 500.0, 600.0]
        # 引擎收到改写后的容量:kpi annual_pv_kwh = 1000 × cap
        assert [r.kpi["annual_pv_kwh"] for r in results] == [400000.0, 500000.0, 600000.0]
        assert all(r.status == "ok" for r in results)
        assert all(r.solver_status == "optimal" for r in results)

    def test_status_mapping_infeasible(self):
        spec = SweepSpec(CAP_PATH, (500.0, 650.0))
        results = run_sweep(_content(), {}, _axis(), spec, engine=_make_engine(fail_at=600.0))
        assert results[0].status == "ok"
        assert results[0].financial is not None
        assert results[1].status == "infeasible"
        assert results[1].kpi is None
        assert results[1].financial is None
        assert results[1].solver_status == "infeasible"

    def test_engine_exception_marks_error(self):
        spec = SweepSpec(CAP_PATH, (500.0, 700.0))
        results = run_sweep(_content(), {}, _axis(), spec, engine=_make_engine(raise_at=600.0))
        assert results[0].status == "ok"
        assert results[1].status == "error"
        assert results[1].kpi is None
        assert results[1].financial is None
        assert "engine boom" in results[1].solver_status

    def test_financial_computed_for_ok(self):
        spec = SweepSpec(CAP_PATH, (500.0,))
        result = run_sweep(_content(), {}, _axis(), spec, engine=_make_engine())[0]
        fin = result.financial
        assert fin is not None
        assert fin.capex == Decimal("1750000.0")  # 3500 × 500
        assert len(fin.cashflows) == 21  # CF0 + 20 年
        assert fin.irr is not None and fin.irr > 0
        assert str(fin.irr_status) == "unique"
        assert fin.npv > 0
        assert 0 < fin.payback_years < 20
        assert fin.baseline_cost == Decimal("800000.0")
        # 逐时费用列口径与 kpi 交叉校验 0 偏差(finance.cross_check 结构)
        cc = fin.detail["cross_check"]
        assert "flows_annual_op_cost" in cc
        assert float(cc["deviation"]) < 0.01

    def test_finance_params_override(self):
        spec = SweepSpec(CAP_PATH, (500.0,))
        engine = _make_engine()
        low = run_sweep(
            _content(), {}, _axis(), spec, engine=engine,
            finance_params=FinanceParams(discount_rate=Decimal("0.05")),
        )[0]
        mid = run_sweep(
            _content(), {}, _axis(), spec, engine=engine,
            finance_params=FinanceParams(discount_rate=Decimal("0.08")),
        )[0]
        high = run_sweep(
            _content(), {}, _axis(), spec, engine=engine,
            finance_params=FinanceParams(discount_rate=Decimal("0.15")),
        )[0]
        assert low.financial.npv > mid.financial.npv > high.financial.npv

    def test_finance_params_from_content_defaults(self):
        # 未传 finance_params 时从 content.calc_config 推导(discount_rate=0.08)
        spec = SweepSpec(CAP_PATH, (500.0,))
        result = run_sweep(_content(), {}, _axis(), spec, engine=_make_engine())[0]
        content_spec = SweepSpec(CAP_PATH, (500.0,))
        content_result = run_sweep(
            _content(),
            {},
            _axis(),
            content_spec,
            engine=_make_engine(),
            finance_params=FinanceParams(discount_rate=Decimal("0.08")),
        )[0]
        assert result.financial.npv == content_result.financial.npv

    def test_original_content_untouched(self):
        content = _content()
        run_sweep(
            content, {}, _axis(), SweepSpec(CAP_PATH, (400.0, 600.0)), engine=_make_engine()
        )
        assert content["model"]["devices"][0]["params"]["rated_capacity_kwp"] == 500.0

    def test_result_structure(self):
        spec = SweepSpec(CAP_PATH, (500.0,), unit="kW")
        result = run_sweep(_content(), {}, _axis(), spec, engine=_make_engine())[0]
        assert result.param_path == CAP_PATH
        assert result.param_value == 500.0
        assert result.unit == "kW"
        assert result.status == "ok"
        assert result.kpi is not None
        assert result.financial is not None
        assert result.solver_status == "optimal"


# ---------------------------------------------------------------------------
# run_batch 批量分析(多场景/多参数组合跑)
# ---------------------------------------------------------------------------


class TestRunBatch:
    def test_cartesian_combinations(self):
        sweeps = [
            SweepSpec(CAP_PATH, (400.0, 500.0)),
            SweepSpec(DISCOUNT_PATH, (0.05, 0.08, 0.10)),
        ]
        results = run_batch(_content(), {}, _axis(), sweeps, engine=_make_engine())
        assert len(results) == 6  # 2 × 3
        for r in results:
            assert r.scenario_index == 0
            assert r.status == "ok"
            assert CAP_PATH in r.param_values and DISCOUNT_PATH in r.param_values
        combos = {(r.param_values[CAP_PATH], r.param_values[DISCOUNT_PATH]) for r in results}
        assert combos == {
            (400.0, 0.05), (400.0, 0.08), (400.0, 0.10),
            (500.0, 0.05), (500.0, 0.08), (500.0, 0.10),
        }
        # 组合同时生效: kpi 反映容量,财务反映贴现率(0.05 组 npv 高于 0.10 组)
        by_discount = {r.param_values[DISCOUNT_PATH]: r for r in results if r.param_values[CAP_PATH] == 500.0}
        assert by_discount[0.05].financial.npv > by_discount[0.10].financial.npv

    def test_multi_scenario(self):
        scenario_b = _content()
        scenario_b["model"]["devices"][0]["params"]["unit_invest_cost"] = 4000.0
        results = run_batch(
            _content(), {}, _axis(), [SweepSpec(CAP_PATH, (400.0, 500.0))],
            scenarios=[_content(), scenario_b], engine=_make_engine(),
        )
        assert len(results) == 4  # 2 场景 × 2 取值
        assert {r.scenario_index for r in results} == {0, 1}
        # 场景 1 投资更高 → capex 更大
        cap_400 = [r for r in results if r.scenario_index == 1 and r.param_values[CAP_PATH] == 400.0][0]
        assert cap_400.financial.capex == Decimal("1600000.0")  # 4000 × 400

    def test_empty_sweeps_raises(self):
        with pytest.raises(AnalysisError):
            run_batch(_content(), {}, _axis(), [])

    def test_infeasible_combo_kept(self):
        results = run_batch(
            _content(), {}, _axis(), [SweepSpec(CAP_PATH, (500.0, 700.0))],
            engine=_make_engine(fail_at=600.0),
        )
        assert [r.status for r in results] == ["ok", "infeasible"]
        assert results[1].financial is None


# ---------------------------------------------------------------------------
# summarize_sweep 结构化输出
# ---------------------------------------------------------------------------


class TestSummarizeSweep:
    def _results(self, values=(400.0, 500.0, 600.0)):
        return run_sweep(_content(), {}, _axis(), SweepSpec(CAP_PATH, values), engine=_make_engine())

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
        engine = _engine_kpi(lambda cap: {"fixed": 42.0})
        spec = SweepSpec(CAP_PATH, (400.0, 500.0, 600.0))
        results = run_sweep(_content(), {}, _axis(), spec, engine=engine)
        assert summarize_sweep(results)["indicators"]["fixed"]["monotonicity"] == "flat"

    def test_monotonicity_non_monotonic(self):
        engine = _engine_kpi(lambda cap: {"wavy": (cap - 500.0) ** 2})
        spec = SweepSpec(CAP_PATH, (400.0, 500.0, 600.0))
        results = run_sweep(_content(), {}, _axis(), spec, engine=engine)
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
        results = run_sweep(
            _content(), {}, _axis(), SweepSpec(CAP_PATH, (400.0, 700.0)),
            engine=_make_engine(fail_at=600.0),
        )
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
        results = run_batch(
            _content(), {}, _axis(),
            [SweepSpec(CAP_PATH, (400.0, 600.0)), SweepSpec(DISCOUNT_PATH, (0.05, 0.10))],
            engine=_make_engine(),
        )
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
        spec = SweepSpec(CAP_PATH, (400.0, 500.0, 600.0))
        results = run_sweep(_content(), {}, _axis(), spec, engine=_make_engine())
        ranked = rank_indicators(results)
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
        results = run_sweep(
            _content(), {}, _axis(), SweepSpec(CAP_PATH, (400.0, 600.0)), engine=_make_engine()
        )
        ranked = rank_indicators(results, indicator_keys=("npv", "irr"))
        assert {d["indicator"] for d in ranked} == {"npv", "irr"}

    def test_rank_parameters(self):
        cap_results = run_sweep(
            _content(), {}, _axis(), SweepSpec(CAP_PATH, (400.0, 500.0, 600.0)),
            engine=_make_engine(),
        )
        disc_results = run_sweep(
            _content(), {}, _axis(), SweepSpec(DISCOUNT_PATH, (0.075, 0.08, 0.085)),
            engine=_make_engine(),
        )
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
        spec = SweepSpec(CAP_PATH, (400.0, 600.0))
        results = run_sweep(_content(), {}, _axis(), spec, engine=_make_engine())
        payload = build_analysis_payload(results)
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
        results = run_sweep(
            _content(), {}, _axis(), SweepSpec(CAP_PATH, (700.0,)),
            engine=_make_engine(fail_at=600.0),
        )
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
        # 端到端: run_sweep 的财务块可被四维评估消费(修复"财务恒 unknown")
        results = run_sweep(_content(), {}, _axis(), SweepSpec(CAP_PATH, (500.0,)), engine=_make_engine())
        payload = build_analysis_payload(results)
        level, checks = check_financial({"financial": payload["financial"]})
        assert level == FinancialValidity.passed
        assert checks["cashflows_len"] == 21


# ---------------------------------------------------------------------------
# 指标门面(indicators 转发)与最小财务实现
# ---------------------------------------------------------------------------


class TestIndicatorsFacade:
    def test_energy_balance_summary(self):
        from iesplan.analysis import energy_balance_summary

        summary = energy_balance_summary({"e_load": 1000.0, "p_grid_buy": 1000.0})
        assert summary["electric"]["residual_kwh"] == 0.0

    def test_operational_emissions(self):
        from iesplan.analysis import operational_emissions

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
# 真实引擎端到端冒烟(evaluate_plan,4 步迷你算例)
# ---------------------------------------------------------------------------


class TestRealEngineEndToEnd:
    """run_sweep 全链路(装配 → 引擎 → 财务)与真实 evaluate_plan 冒烟。"""

    def _content(self) -> dict:
        return {
            "model": {
                "devices": [
                    {
                        "device_type": "ies.device.grid_connection",
                        "name": "grid",
                        "kind": "existing",
                        "is_new": False,
                        "params": {"max_import_power_kw": 5.0, "max_export_power_kw": 0.0,
                                   "export_tariff": 0.35, "demand_charge": 0.0},
                    },
                    {
                        "device_type": "ies.device.electric_load",
                        "name": "load",
                        "kind": "existing",
                        "is_new": False,
                        "params": {"peak_power_kw": 1.0},
                    },
                ]
            },
            "calc_config": {
                "params": {"discount_rate": 0.08, "tax_rate": 0.25, "project_years": 20,
                           "depreciation_years": 10, "baseline_cost": 10000.0}
            },
        }

    def _data(self) -> dict:
        return {
            "e_load": np.array([1000.0, 1000.0, 1000.0, 1000.0]),
            "h_load": np.zeros(4),
            "c_load": np.zeros(4),
            "temperature": np.zeros(4),
            "ghi": np.zeros(4),
            "tariff_buy": np.array([0.3, 0.3, 1.1, 1.1]),
            "tariff_sell": 0.35,
            "gas_price": 3.2,
            "emission_factor_grid": 0.581,
            "emission_factor_gas": 2.0,
        }

    def _axis(self) -> TimeAxis:
        return TimeAxis(
            resolution="1h",
            n=4,
            step_minutes=60,
            utc_offset_minutes=480,
            t0_utc=datetime(2025, 1, 1, tzinfo=UTC),
            hour_of_year=np.arange(4, dtype=np.int64),
            day_of_year=np.zeros(4, dtype=np.int64),
            season=np.zeros(4, dtype=np.int64),
        )

    def test_sweep_import_limit(self):
        spec = SweepSpec("device.grid.params.max_import_power_kw", (2.0, 0.5))
        results = run_sweep(self._content(), self._data(), self._axis(), spec, engine=evaluate_plan)
        # 2 kW ≥ 1 kW 负荷 → 可行;0.5 kW < 1 kW 负荷 → 不可行(无电池/削减)
        assert results[0].status == "ok"
        assert results[0].kpi is not None and "total_op_cost" in results[0].kpi
        assert results[0].financial is not None
        assert len(results[0].financial.cashflows) == 21
        assert results[1].status == "infeasible"
        assert results[1].financial is None
