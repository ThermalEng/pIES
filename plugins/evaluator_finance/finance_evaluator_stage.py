"""Official built-in `ies.evaluator.finance@1.0.0` forward financial evaluator stage."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from iesplan.assembly import ValidatedAssemblyArtifact, validate_assembly_text
from iesplan.finance import EffectiveFinanceConfig, EvaluationInput, evaluate_forward_finance
from pies_sdk import (
    ComputeResultPayload,
    StageContext,
    StageIOError,
    load_safe_yaml,
    read_engineering_solution,
    read_json_file,
)

from pies_builtin_plugins.stage_io import (
    artifact_from_frozen,
    effective_finance_from_frozen,
    materialized_from_frozen,
    materialized_from_frozen_series_inputs,
)


def _read_optional_json(path: Path) -> Any | None:
    return read_json_file(path) if path.is_file() else None


def _load_artifact(
    work_in: Path, request: Mapping[str, Any]
) -> ValidatedAssemblyArtifact | Mapping[str, Any]:
    raw_artifact = request.get("artifact")
    if raw_artifact is None:
        raw_artifact = _read_optional_json(work_in / "assembly.json")
    if raw_artifact is not None:
        if not isinstance(raw_artifact, Mapping):
            raise StageIOError("evaluator_finance 'artifact' must be a JSON object")
        if "canonical_text" in raw_artifact and "validation" in raw_artifact:
            return artifact_from_frozen(raw_artifact)
        return raw_artifact

    yaml_path = work_in / "assembly.yaml"
    if yaml_path.is_file():
        validated = validate_assembly_text(
            yaml_path.read_text(encoding="utf-8"),
            package_dir=work_in,
        )
        if not validated.ok or validated.artifact is None:
            raise StageIOError("evaluator_finance /work/in assembly.yaml failed validation")
        return validated.artifact

    raise KeyError(
        "evaluator_finance /work/in missing 'artifact' in request.json, "
        "assembly.json, or assembly.yaml"
    )


def _artifact_resolution(artifact: ValidatedAssemblyArtifact | Mapping[str, Any]) -> str:
    if isinstance(artifact, ValidatedAssemblyArtifact):
        doc = json.loads(artifact.canonical_text)
    elif "canonical_text" in artifact:
        doc = json.loads(str(artifact["canonical_text"]))
    else:
        doc = artifact
    return str(doc["project_baseline"]["resolution"])


def _load_finance(work_in: Path, request: Mapping[str, Any]) -> EffectiveFinanceConfig:
    if request.get("finance") is not None:
        return effective_finance_from_frozen(request["finance"])
    if (work_in / "finance.json").is_file():
        return effective_finance_from_frozen(read_json_file(work_in / "finance.json"))
    if (work_in / "effective_finance.yaml").is_file():
        parsed = load_safe_yaml((work_in / "effective_finance.yaml").read_text(encoding="utf-8"))
        return effective_finance_from_frozen(parsed)
    raise KeyError(
        "evaluator_finance /work/in missing 'finance' in request.json, "
        "finance.json, or effective_finance.yaml"
    )


def _load_evaluation(work_in: Path, request: Mapping[str, Any]) -> EvaluationInput:
    raw_eval = request.get("evaluation")
    if raw_eval is None:
        raw_eval = _read_optional_json(work_in / "evaluation.json")
    if raw_eval is None:
        raise KeyError(
            "evaluator_finance /work/in missing 'evaluation' in request.json or evaluation.json"
        )
    if not isinstance(raw_eval, Mapping):
        raise StageIOError("evaluator_finance 'evaluation' must be a JSON object")

    raw_eval_copy = dict(raw_eval)
    if "comparison" not in raw_eval_copy:
        raw_comp = request.get("comparison")
        if raw_comp is None:
            raw_comp = _read_optional_json(work_in / "comparison.json")
        if raw_comp is not None:
            raw_eval_copy["comparison"] = raw_comp
    return EvaluationInput.from_frozen(raw_eval_copy)


def evaluate(ctx: StageContext) -> None:
    """Evaluate `ies.engineering-solution@1.0.0` via `iesplan.finance.evaluate_forward_finance`."""
    solution_path = ctx.work_in / "solution.json"
    solution = read_engineering_solution(solution_path)
    if not solution.solver_summary.has_feasible_solution:
        raise StageIOError(
            "ies.evaluator.finance cannot evaluate non-feasible engineering solution "
            f"(math_status={solution.solver_summary.math_status!r})"
        )

    req_path = ctx.work_in / "request.json"
    request_raw = read_json_file(req_path) if req_path.is_file() else {}
    if not isinstance(request_raw, Mapping):
        raise StageIOError("evaluator_finance /work/in/request.json must be a JSON object")

    artifact = _load_artifact(ctx.work_in, request_raw)
    finance = _load_finance(ctx.work_in, request_raw)
    evaluation = _load_evaluation(ctx.work_in, request_raw)

    raw_models = request_raw.get("device_models")
    if raw_models is None:
        raw_models = _read_optional_json(ctx.work_in / "device_models.json")
    if raw_models is not None and not isinstance(raw_models, Mapping):
        raise StageIOError("evaluator_finance 'device_models' must be a JSON object")

    prediction_payload = request_raw.get("prediction")
    if prediction_payload is None:
        prediction_payload = _read_optional_json(ctx.work_in / "prediction.json")

    resolution = _artifact_resolution(artifact)

    materialized = None
    prices = None
    device_series = None

    raw_frozen_inputs = request_raw.get("frozen_series_inputs")
    if raw_frozen_inputs is None:
        raw_frozen_inputs = _read_optional_json(ctx.work_in / "frozen_series_inputs.json")

    if raw_frozen_inputs is not None:
        if not isinstance(raw_frozen_inputs, Mapping):
            raise StageIOError("evaluator_finance 'frozen_series_inputs' must be a JSON object")
        if not isinstance(artifact, ValidatedAssemblyArtifact):
            artifact = artifact_from_frozen(artifact)
        materialized = materialized_from_frozen_series_inputs(
            raw_frozen_inputs,
            work_in=ctx.work_in,
            artifact=artifact,
            finance=finance,
            operation_years=evaluation.operation_years,
            prediction_payload=prediction_payload,
            resolution=resolution,
        )
    else:
        raw_materialized = request_raw.get("materialized")
        if raw_materialized is None:
            raw_materialized = _read_optional_json(ctx.work_in / "sequences.json")
        if raw_materialized is not None:
            if not isinstance(raw_materialized, Mapping):
                raise StageIOError("evaluator_finance 'materialized' must be a JSON object")
            materialized = materialized_from_frozen(
                raw_materialized,
                prediction_payload=prediction_payload,
                resolution=resolution,
            )
        else:
            prices = request_raw.get("prices")
            if prices is None:
                prices = _read_optional_json(ctx.work_in / "prices.json")
            device_series = request_raw.get("device_series")
            if device_series is None:
                device_series = _read_optional_json(ctx.work_in / "device_series.json")

    fin_eval = evaluate_forward_finance(
        solution.to_dict(),
        artifact=artifact,
        finance=finance,
        evaluation=evaluation,
        materialized=materialized,
        prices=prices,
        device_series=device_series,
        device_models=raw_models,
    )

    receipt_raw = request_raw.get("receipt")
    if receipt_raw is None:
        receipt_raw = _read_optional_json(ctx.work_in / "receipt.json")
    receipt_map = receipt_raw if isinstance(receipt_raw, Mapping) else {}

    bundle_id = str(
        request_raw.get("bundle_id")
        or receipt_map.get("bundle_id")
        or "ies.engineering-solution@1.0.0"
    )
    receipt_id = str(
        request_raw.get("receipt_id")
        or receipt_map.get("receipt_id")
        or ctx.manifest.ref
    )

    ctx.write_result(
        ComputeResultPayload(
            bundle_id=bundle_id,
            receipt_id=receipt_id,
            execution_status="completed",
            math_status=solution.solver_summary.math_status,
            outputs={"financial_evaluation": fin_eval},
        )
    )
