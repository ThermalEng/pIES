"""对象存储管理 API(U11): /api/admin/storage、/api/admin/objects/cleanup、/api/admin/health。

对象写入/读取以服务层(services/objects.py)为主, 供其他业务单元内部调用;
本模块暴露管理接口(均仅管理员):

- GET  /api/admin/storage          存储视图(用量/对象数/引用数/健康) + 抽样校验
- POST /api/admin/objects/cleanup  两阶段清理: {dry_run: true} 先出计划,
                                   {dry_run: false} 再执行(RPD 23.3/23.4)
- GET  /api/admin/health           运维健康视图(存活/就绪/指标/队列/存储) +
                                   对象抽样完整性校验(存储健康)

集成说明: U16(iesplan.api.admin)历史版本也在 /api/admin 下实现了
/storage 与 /health 两个路径(认证与响应形状不同)。为避免路径遮蔽, 本模块
统一提供这两个端点: 认证仅接受窗口会话凭证(真实会话 + 全局 admin 角色),
响应为两版视图的并集(兼容既有调用方与测试)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from iesplan import __version__
from iesplan.api.auth import get_auth_context
from iesplan.core.errors import ForbiddenError
from iesplan.db import get_db
from iesplan.models.audit import StoredObject
from iesplan.models.calc import Task
from iesplan.models.identity import User
from iesplan.models.project import Project
from iesplan.services import identity, queue
from iesplan.services import objects as objects_service

#: 对象域管理路由: 挂载前缀 /api/admin(仅管理员)
router = APIRouter(prefix="/api/admin", tags=["admin-storage"])

DbSession = Annotated[Session, Depends(get_db)]


class CleanupRequest(BaseModel):
    """清理请求: dry_run=true 只返回计划, false 执行清理。"""

    dry_run: bool = True


def get_current_admin(request: Request, db: DbSession) -> User:
    """统一管理员认证依赖: 仅接受真实窗口会话 + 全局 admin 角色判定。

    - 会话无效/未认证 → 401(AuthRequiredError/SessionInvalidError), 不回退任何
      客户端声明的身份输入(已删除 X-User-Id 兼容认证, 防身份伪造, C-01);
    - 主体非管理员 → 403(ForbiddenError, ies.diag.perm.denied)。
    """
    ctx = get_auth_context(request, db)
    if not identity.has_role(db, ctx.user, "admin"):
        raise ForbiddenError(
            "需要管理员权限",
            params={"user_id": ctx.user.id},
            location={"object_type": "user", "object_id": ctx.user.id},
        )
    return ctx.user


#: 当前管理员依赖(须在 get_current_admin 定义之后声明)
CurrentAdmin = Annotated[User, Depends(get_current_admin)]


def _ops_health_view(db: Session) -> dict:
    """运维健康视图(RPD 13.3): 存活/就绪/任务指标/队列指标/存储容量。"""
    db_ok = True
    try:
        db.execute(select(1))
    except Exception:  # noqa: BLE001  (健康检查只上报状态, 不抛错)
        db_ok = False
    tasks_by_status = dict(
        db.execute(select(Task.status, func.count()).group_by(Task.status)).all()
    )
    projects_by_status = dict(
        db.execute(select(Project.status, func.count()).group_by(Project.status)).all()
    )
    users_count = db.execute(select(func.count(User.id))).scalar_one()
    objects_count = db.execute(select(func.count(StoredObject.id))).scalar_one()
    storage = objects_service.check_capacity(db)
    queue_view = queue.queue_status()
    # 健康判定: 存活 + 就绪 + 存储门禁; 队列为可重建视图(Redis 可重建),
    # 其降级状态在 queue 节单独上报, 不影响整体状态(RPD 13.3)
    healthy = db_ok and storage["ok"]
    return {
        "status": "ok" if healthy else "degraded",
        "service": "iesplan",
        "version": __version__,
        "time": datetime.now(UTC).isoformat(),
        "liveness": {"ok": True, "process": "alive"},
        "readiness": {"db": db_ok},
        "metrics": {
            "tasks_by_status": {str(k): int(v) for k, v in tasks_by_status.items()},
            "projects_by_status": {str(k): int(v) for k, v in projects_by_status.items()},
            "users": int(users_count),
            "objects": int(objects_count),
        },
        "queue": queue_view,
        "storage": storage,
    }


@router.get("/storage", summary="存储视图(管理员)")
def admin_storage(db: DbSession, _admin: CurrentAdmin) -> dict:
    """存储视图: 用量/对象数/引用数/健康 + 抽样校验(两版视图并集)。"""
    stats = objects_service.storage_stats(db)
    return {**stats, "stats": stats, "sample_verify": objects_service.sample_verify(db, limit=10)}


@router.post("/objects/cleanup", summary="对象清理: 先计划后执行(管理员)")
def admin_cleanup(
    req: CleanupRequest,
    db: DbSession,
    _admin: CurrentAdmin,
) -> dict:
    """对象清理(两阶段, 仅管理员)。

    第一次以 dry_run=true 调用获得清理计划(不删任何数据);
    确认后以 dry_run=false 调用执行: 删文件 + 删记录 + 审计。
    被引用(含项目版本/快照/证据包/报告引用)的对象不可清理, 不计入计划。
    """
    return objects_service.safe_cleanup(
        db, dry_run=req.dry_run, actor_id=_admin.id, actor_type="admin"
    )


@router.get("/health", summary="运维健康 + 存储抽样校验(管理员)")
def admin_health(db: DbSession, _admin: CurrentAdmin) -> dict:
    """运维健康视图(存活/就绪/指标/队列/存储) + 对象抽样完整性校验。"""
    view = _ops_health_view(db)
    verify = objects_service.sample_verify(db, limit=20)
    return {**view, **verify}
