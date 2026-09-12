"""Wave 1 W1-Analysis 解耦门禁测试: analysis 只消费计算结果与声明输出。

覆盖:
  - analysis 包无 iesplan.engines / iesplan.services /
    iesplan.assembly.plan 导入(静态 + 懒导入, 与架构门禁 7 同口径);
  - run_sweep / run_batch 缺计算结果提供者(engine=None)时抛显式未实现
    错误(AnalysisError, ANA-ENGINE-001),不 fallback、不静默;
  - 调用方注入的计算边界仍可聚合(注入式 engine 正常产出 Sweep/Batch 结果);
  - 纯结果消费路径(summarize/rank/payload/check_financial/apply_param/
    build_sensitivity_task_config)无需计算执行即可工作;
  - services 编排入口 run_sensitivity_analysis 已从 analysis 门面移除。

纯计算测试,不依赖 DB。
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import iesplan.analysis as analysis_pkg
from iesplan.analysis import (
    AnalysisError,
    SweepResult,
    SweepSpec,
    apply_param,
    build_analysis_payload,
    build_sensitivity_task_config,
    rank_indicators,
    rank_parameters,
    run_batch,
    run_sweep,
    summarize_sweep,
)
from iesplan.analysis.assessment import check_financial
from iesplan.core.timeaxis import build_axis

CAP_PATH = "device.pv1.params.rated_capacity_kwp"

_ANALYSIS_DIR = Path(__file__).resolve().parent.parent / "iesplan" / "analysis"


def _forbidden_imports() -> list[tuple[str, int, str]]:
    """扫描 analysis 包全部 engines/services/assembly.plan 导入(含函数内懒导入)。"""
    found: list[tuple[str, int, str]] = []
    for path in sorted(_ANALYSIS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            elif isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            for mod in modules:
                if (
                    mod == "iesplan.engines"
                    or mod.startswith("iesplan.engines.")
                    or mod == "iesplan.services"
                    or mod.startswith("iesplan.services.")
                    or mod == "iesplan.assembly.plan"
                    or mod.startswith("iesplan.assembly.plan.")
                ):
                    found.append((path.name, node.lineno, mod))
    return found


def _content() -> dict:
    return {
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


def _fake_engine(plan: dict, data: dict, axis, options: dict | None = None):
    """调用方注入的计算结果提供者: kpi 与 plan 中 pv 容量线性相关。"""
    cap = sum(
        float(dev.get("params", {}).get("rated_capacity_kwp", 0.0) or 0.0)
        for dev in plan.get("devices", [])
    )
    n = int(axis.n)
    buy, gas = 900.0 - 0.5 * cap, 100.0
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
    return SimpleNamespace(status="ok", kpi=kpi, flows=flows, diagnostics=[], stop_reason="optimal")


class TestNoForbiddenDeps:
    def test_no_engines_services_assembly_plan_imports(self):
        assert _forbidden_imports() == []

    def test_services_orchestration_removed_from_facade(self):
        assert not hasattr(analysis_pkg, "run_sensitivity_analysis")


class TestExplicitUnimplemented:
    def test_run_sweep_without_engine_raises(self):
        with pytest.raises(AnalysisError) as excinfo:
            run_sweep(_content(), {}, build_axis("1h"), SweepSpec(CAP_PATH, (400.0, 500.0)))
        assert excinfo.value.code == "ANA-ENGINE-001"

    def test_run_batch_without_engine_raises(self):
        with pytest.raises(AnalysisError) as excinfo:
            run_batch(
                _content(), {}, build_axis("1h"), [SweepSpec(CAP_PATH, (400.0, 500.0))]
            )
        assert excinfo.value.code == "ANA-ENGINE-001"

    def test_missing_engine_never_silently_falls_back(self):
        # 缺计算结果时不得产出任何结果行: 要么显式错误,要么不存在第三种路径
        with pytest.raises(AnalysisError):
            run_sweep(_content(), {}, build_axis("1h"), SweepSpec(CAP_PATH, (500.0,)))
        with pytest.raises(AnalysisError):
            run_batch(_content(), {}, build_axis("1h"), [SweepSpec(CAP_PATH, (500.0,))])


class TestInjectedComputeBoundary:
    def test_run_sweep_aggregates_injected_results(self):
        results = run_sweep(
            _content(), {}, build_axis("1h"), SweepSpec(CAP_PATH, (400.0, 500.0)),
            engine=_fake_engine,
        )
        assert [r.status for r in results] == ["ok", "ok"]
        assert results[0].financial is not None
        assert results[1].kpi["annual_pv_kwh"] == 500000.0

    def test_run_batch_aggregates_injected_results(self):
        results = run_batch(
            _content(), {}, build_axis("1h"), [SweepSpec(CAP_PATH, (400.0, 500.0))],
            engine=_fake_engine,
        )
        assert len(results) == 2
        assert all(r.status == "ok" for r in results)


class TestPureResultConsumption:
    def _results(self):
        return run_sweep(
            _content(), {}, build_axis("1h"), SweepSpec(CAP_PATH, (400.0, 500.0)),
            engine=_fake_engine,
        )

    def test_summarize_and_rank_without_engine(self):
        results = self._results()
        summary = summarize_sweep(results)
        assert summary["indicators"]["annual_pv_kwh"]["base_value"] == 400000.0
        ranked = rank_indicators(results)
        assert ranked[0]["indicator"] == "annual_pv_kwh"
        by_param = rank_parameters({CAP_PATH: results})
        assert by_param[0]["param_path"] == CAP_PATH

    def test_payload_and_check_financial_without_engine(self):
        payload = build_analysis_payload(self._results())
        assert payload["result_kind"] == "analysis_result"
        assert payload["financial"] is not None
        from iesplan.analysis.assessment import FinancialValidity

        level, _ = check_financial({"financial": payload["financial"]})
        assert level == FinancialValidity.passed

    def test_apply_param_and_task_config_without_engine(self):
        modified = apply_param(_content(), CAP_PATH, 600.0)
        assert modified["model"]["devices"][0]["params"]["rated_capacity_kwp"] == 600.0
        config = build_sensitivity_task_config([SweepSpec(CAP_PATH, (400.0, 500.0))])
        assert config["sweeps"][0]["param_path"] == CAP_PATH

    def test_hand_built_results_need_no_compute(self):
        # 手工声明输出(ComputeResult 风格)直接可聚合: analysis 只消费结果
        made = [
            SweepResult(
                param_path=CAP_PATH, param_value=v, unit="kWp", status="ok",
                kpi={"annual_pv_kwh": 1000.0 * v}, financial=None, solver_status="optimal",
            )
            for v in (400.0, 500.0)
        ]
        summary = summarize_sweep(made)
        assert summary["indicators"]["annual_pv_kwh"]["extremum"]["max"]["value"] == 500000.0
