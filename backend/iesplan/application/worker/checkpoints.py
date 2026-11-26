"""Durable stage handoff through existing storage objects and attempt owners."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import MappingProxyType
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
from iesplan.core.content import Content, GeneratedContent, MemoryContent, json_chunks, read_content
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


def _find_for_task(db: Session, task_id: int, number: int, stage: str) -> tuple[int, dict] | None:
    purpose = f"round-{number}:{stage}"
    for attempt in reversed(tasks.list_attempts(db, task_id)):
        refs = find_refs_by_owner(db, "task_checkpoint", attempt.id, "task_attempts")
        for ref in refs:
            if ref["purpose"] == purpose:
                return int(ref["object_id"]), _read(db, ref["object_id"])
    return None


def _find(db: Session, claim: Claim, number: int, stage: str) -> tuple[int, dict] | None:
    return _find_for_task(db, claim.task_id, number, stage)


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
    solver_ref: str,
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
        signal_number = -exit_code
        exit_code = None
        if status == "process_failed" and termination_reason == "exited":
            termination_reason = "signaled"
    elif termination_reason == "oom" and exit_code == 137:
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
        solver_ref=solver_ref,
        executor_ref="ies.executor.container@1.0.0",
        executable_ref=solver_ref,
        result_adapter_ref="ies.backend.result_adapter@1.0.0",
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
    *,
    plane: Any,
    bundle: SolverBundle,
    solver_ref: str,
    time_limit_seconds: int = 43200,
    seed: int = 0,
    solver_options: Mapping[str, Any] = MappingProxyType({}),
    attempt: AttemptContext,
    claim: Claim,
    number: int,
    generated_id: int,
    session_factory: Callable[[], Session],
    stop=None,
    detach: Any = None,
) -> tuple[ExecutionReceipt, Mapping[str, Content], dict]:
    try:
        solver_stage = plane.stage_catalog.get_by_ref(solver_ref)
    except KeyError:
        raise ComputationUnavailableError(
            f"solver阶段容器未发布: {solver_ref!r}",
            reason="no-provider",
        ) from None

    input_files: dict[str, Any] = {
        f"bundle/{rel}": content for rel, content in export_bundle_files(bundle).items()
    }
    request_doc: dict[str, Any] = {
        "time_limit_seconds": int(time_limit_seconds),
        "seed": int(seed),
        "parameters": thaw_json_value(solver_options),
    }
    input_files["request.json"] = MemoryContent(
        json.dumps(request_doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    timeout_seconds = float(time_limit_seconds) + 600.0
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
            solver_ref=solver_ref,
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
            solver_ref=solver_ref,
            executor_ref="ies.executor.container@1.0.0",
            executable_ref=solver_ref,
            result_adapter_ref="ies.backend.result_adapter@1.0.0",
            status="fenced",
            termination_reason="fenced",
        )
        return fenced_receipt, {}, {}

    if not receipt_box:
        pre_stop_receipt = _build_receipt_from_stage(
            attempt=attempt,
            bundle=bundle,
            solver_ref=solver_ref,
            stage_result=stage_result,
            outputs={},
            meta=None,
        )
        return pre_stop_receipt, {}, {}
    return receipt_box[0], reopened_outputs, files


def _prediction_group_stage(group_key: str) -> str:
    return f"prediction-group:{group_key}"


def _restore_prediction_data(db: Session, data: Mapping[str, Any]) -> tuple:
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


def load_prediction_checkpoint_for_task(db: Session, task_id: int, number: int) -> tuple | None:
    checkpoint = _find_for_task(db, task_id, number, "prediction")
    if checkpoint is None:
        return None
    return _restore_prediction_data(db, checkpoint[1]["data"])


def load_prediction_checkpoint(db: Session, claim: Claim, number: int) -> tuple | None:
    return load_prediction_checkpoint_for_task(db, claim.task_id, number)


def load_prediction_group_checkpoint(
    db: Session, claim: Claim, number: int, group_key: str | int,
) -> tuple | None:
    """Restore a single predictor stage group's sealed outputs by stable group key."""
    checkpoint = _find(db, claim, number, _prediction_group_stage(str(group_key)))
    if checkpoint is None:
        return None
    return _restore_prediction_data(db, checkpoint[1]["data"])


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
    db: Session, claim: Claim, number: int, group_key: str | int, series: tuple, predicted: tuple,
) -> dict:
    """Persist a single predictor stage group's artifacts and checkpoint before cleanup_stage."""
    try:
        artifacts = seal_output_objects(db, {
            f"{item.device}/{item.interface}": (item.artifact, item.artifact_media_type)
            for item in predicted
        }, actor_id=None)
        return publish_prediction_checkpoint_with_artifacts(
            db, claim, number, series, predicted, artifacts,
            stage=_prediction_group_stage(str(group_key)),
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


def _evaluated_from_checkpoint(
    raw: Mapping[str, Any],
    *,
    solution_ref: Mapping[str, Any] | None = None,
    hourly_ref: Mapping[str, Any] | None = None,
    hourly_meta: Mapping[str, Any] | None = None,
    planning_variables: Sequence[Mapping[str, Any]] = (),
) -> EvaluatedStageOutput:
    """Reconstruct `EvaluatedStageOutput` from trusted stored checkpoint without revalidating."""
    outputs = dict(raw.get("outputs") or {})
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
        hourly_ref=dict(hourly_ref) if hourly_ref is not None else None,
        hourly_meta=dict(hourly_meta) if isinstance(hourly_meta, Mapping) else None,
        planning_variables=tuple(planning_variables),
        solution_ref=dict(solution_ref) if solution_ref is not None else None,
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
        raw = json.loads(
            read_content(get_object_content(db, int(result_file["object_id"]))).decode("utf-8")
        )
    else:
        raw = data["result"]
    return _evaluated_from_checkpoint(
        raw,
        solution_ref=data.get("solution_file"),
        hourly_ref=data.get("hourly_file"),
        hourly_meta=data.get("hourly_meta"),
        planning_variables=data.get("planning_variables") or (),
    )


def _save_evaluated_stage_checkpoint(
    db: Session,
    claim: Claim,
    number: int,
    stage: str,
    raw_result: Mapping[str, Any],
    *,
    solution_content: Content | bytes | None = None,
    hourly_content: Content | bytes | None = None,
    hourly_meta: Mapping[str, Any] | None = None,
    planning_variables: Sequence[Mapping[str, Any]] = (),
) -> int:
    try:
        to_seal: dict[str, tuple[Content | bytes, str]] = {
            "result.json": (
                GeneratedContent(partial(json_chunks, dict(raw_result))),
                "application/json",
            )
        }
        if solution_content is not None:
            to_seal["solution.json"] = (solution_content, "application/json")
        if hourly_content is not None:
            to_seal["hourly/series.json"] = (hourly_content, "application/json")
        files = seal_output_objects(db, to_seal, actor_id=None)
        data: dict[str, Any] = {
            "result_file": files["result.json"],
            "planning_variables": thaw_json_value(list(planning_variables)),
        }
        if "solution.json" in files:
            data["solution_file"] = files["solution.json"]
        if "hourly/series.json" in files:
            data["hourly_file"] = files["hourly/series.json"]
        if hourly_meta is not None:
            data["hourly_meta"] = thaw_json_value(hourly_meta)
        return _publish(db, claim, number, stage, data, {stage: files})
    except Exception:
        db.rollback()
        raise


def load_algorithm_checkpoint(
    db: Session, claim: Claim
) -> EvaluatedStageOutput | None:
    """Consume persisted algorithm stage checkpoint for ``claim.task_id``."""
    return _load_evaluated_stage_checkpoint(db, claim, 1, "algorithm")


def save_algorithm_checkpoint(
    db: Session,
    claim: Claim,
    raw_result: Mapping[str, Any],
    *,
    solution_content: Content | bytes | None = None,
    hourly_content: Content | bytes | None = None,
    hourly_meta: Mapping[str, Any] | None = None,
    planning_variables: Sequence[Mapping[str, Any]] = (),
) -> int:
    """Persist unified algorithm stage outputs durably with Fencing Token before cleanup_stage."""
    return _save_evaluated_stage_checkpoint(
        db,
        claim,
        1,
        "algorithm",
        raw_result,
        solution_content=solution_content,
        hourly_content=hourly_content,
        hourly_meta=hourly_meta,
        planning_variables=planning_variables,
    )


def save_evaluator_checkpoint(
    db: Session,
    claim: Claim,
    number: int,
    idx: int,
    *,
    result_entry: Mapping[str, Any],
    report: Mapping[str, Any] | None = None,
    financial_evaluation: Mapping[str, Any] | None = None,
) -> int:
    """Persist a single parallel evaluator stage outcome durably with Fencing Token before cleanup."""
    try:
        payload_doc: dict[str, Any] = {
            "result_entry": thaw_json_value(result_entry),
            "report": thaw_json_value(report) if report is not None else None,
            "financial_evaluation": (
                thaw_json_value(financial_evaluation)
                if financial_evaluation is not None
                else None
            ),
        }
        files = seal_output_objects(
            db,
            {
                "evaluator.json": (
                    GeneratedContent(partial(json_chunks, payload_doc)),
                    "application/json",
                )
            },
            actor_id=None,
        )
        return _publish(
            db, claim, number, f"eval-{idx}", {"evaluator_file": files["evaluator.json"]},
            {f"eval-{idx}": files},
        )
    except Exception:
        db.rollback()
        raise


def load_evaluator_checkpoint(
    db: Session, claim: Claim, number: int, idx: int,
) -> dict[str, Any] | None:
    """Load persisted evaluator stage result for ``(claim.task_id, number, idx)`` if reusable."""
    checkpoint = _find(db, claim, number, f"eval-{idx}")
    if checkpoint is None:
        return None
    data = checkpoint[1]["data"]
    ev_file = data["evaluator_file"]
    loaded = json.loads(
        read_content(get_object_content(db, int(ev_file["object_id"]))).decode("utf-8")
    )
    entry = loaded.get("result_entry") or {}
    # Failed/timed_out checkpoints from prior attempts must not block retry on a new attempt.
    if entry.get("status") != "succeeded" and entry.get("attempt_id") != str(claim.attempt_id):
        return None
    return loaded


def save_restoration_checkpoint(
    db: Session,
    claim: Claim,
    number: int,
    *,
    compute_result: Mapping[str, Any],
    solution_content: Content | bytes | None,
    hourly_content: Content | bytes | None = None,
    hourly_meta: Mapping[str, Any] | None = None,
    planning_variables: list[Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, object]]:
    """Seal restored ``ies.engineering-solution@1.0.0``, ``hourly/series.json``, and metadata."""
    try:
        to_seal: dict[str, tuple[Content | bytes, str]] = {
            "result.json": (
                GeneratedContent(
                    partial(
                        json_chunks,
                        {
                            "compute_result": thaw_json_value(compute_result),
                            "hourly_meta": thaw_json_value(hourly_meta) if hourly_meta else None,
                            "planning_variables": thaw_json_value(planning_variables or []),
                        },
                    )
                ),
                "application/json",
            )
        }
        if solution_content is not None:
            to_seal["solution.json"] = (solution_content, "application/json")
        if hourly_content is not None:
            to_seal["hourly/series.json"] = (hourly_content, "application/json")
        files = seal_output_objects(db, to_seal, actor_id=None)
        _publish(
            db,
            claim,
            number,
            "restoration",
            {"files": files},
            {"restoration": files},
        )
        return files
    except Exception:
        db.rollback()
        raise


def load_restoration_checkpoint(
    db: Session, claim: Claim, number: int,
) -> dict[str, Any] | None:
    """Restore core ``ComputeResult``, sealed ``solution.json``/``hourly/series.json`` refs, and metadata."""
    checkpoint = _find(db, claim, number, "restoration")
    if checkpoint is None:
        return None
    files = checkpoint[1]["data"]["files"]
    raw_doc = json.loads(
        read_content(get_object_content(db, int(files["result.json"]["object_id"]))).decode("utf-8")
    )
    raw_cr = raw_doc["compute_result"]
    cr = ComputeResult(
        schema=raw_cr["schema"],
        schema_version=raw_cr["schema_version"],
        bundle_id=raw_cr["bundle_id"],
        receipt_id=raw_cr["receipt_id"],
        execution_status=raw_cr["execution_status"],
        math_status=raw_cr["math_status"],
        outputs=raw_cr.get("outputs") or {},
    )
    return {
        "compute_result": cr,
        "solution_ref": files.get("solution.json"),
        "hourly_ref": files.get("hourly/series.json"),
        "hourly_meta": raw_doc.get("hourly_meta"),
        "planning_variables": raw_doc.get("planning_variables") or [],
    }


def save_evaluator_retry_request(
    db: Session,
    *,
    retry_task_id: int,
    source_task_id: int,
    evaluator: str,
    base_evidence_id: int,
    idempotency_key: str,
) -> int:
    """Persist durable single-evaluator retry directive linked to retry task and source task (flush-only)."""
    document = {
        "schema": "ies.evaluator-retry",
        "schema_version": "1.0.0",
        "retry_task_id": retry_task_id,
        "source_task_id": source_task_id,
        "evaluator": evaluator,
        "base_evidence_id": base_evidence_id,
        "idempotency_key": idempotency_key,
    }
    handle = put_object(
        db,
        json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        "application/json",
        source_category="evidence",
        purpose="evaluator_retry",
    )
    add_ref(
        db,
        handle.id,
        "evaluator_retry",
        retry_task_id,
        "tasks",
        purpose="evaluator_retry",
    )
    add_ref(
        db,
        handle.id,
        "evaluator_retry_child",
        source_task_id,
        "tasks",
        purpose="evaluator_retry",
    )
    return handle.id


def load_evaluator_retry_directive(db: Session, retry_task_id: int) -> dict[str, Any] | None:
    """Load the durable evaluator retry directive for an independent child analysis task."""
    refs = find_refs_by_owner(db, "evaluator_retry", retry_task_id, "tasks")
    for ref in reversed(refs):
        if ref["purpose"] == "evaluator_retry":
            return _read(db, ref["object_id"])
    return None


def list_evaluator_retry_requests(db: Session, source_task_id: int) -> list[dict[str, Any]]:
    """List durable evaluator retry directives targeting a completed source task."""
    refs = find_refs_by_owner(db, "evaluator_retry_child", source_task_id, "tasks")
    return [_read(db, ref["object_id"]) for ref in refs if ref["purpose"] == "evaluator_retry"]


def find_active_evaluator_retry_for_source(
    db: Session, source_task_id: int
) -> dict[str, Any] | None:
    """Return active queued/running/cancelling child retry directive for ``source_task_id`` if any."""
    for req in reversed(list_evaluator_retry_requests(db, source_task_id)):
        child_id = int(req.get("retry_task_id", 0))
        child = tasks.get_task(db, child_id)
        if child is not None and child.status in ("queued", "running", "cancelling"):
            return req
    return None


def load_pending_evaluator_retry(db: Session, claim: Claim) -> dict[str, Any] | None:
    """Return durable single-evaluator retry directive when ``claim.task_id`` is a child retry."""
    return load_evaluator_retry_directive(db, claim.task_id)


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
