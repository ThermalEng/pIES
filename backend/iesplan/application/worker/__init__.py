"""Worker 边界用例包(application/worker)。

本包收拢 Worker 对 storage/领域门面与行级读写的直接调用,
worker 层只经本包推进, 不再直连 ``services.*`` 与 ``models.*``;
依赖方向: worker → application → (storage/领域门面)。

子模块:
- lease_cases: 领取/进度/完成/失败/槽释放/队列视图/证据存取转调与
  租约 fencing/任务行/结果提交行读写(flush-only,不拥有事务)及
  LeaseRejectedError 错误语义;
- attempt_cases: 完整尝试事务所有者(领取/续租/提交/失败/取消各自
  同事务提交; worker 层不 commit/rollback);
- runner_cases: 快照输入读取转调与输入装配行读;
- evidence_cases: 证据包查询/检查评估追加/不确定性行写。

包级直接提供三个 worker 层消费的边界辅助(非转调, 有独立调用方):
- cancel_requested: 取消信号读(可重建视图);
- store_worker_object / load_worker_object: 对象存储通用写/读。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from iesplan import tasks as tasks_domain
from iesplan.application.worker import attempt_cases, evidence_cases, lease_cases, runner_cases
from iesplan.application.worker.attempt_cases import (
    SubmitReceipt,
    acquire_attempt,
    cancel_attempt,
    fail_attempt,
    renew_attempt_lease,
    submit_attempt_result,
)
from iesplan.application.worker.evidence_cases import (
    append_check_assessment,
    create_sample_row,
    create_uncertainty_snapshot_record,
    get_evidence_record,
    get_latest_evidence_for_project,
    get_latest_evidence_for_task,
    record_sample_value,
)
from iesplan.application.worker.lease_cases import (
    LEASE_TTL_SECONDS,
    CalcSnapshotRecord,
    Claim,
    LeaseRejectedError,
    TaskAttemptRecord,
    TaskDiagnosticRecord,
    TaskLeaseRecord,
    TaskRecord,
    acquire_task,
    attach_result_ref,
    cancel_task_record,
    clear_cancel_signal,
    complete_task,
    create_assessment_record,
    create_evidence_record,
    dequeue_task,
    fail_task,
    fence_release_lease,
    fenced_release_attempt,
    finish_attempt_record,
    flip_result_index,
    get_attempt_record,
    get_snapshot_record,
    get_task_record,
    insert_result_index,
    point_result_assessment,
    publish_heartbeat,
    record_task_progress,
    release_slot,
    renew_lease_once,
    slot_available,
    store_result_blob,
    verify_lease,
    write_diagnostic,
)
from iesplan.application.worker.runner_cases import (
    count_completed_samples,
    get_dataset_data_object,
    get_dataset_version_record,
    get_project_content_id,
    load_dataset_blob,
    load_version_content,
    parse_dataset_csv,
)
from iesplan.dataset import DatasetVersionRecord
from iesplan.results import EvidencePackageRecord, ResultAssessmentRecord
from iesplan.storage import get_object, put_object
from iesplan.tasks import SampleTaskRecord, TaskStateError, UncertaintySnapshotRecord, map_business_outcome

__all__ = [
    "CalcSnapshotRecord",
    "Claim",
    "DatasetVersionRecord",
    "EvidencePackageRecord",
    "LEASE_TTL_SECONDS",
    "LeaseRejectedError",
    "ResultAssessmentRecord",
    "SampleTaskRecord",
    "SubmitReceipt",
    "TaskAttemptRecord",
    "TaskDiagnosticRecord",
    "TaskLeaseRecord",
    "TaskRecord",
    "TaskStateError",
    "UncertaintySnapshotRecord",
    "acquire_attempt",
    "acquire_task",
    "attempt_cases",
    "append_check_assessment",
    "attach_result_ref",
    "cancel_attempt",
    "cancel_task_record",
    "clear_cancel_signal",
    "complete_task",
    "count_completed_samples",
    "create_assessment_record",
    "create_evidence_record",
    "create_sample_row",
    "create_uncertainty_snapshot_record",
    "dequeue_task",
    "evidence_cases",
    "fail_attempt",
    "fail_task",
    "fence_release_lease",
    "fenced_release_attempt",
    "finish_attempt_record",
    "flip_result_index",
    "get_attempt_record",
    "get_dataset_data_object",
    "get_dataset_version_record",
    "get_evidence_record",
    "get_latest_evidence_for_project",
    "get_latest_evidence_for_task",
    "get_project_content_id",
    "get_snapshot_record",
    "get_task_record",
    "insert_result_index",
    "lease_cases",
    "load_dataset_blob",
    "load_version_content",
    "map_business_outcome",
    "parse_dataset_csv",
    "point_result_assessment",
    "publish_heartbeat",
    "record_sample_value",
    "record_task_progress",
    "release_slot",
    "renew_attempt_lease",
    "renew_lease_once",
    "runner_cases",
    "slot_available",
    "store_result_blob",
    "submit_attempt_result",
    "verify_lease",
    "write_diagnostic",
]


def cancel_requested(task_id: int) -> bool:
    """任务是否存在取消信号(可重建视图)。"""
    return tasks_domain.get_cancel(task_id) is not None


def store_worker_object(
    db: Session,
    blob: bytes,
    content_type: str,
    *,
    source_category: str,
    purpose: str,
    actor_id: int | None = None,
) -> int:
    """对象存储写入, 返回对象 id。"""
    return put_object(
        db, blob, content_type, source_category=source_category,
        purpose=purpose, actor_id=actor_id,
    ).id


def load_worker_object(db: Session, object_id: int) -> bytes:
    """对象存储读取。"""
    return get_object(db, object_id)
