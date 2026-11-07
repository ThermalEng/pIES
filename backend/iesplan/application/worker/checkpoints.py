"""Durable stage handoff through existing storage objects and attempt owners."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from iesplan import tasks
from iesplan.application.tasks import Claim
from iesplan.application.worker.compute_cases import seal_output_objects
from iesplan.application.worker.lease_cases import LeaseRejectedError, verify_lease
from iesplan.computation import (
    AttemptContext,
    ExecutionReceipt,
    MaterializedSeries,
    PredictedTarget,
    SolverBundle,
    SolverBundleManifest,
    thaw_json_value,
)
from iesplan.storage import add_ref, find_refs_by_owner, get_object, put_object


@dataclass(frozen=True, slots=True)
class SavedRound:
    generated_id: int
    bundle_id: str
    bundle: SolverBundle
    receipt: ExecutionReceipt | None
    outputs: Mapping[str, bytes]
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
        _lock(db, claim)
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
            outputs = {path: get_object(db, ref["object_id"])
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
        {path: get_object(db, ref["object_id"]) for path, ref in bundle_refs.items()},
    )
    return SavedRound(generated_id, data["bundle_id"], bundle, receipt, outputs,
                      {"bundle_files": bundle_refs, "solution": solution_refs})


def execute_saved_bundle(
    *, plane: Any, bundle: SolverBundle, attempt: AttemptContext, claim: Claim,
    number: int, generated_id: int, session_factory: Callable[[], Session], stop=None,
) -> tuple[ExecutionReceipt, dict[str, bytes], dict]:
    files = {}

    def handoff(receipt: ExecutionReceipt, outputs: Mapping[str, bytes]) -> None:
        nonlocal files
        if receipt.status == "fenced":
            return
        with session_factory() as db:
            try:
                _lock(db, claim)
                media = {entry["path"]: entry["media_type"] for entry in receipt.outputs}
                files = seal_output_objects(
                    db, {path: (raw, media[path]) for path, raw in outputs.items()}, actor_id=None,
                )
                _publish(db, claim, number, "execution", {
                    "generated_id": generated_id, "receipt": receipt.to_dict(), "files": files,
                }, {"solution": files})
            except Exception:
                db.rollback()
                raise

    receipt, outputs = plane.runtime.run(bundle, attempt, stop=stop, handoff=handoff)
    return receipt, outputs, files


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
        _lock(db, claim)
        artifacts = seal_output_objects(db, {
            f"{item.device}/{item.interface}": (item.artifact, item.artifact_media_type)
            for item in predicted
        }, actor_id=None)
        rows = [{
            "device": item.device, "interface": item.interface,
            "target_type": item.target_type, "values": list(item.values),
            "history_object_id": item.history_object_id,
            "artifact_media_type": item.artifact_media_type,
            "artifact": artifacts[f"{item.device}/{item.interface}"],
            "receipt": thaw_json_value(item.receipt),
        } for item in predicted]
        series_rows = [{key: value for key, value in item.to_dict().items() if key != "values"}
                       for item in series]
        _publish(db, claim, number, "prediction", {"predicted": rows, "series": series_rows},
                 {"prediction": artifacts})
        return artifacts
    except Exception:
        db.rollback()
        raise
