"""E5b 参考制品库与 1.2.0 版本收口测试。

覆盖：
1. `iesplan.__version__ == "1.2.0"` 与 `pies_job_runner` `app.version == "1.2.0"`；
2. 4 个参考设备模型（chp / thermal_storage / absorption_chiller / wind_turbine）校验与多能互补装配真实求解；
3. 4 个参考 Python 插件（seasonal_joint / highspy_ref / economic_dispatch / carbon_reliability）
   `pies_sdk.validate_plugin_package(..., require_market_fields=True)` 全量离线自检；
4. 真实执行与数值比对：`highspy_ref` 与内置 `ies.solver.highs@1.7.2` 同包同解（`1e-6`），
   `economic_dispatch` 迭代日志 + 工程解投影，`carbon_reliability` 报告校验 + Excel 导出。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import iesplan
import iesplan.audit.persistence  # noqa: F401
import iesplan.configuration.persistence  # noqa: F401
import iesplan.dataset.persistence  # noqa: F401
import iesplan.ecosystem.persistence  # noqa: F401
import iesplan.identity.persistence  # noqa: F401
import iesplan.model.persistence  # noqa: F401
import iesplan.package.persistence  # noqa: F401
import iesplan.project.persistence  # noqa: F401
import iesplan.results.persistence  # noqa: F401
import iesplan.storage.persistence  # noqa: F401
import iesplan.tasks.persistence  # noqa: F401
from iesplan import identity as identity_domain
from iesplan import project as project_domain
from iesplan.application.packages.transfers import export_excel
from iesplan.assembly import validate_assembly_doc
from iesplan.computation import (
    PRIMAL_SOLUTION_OUTPUT_PATH,
    CalculationConfig,
    MaterializedSet,
    generate_highs_bundle,
    materialize_non_predictive_series,
)
from iesplan.computation.bundle import export_bundle_files
from iesplan.computation.contracts.outputs import validate_evaluation_report_doc
from iesplan.computation.generators.acceleration import ACCELERATION_PATH
from iesplan.computation.generators.highs_milp import EPIGRAPHS_PATH
from iesplan.computation.result_adapters import (
    PRIMAL_SOLUTION_SCHEMA,
    PRIMAL_SOLUTION_VERSION,
    ResultAdapter,
    project_engineering_solution,
)
from iesplan.config import settings as app_settings
from iesplan.core.content import MemoryContent, read_content
from iesplan.core.yamlmini import load as yaml_load
from iesplan.db import Base
from iesplan.devices import init_registry, list_devices
from iesplan.devices.parser2 import parse_device_model_v2
from iesplan.finance import EvaluationInput, effective_from_yaml
from iesplan.identity import UserRecord
from iesplan.modeling import build_math_contribution
from iesplan.results.persistence import EvidencePackage, ResultAssessment, ResultIndex
from iesplan.storage import put_object
from iesplan.storage import service as storage_service
from iesplan.storage.adapters.filesystem import FileSystemBlobStore
from iesplan.tasks.persistence import CalcSnapshot, Task

HIGHS_EXECUTABLE = Path("/usr/local/bin/ies-highs-solver")

_DEVICE_IDS = (
    "ies.device.chp",
    "ies.device.thermal_storage",
    "ies.device.absorption_chiller",
    "ies.device.wind_turbine",
)
_PLUGIN_IDS = (
    "ies.predictor.seasonal_joint",
    "ies.solver.highspy_ref",
    "ies.algorithm.economic_dispatch",
    "ies.evaluator.carbon_reliability",
)

_GAS_SOURCE_YAML = """
schema: ies.device-model
schema_version: "1.0.0"
device:
  id: acme.device.gas_source
  names:
    zh-CN: 天然气气源
    en-US: Natural Gas Supply
properties:
  max_flow_m3h:
    value: 5000
    unit: m3/h
    valid_range:
      minimum: 0
      maximum: 100000
interfaces:
  gas_out:
    type: out
    carrier: gas
    unit: m3/h
    valid_range:
      minimum: 0
      maximum: {property_ref: max_flow_m3h}
equations:
  variables: {}
  relations: []
"""

_MULTI_ENERGY_FINANCE_YAML = """
schema: ies.effective-finance-config
schema_version: "1.0.0"
profile_id: e5b-ref-finance
currency: CNY
base_year: 2025
price_basis: tax_inclusive
cost_method: fixed_plus_linear
demand_charges: {}
taxes: {}
credit_pools: {}
tax_rules: {}
net_cashflows: {}
finance_types:
  chp_sys:
    upfront_capex:
      linear:
        rated_kw:
          unit_cost:
            value: "100"
            unit: CNY/kW
energy_prices:
  grid_import:
    carrier: electricity
    direction: purchase
    kind: constant
    value:
      value: "0.80"
      unit: CNY/kWh
  gas_purchase:
    carrier: gas
    direction: purchase
    kind: constant
    value:
      value: "3.20"
      unit: CNY/m3
"""


def _repo_root() -> Path:
    env_root = os.environ.get("PIES_REPO_ROOT")
    if env_root:
        return Path(env_root)
    return Path(__file__).resolve().parents[2]


def _ensure_repo_module_path(subdir: str) -> Path:
    root = _repo_root() / subdir
    if not root.is_dir():
        pytest.skip(f"{subdir}/ not mounted in container context")
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    return root


def _v1_sol_to_primal_json(sol_text: str) -> dict[str, Any]:
    lines = sol_text.splitlines()
    assert lines[0] == "HIGHS_SOLUTION_V1"
    facts: dict[str, str] = {}
    values: list[float] = []
    in_primal = False
    for line in lines[1:]:
        if line == "primal:":
            in_primal = True
            continue
        k, _, v = line.partition(":")
        if not in_primal:
            facts[k.strip()] = v.strip()
        elif line.strip():
            values.append(float(v.strip()))
    status = facts["model_status"]
    has_feasible = facts["has_feasible_solution"] == "true"
    assert status == "HighsModelStatus.kOptimal" and has_feasible
    col_count = int(facts.get("model_num_columns", facts["num_columns"]))
    return {
        "schema": PRIMAL_SOLUTION_SCHEMA,
        "schema_version": PRIMAL_SOLUTION_VERSION,
        "math_status": "solved",
        "termination_reason": "optimal",
        "has_feasible_solution": True,
        "column_count": col_count,
        "values": values,
        "objective_value": float(facts["objective_value"]),
        "best_bound": float(facts["best_bound"]),
        "relative_gap": float(facts["relative_gap"]),
        "receipt": {
            "solver": "ies.solver.highs@1.7.2",
            "solve_domain": facts.get("solve_domain", "original"),
            "original_model_verified": facts.get("original_model_verified", "true") == "true",
        },
        "extensions": {},
    }


def _build_multi_energy_inputs(*, include_tes: bool = True) -> tuple[
    Any, dict[str, Any], MaterializedSet, Any, EvaluationInput, dict[str, Any]
]:
    market_dir = _repo_root() / "market" / "artifacts" / "ies"
    if not market_dir.is_dir():
        pytest.skip("market/artifacts/ies not mounted in container context")

    init_registry()
    documents = {doc.device.id: doc for doc in list_devices()}

    for dev_id in _DEVICE_IDS:
        yaml_path = market_dir / dev_id / "r1" / f"{dev_id}.device.yaml"
        assert yaml_path.is_file(), f"Missing reference device model: {yaml_path}"
        parsed = parse_device_model_v2(yaml_load(yaml_path.read_text(encoding="utf-8")), file=str(yaml_path))
        assert parsed.ok, [(d.code, d.message_key, d.params) for d in parsed.diagnostics]
        documents[parsed.document.device.id] = parsed.document

    gas_parsed = parse_device_model_v2(yaml_load(_GAS_SOURCE_YAML), file="gas_source.yaml")
    assert gas_parsed.ok
    documents[gas_parsed.document.device.id] = gas_parsed.document

    finance = effective_from_yaml(_MULTI_ENERGY_FINANCE_YAML)
    devices: dict[str, Any] = {
        "grid": {
            "definition": {"id": "ies.device.grid_connection"},
            "asset_origin": "existing",
            "properties": {
                "max_import_power_kw": {"value": 2000, "unit": "kW"},
                "max_export_power_kw": {"value": 0, "unit": "kW"},
            },
            "predefined_interfaces": {},
        },
        "gas_src": {
            "definition": {"id": "acme.device.gas_source"},
            "asset_origin": "existing",
            "properties": {"max_flow_m3h": {"value": 1000, "unit": "m3/h"}},
            "predefined_interfaces": {},
        },
        "wind": {
            "definition": {"id": "ies.device.wind_turbine"},
            "asset_origin": "existing",
            "properties": {"rated_capacity_kw": {"value": 500, "unit": "kW"}},
            "predefined_interfaces": {
                "wind_capacity_factor": {"mode": "constant", "value": 0.4},
            },
        },
        "chp": {
            "definition": {"id": "ies.device.chp"},
            "asset_origin": "new",
            "properties": {"rated_electric_kw": {"value": 1000, "unit": "kW"}},
            "predefined_interfaces": {},
        },
        "chiller": {
            "definition": {"id": "ies.device.absorption_chiller"},
            "asset_origin": "existing",
            "properties": {"rated_cooling_kw": {"value": 600, "unit": "kW"}},
            "predefined_interfaces": {},
        },
        "elec_load": {
            "definition": {"id": "ies.device.electric_load"},
            "asset_origin": "existing",
            "properties": {"peak_power_kw": {"value": 800, "unit": "kW"}},
            "predefined_interfaces": {
                "electricity_demand": {"mode": "constant", "value": 600},
            },
        },
        "heat_load": {
            "definition": {"id": "ies.device.heat_load"},
            "asset_origin": "existing",
            "properties": {"peak_heat_kw": {"value": 150, "unit": "kW"}},
            "predefined_interfaces": {
                "heat_demand": {"mode": "constant", "value": 150},
            },
        },
        "cool_load": {
            "definition": {"id": "ies.device.cooling_load"},
            "asset_origin": "existing",
            "properties": {"peak_cooling_kw": {"value": 120, "unit": "kW"}},
            "predefined_interfaces": {
                "cool_demand": {"mode": "constant", "value": 120},
            },
        },
    }
    connections: dict[str, Any] = {
        "gas_to_chp": {"from": "gas_src.gas_out", "to": "chp.gas_in"},
        "grid_to_load": {"from": "grid.electricity_import", "to": "elec_load.electricity_in"},
        "wind_to_load": {"from": "wind.electric_out", "to": "elec_load.electricity_in"},
        "chp_to_load": {"from": "chp.electric_out", "to": "elec_load.electricity_in"},
        "grid_to_chiller": {"from": "grid.electricity_import", "to": "chiller.electricity_in"},
        "wind_to_chiller": {"from": "wind.electric_out", "to": "chiller.electricity_in"},
        "chp_to_chiller_e": {"from": "chp.electric_out", "to": "chiller.electricity_in"},
        "chp_to_heat": {"from": "chp.heat_out", "to": "heat_load.heat_in"},
        "chp_to_chiller_h": {"from": "chp.heat_out", "to": "chiller.heat_in"},
        "chiller_to_cool": {"from": "chiller.cool_out", "to": "cool_load.cool_in"},
    }
    none_finance_names = [
        "grid",
        "gas_src",
        "wind",
        "chiller",
        "elec_load",
        "heat_load",
        "cool_load",
    ]
    if include_tes:
        devices["tes"] = {
            "definition": {"id": "ies.device.thermal_storage"},
            "asset_origin": "existing",
            "properties": {
                "capacity_kwh": {"value": 1000, "unit": "kWh"},
                "rated_power_kw": {"value": 300, "unit": "kW"},
            },
            "predefined_interfaces": {},
        }
        connections["chp_to_tes"] = {"from": "chp.heat_out", "to": "tes.heat"}
        connections["tes_to_heat"] = {"from": "tes.heat", "to": "heat_load.heat_in"}
        connections["tes_to_chiller_h"] = {"from": "tes.heat", "to": "chiller.heat_in"}
        none_finance_names.append("tes")

    assembly_doc: dict[str, Any] = {
        "schema": "ies.assembly",
        "schema_version": "1.0.0",
        "assembly": {"id": "e5b_multi_energy", "name": "E5b Reference Multi-Energy Campus"},
        "project_baseline": {"resolution": "1h", "start_year": 2026, "scenario_mode": "single"},
        "devices": devices,
        "connections": connections,
        "finance": {
            "ref": {"kind": "object", "object_id": "obj-finance", "media_type": "application/yaml"},
            "profile_id": "e5b-ref-finance",
        },
        "finance_binding": {
            "instances": {
                **{name: {"type": "none"} for name in none_finance_names},
                "chp": {
                    "type": "costed",
                    "finance_type": "chp_sys",
                    "drivers": {"rated_kw": {"property": "rated_electric_kw"}},
                },
            }
        },
        "tariff_bindings": {
            "grid_meter": {
                "price": "grid_import",
                "instance": "grid",
                "interface": "electricity_import",
                "aggregation": "integrate_positive",
            },
            "gas_meter": {
                "price": "gas_purchase",
                "instance": "chp",
                "interface": "gas_in",
                "aggregation": "integrate_positive",
            },
        },
        "demand_bindings": {},
        "planning": {
            "objective": {
                "sense": "minimize",
                "expression": (
                    "chp__upfront_capex + grid_meter__period_energy_purchase"
                    " + gas_meter__period_energy_purchase"
                ),
            },
            "variables": {
                "chp_cap": {
                    "device_ref": "chp",
                    "parameter": "rated_electric_kw",
                    "lower_bound": "0",
                    "upper_bound": "1500",
                    "unit": "kW",
                }
            },
            "constraints": {},
        },
        "outputs": {
            "series": ["grid.electricity_import", "chp.gas_in", "wind.electric_out", "chiller.cool_out"],
            "metrics": ["system.total_energy_cost"],
        },
        "extensions": {},
    }

    checked = validate_assembly_doc(
        assembly_doc,
        device_documents=documents,
        finance_snapshots={"obj-finance": finance},
    )
    assert checked.ok, [(d.code, d.location, d.params) for d in checked.diagnostics if d.blocking]
    assert checked.artifact is not None

    models = {}
    for inst, dev in assembly_doc["devices"].items():
        built = build_math_contribution(documents[dev["definition"]["id"]])
        assert built.ok, (inst, built.diagnostics)
        models[inst] = built.contribution

    materialized = materialize_non_predictive_series(
        checked.artifact,
        {},
        operation_years=1,
    )
    evaluation = EvaluationInput(
        evaluation_years=1,
        construction_period=0,
        operation_years=1,
        discount_rate=0.08,
        construction_payments=(),
        maintenance_expenses=(),
    )
    return checked.artifact, models, materialized, finance, evaluation, assembly_doc


def _write_bundle_to_dir(bundle: Any, run_dir: Path) -> None:
    for rel, content in export_bundle_files(bundle).items():
        dest = run_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(read_content(content))
    (run_dir / "output").mkdir(parents=True, exist_ok=True)


def _run_builtin_highs(bundle: Any, run_dir: Path, *, seed: int = 7) -> dict[str, Any]:
    _write_bundle_to_dir(bundle, run_dir)
    args = [
        str(HIGHS_EXECUTABLE),
        "input/problem.mps",
        "--solution_file",
        "output/solution.sol",
        "--time_limit",
        "60",
        "--random_seed",
        str(seed),
        "--report_model_columns",
        "true",
    ]
    if ACCELERATION_PATH in bundle.files:
        args.extend(["--acceleration_mode", "on_demand_mutex", "--acceleration_file", ACCELERATION_PATH])
    if EPIGRAPHS_PATH in bundle.files:
        args.extend(["--epigraph_file", EPIGRAPHS_PATH])
    proc = subprocess.run(args, cwd=run_dir, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    sol_text = (run_dir / "output" / "solution.sol").read_text(encoding="utf-8")
    return _v1_sol_to_primal_json(sol_text)


def test_release_versions_are_1_2_0() -> None:
    assert iesplan.__version__ == "1.2.0"
    _ensure_repo_module_path("job_runner")
    from pies_job_runner.app import create_app as create_jr_app

    jr_app = create_jr_app()
    assert jr_app.version == "1.2.0"


def test_four_reference_device_models_validate_and_solve_multi_energy_assembly(tmp_path: Path) -> None:
    artifact, models, materialized, finance, evaluation, _ = _build_multi_energy_inputs(include_tes=True)
    cfg = CalculationConfig(
        mode="planning",
        predictions=(),
        generator="ies.generator.highs_milp@1.0.0",
        solver="ies.solver.highs@1.7.2",
        algorithm=None,
        evaluators=(),
        precision="medium",
        time_limit_seconds=60,
        options={"generator": {"acceleration": {"mode": "on_demand_mutex"}}, "solver": {}},
        seed=7,
        outputs=("solution", "metrics"),
    )
    bundle = generate_highs_bundle(artifact, models, materialized, finance, cfg, evaluation)
    primal_payload = _run_builtin_highs(bundle, tmp_path / "builtin_run", seed=7)
    receipt = {
        "schema": "ies.execution-receipt",
        "schema_version": "1.0.0",
        "bundle_id": "e5b-builtin-bundle",
        "receipt_id": "rcpt-e5b-builtin",
        "status": "succeeded",
        "termination_reason": "exited",
        "started_at": "2026-11-28T00:00:00Z",
        "ended_at": "2026-11-28T00:00:01Z",
    }
    outcome = ResultAdapter().restore_with_outcome(
        bundle,
        receipt,
        {PRIMAL_SOLUTION_OUTPUT_PATH: json.dumps(primal_payload).encode("utf-8")},
        solution_output_path=tmp_path / "eng_sol.json",
    )
    cr = outcome.compute_result
    assert cr.math_status == "solved"
    assert cr.outputs["solver_summary"]["has_feasible_solution"] is True
    eng = json.loads((tmp_path / "eng_sol.json").read_text(encoding="utf-8"))
    assert eng["series"]["wind.electric_out"]["values"][0] == pytest.approx(196.0, abs=1e-6)
    assert sum(eng["series"]["chp.gas_in"]["values"]) > 0.0
    assert eng["series"]["chiller.cool_out"]["values"][0] == pytest.approx(120.0, abs=1e-6)


def test_four_reference_plugins_validate_and_execute_with_exact_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ensure_repo_module_path("sdk")
    from pies_sdk import (
        read_iterations,
        read_prediction_output,
        validate_plugin_package,
    )
    from pies_sdk.runner import run_plugin

    market_dir = _repo_root() / "market" / "artifacts" / "ies"
    if not market_dir.is_dir():
        pytest.skip("market/artifacts/ies not mounted in container context")

    # 1. Validate all 4 reference plugins with require_market_fields=True and run_tests=True
    for pid in _PLUGIN_IDS:
        plugin_dir = market_dir / pid / "1.0.0" / "plugin"
        assert plugin_dir.is_dir(), f"Missing plugin dir: {plugin_dir}"
        report = validate_plugin_package(plugin_dir, run_tests=True, require_market_fields=True)
        assert report["plugin_id"] == pid

    # 2. Execute ies.predictor.seasonal_joint@1.0.0 on 2 targets
    pred_dir = market_dir / "ies.predictor.seasonal_joint" / "1.0.0" / "plugin"
    pred_in = tmp_path / "pred_in"
    pred_out = tmp_path / "pred_out"
    shutil.copytree(pred_dir / "tests" / "joint_multi_target" / "in", pred_in)
    run_plugin(manifest_path=pred_dir / "plugin.yaml", work_in=pred_in, work_out=pred_out)
    pred_payload = read_prediction_output(pred_out)
    assert {f"{p['instance']}.{p['interface']}" for p in pred_payload.predictions} == {
        "load_1.electricity_demand",
        "wind_1.wind_capacity_factor",
    }
    assert all(len(p["values"]) == 6 for p in pred_payload.predictions)

    # 3. Execute ies.solver.highspy_ref@1.0.0 vs built-in ies.solver.highs@1.7.2 on identical MPS bundle
    artifact, models, materialized, finance, evaluation, _ = _build_multi_energy_inputs(include_tes=False)
    cfg = CalculationConfig(
        mode="planning",
        predictions=(),
        generator="ies.generator.highs_milp@1.0.0",
        solver="ies.solver.highs@1.7.2",
        algorithm=None,
        evaluators=(),
        precision="medium",
        time_limit_seconds=60,
        options={"generator": {"acceleration": {"mode": "off"}}, "solver": {}},
        seed=7,
        outputs=("solution", "metrics"),
    )
    bundle = generate_highs_bundle(artifact, models, materialized, finance, cfg, evaluation)
    builtin_primal = _run_builtin_highs(bundle, tmp_path / "parity_builtin", seed=7)

    solver_plugin_dir = market_dir / "ies.solver.highspy_ref" / "1.0.0" / "plugin"
    solver_in = tmp_path / "solver_in"
    solver_out = tmp_path / "solver_out"
    _write_bundle_to_dir(bundle, solver_in / "bundle")
    (solver_in / "request.json").write_text(
        json.dumps({"time_limit_seconds": 60, "seed": 7, "parameters": {"threads": 1}}),
        encoding="utf-8",
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pies_sdk.runner",
            "--manifest",
            str(solver_plugin_dir / "plugin.yaml"),
            "--work-in",
            str(solver_in),
            "--work-out",
            str(solver_out),
        ],
        env={**os.environ, "PYTHONPATH": str(_repo_root() / "sdk")},
        capture_output=True,
        text=True,
        check=True,
    )
    ref_primal_bytes = (solver_out / "primal_solution.json").read_bytes()

    receipt_builtin = {
        "schema": "ies.execution-receipt",
        "schema_version": "1.0.0",
        "bundle_id": "e5b-parity-bundle",
        "receipt_id": "rcpt-builtin",
        "status": "succeeded",
        "termination_reason": "exited",
        "started_at": "2026-11-28T00:00:00Z",
        "ended_at": "2026-11-28T00:00:01Z",
    }
    receipt_ref = {**receipt_builtin, "receipt_id": "rcpt-highspy-ref"}

    adapter = ResultAdapter()
    out_builtin = adapter.restore_with_outcome(
        bundle,
        receipt_builtin,
        {PRIMAL_SOLUTION_OUTPUT_PATH: json.dumps(builtin_primal).encode("utf-8")},
        solution_output_path=tmp_path / "eng_builtin.json",
    )
    out_ref = adapter.restore_with_outcome(
        bundle,
        receipt_ref,
        {PRIMAL_SOLUTION_OUTPUT_PATH: ref_primal_bytes},
        solution_output_path=tmp_path / "eng_ref.json",
    )
    assert out_builtin.compute_result.math_status == "solved"
    assert out_ref.compute_result.math_status == "solved"
    obj_b = out_builtin.compute_result.outputs["solver_summary"]["objective_value"]
    obj_r = out_ref.compute_result.outputs["solver_summary"]["objective_value"]
    assert obj_b == pytest.approx(obj_r, rel=1e-6, abs=1e-6)

    eng_b = json.loads((tmp_path / "eng_builtin.json").read_text(encoding="utf-8"))
    eng_r = json.loads((tmp_path / "eng_ref.json").read_text(encoding="utf-8"))
    assert eng_b["series"].keys() == eng_r["series"].keys()
    for skey in eng_b["series"]:
        assert eng_b["series"][skey]["values"] == pytest.approx(
            eng_r["series"][skey]["values"], rel=1e-6, abs=1e-6
        )

    # 4. Execute ies.algorithm.economic_dispatch@1.0.0 on run_algorithm_stage-shaped input
    algo_dir = market_dir / "ies.algorithm.economic_dispatch" / "1.0.0" / "plugin"
    algo_in = tmp_path / "algo_in"
    algo_out = tmp_path / "algo_out"
    algo_in.mkdir(parents=True)
    algo_req = {
        "artifact": {"canonical_text": artifact.canonical_text, "receipt": {}},
        "device_models": {},
        "finance": finance.to_dict(),
        "calculation_config": {
            "schema": "ies.calculation-config",
            "schema_version": "1.0.0",
            "mode": "planning",
            "predictions": [],
            "generator": None,
            "solver": None,
            "algorithm": "ies.algorithm.economic_dispatch@1.0.0",
            "evaluators": [],
            "precision": "medium",
            "time_limit_seconds": 60,
            "options": {"algorithm": {"max_iterations": 12, "tolerance": 1e-4}},
            "seed": 7,
            "outputs": ["solution", "metrics"],
        },
        "parameters": {"max_iterations": 12, "tolerance": 1e-4},
        "evaluation": {"evaluation_years": 1, "construction_period": 0, "operation_years": 1},
        "time_limit_seconds": 60,
        "seed": 7,
        "materialized": {
            "series": [
                {"instance": "elec_load", "interface": "electricity_demand", "values": [120.0, 210.0, 180.0]}
            ]
        },
    }
    (algo_in / "request.json").write_text(json.dumps(algo_req), encoding="utf-8")
    run_plugin(manifest_path=algo_dir / "plugin.yaml", work_in=algo_in, work_out=algo_out)
    iters = read_iterations(algo_out / "iterations.jsonl")
    assert len(iters) >= 1 and iters[-1].iteration >= 1
    algo_proj = project_engineering_solution(
        MemoryContent((algo_out / "solution.json").read_bytes()),
        bundle_id="e5b-algo-bundle",
        receipt_id="e5b-algo-rcpt",
        outputs=("solution", "metrics"),
        solution_output_path=tmp_path / "algo_projected.json",
    )
    assert algo_proj.compute_result.math_status in ("solved", "feasible")

    # 5. Execute ies.evaluator.carbon_reliability@1.0.0 on restored engineering solution & export Excel
    eval_dir = market_dir / "ies.evaluator.carbon_reliability" / "1.0.0" / "plugin"
    eval_in = tmp_path / "eval_in"
    eval_out = tmp_path / "eval_out"
    eval_in.mkdir(parents=True)
    shutil.copy2(tmp_path / "eng_ref.json", eval_in / "solution.json")
    (eval_in / "request.json").write_text(
        json.dumps({"parameters": {"grid_emission_factor_kg_per_kwh": 0.5703, "step_hours": 1.0}}),
        encoding="utf-8",
    )
    run_plugin(manifest_path=eval_dir / "plugin.yaml", work_in=eval_in, work_out=eval_out)
    eval_doc = json.loads((eval_out / "evaluation_report.json").read_text(encoding="utf-8"))
    validated_report = validate_evaluation_report_doc(
        eval_doc, expected_evaluator="ies.evaluator.carbon_reliability@1.0.0"
    )
    assert len(validated_report["metrics"]) >= 5

    # Export Excel and verify independent evaluator sheet
    monkeypatch.setattr(app_settings, "data_dir", tmp_path / "store")
    storage_service._blob_store = FileSystemBlobStore()
    eng = create_engine("sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    try:
        with Session(eng, expire_on_commit=False) as db:
            uid = identity_domain.create_user(db, username="e5b_xls", display_name="E5b Xls").id
            db.flush()
            user: UserRecord = identity_domain.get_user(db, uid)  # type: ignore[assignment]
            proj = project_domain.create_project(
                db,
                name="E5b Eval Export",
                owner_id=uid,
                created_by=uid,
                baseline_resolution="1h",
                baseline_start_year=2026,
                baseline_scenario_mode="single",
            )
            ver_obj = put_object(db, b"{}", "application/json", source_category="project")
            ver = project_domain.create_version(
                db,
                project_id=proj.id,
                name="v1",
                reason="manual_save",
                content_object_id=ver_obj.id,
                created_by=uid,
            )
            ev_obj = put_object(
                db,
                json.dumps(
                    {
                        "kpis": [],
                        "evaluator_results": [
                            {
                                "evaluator": "ies.evaluator.carbon_reliability@1.0.0",
                                "status": "succeeded",
                                "contract": "ies.evaluation-report@1.0.0",
                                "error": None,
                            }
                        ],
                        "evaluation_reports": [validated_report],
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
                "application/json",
                source_category="evidence",
            )
            snap = CalcSnapshot(
                project_version_id=ver.id,
                dataset_version_ids=[],
                calc_config_snapshot={"params": {}, "algorithm": "ies.generator.highs_milp@1.0.0"},
                program_version="1.2.0",
                extension_versions={},
                random_seed=7,
                schema_version="1.0.0",
                result_adapter="ies.result-adapter.highs@1.0.0",
                executor="ies.executor.local_process@1.0.0",
                created_by=uid,
            )
            db.add(snap)
            db.flush()
            task = Task(
                project_id=proj.id,
                type="calc",
                status="completed",
                business_outcome="normal_completion",
                calc_snapshot_id=snap.id,
                requested_by=uid,
            )
            db.add(task)
            db.flush()
            ep = EvidencePackage(
                task_id=task.id,
                calc_snapshot_id=snap.id,
                object_id=ev_obj.id,
                status="complete",
                created_by=uid,
            )
            db.add(ep)
            db.flush()
            assess = ResultAssessment(
                evidence_package_id=ep.id,
                assessor="system",
                assessed_by=uid,
                dimension_physical="pass",
                dimension_optimality="pass",
                dimension_financial="pass",
                dimension_reliability="pass",
                overall_score=95.0,
                comment="ok",
                detail={},
            )
            db.add(assess)
            db.flush()
            db.add(
                ResultIndex(
                    project_id=proj.id,
                    project_version_id=ver.id,
                    evidence_package_id=ep.id,
                    assessment_id=assess.id,
                    is_latest=True,
                )
            )
            db.commit()
            xlsx_bytes = export_excel(db, user, proj.id, ep.id, assess.id, lang="zh")
            wb = load_workbook(filename=__import__("io").BytesIO(xlsx_bytes))
            title_sheet = validated_report["title"][:31]
            assert title_sheet in wb.sheetnames
            text = "\n".join(str(c.value) for row in wb[title_sheet].iter_rows() for c in row)
            assert "ies.evaluator.carbon_reliability@1.0.0" in text
    finally:
        eng.dispose()
