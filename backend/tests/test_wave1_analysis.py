"""Wave 1 W1-Analysis 解耦门禁测试: analysis 只消费计算结果与声明输出。

Wave 4-B 后覆盖:
  - analysis 包无 iesplan.engines / iesplan.services /
    iesplan.assembly.plan 导入(静态 + 懒导入, 与架构门禁 7 同口径);
  - 引擎驱动入口(run_sweep/run_batch/_local_plan/engine 调用/逐点财务执行)
    已从 analysis 门面移除(批量扇出与调度预留 application/0.8);
  - 纯结果消费路径(summarize/rank/payload/apply_param/
    build_sensitivity_task_config)只消费手工声明的不可变扫描点结果,
    无需计算执行即可工作(check_financial 覆盖见 test_analysis.py);
  - services 编排入口 run_sensitivity_analysis 已从 analysis 门面移除。

纯计算测试,不依赖 DB。
"""

from __future__ import annotations

import ast
from pathlib import Path

import iesplan.analysis as analysis_pkg
from iesplan.analysis import (
    SweepResult,
    SweepSpec,
    apply_param,
    build_analysis_payload,
    build_sensitivity_task_config,
    rank_indicators,
    rank_parameters,
    summarize_sweep,
)

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


def _results():
    """手工声明的不可变扫描点结果(调度侧产出形状, analysis 只消费, 无需计算执行)。"""
    return [
        SweepResult(
            param_path=CAP_PATH, param_value=v, unit="kWp", status="ok",
            kpi={"annual_pv_kwh": 1000.0 * v}, financial=None, solver_status="optimal",
        )
        for v in (400.0, 500.0)
    ]


class TestNoForbiddenDeps:
    def test_no_engines_services_assembly_plan_imports(self):
        assert _forbidden_imports() == []

    def test_services_orchestration_removed_from_facade(self):
        assert not hasattr(analysis_pkg, "run_sensitivity_analysis")


class TestEngineDrivingRemoved:
    """Wave 4-B: 引擎驱动入口已从门面移除(批量扇出与调度预留 application/0.8)。"""

    def test_sweep_batch_entrypoints_removed(self):
        assert not hasattr(analysis_pkg, "run_sweep")
        assert not hasattr(analysis_pkg, "run_batch")

    def test_plan_and_finance_helpers_removed(self):
        import iesplan.analysis.wrapper as wrapper

        assert not hasattr(wrapper, "_local_plan")
        assert not hasattr(wrapper, "project_financial_inputs")
        assert not hasattr(analysis_pkg, "project_financial_inputs")
        assert not hasattr(analysis_pkg, "CAPACITY_KEYS")


class TestPureResultConsumption:
    def test_summarize_and_rank_without_compute(self):
        results = _results()
        summary = summarize_sweep(results)
        assert summary["indicators"]["annual_pv_kwh"]["base_value"] == 400000.0
        ranked = rank_indicators(results)
        assert ranked[0]["indicator"] == "annual_pv_kwh"
        by_param = rank_parameters({CAP_PATH: results})
        assert by_param[0]["param_path"] == CAP_PATH

    def test_payload_without_compute(self):
        payload = build_analysis_payload(_results())
        assert payload["result_kind"] == "analysis_result"
        assert len(payload["sweeps"]) == 2

    def test_apply_param_and_task_config_without_compute(self):
        modified = apply_param(_content(), CAP_PATH, 600.0)
        assert modified["model"]["devices"][0]["params"]["rated_capacity_kwp"] == 600.0
        config = build_sensitivity_task_config([SweepSpec(CAP_PATH, (400.0, 500.0))])
        assert config["sweeps"][0]["param_path"] == CAP_PATH

    def test_hand_built_results_need_no_compute(self):
        # 手工声明输出(ComputeResult 风格)直接可聚合: analysis 只消费结果
        summary = summarize_sweep(_results())
        assert summary["indicators"]["annual_pv_kwh"]["extremum"]["max"]["value"] == 500000.0
