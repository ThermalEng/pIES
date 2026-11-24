"""End-to-end equivalence tests comparing 4 stage plugins against in-process iesplan."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from iesplan.assembly import validate_assembly_text
from iesplan.computation import (
    HIGHS_ADAPTER,
    HIGHS_ADAPTER_PATH,
    HIGHS_EXECUTABLE_REF,
    HIGHS_MILP_PROVIDER,
    HIGHS_VERSION,
    NAIVE_PREDICTOR,
    NAIVE_PREDICTOR_REF,
    AttemptContext,
    CalculationConfig,
    DeploymentPolicy,
    ExecutableReadiness,
    ExecutorDescriptor,
    LocalExecutable,
    LocalProcessProvider,
    MaterializedSet,
    SolverRuntime,
    build_executor_registry,
    materialize_non_predictive_series,
    materialize_predicted_series,
    thaw_json_value,
)
from iesplan.computation.bundle import export_bundle_files
from iesplan.computation.generators.highs_milp import EXECUTOR_REF, GENERATOR_REF, SOLVER_REF
from iesplan.core.content import read_content
from iesplan.devices import init_registry, list_devices
from iesplan.finance import EvaluationInput, effective_from_yaml
from iesplan.modeling import build_math_contribution, contribution_to_dict
from pies_sdk import (
    read_compute_result,
    read_prediction_output,
    validate_solver_bundle_dir,
    validate_stage_outputs,
    write_json_file,
)
from pies_sdk.runner import run_plugin

from pies_builtin_plugins.stage_io import (
    build_financial_evaluation_payload,
    build_hourly_series_payload,
)

PLUGINS_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PLUGINS_ROOT.parent
IESPLAN_SAMPLES = REPO_ROOT / "backend" / "iesplan" / "assembly" / "samples"


def _runtime() -> SolverRuntime:
    executor_id, _, executor_version = EXECUTOR_REF.rpartition("@")
    provider = LocalProcessProvider(
        ExecutorDescriptor(
            executor_id=executor_id,
            version=executor_version,
            executables=(HIGHS_EXECUTABLE_REF,),
        ),
        (
            LocalExecutable(
                ref=HIGHS_EXECUTABLE_REF,
                path=HIGHS_ADAPTER_PATH,
                readiness=ExecutableReadiness(
                    version=HIGHS_VERSION,
                    self_test=("--self-test",),
                    output_path="self-test.sol",
                ),
            ),
        ),
        base_environ={},
    )
    policy = DeploymentPolicy(
        executables=(HIGHS_EXECUTABLE_REF,),
        environment_vars=(),
        max_timeout_seconds=1200,
        max_memory_mib=4096,
        max_cpu_cores=4,
        allow_network=False,
    )
    return SolverRuntime(build_executor_registry([provider]), policy)


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
            {"t": p.t, "ratio": p.ratio} for p in ev.construction_payments
        ],
        "maintenance_expenses": [],
        "comparison": ev.comparison.to_dict(),
    }


def test_campus_full_pipeline_equivalence(tmp_path: Path) -> None:
    """Full campus MILP: generator -> solver -> evaluator matches in-process iesplan."""
    artifact, models, resources, finance, evaluation = _load_campus_inputs()
    materialized = materialize_non_predictive_series(artifact, resources, operation_years=1)
    assert not materialized.pending_predict

    config = CalculationConfig(
        mode="planning",
        generator=GENERATOR_REF,
        solver=SOLVER_REF,
        evaluator=HIGHS_ADAPTER.ref,
        precision="medium",
        time_limit_seconds=600,
        outputs=("solution", "metrics"),
        seed=None,
    )

    # 1) Direct in-process iesplan execution
    direct_bundle = HIGHS_MILP_PROVIDER.generate(
        artifact, models, materialized, finance, config, evaluation
    )
    direct_attempt = AttemptContext(
        attempt_id="attempt-eq-1",
        bundle_id="bundle-eq-1",
        assembly_id="campus_demo",
        calculation_config_id="cfg-eq-1",
    )
    direct_receipt, direct_outputs = _runtime().run(direct_bundle, direct_attempt)
    assert direct_receipt.status == "succeeded"
    direct_result = HIGHS_ADAPTER.adapt(direct_bundle, direct_receipt, direct_outputs)
    direct_eval = build_financial_evaluation_payload(
        _evaluation_to_dict(evaluation), finance, direct_result
    )
    direct_hourly = build_hourly_series_payload(direct_result.outputs)

    # 2) Stage-by-stage plugin pipeline via pies_sdk.runner
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
    )
    validate_stage_outputs("generator", gen_out)
    validate_solver_bundle_dir(gen_out / "bundle")

    # Byte-for-byte identical Solver Bundle files
    expected_bundle_files = export_bundle_files(direct_bundle)
    for rel_path, content in expected_bundle_files.items():
        actual_bytes = (gen_out / "bundle" / rel_path).read_bytes()
        assert actual_bytes == read_content(content), f"Bundle mismatch in {rel_path}"

    # Stage 3: solver_highs consuming Stage 2 /work/out/bundle via symlink handoff
    solver_in = tmp_path / "solver_in"
    solver_out = tmp_path / "solver_out"
    solver_in.mkdir()
    (solver_in / "bundle").symlink_to(gen_out / "bundle")
    run_plugin(
        manifest_path=PLUGINS_ROOT / "solver_highs" / "plugin.yaml",
        work_in=solver_in,
        work_out=solver_out,
    )
    validate_stage_outputs("solver", solver_out)
    assert (solver_out / "solution.sol").read_bytes() == read_content(
        direct_outputs["output/solution.sol"]
    )

    # Stage 4: evaluator_highs consuming bundle + solver /work/out + frozen finance/evaluation
    eval_in = tmp_path / "eval_in"
    eval_out = tmp_path / "eval_out"
    eval_in.mkdir()
    (eval_in / "bundle").symlink_to(gen_out / "bundle")
    (eval_in / "outputs").symlink_to(solver_out)
    write_json_file(
        eval_in / "request.json",
        {
            "bundle_id": direct_attempt.bundle_id,
            "receipt_id": "rcpt-stage-eq-1",
            "assembly_id": direct_attempt.assembly_id,
            "calculation_config_id": direct_attempt.calculation_config_id,
            "attempt_id": direct_attempt.attempt_id,
            "finance": finance.to_dict(),
            "evaluation": _evaluation_to_dict(evaluation),
        },
    )
    run_plugin(
        manifest_path=PLUGINS_ROOT / "evaluator_highs" / "plugin.yaml",
        work_in=eval_in,
        work_out=eval_out,
    )
    validate_stage_outputs("evaluator", eval_out)
    stage_result = read_compute_result(eval_out)

    assert stage_result.execution_status == direct_result.execution_status == "completed"
    assert stage_result.math_status == direct_result.math_status == "solved"
    assert stage_result.outputs["solution"]["objective_value"] == pytest.approx(850_051.6, abs=1e-5)

    direct_outputs_thawed = dict(thaw_json_value(direct_result.outputs))
    for key in (
        "solver_summary",
        "solution",
        "series",
        "system_series",
        "financial_facts",
        "model_sources",
        "metrics",
    ):
        assert stage_result.outputs[key] == direct_outputs_thawed[key], f"Mismatch in {key}"
    assert stage_result.outputs["financial_evaluation"] == direct_eval
    assert stage_result.outputs["hourly_series"] == direct_hourly


def test_four_stage_pipeline_with_predictor_equivalence(tmp_path: Path) -> None:
    """4-stage pipeline with data_predict: predictor -> generator -> solver -> evaluator."""
    samples_copy = tmp_path / "samples"
    shutil.copytree(IESPLAN_SAMPLES, samples_copy)
    load_csv = samples_copy / "data" / "campus_load.data.csv"
    load_csv.write_text(
        load_csv.read_text(encoding="utf-8").replace(
            "# source_mode: data_repeat", "# source_mode: data_predict", 1
        ),
        encoding="utf-8",
    )
    source_text = (samples_copy / "valid" / "campus.assembly.yaml").read_text(encoding="utf-8")
    predict_text = source_text.replace(
        "      electricity_demand:\n"
        "        mode: data_repeat\n"
        "        path: data/campus_load.data.csv",
        "      electricity_demand:\n"
        "        mode: data_predict\n"
        "        path: data/campus_load.data.csv\n"
        "        target_type: load",
        1,
    )
    assert "mode: data_predict" in predict_text
    artifact, models, resources, finance, evaluation = _load_campus_inputs(
        assembly_text=predict_text, samples_dir=samples_copy
    )

    base_mat = materialize_non_predictive_series(artifact, resources, operation_years=1)
    assert len(base_mat.pending_predict) == 1
    pending = base_mat.pending_predict[0]
    assert pending["device"] == "elec_load"
    assert pending["interface"] == "electricity_demand"

    pred_entry = {
        "target": {"instance": "elec_load", "interface": "electricity_demand"},
        "algorithm": NAIVE_PREDICTOR_REF,
        "parameters": {},
    }
    config = CalculationConfig(
        mode="planning",
        predictions=(pred_entry,),
        generator=GENERATOR_REF,
        solver=SOLVER_REF,
        evaluator=HIGHS_ADAPTER.ref,
        precision="medium",
        time_limit_seconds=600,
        outputs=("solution", "metrics"),
        seed=42,
    )

    # Direct in-process prediction + generation
    predicted_targets = materialize_predicted_series(
        artifact,
        resources,
        config.predictions,
        lambda ref: NAIVE_PREDICTOR if ref == NAIVE_PREDICTOR_REF else None,  # type: ignore[return-value]
        config.seed,
        operation_years=1,
    )
    assert len(predicted_targets) == 1
    pred_target = predicted_targets[0]

    from iesplan.computation import MaterializedSeries

    direct_mat = MaterializedSet(
        operation_years=base_mat.operation_years,
        series=base_mat.series
        + (
            MaterializedSeries(
                device=pred_target.device,
                interface=pred_target.interface,
                mode="data_predict",
                resolution="1h",
                point_count=len(pred_target.values),
                values=pred_target.values,
                source={"kind": "data_predict", "object_id": pred_target.history_object_id},
            ),
        ),
        pending_predict=(),
        prices=base_mat.prices,
    )
    direct_bundle = HIGHS_MILP_PROVIDER.generate(
        artifact, models, direct_mat, finance, config, evaluation
    )

    # Stage 1: predictor_naive using zero-copy CSV relative path
    pred_in = tmp_path / "pred_in"
    pred_out = tmp_path / "pred_out"
    (pred_in / "resources").mkdir(parents=True)
    csv_bytes = resources[str(pending["object_id"])]
    (pred_in / "resources" / "load.csv").write_bytes(csv_bytes)
    write_json_file(
        pred_in / "request.json",
        {
            "point_count": 8760,
            "seed": config.seed,
            "targets": [
                {
                    "instance": "elec_load",
                    "interface": "electricity_demand",
                    "target_type": str(pending["target_type"]),
                    "history_object_id": str(pending["object_id"]),
                    "history_csv_rel": "resources/load.csv",
                    "parameters": {},
                }
            ],
        },
    )
    run_plugin(
        manifest_path=PLUGINS_ROOT / "predictor_naive" / "plugin.yaml",
        work_in=pred_in,
        work_out=pred_out,
    )
    validate_stage_outputs("predictor", pred_out)
    pred_output = read_prediction_output(pred_out)
    assert len(pred_output.predictions) == 1
    p_item = pred_output.predictions[0]
    assert p_item["values"] == list(pred_target.values)
    assert p_item["receipt"] == dict(pred_target.receipt)
    assert (pred_out / p_item["artifact_path"]).read_bytes() == pred_target.artifact

    # Stage 2: generator_highs_milp directly consuming /work/in/prediction.json
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
    )
    expected_bundle_files = export_bundle_files(direct_bundle)
    for rel_path, content in expected_bundle_files.items():
        assert (gen_out / "bundle" / rel_path).read_bytes() == read_content(content)

    # Stage 3 + Stage 4
    solver_in = tmp_path / "solver_in"
    solver_out = tmp_path / "solver_out"
    solver_in.mkdir()
    (solver_in / "bundle").symlink_to(gen_out / "bundle")
    run_plugin(
        manifest_path=PLUGINS_ROOT / "solver_highs" / "plugin.yaml",
        work_in=solver_in,
        work_out=solver_out,
    )

    eval_in = tmp_path / "eval_in"
    eval_out = tmp_path / "eval_out"
    eval_in.mkdir()
    (eval_in / "bundle").symlink_to(gen_out / "bundle")
    (eval_in / "outputs").symlink_to(solver_out)
    write_json_file(
        eval_in / "request.json",
        {
            "bundle_id": "pred-bnd-1",
            "receipt_id": "pred-rcpt-1",
            "assembly_id": "campus_demo",
            "calculation_config_id": "cfg-pred-1",
            "attempt_id": "att-pred-1",
            "finance": finance.to_dict(),
            "evaluation": _evaluation_to_dict(evaluation),
        },
    )
    run_plugin(
        manifest_path=PLUGINS_ROOT / "evaluator_highs" / "plugin.yaml",
        work_in=eval_in,
        work_out=eval_out,
    )
    result = read_compute_result(eval_out)
    assert result.execution_status == "completed"
    assert result.math_status == "solved"
    assert "financial_evaluation" in result.outputs
    assert result.outputs["financial_evaluation"]["evaluation"]["schema"] == "ies.project-evaluation"
    assert "hourly_series" in result.outputs


def test_infeasible_model_pipeline_reports_infeasible(tmp_path: Path) -> None:
    """Infeasible model passes Stage 3 cleanly and Stage 4 maps kInfeasible without evaluation."""
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
        generator=GENERATOR_REF,
        solver=SOLVER_REF,
        evaluator=HIGHS_ADAPTER.ref,
        precision="medium",
        time_limit_seconds=600,
        outputs=("solution", "metrics"),
        seed=0,
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
    )

    solver_in = tmp_path / "solver_in"
    solver_out = tmp_path / "solver_out"
    solver_in.mkdir()
    (solver_in / "bundle").symlink_to(gen_out / "bundle")
    run_plugin(
        manifest_path=PLUGINS_ROOT / "solver_highs" / "plugin.yaml",
        work_in=solver_in,
        work_out=solver_out,
    )

    eval_in = tmp_path / "eval_in"
    eval_out = tmp_path / "eval_out"
    eval_in.mkdir()
    (eval_in / "bundle").symlink_to(gen_out / "bundle")
    (eval_in / "outputs").symlink_to(solver_out)
    write_json_file(
        eval_in / "request.json",
        {
            "bundle_id": "inf-bnd-1",
            "receipt_id": "inf-rcpt-1",
            "assembly_id": "campus_demo",
            "calculation_config_id": "cfg-inf-1",
            "attempt_id": "att-inf-1",
            "finance": finance.to_dict(),
            "evaluation": _evaluation_to_dict(evaluation),
        },
    )
    run_plugin(
        manifest_path=PLUGINS_ROOT / "evaluator_highs" / "plugin.yaml",
        work_in=eval_in,
        work_out=eval_out,
    )

    result = read_compute_result(eval_out)
    assert result.execution_status == "completed"
    assert result.math_status == "infeasible"
    assert result.outputs["solver_summary"]["termination_reason"] == "infeasible"
    assert "financial_evaluation" not in result.outputs
