"""管理端审计用例(application/audits/service.py, W3-A)。

组合 ``iesplan.audit`` 域公开门面现有能力的薄封装(纠偏 Wave 1 切片 C,
旧 services.audit 已删除, 唯一实现归 audit 域)。只做参数透传与审计行
装配，不新增校验/hash/完整性复核/防御分支。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from iesplan import audit as audit_domain
from iesplan.audit.contracts import AuditRecord


def query_audit(
    db: Session,
    *,
    entity_type: str | None = None,
    entity_id: int | None = None,
    action: str | None = None,
    actor_id: int | None = None,
    actor_type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    cursor: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """审计查询(过滤 + 游标分页，按时间倒序；只读，不提交事务)。"""
    return audit_domain.query_audit(
        db,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        actor_id=actor_id,
        actor_type=actor_type,
        since=since,
        until=until,
        cursor=cursor,
        limit=limit,
    )


def record_unlock_audit(
    db: Session,
    *,
    admin_id: int,
    task_id: int,
    task_type: str,
) -> AuditRecord:
    """记录管理员解锁任务审计(只 INSERT，不提交；加入调用方事务)。"""
    return audit_domain.audit(
        db,
        admin_id,
        audit_domain.AUDIT_MAINTENANCE_UNLOCK_TASK,
        "task",
        task_id,
        actor_type="admin",
        result={"from": "running/cancelling", "to": "queued", "task_type": task_type},
    )


__all__ = [
    "query_audit",
    "record_unlock_audit",
]
