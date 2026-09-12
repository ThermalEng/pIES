"""Worker 租约/队列/结果提交用例(application/worker.lease_cases): 领取与收拢转调。

本模块收拢 ``iesplan.worker.lease`` 原先对可重建视图、任务编排与 ORM
行的直接访问, 只做转调与行级读写搬运, 不新增校验/hash/回退:

- 领取/进度/完成/失败/槽释放 → tasks 域门面 + 队列可重建视图;
- 出队/心跳/取消信号清除 → tasks 域队列视图;
- 证据对象写入/引用 → ``storage.put_object`` / ``storage.add_ref``;
- 任务/快照/尝试/诊断读与租约 fencing 写(含带 fencing 的尝试收尾) →
  tasks/results 域门面;
- 租约失效错误 ``LeaseRejectedError`` 由本模块拥有(码 TASK-LEASE-001),
  worker 层仅复出, 不自建错误语义。

``Claim`` / ``TaskStateError`` / ``LEASE_TTL_SECONDS`` 复用任务提交用例
同名公开符号, worker 层经本模块取用, 不再直连 ``services.*`` 与 ``models.*``。

本模块不拥有事务(只经领域公开门面写入 + flush); 完整尝试事务的提交/
回滚由同包 ``attempt_cases`` 用例拥有。

依赖方向: worker → application → (storage/领域门面)。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from iesplan import results as results_domain
from iesplan import tasks as tasks_domain
from iesplan.application.tasks.submissions import (
    IO_SLOT_CAPACITY,
    LEASE_TTL_SECONDS,
    POOL_BY_TYPE,
    TERMINAL_STATUSES,
    VALID_TRANSITIONS,
    Claim,
    TaskStateError,
    map_business_outcome,
)
from iesplan.config import settings
from iesplan.core.diagnostics import (
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    TASK_DATA_HASH_MISMATCH,
    TASK_DATA_SNAPSHOT_MISSING,
    TASK_QUEUED,
)
from iesplan.core.errors import AppError, NotFoundError
from iesplan.storage import add_ref, put_object
from iesplan.tasks import (
    CalcSnapshotRecord,
    TaskAttemptRecord,
    TaskDiagnosticRecord,
    TaskLeaseRecord,
    TaskNotFoundError,
    TaskRecord,
)


class LeaseRejectedError(AppError):
    """租约失效/过期后的迟到写回(03 §6.3 建议登记 TASK-LEASE-001, warning 不阻断)。

    Worker 收到本异常必须立即: 终止子进程 → 停止一切 PG/对象存储写入。
    本类由 application.worker 拥有; ``iesplan.worker.lease`` 仅复出同名符号。
    """

    code = "TASK-LEASE-001"
    severity = SEVERITY_WARNING
    message_key = "ies.diag.task.lease_rejected"


def _get_task(db: Session, task_id: int) -> TaskRecord:
    """按 id 取任务; 不存在 404。"""
    task = tasks_domain.get_task(db, task_id)
    if task is None:
        raise NotFoundError(
            "任务不存在",
            params={"task_id": task_id},
            location={"object_type": "task", "object_id": task_id},
        )
    return task


def _check_transition(task: TaskRecord, new_status: str) -> None:
    """状态机校验: 终态不可迁移; 非法跳转抛 TaskStateError。"""
    if task.status in TERMINAL_STATUSES:
        raise TaskStateError(
            "终态任务不可迁移状态",
            code="TASK-STATE-002",
            params={"task_id": task.id, "status": task.status},
            location={"object_type": "task", "object_id": task.id},
        )
    if new_status not in VALID_TRANSITIONS.get(task.status, frozenset()):
        raise TaskStateError(
            "非法状态迁移",
            code="TASK-STATE-003",
            params={"task_id": task.id, "from": task.status, "to": new_status},
            location={"object_type": "task", "object_id": task.id},
        )


def _finish_attempt(
    db: Session, task: TaskRecord, status: str, stop_reason: str | None
) -> TaskAttemptRecord | None:
    """收尾当前运行尝试: 尝试终态 + 租约释放/吊销 + 槽释放。只 flush, 不提交。"""
    attempt = tasks_domain.get_running_attempt(db, task.id)
    if attempt is None:
        return None
    finished = tasks_domain.finish_attempt(db, attempt.id, status, stop_reason=stop_reason)
    lease = tasks_domain.get_active_lease_for_attempt(db, attempt.id)
    if lease is not None:
        tasks_domain.release_lease(
            db,
            attempt.id,
            lease.lease_token,
            status="released" if status == "succeeded" else "revoked",
        )
    tasks_domain.release_slot(db, attempt.id)
    return finished


def _write_task_diagnostic(
    db: Session,
    task_id: int,
    *,
    level: str,
    code: str,
    message: str,
    attempt_id: int | None = None,
    stack_trace: str | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    """写入任务诊断(不可变, 只 INSERT; 经 tasks 域)。"""
    tasks_domain.append_diagnostic(
        db,
        task_id=task_id,
        level=level,
        message=message,
        attempt_id=attempt_id,
        code=code,
        stack_trace=stack_trace,
        context=context,
    )


def acquire_task(db: Session, task_id: int, worker_id: str) -> Claim | None:
    """领取任务: 占槽 + 建尝试 + 建租约(fencing token) + 任务 running。

    尝试/租约/任务状态/槽占用同批 flush; 无空槽或任务非 queued 时返回 None
    (任务保持排队)。不提交事务。
    """
    tasks_domain.ensure_slots(db, {"compute": settings.compute_slots, "io": IO_SLOT_CAPACITY})
    task = _get_task(db, task_id)
    if task.status != "queued":
        return None
    pool = POOL_BY_TYPE.get(task.type, "compute")
    slot = tasks_domain.acquire_slot(db, pool)
    if slot is None:
        return None
    attempt = tasks_domain.create_attempt(db, task_id=task.id, worker_id=worker_id)
    token = uuid4()
    tasks_domain.acquire_lease(
        db,
        attempt_id=attempt.id,
        lease_token=str(token),
        acquired_by=worker_id,
        ttl_seconds=LEASE_TTL_SECONDS,
    )
    tasks_domain.set_task_status(db, task.id, "running")
    tasks_domain.bind_slot_attempt(db, slot.id, attempt.id)
    tasks_domain.remove(task.id, pool)  # 领取后出队(视图)
    return Claim(
        task_id=task.id,
        attempt_id=attempt.id,
        attempt_no=attempt.attempt_no,
        lease_token=token,
    )


def record_task_progress(
    db: Session,
    task_id: int,
    stage: str,
    percent: float,
    detail: dict[str, Any] | None = None,
    *,
    attempt_id: int | None = None,
) -> Any:
    """记录任务进度(PG UPSERT + 队列秒级进度)。

    attempt_id 缺省取该任务当前尝试; percent 收敛到 0-100。不提交事务。
    """
    task = _get_task(db, task_id)
    attempt = tasks_domain.get_latest_attempt(db, task.id, attempt_id)
    if attempt is None:
        return None
    percent = round(min(max(float(percent), 0.0), 100.0), 2)
    tasks_domain.upsert_progress(
        db, attempt_id=attempt.id, progress_percent=percent, stage=stage, detail=detail
    )
    tasks_domain.set_queue_progress(task.id, attempt.attempt_no, percent, stage, detail)
    return attempt


def complete_task(
    db: Session, task_id: int, *, outcome: str | None = None, solver_status: str | None = None
) -> Any:
    """任务正常完成(running → completed; 取消竞态下 cancelling → completed)。

    business_outcome 与技术状态正交: 未显式给定时按求解器状态映射,
    缺省 normal_completion。重复调用幂等(已 completed 直接返回)。不提交事务。
    """
    task = _get_task(db, task_id)
    if task.status == "completed":
        return task
    _check_transition(task, "completed")
    if outcome is None:
        outcome = map_business_outcome(solver_status) if solver_status else "normal_completion"
    attempt = _finish_attempt(db, task, status="succeeded", stop_reason=None)
    task = tasks_domain.set_task_status(db, task.id, "completed", business_outcome=outcome)
    tasks_domain.clear_cancel(task.id)
    _write_task_diagnostic(
        db,
        task.id,
        level=SEVERITY_INFO,
        code=TASK_QUEUED,
        message="任务完成",
        attempt_id=attempt.id if attempt else None,
        context={"business_outcome": outcome},
    )
    return task


def fail_task(
    db: Session,
    task_id: int,
    *,
    code: str | None = None,
    message: str = "",
    stack_trace: str | None = None,
    level: str = SEVERITY_ERROR,
    outcome: str | None = None,
) -> Any:
    """任务确定性失败收拢(确定性失败不可自动重试; 写 error/blocking 诊断)。不提交事务。"""
    task = _get_task(db, task_id)
    if task.status == "failed":
        return task
    _check_transition(task, "failed")
    if outcome is None:
        # 快照/数据校验失败 → insufficient_evidence
        outcome = (
            "insufficient_evidence" if code in (TASK_DATA_SNAPSHOT_MISSING, TASK_DATA_HASH_MISMATCH) else None
        )
    attempt = _finish_attempt(db, task, status="failed", stop_reason=code or "error")
    task = tasks_domain.set_task_status(db, task.id, "failed", business_outcome=outcome)
    _write_task_diagnostic(
        db,
        task.id,
        level=level,
        code=code or "TASK-SOLVE-001",
        message=message,
        attempt_id=attempt.id if attempt else None,
        stack_trace=stack_trace,
        context={"outcome": outcome},
    )
    return task


def release_slot(db: Session, attempt_id: int) -> None:
    """释放尝试占用的并发槽。不提交事务。"""
    tasks_domain.release_slot(db, attempt_id)


def clear_cancel_signal(task_id: int) -> None:
    """清除任务取消信号(可重建视图)。"""
    tasks_domain.clear_cancel(task_id)


def store_result_blob(db: Session, blob: bytes, *, actor_id: int | None) -> int:
    """证据载荷写入对象存储, 返回对象 id(转调 storage 公开门面)。"""
    return put_object(
        db, blob, "application/json", source_category="evidence",
        purpose="evidence_package", actor_id=actor_id,
    ).id


def attach_result_ref(
    db: Session, object_id: int, evidence_id: int, *, actor_id: int | None
) -> Any:
    """证据包对象引用挂接(转调 storage 公开门面)。"""
    return add_ref(
        db, object_id, "evidence_package", evidence_id,
        purpose="evidence_package", actor_id=actor_id,
    )


def dequeue_task(pool: str) -> int | None:
    """按入队序(FIFO)领取一个任务, 返回 task_id; 队列空返回 None。"""
    return tasks_domain.dequeue(pool)


def publish_heartbeat(worker_id: str, payload: dict[str, Any], ttl: int) -> None:
    """写 Worker 心跳(可重建视图)。"""
    tasks_domain.set_heartbeat(worker_id, payload, ttl)


def slot_available(db: Session, pool: str) -> bool:
    """槽门禁: 池内是否存在可用槽(领取前确认; 槽行未初始化视为有空位)。"""
    return tasks_domain.pool_has_free_slot(db, pool)


def verify_lease(db: Session, attempt_id: int, token: UUID | str) -> TaskLeaseRecord | None:
    """校验租约有效性: 该尝试 + token 匹配 + status='active'; 失效返回 None。"""
    record = tasks_domain.get_active_lease_for_attempt(db, attempt_id)
    if record is None or record.lease_token != str(token):
        return None
    return record


def renew_lease_once(
    db: Session, attempt_id: int, token: UUID | str, *, ttl_seconds: int
) -> int:
    """续租行级更新(renewed_at/expires_at 推进; 返回影响行数, 不提交事务)。"""
    return tasks_domain.fence_renew_lease(db, attempt_id, token, ttl_seconds=ttl_seconds)


def fence_release_lease(
    db: Session, attempt_id: int, token: UUID | str, *, status: str
) -> int:
    """带 fencing 的租约收尾(0 行表示租约不匹配; 返回影响行数, 不提交事务)。"""
    return tasks_domain.fence_release_lease(db, attempt_id, token, status=status)


# ---------------------------------------------------------------------------
# 任务/快照/尝试/诊断行读
# ---------------------------------------------------------------------------


def get_task_record(db: Session, task_id: int) -> TaskRecord | None:
    """按主键取任务公开视图; 不存在返回 None。"""
    return tasks_domain.get_task(db, task_id)


def get_snapshot_record(db: Session, snapshot_id: int) -> CalcSnapshotRecord | None:
    """按主键取计算快照公开视图; 不存在返回 None。"""
    return tasks_domain.get_snapshot(db, snapshot_id)


def get_attempt_record(db: Session, attempt_id: int) -> TaskAttemptRecord | None:
    """按主键取尝试公开视图; 不存在返回 None。"""
    return tasks_domain.get_attempt(db, attempt_id)


def finish_attempt_record(
    db: Session, attempt_id: int, *, status: str, stop_reason: str | None
) -> TaskAttemptRecord | None:
    """收尾尝试(状态/原因/完成时间; 尝试缺失返回 None, 只 flush 不提交)。"""
    try:
        return tasks_domain.finish_attempt(db, attempt_id, status, stop_reason=stop_reason)
    except TaskNotFoundError:
        return None


def fenced_release_attempt(
    db: Session, attempt_id: int, token: UUID | str, *,
    attempt_status: str, stop_reason: str | None,
) -> TaskAttemptRecord:
    """带 fencing 的尝试收尾: 租约 released/revoked + 尝试终态 + 槽释放(03 §4.1 ③)。

    租约不匹配(0 行)或尝试已终态抛 LeaseRejectedError。只 flush 不提交
    (事务由 attempt_cases 用例拥有)。
    """
    lease_status = "released" if attempt_status == "succeeded" else "revoked"
    n = fence_release_lease(db, attempt_id, token, status=lease_status)
    if n != 1:
        raise LeaseRejectedError(
            "租约失效, 尝试收尾被拒绝",
            params={"attempt_id": attempt_id},
        )
    attempt = get_attempt_record(db, attempt_id)
    if attempt is None or attempt.status in ("succeeded", "failed", "stopped"):
        raise LeaseRejectedError("尝试已终态, 不可重复收尾", params={"attempt_id": attempt_id})
    finished = finish_attempt_record(
        db, attempt_id, status=attempt_status, stop_reason=stop_reason
    )
    if finished is None:
        raise LeaseRejectedError("尝试已终态, 不可重复收尾", params={"attempt_id": attempt_id})
    release_slot(db, attempt_id)
    return finished


def write_diagnostic(
    db: Session,
    task_id: int,
    attempt_id: int | None,
    *,
    level: str,
    code: str,
    message: str,
    context: dict[str, Any] | None = None,
) -> TaskDiagnosticRecord:
    """写入任务诊断(不可变表, 只 INSERT; 只 flush 不提交)。"""
    return tasks_domain.append_diagnostic(
        db, task_id=task_id, attempt_id=attempt_id, level=level,
        code=code, message=message, context=context,
    )


def cancel_task_record(
    db: Session, task_id: int, *, outcome: str | None
) -> TaskRecord:
    """任务置 cancelled + 业务结局(刷新 updated_at; 只 flush 不提交)。"""
    return tasks_domain.set_task_status(db, task_id, "cancelled", business_outcome=outcome)


# ---------------------------------------------------------------------------
# 结果提交行写(证据包 + 四维评估 + 结果索引)
# ---------------------------------------------------------------------------


def create_evidence_record(
    db: Session,
    *,
    task_id: int,
    attempt_id: int,
    snapshot_id: int,
    object_id: int,
    created_by: int | None,
) -> int:
    """创建证据包行(不可变, 只 INSERT; 返回证据包 id, 只 flush 不提交)。"""
    return results_domain.create_evidence(
        db, task_id=task_id, calc_snapshot_id=snapshot_id, object_id=object_id,
        status="complete", attempt_id=attempt_id, created_by=created_by,
    ).id


def create_assessment_record(
    db: Session,
    *,
    evidence_package_id: int,
    dimensions: dict[str, str],
    overall_score: float | None,
    comment: str | None,
    detail: dict[str, Any] | None,
) -> int:
    """写入系统四维评估(assessor='system'; 返回评估 id, 只 flush 不提交)。"""
    return results_domain.create_system_assessment(
        db, evidence_package_id=evidence_package_id, dimensions=dimensions,
        overall_score=overall_score, detail=detail, comment=comment,
    ).id


def flip_result_index(db: Session, *, project_version_id: int) -> int:
    """结果索引翻转: 同版本旧 is_latest 行置 false(返回影响行数, 不提交)。"""
    return results_domain.flip_index_for_version(db, project_version_id)


def insert_result_index(
    db: Session,
    *,
    project_id: int,
    project_version_id: int,
    evidence_package_id: int,
    assessment_id: int,
) -> int:
    """插入新结果索引行(is_latest=true; 返回新行 id, 不提交事务)。"""
    return results_domain.insert_index(
        db,
        project_id=project_id,
        project_version_id=project_version_id,
        evidence_package_id=evidence_package_id,
        assessment_id=assessment_id,
    )


def point_result_assessment(
    db: Session, *, evidence_package_id: int, assessment_id: int
) -> int:
    """挂接最新评估引用(同证据包索引行 assessment_id 可 UPDATE; 返回行数)。"""
    return results_domain.point_index_assessment(
        db, evidence_package_id=evidence_package_id, assessment_id=assessment_id
    )
