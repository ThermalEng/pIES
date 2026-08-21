"""对象存储管理 API(STO-07: /api/admin/storage 单一 DTO + 存储自有健康)。

- GET  /api/admin/storage          存储视图: 单一 StorageStatusDto
  (对象用量/引用数/容量/损坏/待清理数量; 不再返回两版响应并集);
- POST /api/admin/objects/cleanup  两阶段清理: {dry_run: true} 先出计划,
                                   {dry_run: false} 再执行(RPD 23.3/23.4);
- GET  /api/admin/storage/health   存储模块自有的健康结果(容量/损坏/孤儿)。

全系统健康聚合(/health)由独立运维聚合层调用各模块公开 health provider;
本路由不查询 Task/Project/User, 不调用队列(STO-07 边界)。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from iesplan.api.auth import get_current_admin
from iesplan.db import get_db
from iesplan.models.identity import User
from iesplan.services import objects as objects_service

#: 对象域管理路由: 挂载前缀 /api/admin(仅管理员)
router = APIRouter(prefix="/api/admin", tags=["admin-storage"])

DbSession = Annotated[Session, Depends(get_db)]
CurrentAdmin = Annotated[User, Depends(get_current_admin)]


class CleanupRequest(BaseModel):
    """清理请求: dry_run=true 只返回计划, false 执行清理。"""

    dry_run: bool = True


@router.get("/storage", summary="存储视图(管理员, 单一 DTO)")
def admin_storage(db: DbSession, _admin: CurrentAdmin) -> dict:
    """存储视图(STO-07: 单一 StorageStatusDto, 无兼容并集)。

    字段: objects{count,total_bytes,by_status,orphan_count} /
          refs{count,referenced_objects} / capacity{free_bytes,safe_threshold,
          ok,message,reason?} / corrupt_count / cleanup_candidates / healthy。
    """
    stats = objects_service.storage_stats(db)
    verify = objects_service.sample_verify(db, limit=10)
    cleanup = objects_service.safe_cleanup(db, dry_run=True, limit=100)
    return {
        "objects": stats["objects"],
        "refs": stats["refs"],
        "capacity": stats["capacity"],
        "corrupt_count": len(verify["failed"]),
        "cleanup_candidates": cleanup["count"],
        "healthy": stats["healthy"] and len(verify["failed"]) == 0,
    }


@router.post("/objects/cleanup", summary="对象清理: 先计划后执行(管理员)")
def admin_cleanup(
    req: CleanupRequest,
    db: DbSession,
    _admin: CurrentAdmin,
) -> dict:
    """对象清理(两阶段, 仅管理员)。

    第一次以 dry_run=true 调用获得清理计划(不删任何数据);
    确认后以 dry_run=false 调用执行: 删文件 + 删记录 + 审计。
    被任意 owner 引用(STO-02: 引用清单为权威)的对象不可清理, 不计入计划。
    """
    return objects_service.safe_cleanup(
        db, dry_run=req.dry_run, actor_id=_admin.id, actor_type="admin"
    )


@router.get("/storage/health", summary="存储模块健康(管理员)")
def admin_storage_health(db: DbSession, _admin: CurrentAdmin) -> dict:
    """存储模块自有的健康结果(STO-07: 供运维聚合层调用, 不聚合其他模块)。

    字段: {ok, capacity, corrupt_count, orphan_count, reconcile}。
    """
    stats = objects_service.storage_stats(db)
    verify = objects_service.sample_verify(db, limit=10)
    return {
        "ok": stats["healthy"] and len(verify["failed"]) == 0,
        "capacity": stats["capacity"],
        "corrupt_count": len(verify["failed"]),
        "orphan_count": stats["objects"]["orphan_count"],
        "object_count": stats["objects"]["count"],
        "reconcile": objects_service.reconcile(db, dry_run=True),
    }
