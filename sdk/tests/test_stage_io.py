"""Tests for /work/in -> /work/out stage I/O contracts and iterations.jsonl helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from pies_sdk.contracts import ComputeResultPayload, PredictionOutputPayload
from pies_sdk.errors import StageIOError
from pies_sdk.io import (
    read_algorithm_request,
    read_compute_result,
    read_engineering_solution,
    read_evaluation_report,
    read_generator_inputs,
    read_prediction_output,
    read_predictor_request,
    read_primal_solution,
    read_solver_bundle,
    validate_stage_outputs,
    write_compute_result,
    write_engineering_solution,
    write_evaluation_report,
    write_json_file,
    write_prediction_output,
    write_primal_solution,
    write_solver_bundle,
)
from pies_sdk.iteration import IterationRecord, IterationWriter, read_iterations


def _sample_bundle_manifest() -> dict:
    return {
        "schema": "ies.solver-bundle",
        "schema_version": "1.0.0",
        "bundle": {
            "generator": "ies.generator.highs_milp@1.0.0",
            "contract": "ies.solver-bundle.mps@1.0.0",
        },
        "inputs": [
            {"path": "input/problem.mps", "media_type": "application/mps"},
        ],
        "outputs": [
            {
                "path": "output/primal_solution.json",
                "media_type": "application/json",
                "required": True,
            },
        ],
        "extensions": {},
    }


def test_predictor_stage_io(tmp_path: Path) -> None:
    work_in = tmp_path / "in"
    work_out = tmp_path / "out"
    write_json_file(
        work_in / "request.json",
        {"parameters": {"window": 4}, "steps": 3, "history": [1.0, 2.0, 3.0]},
    )
    req = read_predictor_request(work_in)
    assert req["steps"] == 3

    out_payload = PredictionOutputPayload(
        predictions=[{"instance": "pv1", "interface": "power_out", "values": [1.0, 2.0, 3.0]}]
    )
    write_prediction_output(work_out, out_payload)
    validate_stage_outputs("predictor", work_out)
    assert read_prediction_output(work_out).predictions[0]["instance"] == "pv1"


def test_generator_solver_evaluator_stage_io(tmp_path: Path) -> None:
    gen_in = tmp_path / "gen_in"
    gen_out = tmp_path / "gen_out"
    write_json_file(gen_in / "calculation_config.json", {"mode": "planning", "options": {}})
    gen_inputs = read_generator_inputs(gen_in)
    assert gen_inputs["calculation_config"]["mode"] == "planning"

    bundle_manifest = _sample_bundle_manifest()
    write_solver_bundle(gen_out, bundle_manifest, {"input/problem.mps": "NAME TEST\nENDATA\n"})
    validate_stage_outputs("generator", gen_out)

    # Solver consumes frozen generator bundle directory and emits primal_solution.json
    solver_in = tmp_path / "solver_in"
    solver_out = tmp_path / "solver_out"
    (solver_in / "bundle" / "input").mkdir(parents=True)
    (solver_in / "bundle" / "bundle.yaml").write_text(
        (gen_out / "bundle" / "bundle.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (solver_in / "bundle" / "input" / "problem.mps").write_text("NAME TEST\nENDATA\n", encoding="utf-8")
    loaded_manifest, _ = read_solver_bundle(solver_in)
    assert loaded_manifest["schema"] == "ies.solver-bundle"

    write_primal_solution(
        solver_out / "primal_solution.json",
        {
            "schema": "ies.primal-solution",
            "schema_version": "1.0.0",
            "math_status": "solved",
            "termination_reason": "optimal",
            "has_feasible_solution": True,
            "column_count": 2,
            "objective_value": 10.0,
            "best_bound": 10.0,
            "relative_gap": 0.0,
            "values": [1.0, 2.0],
            "receipt": {"solver": "ies.solver.highs@1.7.2"},
        },
    )
    validate_stage_outputs("solver", solver_out)
    assert read_primal_solution(solver_out / "primal_solution.json").column_count == 2

    # Algorithm emits ies.engineering-solution@1.0.0 (solution.json)
    alg_out = tmp_path / "alg_out"
    write_engineering_solution(
        alg_out / "solution.json",
        {
            "schema": "ies.engineering-solution",
            "schema_version": "1.0.0",
            "solver_summary": {
                "math_status": "infeasible",
                "termination_reason": "infeasible",
                "has_feasible_solution": False,
                "objective_value": None,
                "objective_unit": None,
                "best_bound": None,
                "relative_gap": None,
            },
        },
    )
    validate_stage_outputs("algorithm", alg_out)
    assert read_engineering_solution(alg_out / "solution.json").solver_summary.math_status == "infeasible"

    # Evaluators support both official result.json and third-party evaluation_report.json
    eval_out = tmp_path / "eval_out"
    write_compute_result(
        eval_out,
        ComputeResultPayload(
            bundle_id="bnd-1",
            receipt_id="rcpt-1",
            execution_status="completed",
            math_status="solved",
            outputs={"objective": 10.0},
        ),
    )
    validate_stage_outputs("evaluator", eval_out)
    assert read_compute_result(eval_out).math_status == "solved"

    report_out = tmp_path / "report_out"
    write_evaluation_report(
        report_out / "evaluation_report.json",
        {
            "schema": "ies.evaluation-report",
            "schema_version": "1.0.0",
            "title": "Carbon Report",
            "evaluator": "acme.evaluator.carbon@1.0.0",
            "metrics": [{"id": "co2", "label": "CO2", "value": 12.5, "unit": "t"}],
            "charts": [],
            "tables": [],
        },
    )
    validate_stage_outputs("evaluator", report_out)
    assert read_evaluation_report(report_out / "evaluation_report.json").title == "Carbon Report"


def test_algorithm_and_iterations_ndjson(tmp_path: Path) -> None:
    work_in = tmp_path / "in"
    work_out = tmp_path / "out"
    write_json_file(work_in / "request.json", {"bundle_id": "b1", "receipt_id": "r1"})
    assert read_algorithm_request(work_in)["bundle_id"] == "b1"

    log_path = work_out / "iterations.jsonl"
    with IterationWriter(log_path, truncate=True) as writer:
        writer.append(
            IterationRecord(
                iteration=1, metric="max_relative_delta", value=0.25, status="running"
            )
        )
        writer.append(
            IterationRecord(
                iteration=2, metric="max_relative_delta", value=0.005, status="converged"
            )
        )

    records = read_iterations(log_path)
    assert len(records) == 2
    assert records[0].unit == "1"
    assert records[1].status == "converged"


def test_rejects_non_finite_and_path_traversal(tmp_path: Path) -> None:
    with pytest.raises(StageIOError, match="non-finite"):
        ComputeResultPayload(
            bundle_id="b1",
            receipt_id="r1",
            execution_status="completed",
            math_status="solved",
            outputs={"bad": float("inf")},
        )

    with pytest.raises(StageIOError, match="escapes"):
        write_solver_bundle(
            tmp_path,
            {
                **_sample_bundle_manifest(),
                "inputs": [{"path": "../escape.txt", "media_type": "text/plain"}],
            },
            {"../escape.txt": "x"},
        )


def test_prediction_output_rejects_malformed_structure_and_coercion() -> None:
    with pytest.raises(StageIOError, match="must be a list"):
        PredictionOutputPayload.from_dict(
            {
                "schema": "ies.prediction-output",
                "schema_version": "1.0.0",
                "predictions": "abc",
            }
        )

    with pytest.raises(StageIOError, match="must be a JSON object"):
        PredictionOutputPayload.from_dict(
            {
                "schema": "ies.prediction-output",
                "schema_version": "1.0.0",
                "predictions": [42],
            }
        )

    with pytest.raises(StageIOError, match="must be a finite number"):
        PredictionOutputPayload(
            predictions=[{"instance": "pv1", "interface": "power_out", "values": [True]}]
        )

    with pytest.raises(StageIOError, match="must be a list"):
        PredictionOutputPayload.from_dict(
            {
                "schema": "ies.prediction-output",
                "schema_version": "1.0.0",
                "predictions": [{"instance": "pv1", "interface": "power_out", "values": [1.0]}],
                "artifacts": "not-a-list",
            }
        )
