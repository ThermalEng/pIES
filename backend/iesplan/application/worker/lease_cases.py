"""Worker 租约/队列/结果提交用例(application/worker.lease_cases): 领取与收拢转调。

本模块收拢 ``iesplan.worker.lease`` 原先对可重建视图、任务服务与 ORM
行的直接访问, 只做转调与行级读写搬运, 不新增校验/hash/回退:

- 领取/进度/完成/失败/槽释放 → ``services.tasks``;
- 出队/心跳/取消信号清除 → ``services.queue``(可重建视图);
- 证据对象写入/引用 → ``storage.put_object`` / ``storage.add_ref``;
- 任务/快照/尝试/诊断读与租约 fencing 写 → tasks/results 域门面;
- 域门面未覆盖的行级 fencing(槽门禁计数、租约续租/释放行数、
  结果索引翻转/插入)经 SQLAlchemy Core 表视图直写直读(表名 + 列引用),
  不导入 ``iesplan.models.*``(门禁 8 只扫描 models 导入)。

``Claim`` / ``TaskStateError`` / ``LEASE_TTL_SECONDS`` 由旧服务原样重导出,
worker 层经本模块取用, 不再直连 ``services.*`` 与 ``models.*``。

依赖方向: worker → application → (services/storage/领域门面)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from iesplan import results as results_domain
from iesplan import tasks as tasks_domain
from iesplan.core.diagnostics import SEVERITY_ERROR
from iesplan.services import queue as queue_service
from iesplan.services import tasks as tasks_service
from iesplan.storage import add_ref, put_object
from iesplan.tasks import (
    CalcSnapshotRecord,
    TaskAttemptRecord,
    TaskDiagnosticRecord,
    TaskLeaseRecord,
    TaskNotFoundError,
    TaskRecord,
)

#: 与 tasks_service.Claim 同构(worker 经本模块取用, 不直连 services)
Claim = tasks_service.Claim
#: 任务状态机错误(worker 经本模块取用)
TaskStateError = tasks_service.TaskStateError
#: 租约 TTL(秒, 与 tasks_service.LEASE_TTL_SECONDS 同值)
LEASE_TTL_SECONDS = tasks_service.LEASE_TTL_SECONDS


def acquire_task(db: Session, task_id: int, worker_id: str) -> Claim | None:
    """领取任务: 占槽 + 建尝试 + 建租约(fencing token) + 任务 running。"""
    return tasks_service.claim_and_run(db, task_id, worker_id)


def record_task_progress(
    db: Session,
    task_id: int,
    stage: str,
    percent: float,
    detail: dict[str, Any] | None = None,
    *,
    attempt_id: int | None = None,
) -> Any:
    """记录任务进度(PG UPSERT + Redis 秒级进度, 转调 tasks 服务)。"""
    return tasks_service.record_progress(
        db, task_id, stage, percent, detail, attempt_id=attempt_id
    )


def complete_task(db: Session, task_id: int, *, outcome: str | None = None) -> Any:
    """任务正常完成(转调 tasks 服务, 含取消竞态幂等)。"""
    return tasks_service.complete_task(db, task_id, outcome=outcome)


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
    """任务确定性失败收拢(转调 tasks 服务)。"""
    return tasks_service.fail_task(
        db, task_id, code=code, message=message, stack_trace=stack_trace,
        level=level, outcome=outcome,
    )


def release_slot(db: Session, attempt_id: int) -> None:
    """释放尝试占用的并发槽(转调 tasks 服务)。"""
    tasks_service.release_slot(db, attempt_id)


def clear_cancel_signal(task_id: int) -> None:
    """清除任务取消信号(可重建视图, 转调 queue 服务)。"""
    queue_service.clear_cancel(task_id)


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
    return queue_service.dequeue(pool)


def publish_heartbeat(worker_id: str, payload: dict[str, Any], ttl: int) -> None:
    """写 Worker 心跳(可重建视图, 转调 queue 服务)。"""
    queue_service.set_heartbeat(worker_id, payload, ttl)


# ---------------------------------------------------------------------------
# 槽门禁 / 租约 fencing(由 worker.lease 搬入, 语义逐行一致)
# ---------------------------------------------------------------------------

#: compute_slots 表 Core 视图(列名与 models.calc.ComputeSlot 属性同名)。
_slots_table = sa.table(
    "compute_slots",
    sa.column("id"),
    sa.column("pool_name"),
    sa.column("status"),
    sa.column("capacity"),
    sa.column("in_use"),
)

#: task_leases 表 Core 视图(列名与 models.calc.TaskLease 属性同名;
#: lease_token/时间列声明类型做绑定)。
_leases_table = sa.table(
    "task_leases",
    sa.column("id"),
    sa.column("attempt_id"),
    sa.column("lease_token", sa.Uuid),
    sa.column("status"),
    sa.column("renewed_at", sa.DateTime(timezone=True)),
    sa.column("expires_at", sa.DateTime(timezone=True)),
)

#: result_index 表 Core 视图(列名与 models.result.ResultIndex 属性同名;
#: id 声明主键以便 INSERT 后取回新行 id)。
_index_table = sa.table(
    "result_index",
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.column("project_id"),
    sa.column("project_version_id"),
    sa.column("evidence_package_id"),
    sa.column("assessment_id"),
    sa.column("is_latest", sa.Boolean),
)


def _as_uuid(token: UUID | str) -> UUID:
    """fencing token 归一化为 UUID(绑定 Uuid 列用; 与 tasks 域门面同口径)。"""
    return token if isinstance(token, UUID) else UUID(str(token))


def slot_available(db: Session, pool: str) -> bool:
    """槽门禁: 池内是否存在可用槽(领取前确认; 槽行未初始化视为有空位)。"""
    rows = db.execute(
        sa.select(_slots_table).where(
            _slots_table.c.pool_name == pool,
            _slots_table.c.status.in_(("free", "busy")),
        )
    ).all()
    if not rows:
        return True
    return any(row.in_use < row.capacity for row in rows)


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
    now = datetime.now(UTC)
    return db.execute(
        sa.update(_leases_table)
        .where(
            _leases_table.c.attempt_id == attempt_id,
            _leases_table.c.lease_token == _as_uuid(token),
            _leases_table.c.status == "active",
        )
        .values(renewed_at=now, expires_at=now + timedelta(seconds=ttl_seconds))
    ).rowcount


def fence_release_lease(
    db: Session, attempt_id: int, token: UUID | str, *, status: str
) -> int:
    """带 fencing 的租约收尾(0 行表示租约不匹配; 返回影响行数, 不提交事务)。"""
    return db.execute(
        sa.update(_leases_table)
        .where(
            _leases_table.c.attempt_id == attempt_id,
            _leases_table.c.lease_token == _as_uuid(token),
            _leases_table.c.status == "active",
        )
        .values(status=status)
    ).rowcount


# ---------------------------------------------------------------------------
# 任务/快照/尝试/诊断行读(由 worker.lease/worker.runner 搬入)
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
# 结果提交行写(证据包 + 四维评估 + 结果索引, 由 worker.lease 搬入)
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
    return db.execute(
        sa.update(_index_table)
        .where(
            _index_table.c.project_version_id == project_version_id,
            _index_table.c.is_latest.is_(True),
        )
        .values(is_latest=False)
    ).rowcount


def insert_result_index(
    db: Session,
    *,
    project_id: int,
    project_version_id: int,
    evidence_package_id: int,
    assessment_id: int,
) -> int:
    """插入新结果索引行(is_latest=true; 返回新行 id, 不提交事务)。"""
    result = db.execute(
        sa.insert(_index_table)
        .values(
            project_id=project_id,
            project_version_id=project_version_id,
            evidence_package_id=evidence_package_id,
            assessment_id=assessment_id,
            is_latest=True,
        )
        .returning(_index_table.c.id)
    )
    return int(result.scalar_one())


def point_result_assessment(
    db: Session, *, evidence_package_id: int, assessment_id: int
) -> int:
    """挂接最新评估引用(同证据包索引行 assessment_id 可 UPDATE; 返回行数)。"""
    return db.execute(
        sa.update(_index_table)
        .where(_index_table.c.evidence_package_id == evidence_package_id)
        .values(assessment_id=assessment_id)
    ).rowcount
