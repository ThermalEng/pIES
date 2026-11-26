"""End-to-end 1.2.0 pipeline equivalence and replaceability tests across built-in stage plugins."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from iesplan.assembly import validate_assembly_text
from iesplan.computation import (
    HIGHS_EXECUTABLE_REF,
    HIGHS_MILP_PROVIDER,
    NAIVE_PREDICTOR,
    NAIVE_PREDICTOR_REF,
    PRIMAL_SOLUTION_OUTPUT_PATH,
    CalculationConfig,
    ExecutionReceipt,
    MaterializedSeries,
    MaterializedSet,
    materialize_non_predictive_series,
    materialize_predicted_series,
)
from iesplan.computation.bundle import export_bundle_files
from iesplan.computation.generators.highs_milp import EXECUTOR_REF, GENERATOR_REF, SOLVER_REF
from iesplan.computation.result_adapters import ResultAdapter
from iesplan.core.content import read_content
from iesplan.devices import init_registry, list_devices
from iesplan.finance import EvaluationInput, effective_from_yaml, evaluate_forward_finance
from iesplan.modeling import build_math_contribution, contribution_to_dict
from pies_sdk import (
    StageIOError,
    read_engineering_solution,
    read_financial_evaluation,
    read_prediction_output,
    read_primal_solution,
    validate_solver_bundle_dir,
    validate_stage_outputs,
    write_json_file,
)
from pies_sdk.runner import run_plugin

from pies_builtin_plugins.stage_io import effective_finance_from_frozen

PLUGINS_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PLUGINS_ROOT.parent
IESPLAN_SAMPLES = REPO_ROOT / "backend" / "iesplan" / "assembly" / "samples"


def _collect_resources(doc: dict[str, Any], samples_dir: Path = IESPLAN_SAMPLES) -> dict[str, bytes]:
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            content = node.get("content")
            if isinstance(content, dict) and "object_id" in content:
                found.add(str(content["object_id"]))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(doc.get("devices") or {})
    resources: dict[str, bytes] = {}
    for oid in sorted(found):
        rel = oid[5:] if oid.startswith("file:") else oid
        resources[oid] = (samples_dir / rel).read_bytes()
    return resources


def _load_campus_inputs(*, assembly_text: str | None = None, samples_dir: Path = IESPLAN_SAMPLES):
    init_registry()
    text = assembly_text or (samples_dir / "valid" / "campus.assembly.yaml").read_text(
        encoding="utf-8"
    )
    if "  construction_units:\n" not in text and "  constraints:\n" in text:
        unit_block = (
            "  construction_units:\n"
            "    u_pv1:\n"
            "      device_ref: pv1\n"
            "      mode: optional\n"
            "      scales:\n"
            "        pv_cap:\n"
            '          active_lower_bound: "0"\n'
            "          active_upper_bound: null\n"
        )
        text = text.replace("  constraints:\n", unit_block + "  constraints:\n", 1)
    validated = validate_assembly_text(text, package_dir=samples_dir)
    assert validated.ok, [(d.code, d.params) for d in validated.diagnostics if d.blocking]
    artifact = validated.artifact

    contributions = {}
    for dev in list_devices():
        built = build_math_contribution(dev)
        assert built.ok, (dev.device.id, built.diagnostics)
        contributions[dev.device.id] = built.contribution

    doc = json.loads(artifact.canonical_text)
    models = {
        inst: contributions[dev["definition"]["id"]] for inst, dev in doc["devices"].items()
    }
    finance = effective_from_yaml(
        (samples_dir / "finance" / "campus.effective-finance.yaml").read_text(
            encoding="utf-8"
        )
    )
    resources = _collect_resources(doc, samples_dir)
    evaluation = EvaluationInput(
        evaluation_years=1,
        construction_period=0,
        operation_years=1,
        discount_rate=0.08,
        construction_payments=(),
        maintenance_expenses=(),
    )
    return artifact, models, resources, finance, evaluation


def _evaluation_to_dict(ev: EvaluationInput) -> dict[str, Any]:
    return {
        "evaluation_years": ev.evaluation_years,
        "construction_period": ev.construction_period,
        "operation_years": ev.operation_years,
        "discount_rate": ev.discount_rate,
        "construction_payments": [
            {"t": p.t, "ratio": str(p.ratio)} for p in ev.construction_payments
        ],
        "maintenance_expenses": [],
        "comparison": ev.comparison.to_dict(),
    }


def _stage_receipt(
    *,
    receipt_id: str,
    bundle_id: str,
    assembly_id: str,
    config_id: str,
    attempt_id: str,
    solver_ref: str = SOLVER_REF,
    primal_size: int = 1,
) -> dict[str, Any]:
    return ExecutionReceipt(
        receipt_id=receipt_id,
        bundle_id=bundle_id,
        assembly_id=assembly_id,
        calculation_config_id=config_id,
        attempt_id=attempt_id,
        generator_ref=GENERATOR_REF,
        solver_ref=solver_ref,
        executor_ref=EXECUTOR_REF,
        executable_ref=HIGHS_EXECUTABLE_REF,
        result_adapter_ref=None,
        status="succeeded",
        exit_code=0,
        termination_reason="exited",
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:00:01Z",
        outputs=(
            {
                "path": PRIMAL_SOLUTION_OUTPUT_PATH,
                "media_type": "application/json",
                "size_bytes": primal_size,
                "required": True,
            },
        ),
    ).to_dict()


def test_effective_finance_from_frozen_roundtrip() -> None:
    """Trusted finance reconstruction preserves all EffectiveFinanceConfig fields identically."""
    finance = effective_from_yaml(
        (IESPLAN_SAMPLES / "finance" / "campus.effective-finance.yaml").read_text(
            encoding="utf-8"
        )
    )
    restored = effective_finance_from_frozen(finance.to_dict())
    assert restored.to_dict() == finance.to_dict()


def test_campus_full_pipeline_equivalence(tmp_path: Path) -> None:
    """1.2.0 campus MILP: generator -> solver -> ResultAdapter -> evaluator_finance parity."""
    artifact, models, resources, finance, evaluation = _load_campus_inputs()
    materialized = materialize_non_predictive_series(artifact, resources, operation_years=1)
    assert not materialized.pending_predict

    config = CalculationConfig(
        mode="planning",
        predictions=(),
        generator=GENERATOR_REF,
        solver=SOLVER_REF,
        algorithm=None,
        evaluators=({"evaluator": "ies.evaluator.finance@1.0.0", "parameters": {}},),
        precision="medium",
        time_limit_seconds=600,
        options={"generator": {}, "solver": {}},
        seed=0,
        outputs=("solution", "metrics"),
    )

    direct_bundle = HIGHS_MILP_PROVIDER.generate(
        artifact, models, materialized, finance, config, evaluation
    )

    # Stage 2: generator_highs_milp
    gen_in = tmp_path / "gen_in"
    gen_out = tmp_path / "gen_out"
    write_json_file(
        gen_in / "request.json",
        {
            "artifact": artifact.to_dict(),
            "device_models": {inst: contribution_to_dict(c) for inst, c in models.items()},
            "materialized": materialized.to_dict(),
            "finance": finance.to_dict(),
            "calculation_config": config.to_dict(),
            "evaluation": _evaluation_to_dict(evaluation),
        },
    )
    run_plugin(
        manifest_path=PLUGINS_ROOT / "generator_highs_milp" / "plugin.yaml",
        work_in=gen_in,
        work_out=gen_out,
        pies_version="1.2.0",
    )
    validate_stage_outputs("generator", gen_out)
    validate_solver_bundle_dir(gen_out / "bundle")

    expected_bundle_files = export_bundle_files(direct_bundle)
    for rel_path, content in expected_bundle_files.items():
        actual_bytes = (gen_out / "bundle" / rel_path).read_bytes()
        assert actual_bytes == read_content(content), f"Bundle mismatch in {rel_path}"

    # Stage 3: solver_highs -> strictly 0-based output/primal_solution.json
    solver_in = tmp_path / "solver_in"
    solver_out = tmp_path / "solver_out"
    solver_in.mkdir()
    (solver_in / "bundle").symlink_to(gen_out / "bundle")
    write_json_file(
        solver_in / "request.json",
        {"time_limit_seconds": 600, "seed": 0, "parameters": {"threads": 2}},
    )
    run_plugin(
        manifest_path=PLUGINS_ROOT / "solver_highs" / "plugin.yaml",
        work_in=solver_in,
        work_out=solver_out,
        pies_version="1.2.0",
    )
    validate_stage_outputs("solver", solver_out)
    primal_bytes = (solver_out / "primal_solution.json").read_bytes()
    primal = read_primal_solution(solver_out / "primal_solution.json")
    assert primal.math_status == "solved"
    assert primal.has_feasible_solution is True
    assert primal.objective_value == pytest.approx(850_051.6, abs=1e-5)

    # Bounded restoration -> canonical ies.engineering-solution@1.0.0
    sol_json_out = tmp_path / "solution.json"
    receipt_dict = _stage_receipt(
        receipt_id="eq-rcpt-1",
        bundle_id="eq-bnd-1",
        assembly_id="campus_demo",
        config_id="cfg-eq-1",
        attempt_id="attempt-eq-1",
        primal_size=len(primal_bytes),
    )
    outcome = ResultAdapter().restore_with_outcome(
        direct_bundle,
        receipt_dict,
        {PRIMAL_SOLUTION_OUTPUT_PATH: primal_bytes},
        solution_output_path=sol_json_out,
    )
    assert outcome.compute_result.math_status == "solved"
    eng_sol = read_engineering_solution(sol_json_out)
    assert eng_sol.solver_summary.objective_value == pytest.approx(850_051.6, abs=1e-5)

    direct_eval = evaluate_forward_finance(
        eng_sol.to_dict(),
        artifact=artifact,
        finance=finance,
        evaluation=evaluation,
        materialized=materialized,
        device_models=models,
    )

    # Stage 4: evaluator_finance consuming ONLY solution.json + hardlinked frozen context
    eval_in = tmp_path / "eval_in"
    eval_out = tmp_path / "eval_out"
    eval_in.mkdir()
    (eval_in / "solution.json").symlink_to(sol_json_out)
    write_json_file(eval_in / "assembly.json", artifact.to_dict())
    write_json_file(
        eval_in / "device_models.json",
        {inst: contribution_to_dict(c) for inst, c in models.items()},
    )
    write_json_file(eval_in / "sequences.json", materialized.to_dict())
    write_json_file(eval_in / "finance.json", finance.to_dict())
    write_json_file(eval_in / "evaluation.json", _evaluation_to_dict(evaluation))
    write_json_file(eval_in / "receipt.json", receipt_dict)

    run_plugin(
        manifest_path=PLUGINS_ROOT / "evaluator_finance" / "plugin.yaml",
        work_in=eval_in,
        work_out=eval_out,
        pies_version="1.2.0",
    )
    validate_stage_outputs(
        "evaluator",
        eval_out,
        produces=("ies.financial-evaluation@1.0.0",),
    )
    actual_eval = read_financial_evaluation(eval_out / "financial_evaluation.json")
    assert actual_eval == direct_eval


def test_four_stage_pipeline_with_grouped_joint_predictor_equivalence(tmp_path: Path) -> None:
    """Single-pass grouped multi-target predictor -> generator -> solver -> ResultAdapter -> evaluator."""
    samples_copy = tmp_path / "samples"
    shutil.copytree(IESPLAN_SAMPLES, samples_copy)
    for csv_name in ("campus_load.data.csv", "campus_heat.data.csv"):
        csv_file = samples_copy / "data" / csv_name
        csv_file.write_text(
            csv_file.read_text(encoding="utf-8").replace(
                "# source_mode: data_repeat", "# source_mode: data_predict", 1
            ),
            encoding="utf-8",
        )
    source_text = (samples_copy / "valid" / "campus.assembly.yaml").read_text(encoding="utf-8")
    predict_text = (
        source_text.replace(
            "      electricity_demand:\n"
            "        mode: data_repeat\n"
            "        path: data/campus_load.data.csv",
            "      electricity_demand:\n"
            "        mode: data_predict\n"
            "        path: data/campus_load.data.csv\n"
            "        target_type: load",
            1,
        ).replace(
            "      heat_demand:\n"
            "        mode: data_repeat\n"
            "        path: data/campus_heat.data.csv",
            "      heat_demand:\n"
            "        mode: data_predict\n"
            "        path: data/campus_heat.data.csv\n"
            "        target_type: heat_load",
            1,
        )
    )
    assert predict_text.count("mode: data_predict") == 2
    artifact, models, resources, finance, evaluation = _load_campus_inputs(
        assembly_text=predict_text, samples_dir=samples_copy
    )

    base_mat = materialize_non_predictive_series(artifact, resources, operation_years=1)
    assert len(base_mat.pending_predict) == 2
    pending_by_key = {(p["device"], p["interface"]): p for p in base_mat.pending_predict}

    pred_entries = (
        {
            "target": {"instance": "elec_load", "interface": "electricity_demand"},
            "algorithm": NAIVE_PREDICTOR_REF,
            "parameters": {},
        },
        {
            "target": {"instance": "heat_load", "interface": "heat_demand"},
            "algorithm": NAIVE_PREDICTOR_REF,
            "parameters": {},
        },
    )
    config = CalculationConfig(
        mode="planning",
        predictions=pred_entries,
        generator=GENERATOR_REF,
        solver=SOLVER_REF,
        algorithm=None,
        evaluators=({"evaluator": "ies.evaluator.finance@1.0.0", "parameters": {}},),
        precision="medium",
        time_limit_seconds=600,
        options={"generator": {}, "solver": {}},
        seed=42,
        outputs=("solution", "metrics"),
    )

    predicted_targets = materialize_predicted_series(
        artifact,
        resources,
        config.predictions,
        lambda ref: NAIVE_PREDICTOR if ref == NAIVE_PREDICTOR_REF else None,  # type: ignore[return-value]
        config.seed,
        operation_years=1,
    )
    assert len(predicted_targets) == 2

    direct_mat = MaterializedSet(
        operation_years=base_mat.operation_years,
        series=base_mat.series
        + tuple(
            MaterializedSeries(
                device=pt.device,
                interface=pt.interface,
                mode="data_predict",
                resolution="1h",
                point_count=len(pt.values),
                values=pt.values,
                source={"kind": "data_predict", "object_id": pt.history_object_id},
            )
            for pt in predicted_targets
        ),
        pending_predict=(),
        prices=base_mat.prices,
    )
    direct_bundle = HIGHS_MILP_PROVIDER.generate(
        artifact, models, direct_mat, finance, config, evaluation
    )

    # Stage 1: single-pass joint predictor_naive over BOTH targets (no `previous` field)
    pred_in = tmp_path / "pred_in"
    pred_out = tmp_path / "pred_out"
    (pred_in / "resources").mkdir(parents=True)
    elec_pending = pending_by_key[("elec_load", "electricity_demand")]
    heat_pending = pending_by_key[("heat_load", "heat_demand")]
    (pred_in / "resources" / "load.csv").write_bytes(resources[str(elec_pending["object_id"])])
    (pred_in / "resources" / "heat.csv").write_bytes(resources[str(heat_pending["object_id"])])
    write_json_file(
        pred_in / "request.json",
        {
            "point_count": 8760,
            "seed": config.seed,
            "targets": [
                {
                    "instance": "elec_load",
                    "interface": "electricity_demand",
                    "target_type": str(elec_pending["target_type"]),
                    "history_object_id": str(elec_pending["object_id"]),
                    "history_csv_rel": "resources/load.csv",
                    "parameters": {},
                },
                {
                    "instance": "heat_load",
                    "interface": "heat_demand",
                    "target_type": str(heat_pending["target_type"]),
                    "history_object_id": str(heat_pending["object_id"]),
                    "history_csv_rel": "resources/heat.csv",
                    "parameters": {},
                },
            ],
        },
    )
    run_plugin(
        manifest_path=PLUGINS_ROOT / "predictor_naive" / "plugin.yaml",
        work_in=pred_in,
        work_out=pred_out,
        pies_version="1.2.0",
    )
    validate_stage_outputs("predictor", pred_out)
    pred_output = read_prediction_output(pred_out)
    assert len(pred_output.predictions) == 2
    by_target = {(p["instance"], p["interface"]): p for p in pred_output.predictions}
    for pt in predicted_targets:
        p_item = by_target[(pt.device, pt.interface)]
        assert p_item["values"] == list(pt.values)
        assert p_item["receipt"] == dict(pt.receipt)
        assert (pred_out / p_item["artifact_path"]).read_bytes() == pt.artifact

    # Stage 2: generator_highs_milp directly consuming merged /work/in/prediction.json
    gen_in = tmp_path / "gen_in"
    gen_out = tmp_path / "gen_out"
    gen_in.mkdir()
    (gen_in / "prediction.json").symlink_to(pred_out / "prediction.json")
    write_json_file(
        gen_in / "request.json",
        {
            "artifact": artifact.to_dict(),
            "device_models": {inst: contribution_to_dict(c) for inst, c in models.items()},
            "materialized": base_mat.to_dict(),
            "finance": finance.to_dict(),
            "calculation_config": config.to_dict(),
            "evaluation": _evaluation_to_dict(evaluation),
        },
    )
    run_plugin(
        manifest_path=PLUGINS_ROOT / "generator_highs_milp" / "plugin.yaml",
        work_in=gen_in,
        work_out=gen_out,
        pies_version="1.2.0",
    )
    for rel_path, content in export_bundle_files(direct_bundle).items():
        assert (gen_out / "bundle" / rel_path).read_bytes() == read_content(content)

    # Stage 3 + ResultAdapter + Stage 4
    solver_in = tmp_path / "solver_in"
    solver_out = tmp_path / "solver_out"
    solver_in.mkdir()
    (solver_in / "bundle").symlink_to(gen_out / "bundle")
    run_plugin(
        manifest_path=PLUGINS_ROOT / "solver_highs" / "plugin.yaml",
        work_in=solver_in,
        work_out=solver_out,
        pies_version="1.2.0",
    )
    primal_bytes = (solver_out / "primal_solution.json").read_bytes()
    sol_json_out = tmp_path / "pred_solution.json"
    ResultAdapter().restore_with_outcome(
        direct_bundle,
        _stage_receipt(
            receipt_id="pred-rcpt-1",
            bundle_id="pred-bnd-1",
            assembly_id="campus_demo",
            config_id="cfg-pred-1",
            attempt_id="att-pred-1",
            primal_size=len(primal_bytes),
        ),
        {PRIMAL_SOLUTION_OUTPUT_PATH: primal_bytes},
        solution_output_path=sol_json_out,
    )

    eval_in = tmp_path / "eval_in"
    eval_out = tmp_path / "eval_out"
    eval_in.mkdir()
    (eval_in / "solution.json").symlink_to(sol_json_out)
    (eval_in / "prediction.json").symlink_to(pred_out / "prediction.json")
    write_json_file(eval_in / "assembly.json", artifact.to_dict())
    write_json_file(
        eval_in / "device_models.json",
        {inst: contribution_to_dict(c) for inst, c in models.items()},
    )
    write_json_file(eval_in / "sequences.json", base_mat.to_dict())
    write_json_file(eval_in / "finance.json", finance.to_dict())
    write_json_file(eval_in / "evaluation.json", _evaluation_to_dict(evaluation))
    run_plugin(
        manifest_path=PLUGINS_ROOT / "evaluator_finance" / "plugin.yaml",
        work_in=eval_in,
        work_out=eval_out,
        pies_version="1.2.0",
    )
    actual_eval = read_financial_evaluation(eval_out / "financial_evaluation.json")
    assert actual_eval["status"] == "evaluated"
    assert actual_eval["evaluation"]["schema"] == "ies.project-evaluation"


def test_third_party_mps_solver_replaceability(tmp_path: Path) -> None:
    """Any compliant 3rd-party MPS solver emitting primal_solution.json replaces solver_highs."""
    artifact, models, resources, finance, evaluation = _load_campus_inputs()
    materialized = materialize_non_predictive_series(artifact, resources, operation_years=1)
    config = CalculationConfig(
        mode="planning",
        predictions=(),
        generator=GENERATOR_REF,
        solver="acme.solver.custom_mps@1.0.0",
        algorithm=None,
        evaluators=({"evaluator": "ies.evaluator.finance@1.0.0", "parameters": {}},),
        precision="medium",
        time_limit_seconds=120,
        options={"generator": {}, "solver": {}},
        seed=0,
        outputs=("solution", "metrics"),
    )
    gen_in = tmp_path / "gen_in"
    gen_out = tmp_path / "gen_out"
    write_json_file(
        gen_in / "request.json",
        {
            "artifact": artifact.to_dict(),
            "device_models": {inst: contribution_to_dict(c) for inst, c in models.items()},
            "materialized": materialized.to_dict(),
            "finance": finance.to_dict(),
            "calculation_config": config.to_dict(),
            "evaluation": _evaluation_to_dict(evaluation),
        },
    )
    run_plugin(
        manifest_path=PLUGINS_ROOT / "generator_highs_milp" / "plugin.yaml",
        work_in=gen_in,
        work_out=gen_out,
        pies_version="1.2.0",
    )

    custom_solver = tmp_path / "custom_solver"
    custom_solver.mkdir()
    (custom_solver / "plugin.yaml").write_text(
        "\n".join(
            [
                "schema: ies.algorithm-plugin",
                'schema_version: "1.0.0"',
                "plugin_id: acme.solver.custom_mps",
                'version: "1.0.0"',
                "kind: solver",
                "name:",
                "  en_us: Custom MPS Solver",
                "description: Third-party MPS solver emitting 0-based ies.primal-solution@1.0.0.",
                "compatibility:",
                '  pies: ">=1.2.0,<2.0.0"',
                "consumes:",
                "  - ies.solver-bundle.mps@1.0.0",
                "produces:",
                "  - ies.primal-solution@1.0.0",
                "runtime:",
                "  language: python",
                '  python: "3.12"',
                "entrypoint: custom_solver_mod:solve",
                "parameters_schema:",
                "  type: object",
                "  additionalProperties: false",
                "  properties: {}",
                "resources:",
                "  default: {cpu: 1, memory_mb: 1024, timeout_seconds: 120, process_limit: 8}",
                "  maximum: {cpu: 2, memory_mb: 2048, timeout_seconds: 300, process_limit: 16}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (custom_solver / "custom_solver_mod.py").write_text(
        "\n".join(
            [
                "import highspy",
                "from pies_sdk import StageContext, read_solver_bundle",
                "def solve(ctx: StageContext) -> None:",
                "    _manifest, bundle_dir = read_solver_bundle(ctx.work_in)",
                "    h = highspy.Highs()",
                "    h.setOptionValue('output_flag', False)",
                "    h.readModel(str(bundle_dir / 'input' / 'problem.mps'))",
                "    h.run()",
                "    info = h.getInfo()",
                "    sol = h.getSolution()",
                "    ctx.write_primal_solution({",
                "        'schema': 'ies.primal-solution',",
                "        'schema_version': '1.0.0',",
                "        'math_status': 'solved',",
                "        'termination_reason': 'optimal',",
                "        'has_feasible_solution': True,",
                "        'column_count': h.getNumCol(),",
                "        'objective_value': float(info.objective_function_value),",
                "        'best_bound': float(info.objective_function_value),",
                "        'relative_gap': 0.0,",
                "        'values': [float(v) for v in sol.col_value],",
                "        'receipt': {'solver': ctx.manifest.ref},",
                "    })",
                "",
            ]
        ),
        encoding="utf-8",
    )

    solver_in = tmp_path / "solver_in"
    solver_out = tmp_path / "solver_out"
    solver_in.mkdir()
    (solver_in / "bundle").symlink_to(gen_out / "bundle")
    run_plugin(
        manifest_path=custom_solver / "plugin.yaml",
        work_in=solver_in,
        work_out=solver_out,
        pies_version="1.2.0",
    )
    validate_stage_outputs("solver", solver_out)

    bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
    primal_bytes = (solver_out / "primal_solution.json").read_bytes()
    sol_json_out = tmp_path / "custom_solution.json"
    ResultAdapter().restore_with_outcome(
        bundle,
        _stage_receipt(
            receipt_id="custom-rcpt-1",
            bundle_id="custom-bnd-1",
            assembly_id="campus_demo",
            config_id="cfg-custom-1",
            attempt_id="att-custom-1",
            solver_ref="acme.solver.custom_mps@1.0.0",
            primal_size=len(primal_bytes),
        ),
        {PRIMAL_SOLUTION_OUTPUT_PATH: primal_bytes},
        solution_output_path=sol_json_out,
    )
    assert read_engineering_solution(sol_json_out).solver_summary.objective_value == pytest.approx(
        850_051.6, abs=1e-5
    )


def test_infeasible_model_pipeline_produces_non_feasible_solution_and_blocks_evaluator(
    tmp_path: Path,
) -> None:
    """Infeasible solve yields non-feasible ies.engineering-solution@1.0.0; evaluator refuses cleanly."""
    source_text = (IESPLAN_SAMPLES / "valid" / "campus.assembly.yaml").read_text(encoding="utf-8")
    anchor = '      expression: "import_meter__period_energy_purchase <= 900000 CNY"\n'
    pin_line = (
        "    chain_infeasible_pin:\n"
        "      type: capacity_limit\n"
        '      expression: "pv_cap >= 5000 kWp"\n'
    )
    assert anchor in source_text
    pinned_text = source_text.replace(anchor, anchor + pin_line, 1)

    artifact, models, resources, finance, evaluation = _load_campus_inputs(assembly_text=pinned_text)
    materialized = materialize_non_predictive_series(artifact, resources, operation_years=1)
    config = CalculationConfig(
        mode="planning",
        predictions=(),
        generator=GENERATOR_REF,
        solver=SOLVER_REF,
        algorithm=None,
        evaluators=({"evaluator": "ies.evaluator.finance@1.0.0", "parameters": {}},),
        precision="medium",
        time_limit_seconds=600,
        options={"generator": {}, "solver": {}},
        seed=0,
        outputs=("solution", "metrics"),
    )

    gen_in = tmp_path / "gen_in"
    gen_out = tmp_path / "gen_out"
    write_json_file(
        gen_in / "request.json",
        {
            "artifact": artifact.to_dict(),
            "device_models": {inst: contribution_to_dict(c) for inst, c in models.items()},
            "materialized": materialized.to_dict(),
            "finance": finance.to_dict(),
            "calculation_config": config.to_dict(),
            "evaluation": _evaluation_to_dict(evaluation),
        },
    )
    run_plugin(
        manifest_path=PLUGINS_ROOT / "generator_highs_milp" / "plugin.yaml",
        work_in=gen_in,
        work_out=gen_out,
        pies_version="1.2.0",
    )

    solver_in = tmp_path / "solver_in"
    solver_out = tmp_path / "solver_out"
    solver_in.mkdir()
    (solver_in / "bundle").symlink_to(gen_out / "bundle")
    run_plugin(
        manifest_path=PLUGINS_ROOT / "solver_highs" / "plugin.yaml",
        work_in=solver_in,
        work_out=solver_out,
        pies_version="1.2.0",
    )
    primal_bytes = (solver_out / "primal_solution.json").read_bytes()
    primal = read_primal_solution(solver_out / "primal_solution.json")
    assert primal.math_status == "infeasible"
    assert primal.has_feasible_solution is False

    bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
    sol_json_out = tmp_path / "infeasible_solution.json"
    outcome = ResultAdapter().restore_with_outcome(
        bundle,
        _stage_receipt(
            receipt_id="inf-rcpt-1",
            bundle_id="inf-bnd-1",
            assembly_id="campus_demo",
            config_id="cfg-inf-1",
            attempt_id="att-inf-1",
            primal_size=len(primal_bytes),
        ),
        {PRIMAL_SOLUTION_OUTPUT_PATH: primal_bytes},
        solution_output_path=sol_json_out,
    )
    assert outcome.compute_result.math_status == "infeasible"
    eng_sol = read_engineering_solution(sol_json_out)
    assert eng_sol.solver_summary.has_feasible_solution is False
    assert not eng_sol.construction_units
    assert not eng_sol.series

    eval_in = tmp_path / "eval_in"
    eval_out = tmp_path / "eval_out"
    eval_in.mkdir()
    (eval_in / "solution.json").symlink_to(sol_json_out)
    write_json_file(eval_in / "assembly.json", artifact.to_dict())
    write_json_file(eval_in / "finance.json", finance.to_dict())
    write_json_file(eval_in / "evaluation.json", _evaluation_to_dict(evaluation))
    with pytest.raises(StageIOError, match="non-feasible"):
        run_plugin(
            manifest_path=PLUGINS_ROOT / "evaluator_finance" / "plugin.yaml",
            work_in=eval_in,
            work_out=eval_out,
            pies_version="1.2.0",
        )
