"""对象管理用例实现(application/objects/service.py, Wave 4)。

``iesplan.api.objects`` 路由层编排的整体下沉: 存储调用原样委托 storage
公开门面, 事务边界由本层写用例拥有(``db.commit`` 收尾, 失败
``db.rollback``; 存储服务只 flush, RR-P1-03)。读用例不提交事务。

本层不新增校验/hash/完整性复核/防御分支: 请求字段约束保留在 API 请求
模型, 计划标识比对与保留期语义保留在 storage 服务内。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from iesplan import audit as audit_domain
from iesplan.core.errors import ConflictError
from iesplan.storage import (
    DEFAULT_PENDING_DELETE_DAYS,
    list_pending_deleted,
    purge_expired,
    reconcile,
    safe_cleanup,
    sample_verify,
    storage_stats,
    undelete_object,
)

#: 软删保留期默认天数唯一所有者为 storage 域公开门面
#: (iesplan.storage.DEFAULT_PENDING_DELETE_DAYS); 本用例只复用,
#: 不再本地复述字面量, 避免两处缺省漂移。


def _audit_entry(
    db: Session,
    *,
    action: str,
    object_id: int,
    actor_id: int | None,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    """对象管理审计(成功路径, 与业务写入同事务, 只 INSERT)。"""
    audit_domain.append_entry(
        db,
        actor_id=actor_id,
        action=action,
        entity_type="objects",
        entity_id=object_id,
        actor_type="admin",
        before=before,
        extra=after,
    )


def get_storage_view(db: Session) -> dict:
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


def preview_cleanup(
    db: Session,
    *,
    actor_id: int,
    pending_delete_days: int | None = None,
) -> dict:
    """清理预览: 只返回清理计划(plan_id + 候选摘要), 不删数据(不提交事务)。"""
    days = pending_delete_days if pending_delete_days is not None else DEFAULT_PENDING_DELETE_DAYS
    return safe_cleanup(
        db,
        dry_run=True,
        actor_id=actor_id,
        actor_type="admin",
        pending_delete_days=days,
    )


def execute_cleanup(
    db: Session,
    *,
    actor_id: int,
    pending_delete_days: int | None = None,
    plan_id: str | None = None,
) -> dict:
    """清理执行: 携带 dry-run 返回的 plan_id 执行软删标记(事务型提交)。

    事务内重新验证引用与候选集合, 候选变化则拒绝执行并要求重新预览。
    执行把候选对象标记为待物理回收(默认保留 7 天), 保留期内可经 restore /
    重新 attach 恢复; 到期由 purge(或 reconcile 巡检)物理删文件 + 删记录。
    """
    if not plan_id:
        raise ConflictError(
            "执行清理必须携带 dry-run 返回的 plan_id",
            code="OBJ-CLEAN-001",
            message_key="ies.diag.obj.cleanup_plan_required",
        )
    try:
        days = pending_delete_days if pending_delete_days is not None else DEFAULT_PENDING_DELETE_DAYS
        result = safe_cleanup(
            db,
            dry_run=False,
            actor_id=actor_id,
            actor_type="admin",
            expected_plan_id=plan_id,
            pending_delete_days=days,
        )
        for item in result.get("marked", []):
            _audit_entry(
                db,
                action="object_marked_pending_deletion",
                object_id=item["id"],
                actor_id=actor_id,
                before={"oid": item["oid"], "size_bytes": item["size_bytes"]},
                after={"status": "pending_deletion"},
            )
        db.commit()  # 应用用例拥有事务边界(软删标记 + 审计同事务提交)
        return result
    except Exception:
        db.rollback()
        raise


def list_pending(db: Session, *, expired_only: bool = False) -> list[dict]:
    """列出"已删除待回收"对象(0.2.0-B3 软删/保留期, 不提交事务)。

    expired_only=true 时只列出已过保留期、可由 purge 物理回收的对象。
    """
    return list_pending_deleted(db, expired_only=expired_only)


def restore_object(db: Session, *, object_id: int, actor_id: int) -> dict:
    """恢复误清理对象(0.2.0-B3 恢复路径, 事务型提交)。

    只允许恢复仍在保留期内的待回收对象; 恢复后对象回到可用状态
    (有引用 restored / 无引用 orphaned), 文件保留在磁盘上立即可访问。
    """
    try:
        result = undelete_object(db, object_id, actor_id=actor_id, actor_type="admin")
        _audit_entry(
            db,
            action="object_restored",
            object_id=result["id"],
            actor_id=actor_id,
            before={"status": "pending_deletion"},
            after={"status": result["status"], "reason": "undelete"},
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def purge(db: Session, *, dry_run: bool = True, actor_id: int) -> dict:
    """物理回收已过保留期的待回收对象(0.2.0-B3 延迟物理删除, 事务型提交)。

    dry_run=true 先出清单(不删); false 执行物理删除文件 + 删除记录。
    只处理已过保留期的 pending_deletion 对象, 保留期内对象绝不物理删除。
    """
    try:
        result = purge_expired(db, dry_run=dry_run, actor_id=actor_id, actor_type="admin")
        if not dry_run:
            for item in result.get("purged", []):
                _audit_entry(
                    db,
                    action="object_purged",
                    object_id=item["id"],
                    actor_id=actor_id,
                    before={
                        "oid": item["oid"],
                        "size_bytes": item["size_bytes"],
                        "status": "pending_deletion",
                    },
                )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def get_storage_health(db: Session) -> dict:
    """存储模块自有的健康结果(STO-07: 供运维聚合层调用, 不聚合其他模块, 不提交事务)。

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
