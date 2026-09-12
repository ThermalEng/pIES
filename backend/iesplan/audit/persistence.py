"""审计域 repository SQL 实现（audit_log，归属 audit）。

实现规则：
- 只追加写入与过滤查询；不提供修改/删除；
- 查询、写入、flush 由本模块完成；绝不 commit/rollback。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from iesplan.audit.contracts import AuditRecord
from iesplan.core.jsonutil import jsonable
from iesplan.models.audit import AuditLog


def _iso(value: datetime | None) -> str | None:
    """ORM 时间 → 记录字符串（原样 isoformat，不增减时区后缀）。"""
    return value.isoformat() if value is not None else None


def _row_to_audit(row: AuditLog) -> AuditRecord:
    return AuditRecord(
        id=row.id,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        action=row.action,
        actor_id=row.actor_id,
        actor_type=row.actor_type,
        occurred_at=_iso(row.occurred_at),
        ip=str(row.ip) if row.ip is not None else None,
        before=dict(row.before) if isinstance(row.before, dict) else None,
        after=dict(row.after) if isinstance(row.after, dict) else None,
        request_id=row.request_id,
        trace_id=row.trace_id,
    )


def append_entry(
    db: Session,
    *,
    actor_id: int | None,
    action: str,
    entity_type: str,
    entity_id: int,
    revision: int | None = None,
    result: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    before: dict[str, Any] | None = None,
    actor_type: str = "user",
    ip: str | None = None,
    request_id: str | None = None,
    trace_id: str | None = None,
) -> AuditRecord:
    """追加不可变审计行；after 由 revision/result/extra 组装（宪法 §16 最小化脱敏）。"""
    after: dict[str, Any] = {}
    if revision is not None:
        after["revision"] = revision
    if result is not None:
        after["result"] = result
    if extra:
        after.update(extra)
    row = AuditLog(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        actor_id=actor_id,
        actor_type=actor_type,
        ip=ip,
        before=jsonable(before) if before else None,
        after=jsonable(after) or None,
        request_id=request_id,
        trace_id=trace_id,
    )
    db.add(row)
    db.flush()
    return _row_to_audit(row)


def list_entries(
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
) -> list[AuditRecord]:
    """过滤 + 游标分页（id 倒序）；调用方传 limit+1 探测下一页。"""
    stmt = select(AuditLog)
    if entity_type is not None:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    if actor_id is not None:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if actor_type is not None:
        stmt = stmt.where(AuditLog.actor_type == actor_type)
    if since is not None:
        stmt = stmt.where(AuditLog.occurred_at >= since)
    if until is not None:
        stmt = stmt.where(AuditLog.occurred_at <= until)
    if cursor is not None:
        stmt = stmt.where(AuditLog.id < cursor)
    rows = db.execute(stmt.order_by(AuditLog.id.desc()).limit(limit)).scalars().all()
    return [_row_to_audit(row) for row in rows]
