"""对象存储管理用例。"""

from sqlalchemy.orm import Session

from iesplan import audit as audit_domain
from iesplan.storage import safe_cleanup, storage_stats


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
    return {**storage_stats(db), "cleanup_candidates": safe_cleanup(db, limit=100)["count"]}


def preview_cleanup(db: Session) -> dict:
    return safe_cleanup(db)


def execute_cleanup(db: Session, *, actor_id: int) -> dict:
    try:
        result = safe_cleanup(db, dry_run=False)
        for item in result["cleaned"]:
            _audit_entry(
                db, action="object_deleted", object_id=item["id"], actor_id=actor_id,
                before={"oid": item["oid"], "size_bytes": item["size_bytes"]},
            )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def get_storage_health(db: Session) -> dict:
    stats = storage_stats(db)
    return {
        "ok": stats["healthy"],
        "capacity": stats["capacity"],
        "orphan_count": stats["objects"]["orphan_count"],
        "object_count": stats["objects"]["count"],
    }
