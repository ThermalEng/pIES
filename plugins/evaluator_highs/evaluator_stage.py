"""Official built-in `ies.result-adapter.highs@1.0.0` stage plugin entrypoint."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from iesplan.computation.protocols import (
    ExecutionReceipt,
    SolverBundle,
    SolverBundleManifest,
    thaw_json_value,
)
from iesplan.computation.result_adapters.highs import HIGHS_ADAPTER
from iesplan.core.content import Content
from iesplan.finance import EffectiveFinanceConfig, effective_from_yaml
from pies_sdk import ComputeResultPayload, StageContext, load_safe_yaml, read_json_file

from pies_builtin_plugins.stage_io import (
    FileContent,
    build_financial_evaluation_payload,
    build_hourly_series_payload,
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


def _build_receipt(
    *,
    work_in: Path,
    manifest: SolverBundleManifest,
    request: Mapping[str, Any],
    solution_path: Path | None,
) -> ExecutionReceipt:
    raw_receipt: Mapping[str, Any] | None = None
    if (work_in / "receipt.json").is_file():
        raw_receipt = read_json_file(work_in / "receipt.json")
    elif (work_in / "outputs" / "receipt.json").is_file():
        raw_receipt = read_json_file(work_in / "outputs" / "receipt.json")
    elif isinstance(request.get("receipt"), Mapping):
        raw_receipt = request["receipt"]

    if raw_receipt is not None:
        return ExecutionReceipt(
            receipt_id=str(raw_receipt["receipt_id"]),
            bundle_id=str(raw_receipt["bundle_id"]),
            assembly_id=str(raw_receipt["assembly_id"]),
            calculation_config_id=str(raw_receipt["calculation_config_id"]),
            attempt_id=str(raw_receipt["attempt_id"]),
            generator_ref=str(raw_receipt.get("generator_ref") or manifest.bundle["generator"]),
            solver_ref=str(raw_receipt.get("solver_ref") or manifest.bundle["solver"]),
            executor_ref=str(raw_receipt.get("executor_ref") or manifest.command["executor"]),
            executable_ref=str(raw_receipt.get("executable_ref") or manifest.command["executable"]),
            result_adapter_ref=str(raw_receipt.get("result_adapter_ref") or manifest.result_adapter),
            status=str(raw_receipt["status"]),
            exit_code=raw_receipt.get("exit_code"),
            signal_number=raw_receipt.get("signal_number"),
            termination_reason=str(raw_receipt.get("termination_reason") or ""),
            started_at=str(raw_receipt.get("started_at") or ""),
            ended_at=str(raw_receipt.get("ended_at") or ""),
            error=str(raw_receipt.get("error") or ""),
            stdout=str(raw_receipt.get("stdout") or ""),
            stderr=str(raw_receipt.get("stderr") or ""),
            stdout_truncated=bool(raw_receipt.get("stdout_truncated", False)),
            stderr_truncated=bool(raw_receipt.get("stderr_truncated", False)),
            outputs=tuple(raw_receipt.get("outputs") or ()),
            policy_evaluated=dict(raw_receipt.get("policy_evaluated") or {}),
            resource_usage=dict(raw_receipt.get("resource_usage") or {}),
        )

    meta_path = work_in / "outputs" / "solver_meta.json"
    meta: Mapping[str, Any] = read_json_file(meta_path) if meta_path.is_file() else {}
    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    declared_out = manifest.outputs[0]
    receipt_outputs: list[dict[str, Any]] = []
    if solution_path is not None:
        receipt_outputs.append(
            {
                "path": str(declared_out["path"]),
                "media_type": str(declared_out["media_type"]),
                "size_bytes": solution_path.stat().st_size,
                "required": bool(declared_out["required"]),
            }
        )

    return ExecutionReceipt(
        receipt_id=str(request.get("receipt_id") or uuid.uuid4().hex),
        bundle_id=str(request.get("bundle_id") or "stage-bundle"),
        assembly_id=str(request.get("assembly_id") or "stage-assembly"),
        calculation_config_id=str(request.get("calculation_config_id") or "stage-config"),
        attempt_id=str(request.get("attempt_id") or "stage-attempt"),
        generator_ref=str(manifest.bundle["generator"]),
        solver_ref=str(manifest.bundle["solver"]),
        executor_ref=str(manifest.command["executor"]),
        executable_ref=str(manifest.command["executable"]),
        result_adapter_ref=str(manifest.result_adapter),
        status=str(meta.get("status") or "succeeded"),
        exit_code=int(meta.get("exit_code", 0)),
        signal_number=meta.get("signal_number"),
        termination_reason=str(meta.get("termination_reason") or "exited"),
        started_at=str(meta.get("started_at") or now_iso),
        ended_at=str(meta.get("ended_at") or now_iso),
        error=str(meta.get("error") or ""),
        stdout=str(meta.get("stdout") or ""),
        stderr=str(meta.get("stderr") or ""),
        outputs=tuple(receipt_outputs),
    )


def _load_finance_and_evaluation(
    work_in: Path, request: Mapping[str, Any]
) -> tuple[EffectiveFinanceConfig | None, Mapping[str, Any] | None]:
    finance: EffectiveFinanceConfig | None = None
    if "finance" in request and request["finance"] is not None:
        finance = EffectiveFinanceConfig.from_dict(request["finance"])
    elif (work_in / "finance.json").is_file():
        finance = EffectiveFinanceConfig.from_dict(read_json_file(work_in / "finance.json"))
    elif (work_in / "effective_finance.yaml").is_file():
        finance = effective_from_yaml(
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
    manifest = SolverBundleManifest(**manifest_raw)
    bundle_files: dict[str, Content] = {
        str(entry["path"]): FileContent(bundle_dir / str(entry["path"]))
        for entry in manifest.inputs
    }
    bundle = SolverBundle(manifest=manifest, files=bundle_files)

    request_path = ctx.work_in / "request.json"
    request: Mapping[str, Any] = read_json_file(request_path) if request_path.is_file() else {}
    solution_path = _find_solution_file(ctx.work_in)
    receipt = _build_receipt(
        work_in=ctx.work_in,
        manifest=manifest,
        request=request,
        solution_path=solution_path,
    )

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
