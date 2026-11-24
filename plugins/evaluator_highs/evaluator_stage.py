"""Official built-in `ies.result-adapter.highs@1.0.0` stage plugin entrypoint."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from iesplan.computation.protocols import ExecutionReceipt, thaw_json_value
from iesplan.computation.result_adapters.highs import HIGHS_ADAPTER
from iesplan.core.content import Content
from pies_sdk import ComputeResultPayload, StageContext, load_safe_yaml, read_json_file

from pies_builtin_plugins.stage_io import (
    FileContent,
    build_financial_evaluation_payload,
    build_hourly_series_payload,
    bundle_manifest_from_frozen,
    execution_receipt_from_frozen,
    solver_bundle_from_frozen,
)


def _find_solution_file(work_in: Path) -> Path | None:
    """Locate solver `solution.*` from `/work/in/outputs/`, `/work/in/`, or `/work/in/bundle/output/`."""
    for directory in (work_in / "outputs", work_in, work_in / "bundle" / "output"):
        if directory.is_dir():
            matches = sorted(
                p for p in directory.iterdir() if p.is_file() and p.name.startswith("solution.")
            )
            if matches:
                return matches[0]
    return None


def _load_receipt(work_in: Path, request: Mapping[str, Any]) -> ExecutionReceipt:
    """Require and reconstruct the real `ExecutionReceipt` from `/work/in/` without fabrication."""
    raw_receipt: Mapping[str, Any] | None = None
    if (work_in / "receipt.json").is_file():
        raw_receipt = read_json_file(work_in / "receipt.json")
    elif (work_in / "outputs" / "receipt.json").is_file():
        raw_receipt = read_json_file(work_in / "outputs" / "receipt.json")
    elif isinstance(request.get("receipt"), Mapping):
        raw_receipt = request["receipt"]

    if raw_receipt is None:
        raise KeyError(
            "evaluator /work/in missing required ExecutionReceipt "
            "(receipt.json, outputs/receipt.json, or request.json['receipt'])"
        )
    return execution_receipt_from_frozen(raw_receipt)


def _load_finance_and_evaluation(
    work_in: Path, request: Mapping[str, Any]
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
    finance: Mapping[str, Any] | None = None
    if "finance" in request and request["finance"] is not None:
        finance = request["finance"]
    elif (work_in / "finance.json").is_file():
        finance = read_json_file(work_in / "finance.json")
    elif (work_in / "effective_finance.yaml").is_file():
        finance = load_safe_yaml(
            (work_in / "effective_finance.yaml").read_text(encoding="utf-8")
        )

    evaluation: Mapping[str, Any] | None = None
    if "evaluation" in request and request["evaluation"] is not None:
        evaluation = request["evaluation"]
    elif (work_in / "evaluation.json").is_file():
        evaluation = read_json_file(work_in / "evaluation.json")

    return finance, evaluation


def evaluate(ctx: StageContext) -> None:
    """Adapt solver outputs + frozen finance/evaluation into `/work/out/result.json`."""
    bundle_dir = ctx.work_in / "bundle"
    manifest_raw = load_safe_yaml((bundle_dir / "bundle.yaml").read_text(encoding="utf-8"))
    manifest = bundle_manifest_from_frozen(manifest_raw)
    bundle_files: dict[str, Content] = {
        str(entry["path"]): FileContent(bundle_dir / str(entry["path"]))
        for entry in manifest.inputs
    }
    bundle = solver_bundle_from_frozen(manifest, bundle_files)

    request_path = ctx.work_in / "request.json"
    request: Mapping[str, Any] = read_json_file(request_path) if request_path.is_file() else {}
    receipt = _load_receipt(ctx.work_in, request)
    solution_path = _find_solution_file(ctx.work_in)

    outputs_map: dict[str, Content] = {}
    if solution_path is not None:
        declared_sol_path = str(manifest.outputs[0]["path"])
        outputs_map[declared_sol_path] = FileContent(solution_path)

    result = HIGHS_ADAPTER.adapt(bundle, receipt, outputs_map)
    outputs_payload = dict(thaw_json_value(result.outputs))

    hourly_series = build_hourly_series_payload(outputs_payload)
    if hourly_series is not None:
        outputs_payload["hourly_series"] = hourly_series

    finance, evaluation = _load_finance_and_evaluation(ctx.work_in, request)
    if finance is not None or evaluation is not None:
        if finance is None or evaluation is None:
            raise KeyError(
                "evaluator requires both frozen 'finance' and 'evaluation' when evaluating"
            )
        fin_eval = build_financial_evaluation_payload(evaluation, finance, result)
        if fin_eval is not None:
            outputs_payload["financial_evaluation"] = fin_eval

    ctx.write_result(
        ComputeResultPayload(
            bundle_id=result.bundle_id,
            receipt_id=result.receipt_id,
            execution_status=result.execution_status,
            math_status=result.math_status,
            outputs=outputs_payload,
        )
    )
