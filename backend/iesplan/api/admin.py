"""管理维护 API 路由(U16, prefix /api/admin)。

认证说明: 统一使用 U01 身份单元提供的窗口会话认证(iesplan.api.auth.CurrentAdmin:
窗口凭证校验 + 全局 admin 角色判定, 未认证 401, 非管理员 403)。
管理员经维护入口只读诊断与解锁, 不得直接编辑业务(宪法 §16 安全与审计 + domain-model §身份权限审计)。

路由清单:
- GET  /admin/audit             审计查询(过滤 + 游标分页, domain-model §身份权限审计 + contracts §HTTP语义)
- GET  /admin/diagnostics       运维诊断视图(任务/队列/存储/保留策略/维护记录)
- POST /admin/unlock-task       管理员解锁任务(卡死任务回收 → queued)

集成说明: /admin/storage 与 /admin/health 由 U11(iesplan.api.objects)统一提供
(双认证兼容 + 两版视图并集), 本模块不再重复定义, 避免路径遮蔽。

全部维护操作写不可变审计(audit_log, actor_type='admin')与
admin_maintenance_actions(domain-model §快照任务结果/对象生命周期 + modules/persistence.md)。

W5-admin 收尾: 本层不再直接导入 ORM 与组织 DB 写, 诊断取数与解锁
事务(含提交)全部经 application.tasks.maintenance 用例, 审计查询
经 application.audits 门面。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from iesplan.api.auth import CurrentAdmin
from iesplan.application.audits import query_audit
from iesplan.application.tasks.maintenance import (
    get_diagnostics,
    get_task,
    unlock_task,
)
from iesplan.core.errors import ConflictError, NotFoundError
from iesplan.db import get_db

router = APIRouter(prefix="/api/admin", tags=["admin"])


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
    """审计查询(domain-model §身份权限审计 + contracts §HTTP语义): 过滤 + 游标分页, 按时间倒序。"""
    return query_audit(
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
    return get_diagnostics(db)


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
    """管理员解锁卡死任务(宪法 §16 + domain-model §身份权限审计 维护入口)。

    适用范围: running/cancelling 状态且租约已失活的任务; 执行: 吊销租约、
    释放并发槽、终止当前尝试, 任务回到 queued 重新排队。全程审计
    (audit_log + admin_maintenance_actions)。
    """
    # 存在性检查优先于 confirm: 不存在的任务应返回 404 而非 409(避免泄露)
    task = get_task(db, payload.task_id)
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

    return unlock_task(db, task_id=task.id, admin_id=admin.id)
