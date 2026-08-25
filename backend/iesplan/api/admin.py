"""管理维护 API 路由(U16, prefix /api/admin)。

认证说明: 统一使用 U01 身份单元提供的窗口会话认证(iesplan.api.auth.CurrentAdmin:
窗口凭证校验 + 全局 admin 角色判定, 未认证 401, 非管理员 403)。
管理员经维护入口只读诊断与解锁, 不得直接编辑业务(RPD 3.2)。

路由清单:
- GET  /admin/audit             审计查询(过滤 + 游标分页, RPD 13.2)
- GET  /admin/diagnostics       运维诊断视图(任务/队列/存储/保留策略/维护记录)
- POST /admin/unlock-task       管理员解锁任务(卡死任务回收 → queued)

集成说明: /admin/storage 与 /admin/health 由 U11(iesplan.api.objects)统一提供
(双认证兼容 + 两版视图并集), 本模块不再重复定义, 避免路径遮蔽。

全部维护操作写不可变审计(audit_log, actor_type='admin')与
admin_maintenance_actions(01 §2.3)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from iesplan.api.auth import CurrentAdmin
from iesplan.core.diagnostics import SEVERITY_INFO
from iesplan.core.errors import ConflictError, NotFoundError
from iesplan.db import get_db
from iesplan.models.audit import RetentionRule
from iesplan.models.calc import ComputeSlot, Task, TaskAttempt, TaskDiagnostic, TaskLease
from iesplan.models.identity import User
from iesplan.models.project import AdminMaintenanceAction
from iesplan.services import audit as audit_service
from iesplan.services import queue
from iesplan.services import tasks as tasks_service
from iesplan.storage import storage_stats

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _record_maintenance(
    db: Session,
    admin: User,
    action_type: str,
    *,
    status: str = "succeeded",
    params: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
) -> AdminMaintenanceAction:
    """记录管理员维护操作(01 §2.3, 不可变追加式)。"""
    row = AdminMaintenanceAction(
        action_type=action_type,
        performed_by=admin.id,
        status=status,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        params=params,
        result=result,
    )
    db.add(row)
    db.flush()
    return row


# ---------------------------------------------------------------------------
# 路由: 审计查询 / 诊断视图 / 存储 / 健康
# ---------------------------------------------------------------------------


@router.get("/audit", summary="审计查询(管理员)")
def query_audit_endpoint(
    db: Annotated[Session, Depends(get_db)],
    admin: CurrentAdmin,
    entity_type: str | None = Query(default=None, description="对象类型过滤"),
    entity_id: int | None = Query(default=None, description="对象标识过滤"),
    action: str | None = Query(default=None, description="审计动作过滤"),
    actor_id: int | None = Query(default=None, description="操作者过滤"),
    actor_type: str | None = Query(default=None, description="操作者类型过滤"),
    since: Annotated[datetime | None, Query(description="开始时间(UTC)")] = None,
    until: Annotated[datetime | None, Query(description="结束时间(UTC)")] = None,
    cursor: int | None = Query(default=None, description="游标(上一页末条 id)"),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    """审计查询(RPD 13.2): 过滤 + 游标分页, 按时间倒序。"""
    return audit_service.query_audit(
        db, entity_type=entity_type, entity_id=entity_id, action=action,
        actor_id=actor_id, actor_type=actor_type, since=since, until=until,
        cursor=cursor, limit=limit,
    )


@router.get("/diagnostics", summary="运维诊断视图(管理员)")
def diagnostics_endpoint(
    db: Annotated[Session, Depends(get_db)],
    admin: CurrentAdmin,
) -> dict:
    """运维诊断视图: 任务/队列/存储/保留策略/维护记录/最近失败任务。"""
    tasks_by_status = dict(
        db.execute(select(Task.status, func.count()).group_by(Task.status)).all()
    )
    tasks_by_type = dict(
        db.execute(select(Task.type, func.count()).group_by(Task.type)).all()
    )
    recent_failed = db.execute(
        select(Task)
        .where(Task.status == "failed")
        .order_by(Task.updated_at.desc())
        .limit(5)
    ).scalars().all()
    rules = db.execute(
        select(RetentionRule).where(RetentionRule.status == "active").order_by(RetentionRule.id)
    ).scalars().all()
    actions = db.execute(
        select(AdminMaintenanceAction).order_by(AdminMaintenanceAction.id.desc()).limit(10)
    ).scalars().all()
    storage = storage_stats(db)
    queue_view = queue.queue_status()
    return {
        "tasks": {
            "by_status": {str(k): int(v) for k, v in tasks_by_status.items()},
            "by_type": {str(k): int(v) for k, v in tasks_by_type.items()},
            "recent_failed": [
                {"id": t.id, "type": t.type, "business_outcome": t.business_outcome,
                 "updated_at": t.updated_at}
                for t in recent_failed
            ],
        },
        "queue": queue_view,
        "storage": storage,
        "retention_rules": [
            {"id": r.id, "entity_type": r.entity_type, "object_kind": r.object_kind,
             "retention_days": r.retention_days, "apply_to": r.apply_to}
            for r in rules
        ],
        "maintenance_actions": [
            {"id": a.id, "action_type": a.action_type, "status": a.status,
             "performed_by": a.performed_by, "started_at": a.started_at,
             "params": a.params, "result": a.result}
            for a in actions
        ],
        # 队列为可重建视图(PG 为权威事实), 降级只提示不影响健康判定
        "healthy": storage["healthy"] and not queue_view["degraded"],
    }


# ---------------------------------------------------------------------------
# 路由: 管理员维护操作(解锁)
# ---------------------------------------------------------------------------


class UnlockTaskRequest(BaseModel):
    """管理员解锁任务请求体。

    confirm: 危险操作二次确认(布尔)。解锁会把 running 任务推回 queued,
    可能造成 Worker 竞态/重复计算, 因此须显式确认才执行(0.2.0 B2)。
    """

    task_id: int
    confirm: bool = False


@router.post("/unlock-task", summary="管理员解锁任务")
def unlock_task_endpoint(
    payload: UnlockTaskRequest,
    db: Annotated[Session, Depends(get_db)],
    admin: CurrentAdmin,
) -> dict:
    """管理员解锁卡死任务(RPD 3.2 维护入口)。

    适用范围: running/cancelling 状态且租约已失活的任务; 执行: 吊销租约、
    释放并发槽、终止当前尝试, 任务回到 queued 重新排队。全程审计
    (audit_log + admin_maintenance_actions)。
    """
    # 存在性检查优先于 confirm: 不存在的任务应返回 404 而非 409(避免泄露)
    task = db.get(Task, payload.task_id)
    if task is None:
        raise NotFoundError(
            "任务不存在", params={"task_id": payload.task_id},
            location={"object_type": "task", "object_id": payload.task_id},
        )
    if not payload.confirm:
        raise ConflictError(
            "解锁为危险维护操作, 须携带 confirm=true 确认后执行",
            code="ADMIN-CONFIRM-REQUIRED",
            message_key="ies.diag.admin.confirm_required",
            params={"hint": "解锁会把 running 任务推回 queued, 确认后重新提交"},
            location={"object_type": "task", "object_id": payload.task_id},
        )
    if task.status in ("completed", "cancelled", "timed_out", "failed"):
        raise ConflictError(
            "终态任务无需解锁(可手动重试)",
            params={"task_id": task.id, "status": task.status},
            location={"object_type": "task", "object_id": task.id},
        )
    if task.status == "queued":
        return {"task_id": task.id, "unlocked": False, "status": "queued", "message": "任务已在排队"}

    # 吊销租约 + 终止运行尝试 + 释放并发槽(running/cancelling → queued)
    now = datetime.now(UTC)
    attempts = db.execute(
        select(TaskAttempt).where(TaskAttempt.task_id == task.id)
    ).scalars().all()
    attempt_ids = [a.id for a in attempts] or [0]
    leases = db.execute(
        select(TaskLease).where(
            TaskLease.attempt_id.in_(attempt_ids), TaskLease.status == "active"
        )
    ).scalars().all()
    for lease in leases:
        lease.status = "revoked"
    for attempt in attempts:
        if attempt.status == "running":
            attempt.status = "stopped"
            attempt.stop_reason = "admin_unlock"
            attempt.finished_at = now
    slots = db.execute(
        select(ComputeSlot).where(ComputeSlot.current_attempt_id.in_(attempt_ids))
    ).scalars().all()
    for slot in slots:
        slot.in_use = max(slot.in_use - 1, 0)
        slot.current_attempt_id = None

    task.status = "queued"
    task.business_outcome = None
    task.updated_at = now
    db.flush()
    db.add(
        TaskDiagnostic(
            task_id=task.id, level=SEVERITY_INFO, code="TASK-ADMIN-001",
            message="管理员解锁, 任务重新排队",
            context={"unlocked_by": admin.id},
        )
    )
    queue.clear_cancel(task.id)
    queue.enqueue(
        task.id, tasks_service.POOL_BY_TYPE.get(task.type, "compute"),
        task_type=task.type, snapshot_id=task.calc_snapshot_id,
    )
    _record_maintenance(
        db, admin, "user_override",
        params={"task_id": task.id, "task_type": task.type, "from": "running/cancelling"},
        result={"to": "queued"},
    )
    audit_service.audit(
        db, admin.id, audit_service.AUDIT_MAINTENANCE_UNLOCK_TASK, "task", task.id,
        actor_type="admin",
        result={"from": "running/cancelling", "to": "queued", "task_type": task.type},
    )
    db.commit()
    return {"task_id": task.id, "unlocked": True, "status": "queued"}
