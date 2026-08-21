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
from iesplan.storage import reconcile, safe_cleanup, sample_verify, storage_stats

#: 对象域管理路由: 挂载前缀 /api/admin(仅管理员)
router = APIRouter(prefix="/api/admin", tags=["admin-storage"])

DbSession = Annotated[Session, Depends(get_db)]
CurrentAdmin = Annotated[User, Depends(get_current_admin)]


class CleanupRequest(BaseModel):
    """清理请求: dry_run=true 只返回计划, false 执行清理。

    RR-P2-07: 执行必须携带 dry-run 返回的 plan_id; 候选集合与预览不一致时
    拒绝执行(计划过期), 要求重新预览。
    """

    dry_run: bool = True
    plan_id: str | None = None


@router.get("/storage", summary="存储视图(管理员, 单一 DTO)")
def admin_storage(db: DbSession, _admin: CurrentAdmin) -> dict:
    """存储视图(STO-07: 单一 StorageStatusDto, 无兼容并集)。

    字段: objects{count,total_bytes,by_status,orphan_count} /
          refs{count,referenced_objects} / capacity{free_bytes,safe_threshold,
          ok,message,reason?} / corrupt_count / cleanup_candidates / healthy。
    """
    stats = storage_stats(db)
    verify = sample_verify(db, limit=10)
    cleanup = safe_cleanup(db, dry_run=True, limit=100)
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

    第一次以 dry_run=true 调用获得清理计划(plan_id + 候选摘要, 不删数据);
    确认后携带 plan_id 以 dry_run=false 执行: 事务内重新验证引用与候选集合,
    候选变化则拒绝执行并要求重新预览。提交/回滚由本用例统一决定
    (RR-P1-03: 存储服务只 flush)。
    """
    if req.dry_run:
        return safe_cleanup(
            db, dry_run=True, actor_id=_admin.id, actor_type="admin"
        )
    if not req.plan_id:
        from iesplan.core.errors import ConflictError

        raise ConflictError(
            "执行清理必须携带 dry-run 返回的 plan_id",
            code="OBJ-CLEAN-001",
            message_key="ies.diag.obj.cleanup_plan_required",
        )
    result = safe_cleanup(
        db, dry_run=False, actor_id=_admin.id, actor_type="admin", expected_plan_id=req.plan_id
    )
    db.commit()  # 应用用例拥有事务边界(文件删除 + 记录删除同事务提交)
    return result


@router.get("/storage/health", summary="存储模块健康(管理员)")
def admin_storage_health(db: DbSession, _admin: CurrentAdmin) -> dict:
    """存储模块自有的健康结果(STO-07: 供运维聚合层调用, 不聚合其他模块)。

    字段: {ok, capacity, corrupt_count, orphan_count, reconcile}。
    """
    stats = storage_stats(db)
    verify = sample_verify(db, limit=10)
    return {
        "ok": stats["healthy"] and len(verify["failed"]) == 0,
        "capacity": stats["capacity"],
        "corrupt_count": len(verify["failed"]),
        "orphan_count": stats["objects"]["orphan_count"],
        "object_count": stats["objects"]["count"],
        "reconcile": reconcile(db, dry_run=True),
    }
