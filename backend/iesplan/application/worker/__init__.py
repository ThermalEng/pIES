"""Worker 边界用例包(application/worker)。

本包收拢 Worker 对 storage/领域门面与行级读写的直接调用,
worker 层只经本包推进, 不再直连 ``services.*`` 与 ``models.*``;
依赖方向: worker → application → (storage/领域门面)。

子模块:
- lease_cases: 领取/进度/完成/失败/槽释放/队列视图/证据存取转调与
  租约 fencing/任务行/结果提交行读写;
- runner_cases: 快照输入读取转调与输入装配行读;
- evidence_cases: 证据包查询/检查评估追加/不确定性行写。

包级直接提供三个 worker 层消费的边界辅助(非转调, 有独立调用方):
- cancel_requested: 取消信号读(可重建视图);
- store_worker_object / load_worker_object: 对象存储通用写/读。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from iesplan import tasks as tasks_domain
from iesplan.application.tasks.submissions import map_business_outcome
from iesplan.application.worker import evidence_cases, lease_cases, runner_cases
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
    TaskAttemptRecord,
    TaskDiagnosticRecord,
    TaskLeaseRecord,
    TaskRecord,
    TaskStateError,
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
from iesplan.tasks import SampleTaskRecord, UncertaintySnapshotRecord

__all__ = [
    "CalcSnapshotRecord",
    "Claim",
    "DatasetVersionRecord",
    "EvidencePackageRecord",
    "LEASE_TTL_SECONDS",
    "ResultAssessmentRecord",
    "SampleTaskRecord",
    "TaskAttemptRecord",
    "TaskDiagnosticRecord",
    "TaskLeaseRecord",
    "TaskRecord",
    "TaskStateError",
    "UncertaintySnapshotRecord",
    "acquire_task",
    "append_check_assessment",
    "attach_result_ref",
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
    "fail_task",
    "fence_release_lease",
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
    "renew_lease_once",
    "runner_cases",
    "slot_available",
    "store_result_blob",
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
