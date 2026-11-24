"""Durable stage handoff through existing storage objects and attempt owners."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from iesplan import tasks
from iesplan.application.tasks import Claim
from iesplan.application.worker.compute_cases import EvaluatedStageOutput, seal_output_objects
from iesplan.application.worker.lease_cases import LeaseRejectedError, verify_lease
from iesplan.application.worker.transactions import run_worker_transaction
from iesplan.computation import (
    COMPUTE_RESULT_SCHEMA,
    COMPUTE_RESULT_VERSION,
    AttemptContext,
    ComputationUnavailableError,
    ComputeResult,
    ExecutionReceipt,
    MaterializedSeries,
    PredictedTarget,
    SolverBundle,
    SolverBundleManifest,
    freeze_json_value,
    thaw_json_value,
)
from iesplan.computation.bundle import export_bundle_files
from iesplan.computation.runtime.stage_runner import StageExecutionSpec
from iesplan.core.content import Content, GeneratedContent, json_chunks, read_content
from iesplan.storage import add_ref, find_refs_by_owner, get_object, get_object_content, put_object


@dataclass(frozen=True, slots=True)
class SavedRound:
    generated_id: int
    bundle_id: str
    bundle: SolverBundle
    receipt: ExecutionReceipt | None
    outputs: Mapping[str, Content]
    files: Mapping[str, Mapping[str, Mapping[str, object]]]


def _read(db: Session, object_id: int) -> dict:
    return json.loads(get_object(db, object_id))


def _find(db: Session, claim: Claim, number: int, stage: str) -> tuple[int, dict] | None:
    purpose = f"round-{number}:{stage}"
    for attempt in reversed(tasks.list_attempts(db, claim.task_id)):
        refs = find_refs_by_owner(db, "task_checkpoint", attempt.id, "task_attempts")
        for ref in refs:
            if ref["purpose"] == purpose:
                return int(ref["object_id"]), _read(db, ref["object_id"])
    return None


def _publish(
    db: Session, claim: Claim, number: int, stage: str, data: dict,
    groups: Mapping[str, Mapping[str, Mapping[str, object]]],
) -> int:
    # 调用方先探测数据库再写大文件；发布引用时才锁定租约行。
    _lock(db, claim)
    task = tasks.get_task(db, claim.task_id)
    document = {
        "schema": "ies.worker-checkpoint", "schema_version": "1.0.0",
        "task_id": str(claim.task_id), "snapshot_id": str(task.calc_snapshot_id),
        "source_attempt_id": str(claim.attempt_id), "round": number, "stage": stage,
        "data": data,
    }
    handle = put_object(
        db, json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode(),
        "application/json", source_category="evidence", purpose="worker_checkpoint",
    )
    add_ref(db, handle.id, "task_checkpoint", claim.attempt_id, "task_attempts",
            purpose=f"round-{number}:{stage}")
    for group in groups.values():
        for descriptor in group.values():
            add_ref(db, descriptor["object_id"], "checkpoint_file", handle.id, "objects",
                    purpose="worker_checkpoint")
    db.commit()
    return handle.id


def _lock(db: Session, claim: Claim) -> None:
    if verify_lease(db, claim.attempt_id, claim.lease_token) is None:
        raise LeaseRejectedError("租约失效，拒绝发布阶段产物")


def save_bundle_checkpoint(
    db: Session, claim: Claim, number: int, bundle: SolverBundle, attempt: AttemptContext,
) -> tuple[int, dict]:
    try:
        media = {entry["path"]: entry["media_type"] for entry in bundle.manifest.inputs}
        files = seal_output_objects(
            db, {path: (raw, media[path]) for path, raw in bundle.files.items()}, actor_id=None,
        )
        identity = _publish(db, claim, number, "generated", {
            "bundle_id": attempt.bundle_id, "manifest": bundle.manifest.to_dict(), "files": files,
        }, {"bundle_files": files})
        return identity, files
    except Exception:
        db.rollback()
        raise


def restore_round(db: Session, claim: Claim, number: int) -> SavedRound | None:
    execution = _find(db, claim, number, "execution")
    receipt = None
    outputs = {}
    solution_refs = {}
    if execution is not None:
        execution_data = execution[1]["data"]
        native = ExecutionReceipt(**execution_data["receipt"])
        if native.status == "succeeded":
            receipt = native
            generated_id = execution_data["generated_id"]
            generated = _read(db, generated_id)
            solution_refs = execution_data["files"]
            outputs = {path: get_object_content(db, ref["object_id"])
                       for path, ref in solution_refs.items()}
    if receipt is None:
        entry = _find(db, claim, number, "generated")
        if entry is None:
            return None
        generated_id, generated = entry
    data = generated["data"]
    bundle_refs = data["files"]
    bundle = SolverBundle(
        SolverBundleManifest(**data["manifest"]),
        {path: get_object_content(db, ref["object_id"]) for path, ref in bundle_refs.items()},
    )
    return SavedRound(generated_id, data["bundle_id"], bundle, receipt, outputs,
                      {"bundle_files": bundle_refs, "solution": solution_refs})


def _stage_status_to_receipt_status(status: str, termination_reason: str | None) -> tuple[str, str]:
    if status == "succeeded":
        return "succeeded", "exited"
    if status == "cancelled":
        return "cancelled", "cancelled"
    if status == "fenced":
        return "fenced", "fenced"
    if status == "timed_out":
        return "timed_out", "timeout"
    if status == "oom":
        return "process_failed", "oom"
    if status == "output_protocol_failed":
        return "output_protocol_failed", "exited"
    return "process_failed", "exited"


def _build_receipt_from_stage(
    *,
    attempt: AttemptContext,
    bundle: SolverBundle,
    stage_result: Any,
    outputs: Mapping[str, Content],
    meta: Mapping[str, Any] | None,
) -> ExecutionReceipt:
    status, termination_reason = _stage_status_to_receipt_status(
        str(stage_result.status),
        stage_result.termination_reason,
    )
    declared_outputs = tuple(bundle.manifest.outputs or ())
    if status == "succeeded":
        missing = [
            entry["path"]
            for entry in declared_outputs
            if entry.get("required", True) and entry["path"] not in outputs
        ]
        if missing:
            status = "output_protocol_failed"
            termination_reason = "exited"
    meta_by_path = {
        str(entry["path"]): (
            str(entry.get("media_type") or "application/octet-stream"),
            bool(entry.get("required", True)),
        )
        for entry in declared_outputs
    }
    output_entries = tuple(
        {
            "path": path,
            "media_type": meta_by_path.get(path, ("application/octet-stream", True))[0],
            "size_bytes": content.size_bytes,
            "required": meta_by_path.get(path, ("application/octet-stream", True))[1],
        }
        for path, content in sorted(outputs.items())
    )
    meta_dict = dict(meta or {})
    raw_exit_code = meta_dict.get("exit_code", stage_result.exit_code)
    signal_number: int | None = None
    exit_code: int | None = int(raw_exit_code) if raw_exit_code is not None else None
    started_at = str(meta_dict.get("started_at") or stage_result.started_at or "")
    ended_at = str(meta_dict.get("ended_at") or stage_result.finished_at or "")
    if exit_code is not None and exit_code < 0:
        # Python subprocess reports signal termination as negative return code (-signal).
        signal_number = -exit_code
        exit_code = None
        if status == "process_failed" and termination_reason == "exited":
            termination_reason = "signaled"
    elif termination_reason == "oom" and exit_code == 137:
        # Container OOM killer terminates with SIGKILL (exit code 128 + 9 = 137).
        exit_code = None
        signal_number = 9

    stdout = str(meta_dict.get("stdout") or "")
    stderr = str(meta_dict.get("stderr") or "")
    error_msg = ""
    if status != "succeeded":
        error_msg = stderr.strip() or (
            f"solver container finished with status={stage_result.status} "
            f"(reason={termination_reason})"
        )
    return ExecutionReceipt(
        receipt_id=f"receipt-{attempt.attempt_id}-{stage_result.stage_id}",
        bundle_id=attempt.bundle_id,
        assembly_id=attempt.assembly_id,
        calculation_config_id=attempt.calculation_config_id,
        attempt_id=attempt.attempt_id,
        generator_ref=str(bundle.manifest.bundle["generator"]),
        solver_ref=str(bundle.manifest.bundle["solver"]),
        executor_ref=str(bundle.manifest.command["executor"]),
        executable_ref=str(bundle.manifest.command["executable"]),
        result_adapter_ref=str(bundle.manifest.result_adapter),
        status=status,
        exit_code=exit_code,
        signal_number=signal_number,
        termination_reason=termination_reason,
        started_at=started_at,
        ended_at=ended_at,
        error=error_msg,
        stdout=stdout,
        stderr=stderr,
        outputs=output_entries,
        resource_usage=dict(stage_result.resources or {}),
    )


def execute_saved_bundle(
    *, plane: Any, bundle: SolverBundle, attempt: AttemptContext, claim: Claim,
    number: int, generated_id: int, session_factory: Callable[[], Session], stop=None,
    detach: Any = None,
) -> tuple[ExecutionReceipt, Mapping[str, Content], dict]:
    executable_ref = str(bundle.manifest.command["executable"])
    try:
        solver_stage = plane.stage_catalog.get_by_ref(executable_ref)
    except KeyError:
        raise ComputationUnavailableError(
            f"solver阶段容器未发布: {executable_ref!r}",
            reason="no-provider",
        ) from None

    input_files: dict[str, Any] = {
        f"bundle/{rel}": content for rel, content in export_bundle_files(bundle).items()
    }
    raw_timeout = bundle.manifest.command.get("timeout_seconds")
    timeout_seconds = float(raw_timeout) if raw_timeout is not None else None
    stage_id = f"solve-r{number}"

    files: dict = {}
    receipt_box: list[ExecutionReceipt] = []
    reopened_outputs: Mapping[str, Content] = {}

    def _collect_solution_outputs(stage_result: Any) -> dict[str, Content]:
        declared_outputs = tuple(bundle.manifest.outputs or ())
        collected: dict[str, Content] = {}
        for entry in declared_outputs:
            declared_path = str(entry["path"])
            basename = Path(declared_path).name
            if declared_path in stage_result.outputs:
                collected[declared_path] = stage_result.outputs[declared_path]
            elif basename in stage_result.outputs:
                collected[declared_path] = stage_result.outputs[basename]
        return collected

    def _persist(stage_result: Any) -> Any:
        nonlocal files, reopened_outputs
        if stage_result.status == "fenced":
            raise LeaseRejectedError("求解容器阶段租约已失效(fenced)")
        meta: dict[str, Any] | None = None
        if "solver_meta.json" in stage_result.outputs:
            meta = json.loads(read_content(stage_result.outputs["solver_meta.json"]).decode("utf-8"))
        outputs = _collect_solution_outputs(stage_result)
        receipt = _build_receipt_from_stage(
            attempt=attempt,
            bundle=bundle,
            stage_result=stage_result,
            outputs=outputs,
            meta=meta,
        )

        def publish(db: Session) -> Mapping[str, Content]:
            nonlocal files
            media = {entry["path"]: entry["media_type"] for entry in receipt.outputs}
            files = seal_output_objects(
                db, {path: (raw, media[path]) for path, raw in outputs.items()}, actor_id=None,
            )
            _publish(db, claim, number, "execution", {
                "generated_id": generated_id, "receipt": receipt.to_dict(), "files": files,
            }, {"solution": files})
            return {path: get_object_content(db, ref["object_id"])
                    for path, ref in files.items()}

        reopened_outputs = run_worker_transaction(session_factory, publish)
        receipt_box.append(receipt)
        return receipt

    try:
        stage_result = plane.stage_runner.run_stage(
            StageExecutionSpec(
                attempt_id=attempt.attempt_id,
                stage_id=stage_id,
                fencing_token=str(claim.lease_token),
                image=solver_stage.image,
                command=solver_stage.command,
                input_files=input_files,
                optional_outputs=("solver_meta.json",),
                collect_all_outputs=True,
                timeout_seconds=timeout_seconds,
            ),
            persist=_persist,
            stop=stop,
            **({"detach": detach} if detach is not None else {}),
        )
    except LeaseRejectedError:
        fenced_receipt = ExecutionReceipt(
            receipt_id=f"receipt-{attempt.attempt_id}-{stage_id}",
            bundle_id=attempt.bundle_id,
            assembly_id=attempt.assembly_id,
            calculation_config_id=attempt.calculation_config_id,
            attempt_id=attempt.attempt_id,
            generator_ref=str(bundle.manifest.bundle["generator"]),
            solver_ref=str(bundle.manifest.bundle["solver"]),
            executor_ref=str(bundle.manifest.command["executor"]),
            executable_ref=str(bundle.manifest.command["executable"]),
            result_adapter_ref=str(bundle.manifest.result_adapter),
            status="fenced",
            termination_reason="fenced",
        )
        return fenced_receipt, {}, {}

    if not receipt_box:
        pre_stop_receipt = _build_receipt_from_stage(
            attempt=attempt,
            bundle=bundle,
            stage_result=stage_result,
            outputs={},
            meta=None,
        )
        return pre_stop_receipt, {}, {}
    return receipt_box[0], reopened_outputs, files


def load_prediction_checkpoint(db: Session, claim: Claim, number: int) -> tuple | None:
    checkpoint = _find(db, claim, number, "prediction")
    if checkpoint is None:
        return None
    data = checkpoint[1]["data"]
    predicted = tuple(PredictedTarget(
        **{key: value for key, value in entry.items() if key not in ("artifact", "values")},
        values=tuple(entry["values"]),
        artifact=get_object(db, entry["artifact"]["object_id"]),
    ) for entry in data["predicted"])
    by_key = {(item.device, item.interface): item.values for item in predicted}
    series = tuple(MaterializedSeries(
        **entry, values=by_key[(entry["device"], entry["interface"])],
    ) for entry in data["series"])
    return series, predicted, {
        f"{entry['device']}/{entry['interface']}": entry["artifact"]
        for entry in data["predicted"]
    }


def save_prediction_checkpoint(
    db: Session, claim: Claim, number: int, series: tuple, predicted: tuple,
) -> dict:
    try:
        artifacts = seal_output_objects(db, {
            f"{item.device}/{item.interface}": (item.artifact, item.artifact_media_type)
            for item in predicted
        }, actor_id=None)
        return publish_prediction_checkpoint_with_artifacts(
            db, claim, number, series, predicted, artifacts, stage="prediction"
        )
    except Exception:
        db.rollback()
        raise


def save_prediction_group_checkpoint(
    db: Session, claim: Claim, number: int, group_index: int, series: tuple, predicted: tuple,
) -> dict:
    """Persist a single predictor stage group's artifacts and checkpoint before cleanup_stage."""
    try:
        artifacts = seal_output_objects(db, {
            f"{item.device}/{item.interface}": (item.artifact, item.artifact_media_type)
            for item in predicted
        }, actor_id=None)
        return publish_prediction_checkpoint_with_artifacts(
            db, claim, number, series, predicted, artifacts, stage=f"prediction-g{group_index}"
        )
    except Exception:
        db.rollback()
        raise


def publish_prediction_checkpoint_with_artifacts(
    db: Session,
    claim: Claim,
    number: int,
    series: tuple,
    predicted: tuple,
    artifacts: Mapping[str, Mapping[str, object]],
    *,
    stage: str = "prediction",
) -> dict:
    """Publish a prediction checkpoint referencing already-sealed durable storage artifacts."""
    try:
        rows = [{
            "device": item.device, "interface": item.interface,
            "target_type": item.target_type, "values": list(item.values),
            "history_object_id": item.history_object_id,
            "artifact_media_type": item.artifact_media_type,
            "artifact": dict(artifacts[f"{item.device}/{item.interface}"]),
            "receipt": thaw_json_value(item.receipt),
        } for item in predicted]
        series_rows = [{key: value for key, value in item.to_dict().items() if key != "values"}
                       for item in series]
        _publish(db, claim, number, stage, {"predicted": rows, "series": series_rows},
                 {"prediction": artifacts})
        return {k: dict(v) for k, v in artifacts.items()}
    except Exception:
        db.rollback()
        raise


def _evaluated_from_checkpoint(raw: Mapping[str, Any]) -> EvaluatedStageOutput:
    """Reconstruct `EvaluatedStageOutput` from trusted stored checkpoint without revalidating."""
    outputs = dict(raw.get("outputs") or {})
    hourly_series = outputs.pop("hourly_series", None)
    financial_evaluation = outputs.pop("financial_evaluation", None)
    result = object.__new__(ComputeResult)
    object.__setattr__(result, "schema", raw.get("schema", COMPUTE_RESULT_SCHEMA))
    object.__setattr__(result, "schema_version", raw.get("schema_version", COMPUTE_RESULT_VERSION))
    object.__setattr__(result, "bundle_id", str(raw["bundle_id"]))
    object.__setattr__(result, "receipt_id", str(raw["receipt_id"]))
    object.__setattr__(result, "execution_status", str(raw["execution_status"]))
    object.__setattr__(result, "math_status", str(raw["math_status"]))
    object.__setattr__(result, "outputs", freeze_json_value(outputs))
    return EvaluatedStageOutput(
        result=result,
        hourly_series=hourly_series,
        financial_evaluation=financial_evaluation,
    )


def _load_evaluated_stage_checkpoint(
    db: Session, claim: Claim, number: int, stage: str,
) -> EvaluatedStageOutput | None:
    checkpoint = _find(db, claim, number, stage)
    if checkpoint is None:
        return None
    data = checkpoint[1]["data"]
    result_file = data.get("result_file")
    if isinstance(result_file, Mapping):
        content = get_object_content(db, int(result_file["object_id"]))
        raw = json.loads(read_content(content).decode("utf-8"))
    else:
        raw = data["result"]
    return _evaluated_from_checkpoint(raw)


def _save_evaluated_stage_checkpoint(
    db: Session, claim: Claim, number: int, stage: str, raw_result: Mapping[str, Any],
) -> int:
    try:
        files = seal_output_objects(
            db,
            {
                "result.json": (
                    GeneratedContent(partial(json_chunks, dict(raw_result))),
                    "application/json",
                )
            },
            actor_id=None,
        )
        return _publish(db, claim, number, stage, {"result_file": files["result.json"]}, {stage: files})
    except Exception:
        db.rollback()
        raise


def load_evaluation_checkpoint(
    db: Session, claim: Claim, number: int
) -> EvaluatedStageOutput | None:
    """Consume persisted evaluator stage checkpoint for ``(claim.task_id, number)``."""
    return _load_evaluated_stage_checkpoint(db, claim, number, "evaluation")


def load_algorithm_checkpoint(
    db: Session, claim: Claim
) -> EvaluatedStageOutput | None:
    """Consume persisted algorithm stage checkpoint for ``claim.task_id``."""
    return _load_evaluated_stage_checkpoint(db, claim, 1, "algorithm")


def save_evaluation_checkpoint(
    db: Session, claim: Claim, number: int, raw_result: Mapping[str, Any],
) -> int:
    """Persist evaluator stage result.json durably with Fencing Token before cleanup_stage."""
    return _save_evaluated_stage_checkpoint(db, claim, number, "evaluation", raw_result)


def save_algorithm_checkpoint(
    db: Session, claim: Claim, raw_result: Mapping[str, Any],
) -> int:
    """Persist unified algorithm stage result.json durably with Fencing Token before cleanup_stage."""
    return _save_evaluated_stage_checkpoint(db, claim, 1, "algorithm", raw_result)


def save_stage_outcome_checkpoint(
    db: Session, claim: Claim, number: int, stage: str, stage_result: Any,
) -> int:
    """Persist non-succeeded stage facts and any collected outputs with Fencing Token before cleanup."""
    try:
        sealed_outputs = seal_output_objects(
            db,
            {
                path: (content, "application/octet-stream")
                for path, content in (stage_result.outputs or {}).items()
            },
            actor_id=None,
        )
        data = {
            "stage_execution_id": stage_result.stage_execution_id,
            "stage_id": stage_result.stage_id,
            "status": stage_result.status,
            "exit_code": stage_result.exit_code,
            "termination_reason": stage_result.termination_reason,
            "started_at": stage_result.started_at,
            "finished_at": stage_result.finished_at,
            "resources": thaw_json_value(stage_result.resources or {}),
            "iterations": thaw_json_value(stage_result.iterations or {}),
            "missing_required_outputs": list(stage_result.missing_required_outputs or ()),
            "files": sealed_outputs,
        }
        groups = {"stage_outputs": sealed_outputs} if sealed_outputs else {}
        return _publish(db, claim, number, f"{stage}:outcome", data, groups)
    except Exception:
        db.rollback()
        raise
