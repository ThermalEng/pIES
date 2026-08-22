"""对象存储管理 API(STO-07: /api/admin/storage 单一 DTO + 存储自有健康)。

- GET  /api/admin/storage              存储视图: 单一 StorageStatusDto
  (对象用量/引用数/容量/损坏/待回收数量; 不再返回两版响应并集);
- POST /api/admin/objects/cleanup      两阶段清理: {dry_run: true} 先出计划,
                                       {dry_run: false} 再执行(RPD 23.3/23.4);
 0.2.0-B3: 执行改为软删/保留期(标记待物理回收, 不立即删文件);
- GET  /api/admin/objects/pending      "已删除待回收"清单(待物理回收对象);
- POST /api/admin/objects/restore      恢复保留期内的待回收对象(误清理恢复);
- POST /api/admin/objects/purge        物理回收已过保留期的待回收对象
                                       ({dry_run: true} 先出清单再执行);
- GET  /api/admin/storage/health       存储模块自有的健康结果(容量/损坏/孤儿)。

全系统健康聚合(/health)由独立运维聚合层调用各模块公开 health provider;
本路由不查询 Task/Project/User, 不调用队列(STO-07 边界)。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from iesplan.api.auth import get_current_admin
from iesplan.db import get_db
from iesplan.models.identity import User
from iesplan.storage import (
    list_pending_deleted,
    purge_expired,
    reconcile,
    safe_cleanup,
    sample_verify,
    storage_stats,
    undelete_object,
)

#: 对象域管理路由: 挂载前缀 /api/admin(仅管理员)
router = APIRouter(prefix="/api/admin", tags=["admin-storage"])

DbSession = Annotated[Session, Depends(get_db)]
CurrentAdmin = Annotated[User, Depends(get_current_admin)]


class CleanupRequest(BaseModel):
    """清理请求: dry_run=true 只返回计划, false 执行清理。

    RR-P2-07: 执行必须携带 dry-run 返回的 plan_id; 候选集合与预览不一致时
    拒绝执行(计划过期), 要求重新预览。
    0.2.0-B3: pending_delete_days 为软删保留期天数(默认 7)。保留期内对象
    标记为待物理回收(文件仍保留、可恢复), 过期后由 purge/reconcile 物理回收。
    预览与执行必须保持一致 —— 保留期已固化进 plan_id, 执行时传不同天数会
    判定计划过期并要求重新预览。
    """

    dry_run: bool = True
    plan_id: str | None = None
    # ge=0: 负保留期会使 pending_delete_until 早于 pending_deleted_at, 触发
    # ck_objects_pending_deletion_dates CHECK 约束冲突导致未捕获 500。
    pending_delete_days: int | None = Field(default=None, ge=0)


class RestoreRequest(BaseModel):
    """恢复误清理对象: 只接受数字对象 ID(待回收对象的对象主键)。

    oid(内容寻址字符串)与业务 sha256 混淆时 404 更利于定位, 故仅限数字 ID;
    恢复目标必须是仍在保留期内的 pending_deletion 对象, 已物理回收的不可恢复。
    """

    object_id: int


class PurgeRequest(BaseModel):
    """物理回收请求: dry_run=true 只列出可回收对象, false 执行删除。

    只处理已过保留期的待回收对象; 保留期内对象绝不物理删除(0.2.0-B3)。
    """

    dry_run: bool = True


@router.get("/storage", summary="存储视图(管理员, 单一 DTO)")
def admin_storage(db: DbSession, _admin: CurrentAdmin) -> dict:
    """存储视图(STO-07: 单一 StorageStatusDto, 无兼容并集)。

    字段: objects{count,total_bytes,by_status,orphan_count,pending_deletion_count} /
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
        "pending_deletion_count": stats["objects"]["pending_deletion_count"],
        "healthy": stats["healthy"] and len(verify["failed"]) == 0,
    }


@router.post("/objects/cleanup", summary="对象清理: 先计划后执行(管理员, 软删/保留期)")
def admin_cleanup(
    req: CleanupRequest,
    db: DbSession,
    _admin: CurrentAdmin,
) -> dict:
    """对象清理(两阶段, 仅管理员, 0.2.0-B3 软删/保留期)。

    第一次以 dry_run=true 调用获得清理计划(plan_id + 候选摘要, 不删数据);
    确认后携带 plan_id 以 dry_run=false 执行: 事务内重新验证引用与候选集合,
    候选变化则拒绝执行并要求重新预览。执行把候选对象标记为待物理回收
    (默认保留 7 天), 保留期内可经 restore / 重新 attach 恢复; 到期由
    purge(或 reconcile 巡检)物理删文件 + 删记录。提交/回滚由本用例统一决定
    (RR-P1-03: 存储服务只 flush)。
    """
    pending_delete_days = req.pending_delete_days
    if req.dry_run:
        return safe_cleanup(
            db, dry_run=True, actor_id=_admin.id, actor_type="admin",
            pending_delete_days=pending_delete_days if pending_delete_days is not None else 7,
        )
    if not req.plan_id:
        from iesplan.core.errors import ConflictError

        raise ConflictError(
            "执行清理必须携带 dry-run 返回的 plan_id",
            code="OBJ-CLEAN-001",
            message_key="ies.diag.obj.cleanup_plan_required",
        )
    result = safe_cleanup(
        db, dry_run=False, actor_id=_admin.id, actor_type="admin",
        expected_plan_id=req.plan_id,
        pending_delete_days=pending_delete_days if pending_delete_days is not None else 7,
    )
    db.commit()  # 应用用例拥有事务边界(软删标记 + 引用变更同事务提交)
    return result


@router.get("/objects/pending", summary="已删除待回收对象清单(管理员)")
def admin_pending_objects(
    db: DbSession,
    _admin: CurrentAdmin,
    expired_only: bool = False,
) -> dict:
    """列出"已删除待回收"对象(0.2.0-B3 软删/保留期)。

    管理员可查看将被物理回收的对象及其保留截止时间; expired_only=true 时
    只列出已过保留期、可由 purge 物理回收的对象。
    """
    return {
        "data": list_pending_deleted(db, expired_only=expired_only),
        "meta": {"expired_only": expired_only},
    }


@router.post("/objects/restore", summary="恢复误清理对象(管理员, 保留期内)")
def admin_restore_object(
    req: RestoreRequest,
    db: DbSession,
    _admin: CurrentAdmin,
) -> dict:
    """恢复误清理对象(0.2.0-B3 恢复路径, 仅管理员)。

    只允许恢复仍在保留期内的待回收对象; 恢复后对象回到可用状态
    (有引用 restored / 无引用 orphaned), 文件保留在磁盘上立即可访问。
    """
    result = undelete_object(db, req.object_id, actor_id=_admin.id, actor_type="admin")
    db.commit()
    return {"data": result, "meta": {}}


@router.post("/objects/purge", summary="物理回收已过保留期的待回收对象(管理员)")
def admin_purge_objects(
    req: PurgeRequest,
    db: DbSession,
    _admin: CurrentAdmin,
) -> dict:
    """物理回收已过保留期的待回收对象(0.2.0-B3 延迟物理删除)。

    dry_run=true 先出清单(不删); false 执行物理删除文件 + 删除记录。
    只处理已过保留期的 pending_deletion 对象, 保留期内对象绝不物理删除。
    """
    result = purge_expired(db, dry_run=req.dry_run, actor_id=_admin.id, actor_type="admin")
    db.commit()
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
        "pending_deletion_count": stats["objects"]["pending_deletion_count"],
        "reconcile": reconcile(db, dry_run=True),
    }
