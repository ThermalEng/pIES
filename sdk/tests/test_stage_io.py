"""Tests for /work/in -> /work/out stage I/O contracts and iterations.jsonl helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from pies_sdk.contracts import ComputeResultPayload, PredictionOutputPayload
from pies_sdk.errors import StageIOError
from pies_sdk.io import (
    extract_stage_parameters,
    read_algorithm_request,
    read_engineering_solution,
    read_evaluation_report,
    read_financial_evaluation,
    read_generator_inputs,
    read_prediction_output,
    read_predictor_request,
    read_primal_solution,
    read_solver_bundle,
    validate_stage_outputs,
    write_engineering_solution,
    write_evaluation_report,
    write_financial_evaluation,
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

    # Evaluators emit canonical financial_evaluation.json or evaluation_report.json (no result.json)
    eval_out = tmp_path / "eval_out"
    sample_fin_eval = {
        "schema": "ies.financial-evaluation",
        "schema_version": "1.0.0",
        "status": "evaluated",
        "evaluation_years": 1,
        "construction_period": 0,
        "operation_years": 1,
        "currency": "CNY",
        "price_basis": "tax_inclusive",
        "base_year": 2025,
        "profile_id": "pure-fwd",
        "discount_rate": 0.0,
        "construction_total": "1000.0",
        "construction_total_unit": "CNY",
        "annual_fixed_om": "30.0",
        "payments_applied": [],
        "net_cashflows": [],
        "cost_items": [],
        "amounts": [],
        "cash_movements": [],
        "assets": [],
        "construction_units": [],
        "annual_operation": [],
        "monthly_demand": [],
        "tax_cash": {
            "depreciation": [],
            "derived": [],
            "income": [],
            "loss_balances": [],
            "refunds": [],
            "vat": [],
            "vat_rules": [],
        },
        "annual_net_flows": ["-1000.0", "6072.5"],
        "comparison": {"mode": "absolute"},
        "evaluation": {
            "schema": "ies.project-evaluation",
            "schema_version": "1.0.0",
            "npv": "5072.5",
            "irr_status": "defined",
            "rows": [],
        },
    }
    write_financial_evaluation(eval_out / "financial_evaluation.json", sample_fin_eval)
    validate_stage_outputs(
        "evaluator",
        eval_out,
        produces=("ies.financial-evaluation@1.0.0",),
    )
    assert read_financial_evaluation(eval_out / "financial_evaluation.json")["status"] == "evaluated"

    bare_fin_out = tmp_path / "bare_fin_out"
    write_json_file(
        bare_fin_out / "financial_evaluation.json",
        {"schema": "ies.financial-evaluation", "schema_version": "1.0.0", "status": "evaluated"},
    )
    with pytest.raises(StageIOError, match="requires"):
        validate_stage_outputs("evaluator", bare_fin_out, produces=("ies.financial-evaluation@1.0.0",))

    bad_tax_out = tmp_path / "bad_tax_out"
    bad_tax = {**sample_fin_eval, "tax_cash": {"income": []}}
    write_json_file(bad_tax_out / "financial_evaluation.json", bad_tax)
    with pytest.raises(StageIOError, match="tax_cash"):
        validate_stage_outputs("evaluator", bad_tax_out, produces=("ies.financial-evaluation@1.0.0",))

    for field, bad_val, match in (
        ("annual_operation", [None], "annual_operation\\[0\\]"),
        ("payments_applied", [{"t": 0}], "payments_applied\\[0\\]"),
        ("annual_net_flows", [None], "annual_net_flows"),
        ("construction_units", [{"unit_id": "u1"}], "construction_units\\[0\\]"),
    ):
        bad_nested_out = tmp_path / f"bad_nested_{field}"
        write_json_file(bad_nested_out / "financial_evaluation.json", {**sample_fin_eval, field: bad_val})
        with pytest.raises(StageIOError, match=match):
            validate_stage_outputs(
                "evaluator", bad_nested_out, produces=("ies.financial-evaluation@1.0.0",)
            )

    bad_eval_rows = tmp_path / "bad_eval_rows"
    write_json_file(
        bad_eval_rows / "financial_evaluation.json",
        {**sample_fin_eval, "evaluation": {**sample_fin_eval["evaluation"], "rows": [None]}},
    )
    with pytest.raises(StageIOError, match="evaluation.rows\\[0\\]"):
        validate_stage_outputs("evaluator", bad_eval_rows, produces=("ies.financial-evaluation@1.0.0",))

    bad_report_out = tmp_path / "bad_report_out"
    write_json_file(
        bad_report_out / "evaluation_report.json",
        {
            "schema": "ies.evaluation-report",
            "schema_version": "1.0.0",
            "title": "Bad",
            "evaluator": "acme.evaluator.carbon@1.0.0",
            "metrics": [None],
            "charts": [],
            "tables": [],
        },
    )
    with pytest.raises(StageIOError, match="EvaluationMetric"):
        validate_stage_outputs(
            "evaluator",
            bad_report_out,
            produces=("ies.evaluation-report@1.0.0",),
        )

    legacy_only = tmp_path / "legacy_eval_out"
    write_json_file(legacy_only / "result.json", {"schema": "ies.compute-result", "schema_version": "1.0.0"})
    with pytest.raises(StageIOError, match="missing 'evaluation_report.json' or 'financial_evaluation.json'"):
        validate_stage_outputs("evaluator", legacy_only)

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
    validate_stage_outputs(
        "evaluator",
        report_out,
        produces=("ies.evaluation-report@1.0.0",),
    )
    assert read_evaluation_report(report_out / "evaluation_report.json").title == "Carbon Report"


def test_extract_stage_parameters_reads_canonical_request_parameters_only(tmp_path: Path) -> None:
    req_in = tmp_path / "req_params"
    write_json_file(
        req_in / "request.json",
        {"parameters": {"threads": 4}, "options": {"threads": 1}},
    )
    write_json_file(req_in / "calculation_config.json", {"options": {"threads": 2}})
    assert extract_stage_parameters(req_in) == {"threads": 4}

    opts_only = tmp_path / "opts_only"
    write_json_file(opts_only / "request.json", {"options": {"solver": {"threads": 4}}})
    write_json_file(opts_only / "calculation_config.json", {"options": {"solver": {"threads": 2}}})
    assert extract_stage_parameters(opts_only) == {}

    bad_params = tmp_path / "bad_params"
    write_json_file(bad_params / "request.json", {"parameters": ["not", "an", "object"]})
    with pytest.raises(StageIOError, match="'parameters' must be a JSON object"):
        extract_stage_parameters(bad_params)


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
