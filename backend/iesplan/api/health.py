"""运维健康聚合 API(STO-07: 独立聚合层, 不驻留在存储路由)。

- GET /api/admin/health: 全系统运维健康视图(存活/就绪/任务/队列/存储),
  由本聚合层调用各模块公开 health provider:
  - 存储模块 → /admin/storage/health 的 provider 逻辑(storage 公开函数);
  - 队列 → queue.queue_status();
  - 任务/项目/用户计数 → 只读统计。
- 存储路由不再实现健康聚合(边界: 存储模块只提供自己的健康结果)。

生产探针(/api/healthz, /api/readyz)由 main._build_health_router 提供,
本端点面向管理员运维界面。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from iesplan import __version__
from iesplan.api.auth import CurrentAdmin
from iesplan.db import get_db
from iesplan.models.calc import Task
from iesplan.models.identity import User
from iesplan.models.project import Project
from iesplan.services import queue
from iesplan.storage import sample_verify, storage_stats

#: 运维健康聚合路由: 挂载前缀 /api/admin(仅管理员)
router = APIRouter(prefix="/api/admin", tags=["admin-health"])

DbSession = Annotated[Session, Depends(get_db)]


def ops_health_view(db: Session) -> dict:
    """运维健康视图(RPD 13.3): 存活/就绪/任务指标/队列指标/存储容量。

    STO-07: 本聚合层只编排各模块公开 provider, 不实现任何模块内部逻辑;
    存储健康取 storage.health_view(容量 + 抽样校验), 队列取 queue_status。
    """
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
    storage = storage_health_view(db)
    queue_view = queue.queue_status()
    # 健康判定: 存活 + 就绪 + 存储门禁; 队列为可重建视图(Redis 可重建),
    # 其降级状态在 queue 节单独上报, 不影响整体状态(RPD 13.3)
    healthy = db_ok and storage["capacity"]["ok"]
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
        },
        "queue": queue_view,
        "storage": storage,
    }


def storage_health_view(db: Session) -> dict:
    """存储模块公开 health provider(单一形状, 供聚合层与其他调用方使用)。

    字段: {capacity, corrupt_count, orphan_count, object_count, ok,
    verify{checked, ok_count, failed}}。
    """
    stats = storage_stats(db)
    verify = sample_verify(db, limit=10)
    return {
        "capacity": stats["capacity"],
        "corrupt_count": len(verify["failed"]),
        "orphan_count": stats["objects"]["orphan_count"],
        "object_count": stats["objects"]["count"],
        "ok": stats["healthy"] and len(verify["failed"]) == 0,
        "verify": {
            "checked": verify["checked"],
            "ok_count": verify["ok_count"],
            "failed": verify["failed"],
        },
    }


@router.get("/health", summary="运维健康视图(管理员)")
def admin_health(db: DbSession, _admin: CurrentAdmin) -> dict:
    """运维健康(存活/就绪/指标/队列/存储)——独立聚合层, 非存储路由。"""
    return ops_health_view(db)
