"""任务域 repository SQL 实现（快照/任务/尝试/租约/进度/诊断/槽位/不确定性表，归属 tasks）。

实现规则：
- 只做查询、写入、flush；并发竞争（租约/槽位/幂等键）靠行锁与唯一约束
  化为返回值或领域错误；绝不 commit/rollback；
- 状态推进一律刷新 updated_at；business_outcome=None 表示清空（与直接
  赋值语义一致）；
- 终态集合与 services 保持一致（completed/cancelled/timed_out/failed）。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from iesplan.db import (
    Base,
    BigIntArray,
    JSONB,
    bigint_pk,
    drop_trigger_function_sql,
    immutable_revoke_sql,
    immutable_trigger_sql,
    regex_check,
)
from iesplan.tasks.contracts import (
    IDEMPOTENCY_KEY_RE,
    CalcSnapshotRecord,
    ComputeSlotRecord,
    SampleRecordRecord,
    SampleTaskRecord,
    TaskAttemptRecord,
    TaskConflictError,
    TaskDiagnosticRecord,
    TaskLeaseRecord,
    TaskNotFoundError,
    TaskProgressRecord,
    TaskRecord,
    UncertaintySnapshotRecord,
)

#: 非终态（重复提交去重/子任务传播用；与 services.TERMINAL_STATUSES 互补）。
_NON_TERMINAL = ("queued", "running", "cancelling")


def _iso(value: datetime | None) -> str | None:
    """ORM 时间 → 记录字符串（原样 isoformat，不增减时区后缀）。"""
    return value.isoformat() if value is not None else None


def _now() -> datetime:
    return datetime.now(UTC)


def _row_to_snapshot(row: CalcSnapshot) -> CalcSnapshotRecord:
    return CalcSnapshotRecord(
        id=row.id,
        project_version_id=row.project_version_id,
        dataset_version_ids=tuple(row.dataset_version_ids or []),
        calc_config_snapshot=row.calc_config_snapshot,
        random_seed=row.random_seed,
        program_version=row.program_version,
        extension_versions=row.extension_versions,
        tolerances=row.tolerances,
        canonical_assembly_text=row.canonical_assembly_text,
        assembly_receipt=row.assembly_receipt,
        created_by=row.created_by,
    )


def _row_to_task(row: Task) -> TaskRecord:
    return TaskRecord(
        id=row.id,
        project_id=row.project_id,
        type=row.type,
        status=row.status,
        requested_by=row.requested_by,
        business_outcome=row.business_outcome,
        idempotency_key=row.idempotency_key,
        calc_snapshot_id=row.calc_snapshot_id,
        priority=row.priority,
        deadline=_iso(row.deadline),
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        superseded_by_task_id=row.superseded_by_task_id,
        requested_at=_iso(row.requested_at),
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


def _row_to_attempt(row: TaskAttempt) -> TaskAttemptRecord:
    return TaskAttemptRecord(
        id=row.id,
        task_id=row.task_id,
        attempt_no=row.attempt_no,
        status=row.status,
        worker_id=row.worker_id,
        stop_reason=row.stop_reason,
        started_at=_iso(row.started_at),
        finished_at=_iso(row.finished_at),
    )


def _row_to_lease(row: TaskLease) -> TaskLeaseRecord:
    return TaskLeaseRecord(
        id=row.id,
        attempt_id=row.attempt_id,
        lease_token=str(row.lease_token),
        status=row.status,
        acquired_by=row.acquired_by,
        expires_at=_iso(row.expires_at),
        renewed_at=_iso(row.renewed_at),
    )


def _row_to_progress(row: TaskProgress) -> TaskProgressRecord:
    return TaskProgressRecord(
        id=row.id,
        attempt_id=row.attempt_id,
        progress_percent=float(row.progress_percent),
        stage=row.stage,
        detail=row.detail,
        updated_at=_iso(row.updated_at),
    )


def _row_to_diagnostic(row: TaskDiagnostic) -> TaskDiagnosticRecord:
    return TaskDiagnosticRecord(
        id=row.id,
        task_id=row.task_id,
        level=row.level,
        message=row.message,
        attempt_id=row.attempt_id,
        code=row.code,
        stack_trace=row.stack_trace,
        context=row.context,
        created_at=_iso(row.created_at),
    )


def _row_to_slot(row: ComputeSlot) -> ComputeSlotRecord:
    return ComputeSlotRecord(
        id=row.id,
        pool_name=row.pool_name,
        status=row.status,
        capacity=row.capacity,
        in_use=row.in_use,
        current_attempt_id=row.current_attempt_id,
    )


def _row_to_uncertainty_snapshot(row: UncertaintySnapshot) -> UncertaintySnapshotRecord:
    return UncertaintySnapshotRecord(
        id=row.id,
        calc_snapshot_id=row.calc_snapshot_id,
        method=row.method,
        n_samples=row.n_samples,
        random_seed=row.random_seed,
        distributions=row.distributions,
        created_by=row.created_by,
    )


def _row_to_sample_task(row: SampleTask) -> SampleTaskRecord:
    return SampleTaskRecord(
        id=row.id,
        uncertainty_snapshot_id=row.uncertainty_snapshot_id,
        sample_index=row.sample_index,
        status=row.status,
        parent_task_id=row.parent_task_id,
        parent_sample_id=row.parent_sample_id,
        depth=row.depth,
        params=row.params,
    )


def _row_to_sample_record(row: SampleRecord) -> SampleRecordRecord:
    return SampleRecordRecord(
        id=row.id,
        sample_task_id=row.sample_task_id,
        variable_name=row.variable_name,
        value=float(row.value),
        unit=row.unit,
    )


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------


def create_snapshot(
    db: Session,
    *,
    project_version_id: int,
    dataset_version_ids: list[int],
    calc_config_snapshot: dict[str, Any],
    random_seed: int,
    created_by: int,
    program_version: str | None = None,
    extension_versions: dict[str, Any] | None = None,
    tolerances: dict[str, Any] | None = None,
    canonical_assembly_text: str | None = None,
    assembly_receipt: dict[str, Any] | None = None,
) -> CalcSnapshotRecord:
    """创建不可变快照行。"""
    row = CalcSnapshot(
        project_version_id=project_version_id,
        dataset_version_ids=list(dataset_version_ids),
        calc_config_snapshot=calc_config_snapshot,
        random_seed=random_seed,
        created_by=created_by,
        program_version=program_version,
        extension_versions=extension_versions if extension_versions is not None else {},
        tolerances=tolerances,
        canonical_assembly_text=canonical_assembly_text,
        assembly_receipt=assembly_receipt,
    )
    db.add(row)
    db.flush()
    return _row_to_snapshot(row)


def get_snapshot(db: Session, snapshot_id: int) -> CalcSnapshotRecord | None:
    """按主键取快照；不存在返回 None。"""
    row = db.get(CalcSnapshot, snapshot_id)
    return _row_to_snapshot(row) if row is not None else None


def list_snapshots_for_version(db: Session, project_version_id: int) -> list[CalcSnapshotRecord]:
    """取某项目版本的快照（id 倒序；快照输入复用扫描用）。"""
    rows = (
        db.execute(
            select(CalcSnapshot)
            .where(CalcSnapshot.project_version_id == project_version_id)
            .order_by(CalcSnapshot.id.desc())
        )
        .scalars()
        .all()
    )
    return [_row_to_snapshot(row) for row in rows]


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------


def create_task(
    db: Session,
    *,
    project_id: int,
    type: str,
    requested_by: int,
    idempotency_key: str | None = None,
    calc_snapshot_id: int | None = None,
    priority: int = 0,
    max_attempts: int = 3,
    deadline: datetime | None = None,
) -> TaskRecord:
    """创建 queued 任务；幂等键冲突抛 TaskConflictError。"""
    row = Task(
        project_id=project_id,
        type=type,
        status="queued",
        idempotency_key=idempotency_key,
        calc_snapshot_id=calc_snapshot_id,
        requested_by=requested_by,
        priority=priority,
        max_attempts=max_attempts,
        deadline=deadline,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise TaskConflictError(
            "任务幂等键冲突", params={"project_id": project_id, "idempotency_key": idempotency_key}
        ) from exc
    return _row_to_task(row)


def get_task(db: Session, task_id: int) -> TaskRecord | None:
    """按主键取任务；不存在返回 None。"""
    row = db.get(Task, task_id)
    return _row_to_task(row) if row is not None else None


def get_task_by_idempotency(db: Session, project_id: int, idempotency_key: str) -> TaskRecord | None:
    """按（项目，幂等键）取任务；不存在返回 None。"""
    row = db.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    return _row_to_task(row) if row is not None else None


def list_tasks(
    db: Session,
    project_id: int,
    *,
    task_type: str | None = None,
    status: str | None = None,
    outcome: str | None = None,
    limit: int = 50,
    cursor: int | None = None,
) -> list[TaskRecord]:
    """任务列表（过滤 + 游标分页，id 倒序；limit 原样透传）。"""
    stmt = select(Task).where(Task.project_id == project_id)
    if task_type is not None:
        stmt = stmt.where(Task.type == task_type)
    if status is not None:
        stmt = stmt.where(Task.status == status)
    if outcome is not None:
        stmt = stmt.where(Task.business_outcome == outcome)
    if cursor is not None:
        stmt = stmt.where(Task.id < cursor)
    rows = db.execute(stmt.order_by(Task.id.desc()).limit(limit)).scalars().all()
    return [_row_to_task(row) for row in rows]


def list_task_ids(db: Session, project_id: int) -> list[int]:
    """项目全部任务 id（id 升序；归档枚举用，不分页）。"""
    return list(db.execute(select(Task.id).where(Task.project_id == project_id).order_by(Task.id)).scalars())


def find_active_duplicate(
    db: Session, project_id: int, task_type: str, snapshot_id: int
) -> TaskRecord | None:
    """同（项目，类型，快照）的最新非终态任务；无返回 None。"""
    row = db.execute(
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.type == task_type,
            Task.calc_snapshot_id == snapshot_id,
            Task.status.in_(_NON_TERMINAL),
        )
        .order_by(Task.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _row_to_task(row) if row is not None else None


def count_tasks_by_statuses(db: Session, statuses: Collection[str]) -> int:
    """按状态集合计数任务。"""
    return int(db.execute(select(func.count(Task.id)).where(Task.status.in_(statuses))).scalar() or 0)


def cancel_pending_tasks(db: Session, project_id: int) -> int:
    """取消项目排队/取消中任务（删除协调用）；返回取消行数。"""
    result = db.execute(
        update(Task)
        .where(Task.project_id == project_id, Task.status.in_(("queued", "cancelling")))
        .values(status="cancelled", updated_at=_now())
    )
    db.flush()
    return int(result.rowcount or 0)


def has_running_tasks(db: Session, project_id: int) -> bool:
    """项目是否存在运行中任务（删除一致性检查用）。"""
    return (
        db.execute(
            select(Task.id).where(Task.project_id == project_id, Task.status == "running").limit(1)
        ).first()
        is not None
    )


def list_child_tasks(db: Session, parent_task_id: int) -> list[TaskRecord]:
    """批量父任务的子任务（经 sample_tasks 关联；调用方按状态过滤）。"""
    rows = (
        db.execute(
            select(Task)
            .join(SampleTask, SampleTask.id == Task.id)
            .where(SampleTask.parent_task_id == parent_task_id)
        )
        .scalars()
        .all()
    )
    return [_row_to_task(row) for row in rows]


def set_task_status(
    db: Session,
    task_id: int,
    status: str,
    *,
    business_outcome: str | None = None,
) -> TaskRecord:
    """推进任务状态并刷新 updated_at；缺失抛 TaskNotFoundError。

    business_outcome=None 表示清空（与直接赋值语义一致）。
    """
    row = db.get(Task, task_id)
    if row is None:
        raise TaskNotFoundError("任务不存在", params={"task_id": task_id})
    row.status = status
    row.business_outcome = business_outcome
    row.updated_at = _now()
    db.flush()
    return _row_to_task(row)


# ---------------------------------------------------------------------------
# 尝试
# ---------------------------------------------------------------------------


def create_attempt(db: Session, *, task_id: int, worker_id: str | None = None) -> TaskAttemptRecord:
    """追加尝试行（含 attempt_no 分配与 task.attempt_count 推进）。"""
    task_row = db.get(Task, task_id)
    if task_row is None:
        raise TaskNotFoundError("任务不存在", params={"task_id": task_id})
    attempt_no = int(task_row.attempt_count or 0) + 1
    row = TaskAttempt(
        task_id=task_id,
        attempt_no=attempt_no,
        worker_id=worker_id,
        status="running",
        started_at=_now(),
    )
    db.add(row)
    task_row.attempt_count = attempt_no
    task_row.updated_at = _now()
    try:
        db.flush()
    except IntegrityError as exc:
        raise TaskConflictError(
            "尝试序号冲突", params={"task_id": task_id, "attempt_no": attempt_no}
        ) from exc
    return _row_to_attempt(row)


def finish_attempt(
    db: Session, attempt_id: int, status: str, *, stop_reason: str | None = None
) -> TaskAttemptRecord:
    """收尾尝试；缺失抛 TaskNotFoundError。"""
    row = db.get(TaskAttempt, attempt_id)
    if row is None:
        raise TaskNotFoundError("尝试不存在", params={"attempt_id": attempt_id})
    row.status = status
    row.stop_reason = stop_reason
    row.finished_at = _now()
    db.flush()
    return _row_to_attempt(row)


def get_attempt(db: Session, attempt_id: int) -> TaskAttemptRecord | None:
    """按主键取尝试；不存在返回 None。"""
    row = db.get(TaskAttempt, attempt_id)
    return _row_to_attempt(row) if row is not None else None


def get_latest_attempt(db: Session, task_id: int, attempt_id: int | None = None) -> TaskAttemptRecord | None:
    """取任务最新尝试（attempt_no 最大；指定 attempt_id 则限定该行）。"""
    stmt = select(TaskAttempt).where(TaskAttempt.task_id == task_id)
    if attempt_id is not None:
        stmt = stmt.where(TaskAttempt.id == attempt_id)
    row = db.execute(stmt.order_by(TaskAttempt.attempt_no.desc()).limit(1)).scalar_one_or_none()
    return _row_to_attempt(row) if row is not None else None


def get_running_attempt(db: Session, task_id: int) -> TaskAttemptRecord | None:
    """取任务当前 running 尝试（attempt_no 最大）；无返回 None。"""
    row = db.execute(
        select(TaskAttempt)
        .where(TaskAttempt.task_id == task_id, TaskAttempt.status == "running")
        .order_by(TaskAttempt.attempt_no.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _row_to_attempt(row) if row is not None else None


def list_attempts(db: Session, task_id: int) -> list[TaskAttemptRecord]:
    """取任务全部尝试（attempt_no 升序）。"""
    rows = (
        db.execute(select(TaskAttempt).where(TaskAttempt.task_id == task_id).order_by(TaskAttempt.attempt_no))
        .scalars()
        .all()
    )
    return [_row_to_attempt(row) for row in rows]


# ---------------------------------------------------------------------------
# 租约
# ---------------------------------------------------------------------------


def acquire_lease(
    db: Session,
    *,
    attempt_id: int,
    lease_token: str,
    acquired_by: str | None = None,
    ttl_seconds: int = 60,
) -> TaskLeaseRecord:
    """领取租约；同 attempt 已有 active 租约抛 TaskConflictError。"""
    existing = db.execute(
        select(TaskLease).where(TaskLease.attempt_id == attempt_id, TaskLease.status == "active")
    ).scalar_one_or_none()
    if existing is not None:
        raise TaskConflictError("尝试已有 active 租约", params={"attempt_id": attempt_id})
    now = _now()
    row = TaskLease(
        attempt_id=attempt_id,
        lease_token=UUID(str(lease_token)),
        acquired_by=acquired_by,
        acquired_at=now,
        renewed_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
        status="active",
    )
    db.add(row)
    db.flush()
    return _row_to_lease(row)


def renew_lease(db: Session, attempt_id: int, lease_token: str) -> TaskLeaseRecord:
    """续租（token 不符抛 TaskConflictError）。"""
    row = db.execute(
        select(TaskLease).where(TaskLease.attempt_id == attempt_id, TaskLease.status == "active")
    ).scalar_one_or_none()
    if row is None or str(row.lease_token) != str(lease_token):
        raise TaskConflictError("租约 token 不符或租约非 active", params={"attempt_id": attempt_id})
    row.renewed_at = _now()
    db.flush()
    return _row_to_lease(row)


def release_lease(
    db: Session, attempt_id: int, lease_token: str, *, status: str = "released"
) -> TaskLeaseRecord:
    """释放租约；token 失配或非 active 抛 TaskConflictError。"""
    row = db.execute(
        select(TaskLease).where(TaskLease.attempt_id == attempt_id, TaskLease.status == "active")
    ).scalar_one_or_none()
    if row is None or str(row.lease_token) != str(lease_token):
        raise TaskConflictError("租约 token 不符或租约非 active", params={"attempt_id": attempt_id})
    row.status = status
    db.flush()
    return _row_to_lease(row)


def get_active_lease_for_attempt(db: Session, attempt_id: int) -> TaskLeaseRecord | None:
    """取尝试的 active 租约；无返回 None。"""
    row = db.execute(
        select(TaskLease).where(TaskLease.attempt_id == attempt_id, TaskLease.status == "active")
    ).scalar_one_or_none()
    return _row_to_lease(row) if row is not None else None


def get_active_lease_for_task(db: Session, task_id: int) -> TaskLeaseRecord | None:
    """取任务各尝试中最新的 active 租约；无返回 None。"""
    row = db.execute(
        select(TaskLease)
        .join(TaskAttempt, TaskAttempt.id == TaskLease.attempt_id)
        .where(TaskAttempt.task_id == task_id, TaskLease.status == "active")
        .order_by(TaskLease.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _row_to_lease(row) if row is not None else None


def get_lease_by_token(db: Session, lease_token: str) -> TaskLeaseRecord | None:
    """按 token 取租约（fencing 校验用）；非法 token 或无匹配返回 None。"""
    try:
        token = lease_token if isinstance(lease_token, UUID) else UUID(str(lease_token))
    except (ValueError, TypeError):
        return None
    row = db.execute(select(TaskLease).where(TaskLease.lease_token == token)).scalar_one_or_none()
    return _row_to_lease(row) if row is not None else None


# ---------------------------------------------------------------------------
# 槽位
# ---------------------------------------------------------------------------


def ensure_slots(db: Session, pools: Mapping[str, int]) -> None:
    """惰性初始化槽表（每池按并发额度建 capacity 行，幂等）。"""
    for pool, capacity in pools.items():
        existing = (
            db.execute(select(func.count(ComputeSlot.id)).where(ComputeSlot.pool_name == pool)).scalar() or 0
        )
        for _ in range(max(int(capacity) - int(existing), 0)):
            db.add(ComputeSlot(pool_name=pool, status="free", capacity=1, in_use=0))
    db.flush()


def acquire_slot(db: Session, pool_name: str) -> ComputeSlotRecord | None:
    """取可用槽位并标记占用（行锁，in_use+1，不绑定尝试）；无可用返回 None。"""
    slots = (
        db.execute(
            select(ComputeSlot)
            .where(ComputeSlot.pool_name == pool_name, ComputeSlot.status.in_(("free", "busy")))
            .order_by(ComputeSlot.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    for slot in slots:
        if slot.in_use < slot.capacity:
            slot.in_use += 1
            db.flush()
            return _row_to_slot(slot)
    return None


def bind_slot_attempt(db: Session, slot_id: int, attempt_id: int) -> ComputeSlotRecord:
    """槽绑定尝试（领取成功后）；缺失抛 TaskNotFoundError。"""
    row = db.get(ComputeSlot, slot_id)
    if row is None:
        raise TaskNotFoundError("槽位不存在", params={"slot_id": slot_id})
    row.current_attempt_id = attempt_id
    db.flush()
    return _row_to_slot(row)


def release_slot(db: Session, attempt_id: int) -> None:
    """释放尝试绑定的槽。"""
    row = db.execute(
        select(ComputeSlot).where(ComputeSlot.current_attempt_id == attempt_id)
    ).scalar_one_or_none()
    if row is not None:
        row.in_use = max(row.in_use - 1, 0)
        row.current_attempt_id = None
        db.flush()


# ---------------------------------------------------------------------------
# 进度 / 诊断
# ---------------------------------------------------------------------------


def get_progress(db: Session, attempt_id: int) -> TaskProgressRecord | None:
    """取尝试进度行；无返回 None。"""
    row = db.execute(select(TaskProgress).where(TaskProgress.attempt_id == attempt_id)).scalar_one_or_none()
    return _row_to_progress(row) if row is not None else None


def latest_progress_for_task(db: Session, task_id: int) -> TaskProgressRecord | None:
    """取任务最新尝试的进度行（attempt_no 最大）；无返回 None。"""
    row = db.execute(
        select(TaskProgress)
        .join(TaskAttempt, TaskAttempt.id == TaskProgress.attempt_id)
        .where(TaskAttempt.task_id == task_id)
        .order_by(TaskAttempt.attempt_no.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _row_to_progress(row) if row is not None else None


def upsert_progress(
    db: Session,
    *,
    attempt_id: int,
    progress_percent: float,
    stage: str | None = None,
    detail: dict[str, Any] | None = None,
) -> TaskProgressRecord:
    """进度 UPSERT（每尝试一行）。"""
    row = db.execute(select(TaskProgress).where(TaskProgress.attempt_id == attempt_id)).scalar_one_or_none()
    if row is None:
        row = TaskProgress(
            attempt_id=attempt_id,
            progress_percent=progress_percent,
            stage=stage,
            detail=detail,
            updated_at=_now(),
        )
        db.add(row)
    else:
        row.progress_percent = progress_percent
        row.stage = stage
        row.detail = detail
        row.updated_at = _now()
    db.flush()
    return _row_to_progress(row)


def append_diagnostic(
    db: Session,
    *,
    task_id: int,
    level: str,
    message: str,
    attempt_id: int | None = None,
    code: str | None = None,
    stack_trace: str | None = None,
    context: dict[str, Any] | None = None,
) -> TaskDiagnosticRecord:
    """追加诊断（不可变，只 INSERT）。"""
    row = TaskDiagnostic(
        task_id=task_id,
        attempt_id=attempt_id,
        level=level,
        code=code,
        message=message,
        stack_trace=stack_trace,
        context=context,
    )
    db.add(row)
    db.flush()
    return _row_to_diagnostic(row)


def list_diagnostics(db: Session, task_id: int) -> list[TaskDiagnosticRecord]:
    """取任务全部诊断（id 升序）。"""
    rows = (
        db.execute(
            select(TaskDiagnostic).where(TaskDiagnostic.task_id == task_id).order_by(TaskDiagnostic.id)
        )
        .scalars()
        .all()
    )
    return [_row_to_diagnostic(row) for row in rows]


def latest_diagnostic(db: Session, task_id: int, code: str) -> TaskDiagnosticRecord | None:
    """取任务某 code 最新诊断；无返回 None。"""
    row = db.execute(
        select(TaskDiagnostic)
        .where(TaskDiagnostic.task_id == task_id, TaskDiagnostic.code == code)
        .order_by(TaskDiagnostic.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _row_to_diagnostic(row) if row is not None else None


# ---------------------------------------------------------------------------
# 不确定性
# ---------------------------------------------------------------------------


def create_uncertainty_snapshot(
    db: Session,
    *,
    calc_snapshot_id: int,
    method: str,
    n_samples: int,
    random_seed: int,
    distributions: dict[str, Any],
    created_by: int,
) -> UncertaintySnapshotRecord:
    """创建不确定性快照（不可变）。"""
    row = UncertaintySnapshot(
        calc_snapshot_id=calc_snapshot_id,
        method=method,
        n_samples=n_samples,
        random_seed=random_seed,
        distributions=distributions,
        created_by=created_by,
    )
    db.add(row)
    db.flush()
    return _row_to_uncertainty_snapshot(row)


def get_sample_task(db: Session, sample_id: int) -> SampleTaskRecord | None:
    """按主键取采样任务；不存在返回 None。"""
    row = db.get(SampleTask, sample_id)
    return _row_to_sample_task(row) if row is not None else None


def create_sample_task(
    db: Session,
    *,
    uncertainty_snapshot_id: int,
    sample_index: int,
    parent_task_id: int | None = None,
    parent_sample_id: int | None = None,
    params: dict[str, Any] | None = None,
) -> SampleTaskRecord:
    """创建采样任务行（初始 queued）。"""
    row = SampleTask(
        uncertainty_snapshot_id=uncertainty_snapshot_id,
        sample_index=sample_index,
        parent_task_id=parent_task_id,
        parent_sample_id=parent_sample_id,
        params=params,
        status="queued",
    )
    db.add(row)
    db.flush()
    return _row_to_sample_task(row)


def record_sample(
    db: Session,
    *,
    sample_task_id: int,
    variable_name: str,
    value: float,
    unit: str | None = None,
) -> SampleRecordRecord:
    """记录样本取值（不可变，只 INSERT）。"""
    row = SampleRecord(
        sample_task_id=sample_task_id,
        variable_name=variable_name,
        value=value,
        unit=unit,
    )
    db.add(row)
    db.flush()
    return _row_to_sample_record(row)


def create_sample_row(
    db: Session,
    *,
    uncertainty_snapshot_id: int,
    parent_task_id: int,
    sample_index: int,
    status: str,
    params: dict[str, Any] | None = None,
) -> SampleTaskRecord:
    """创建样本行（执行态 status 直写；顶层单样本，非批量子节点）。"""
    row = SampleTask(
        uncertainty_snapshot_id=uncertainty_snapshot_id,
        parent_task_id=parent_task_id,
        parent_sample_id=None,
        sample_index=sample_index,
        depth=0,
        params=params,
        status=status,
    )
    db.add(row)
    db.flush()
    return _row_to_sample_task(row)


def count_completed_samples(db: Session, parent_task_id: int) -> int:
    """已完成样本数（父任务部分完成判定用；无返回 0）。"""
    return int(
        db.execute(
            select(func.count(SampleTask.id)).where(
                SampleTask.parent_task_id == parent_task_id,
                SampleTask.status == "completed",
            )
        ).scalar()
        or 0
    )


def count_tasks_by_status(db: Session) -> dict[str, int]:
    """任务按状态分组计数（运维诊断视图用）。"""
    return {
        str(status): int(count)
        for status, count in db.execute(select(Task.status, func.count()).group_by(Task.status)).all()
    }


def count_tasks_by_type(db: Session) -> dict[str, int]:
    """任务按类型分组计数（运维诊断视图用）。"""
    return {
        str(task_type): int(count)
        for task_type, count in db.execute(select(Task.type, func.count()).group_by(Task.type)).all()
    }


def list_recent_failed_tasks(db: Session, limit: int = 5) -> list[TaskRecord]:
    """最近失败任务（updated_at 倒序；运维诊断视图用）。"""
    rows = (
        db.execute(select(Task).where(Task.status == "failed").order_by(Task.updated_at.desc()).limit(limit))
        .scalars()
        .all()
    )
    return [_row_to_task(row) for row in rows]


def revoke_leases_for_attempts(db: Session, attempt_ids: Collection[int]) -> int:
    """吊销尝试清单上全部 active 租约（管理员解锁用；返回吊销行数）。"""
    ids = list(attempt_ids)
    if not ids:
        return 0
    result = db.execute(
        update(TaskLease)
        .where(TaskLease.attempt_id.in_(ids), TaskLease.status == "active")
        .values(status="revoked")
    )
    db.flush()
    return int(result.rowcount or 0)


def pool_has_free_slot(db: Session, pool_name: str) -> bool:
    """槽门禁：池内是否存在可用槽（领取前确认；槽行未初始化视为有空位）。"""
    rows = (
        db.execute(
            select(ComputeSlot).where(
                ComputeSlot.pool_name == pool_name,
                ComputeSlot.status.in_(("free", "busy")),
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return True
    return any(row.in_use < row.capacity for row in rows)


def fence_renew_lease(
    db: Session, attempt_id: int, lease_token: UUID | str, *, ttl_seconds: int
) -> int:
    """带 fencing 的租约续期（renewed_at/expires_at 推进；返回影响行数）。"""
    token = lease_token if isinstance(lease_token, UUID) else UUID(str(lease_token))
    now = _now()
    return int(
        db.execute(
            update(TaskLease)
            .where(
                TaskLease.attempt_id == attempt_id,
                TaskLease.lease_token == token,
                TaskLease.status == "active",
            )
            .values(renewed_at=now, expires_at=now + timedelta(seconds=ttl_seconds))
        ).rowcount
        or 0
    )


def fence_release_lease(
    db: Session, attempt_id: int, lease_token: UUID | str, *, status: str
) -> int:
    """带 fencing 的租约收尾（0 行表示租约不匹配；返回影响行数）。"""
    token = lease_token if isinstance(lease_token, UUID) else UUID(str(lease_token))
    return int(
        db.execute(
            update(TaskLease)
            .where(
                TaskLease.attempt_id == attempt_id,
                TaskLease.lease_token == token,
                TaskLease.status == "active",
            )
            .values(status=status)
        ).rowcount
        or 0
    )


# ---------------------------------------------------------------------------
# ORM 表定义: Wave2A 由 iesplan.models.calc(任务侧) 迁入, 表真相归本域所有。
# ---------------------------------------------------------------------------

#: 任务状态枚举(01 §7.2, 与契约第3节一致)
TASK_STATUSES: tuple[str, ...] = (
    "queued", "running", "completed", "cancelling", "cancelled", "timed_out", "failed"
)

#: 业务结局枚举(01 §7.2, 与契约第3节一致)
TASK_OUTCOMES: tuple[str, ...] = (
    "normal_completion", "no_recommendation", "no_feasible_multi_objective",
    "partial_batch", "restricted_results", "insufficient_evidence",
)


class CalcSnapshot(Base):
    """计算快照(任务唯一输入, 不可变, 01 §7.1)。"""

    __tablename__ = "calc_snapshots"

    id: Mapped[int] = bigint_pk()
    project_version_id: Mapped[int] = mapped_column(ForeignKey("project_versions.id"), nullable=False)
    dataset_version_ids: Mapped[list[int]] = mapped_column(
        BigIntArray, nullable=False, server_default=sa.text("'{}'")
    )
    calc_config_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    program_version: Mapped[str | None] = mapped_column(Text)
    extension_versions: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'"))
    random_seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tolerances: Mapped[dict | None] = mapped_column(JSONB)
    #: 规范装配文本 + 确定性校验回执（二件套，文本仅校验字头，不做 SHA）。
    #: 旧库 assembly_text 列已删除(W2-B: 当前 schema 从空库直接建立，不做升级
    #: 回填)；Worker 必须拒绝缺任一成员的快照；所有新快照由统一校验入口完整写入。
    canonical_assembly_text: Mapped[str | None] = mapped_column(Text)
    assembly_receipt: Mapped[dict | None] = mapped_column(JSONB)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        Index("idx_calc_snapshots_version", "project_version_id", sa.text("created_at DESC")),
    )


class Task(Base):
    """任务(状态机、类型、业务结局、幂等键, 01 §7.2)。"""

    __tablename__ = "tasks"

    id: Mapped[int] = bigint_pk()
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    business_outcome: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    calc_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("calc_snapshots.id"))
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=sa.text("0"))
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by_task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("3"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "type IN ('calc','optimization','uncertainty','analysis',"
            "'import','export','report','dataset_build')",
            name="ck_tasks_type",
        ),
        CheckConstraint(
            f"status IN {TASK_STATUSES!r}", name="ck_tasks_status"
        ),
        CheckConstraint(
            f"business_outcome IS NULL OR business_outcome IN {TASK_OUTCOMES!r}",
            name="ck_tasks_outcome",
        ),
        regex_check(
            f"idempotency_key IS NULL OR idempotency_key ~ '{IDEMPOTENCY_KEY_RE}'",
            name="ck_tasks_idempotency_key",
        ),
        CheckConstraint("max_attempts BETWEEN 1 AND 10", name="ck_tasks_max_attempts"),
        # RR-P1-05: 幂等键唯一性限定项目范围 —— 前端幂等键由 config+params 哈希
        # 生成, 跨项目相同; 全局唯一会让另一项目同键提交命中他项目任务(replay)
        UniqueConstraint("project_id", "idempotency_key", name="uq_tasks_idempotency_key"),
        Index("idx_tasks_status", "status", sa.text("priority DESC"), "requested_at"),
        Index("idx_tasks_project", "project_id", sa.text("requested_at DESC")),
    )


class TaskAttempt(Base):
    """任务尝试(尝试序号、心跳、停止原因, 01 §7.3)。"""

    __tablename__ = "task_attempts"

    id: Mapped[int] = bigint_pk()
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','succeeded','failed','stopped')",
            name="ck_task_attempts_status",
        ),
        UniqueConstraint("task_id", "attempt_no", name="uq_task_attempts_no"),
        Index("idx_task_attempts_task", "task_id", sa.text("attempt_no DESC")),
    )


class TaskLease(Base):
    """任务租约(fencing token, 01 §7.4)。"""

    __tablename__ = "task_leases"

    id: Mapped[int] = bigint_pk()
    attempt_id: Mapped[int] = mapped_column(ForeignKey("task_attempts.id"), nullable=False)
    lease_token: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    acquired_by: Mapped[str | None] = mapped_column(Text)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    renewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('active','expired','released','revoked')", name="ck_task_leases_status"
        ),
        UniqueConstraint("lease_token", name="uq_task_leases_token"),
        Index(
            "uq_task_leases_one_active",
            "attempt_id",
            unique=True,
            postgresql_where=sa.text("status = 'active'"),
            sqlite_where=sa.text("status = 'active'"),
        ),
        Index("idx_task_leases_token", "lease_token"),
        Index(
            "idx_task_leases_expiry",
            "expires_at",
            postgresql_where=sa.text("status = 'active'"),
            sqlite_where=sa.text("status = 'active'"),
        ),
    )


class TaskProgress(Base):
    """任务进度(PG 持久进度, 每尝试至多一行, 01 §7.5)。"""

    __tablename__ = "task_progress"

    id: Mapped[int] = bigint_pk()
    attempt_id: Mapped[int] = mapped_column(ForeignKey("task_attempts.id"), nullable=False)
    progress_percent: Mapped[float] = mapped_column(
        Numeric(5, 2), nullable=False, server_default=sa.text("0")
    )
    stage: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint("progress_percent BETWEEN 0 AND 100", name="ck_task_progress_percent"),
        Index("uq_task_progress_latest", "attempt_id", unique=True),
        Index("idx_task_progress_attempt", "attempt_id"),
    )


class TaskDiagnostic(Base):
    """任务诊断(不可变, 01 §7.6)。"""

    __tablename__ = "task_diagnostics"

    id: Mapped[int] = bigint_pk()
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    attempt_id: Mapped[int | None] = mapped_column(ForeignKey("task_attempts.id"))
    level: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    stack_trace: Mapped[str | None] = mapped_column(Text)
    context: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "level IN ('blocking','error','warning','info')", name="ck_task_diagnostics_level"
        ),
        Index("idx_task_diagnostics_task", "task_id", "created_at"),
        Index("idx_task_diagnostics_level", "level", "created_at"),
    )


class ComputeSlot(Base):
    """计算并发槽(01 §7.7)。"""

    __tablename__ = "compute_slots"

    id: Mapped[int] = bigint_pk()
    pool_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("1"))
    in_use: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    current_attempt_id: Mapped[int | None] = mapped_column(ForeignKey("task_attempts.id"))
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('free','busy','draining','offline')", name="ck_compute_slots_status"
        ),
        CheckConstraint("capacity >= 1", name="ck_compute_slots_capacity"),
        CheckConstraint("in_use >= 0", name="ck_compute_slots_in_use"),
        CheckConstraint("in_use <= capacity", name="ck_compute_slots_capacity_bound"),
        Index(
            "uq_compute_slots_attempt",
            "current_attempt_id",
            unique=True,
            postgresql_where=sa.text("current_attempt_id IS NOT NULL"),
            sqlite_where=sa.text("current_attempt_id IS NOT NULL"),
        ),
        Index("idx_compute_slots_pool", "pool_name", "status"),
    )


# ---------------------------------------------------------------------------
# ORM 表定义: Wave2A 由 iesplan.models.uncertainty 迁入, 表真相归本域所有。
# ---------------------------------------------------------------------------

class UncertaintySnapshot(Base):
    """不确定性快照(不可变, 01 §9.1)。"""

    __tablename__ = "uncertainty_snapshots"

    id: Mapped[int] = bigint_pk()
    calc_snapshot_id: Mapped[int] = mapped_column(ForeignKey("calc_snapshots.id"), nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    n_samples: Mapped[int] = mapped_column(Integer, nullable=False)
    random_seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    distributions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "method IN ('monte_carlo','lhs','scenario','robust')", name="ck_uncertainty_method"
        ),
        CheckConstraint("n_samples BETWEEN 1 AND 1000000", name="ck_uncertainty_n_samples"),
        Index("idx_uncertainty_snapshots_calc", "calc_snapshot_id"),
    )


class SampleTask(Base):
    """采样任务(父子树形分解, 01 §9.2)。"""

    __tablename__ = "sample_tasks"

    id: Mapped[int] = bigint_pk()
    uncertainty_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("uncertainty_snapshots.id"), nullable=False
    )
    parent_task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))
    parent_sample_id: Mapped[int | None] = mapped_column(ForeignKey("sample_tasks.id"))
    sample_index: Mapped[int] = mapped_column(Integer, nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    params: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("depth BETWEEN 0 AND 10", name="ck_sample_tasks_depth"),
        CheckConstraint(f"status IN {TASK_STATUSES!r}", name="ck_sample_tasks_status"),
        Index(
            "uq_sample_tasks_top",
            "uncertainty_snapshot_id",
            "sample_index",
            unique=True,
            postgresql_where=sa.text("parent_sample_id IS NULL"),
            sqlite_where=sa.text("parent_sample_id IS NULL"),
        ),
        Index(
            "uq_sample_tasks_child",
            "parent_sample_id",
            "sample_index",
            unique=True,
            postgresql_where=sa.text("parent_sample_id IS NOT NULL"),
            sqlite_where=sa.text("parent_sample_id IS NOT NULL"),
        ),
        Index("idx_sample_tasks_snapshot", "uncertainty_snapshot_id", "status"),
        Index("idx_sample_tasks_parent", "parent_sample_id"),
    )


class SampleRecord(Base):
    """样本记录(01 §9.3)。"""

    __tablename__ = "sample_records"

    id: Mapped[int] = bigint_pk()
    sample_task_id: Mapped[int] = mapped_column(ForeignKey("sample_tasks.id"), nullable=False)
    variable_name: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    unit: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("sample_task_id", "variable_name", name="uq_sample_records_variable"),
        Index("idx_sample_records_task", "sample_task_id"),
    )


#: 本域拥有的不可变表(仅 INSERT, 禁止 UPDATE/DELETE)
IMMUTABLE_TABLES: tuple[str, ...] = (
    "calc_snapshots",
    "task_diagnostics",
    "uncertainty_snapshots",
)

#: tasks: 终态(completed/cancelled/timed_out/failed)禁止再迁移状态(01 §7.2)
TASKS_TERMINAL_TRIGGER_SQL: str = """\
-- tasks: 终态任务不可迁移状态
CREATE FUNCTION tg_tasks_terminal() RETURNS trigger AS $$
BEGIN
  IF OLD.status IN ('completed','cancelled','timed_out','failed') AND NEW.status <> OLD.status THEN
    RAISE EXCEPTION '终态任务不可迁移状态';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER tg_tasks_terminal BEFORE UPDATE ON tasks
  FOR EACH ROW EXECUTE FUNCTION tg_tasks_terminal();
"""


def install_tables() -> None:
    """公开安装钩子: 导入本模块即完成 Base.metadata 表注册; 幂等, 无其他副作用。"""
    return None


def install_triggers() -> tuple[str, ...]:
    """公开钩子: 返回本域触发器部署语句(按执行序, 含幂等 DROP, 供组合根编排收集)。"""
    statements = [drop_trigger_function_sql(f"tg_{table}_immutable") for table in IMMUTABLE_TABLES]
    statements.extend(immutable_trigger_sql(table) for table in IMMUTABLE_TABLES)
    statements.extend(immutable_revoke_sql(table) for table in IMMUTABLE_TABLES)
    statements.append(drop_trigger_function_sql("tg_tasks_terminal"))
    statements.append(TASKS_TERMINAL_TRIGGER_SQL)
    return tuple(statements)
