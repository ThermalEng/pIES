"""审计域 repository SQL 实现（audit_log/retention_rules，归属 audit）。

实现规则：
- 只追加写入与过滤查询；不提供修改/删除；
- 查询、写入、flush 由本模块完成；绝不 commit/rollback。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from iesplan.audit.contracts import AuditRecord, RetentionRuleRecord
from iesplan.core.jsonutil import jsonable
from iesplan.db import (
    Base,
    JSONB,
    InetType,
    bigint_pk,
    drop_trigger_function_sql,
    immutable_revoke_sql,
    immutable_trigger_sql,
)


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


def _row_to_retention_rule(row: RetentionRule) -> RetentionRuleRecord:
    return RetentionRuleRecord(
        id=row.id,
        entity_type=row.entity_type,
        object_kind=row.object_kind,
        retention_days=row.retention_days,
        apply_to=row.apply_to,
    )


def list_active_retention_rules(db: Session) -> list[RetentionRuleRecord]:
    """列出全部 active 保留规则（id 升序；运维诊断消费，只读）。"""
    rows = (
        db.execute(
            select(RetentionRule).where(RetentionRule.status == "active").order_by(RetentionRule.id)
        )
        .scalars()
        .all()
    )
    return [_row_to_retention_rule(row) for row in rows]


# ---------------------------------------------------------------------------
# ORM 表定义: Wave2A 由 iesplan.models.audit(AuditLog/RetentionRule) 迁入, 表真相归本域所有。
# ---------------------------------------------------------------------------

class AuditLog(Base):
    """通用审计日志(不可变, 01 §10.3)。"""

    __tablename__ = "audit_log"

    id: Mapped[int] = bigint_pk()
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    ip: Mapped[str | None] = mapped_column(InetType)
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    request_id: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("actor_type IN ('user','system','admin')", name="ck_audit_log_actor_type"),
        Index("idx_audit_log_entity", "entity_type", "entity_id", sa.text("occurred_at DESC")),
        Index("idx_audit_log_time", "occurred_at"),
        Index("idx_audit_log_actor", "actor_id", sa.text("occurred_at DESC")),
    )


class RetentionRule(Base):
    """保留策略(01 §10.5)。"""

    __tablename__ = "retention_rules"

    id: Mapped[int] = bigint_pk()
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    object_kind: Mapped[str] = mapped_column(Text, nullable=False)
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False)
    apply_to: Mapped[str] = mapped_column(Text, nullable=False, server_default="all")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("retention_days BETWEEN 1 AND 36500", name="ck_retention_rules_days"),
        CheckConstraint("apply_to IN ('all','orphaned','referenced')", name="ck_retention_rules_apply"),
        CheckConstraint("status IN ('active','paused')", name="ck_retention_rules_status"),
        UniqueConstraint("entity_type", "object_kind", "apply_to", name="uq_retention_rules_key"),
    )


#: 本域拥有的不可变表(仅 INSERT, 禁止 UPDATE/DELETE)
IMMUTABLE_TABLES: tuple[str, ...] = ("audit_log",)


def install_triggers() -> tuple[str, ...]:
    """公开钩子: 返回本域触发器部署语句(按执行序, 含幂等 DROP, 供组合根编排收集)。"""
    statements = [drop_trigger_function_sql(f"tg_{table}_immutable") for table in IMMUTABLE_TABLES]
    statements.extend(immutable_trigger_sql(table) for table in IMMUTABLE_TABLES)
    statements.extend(immutable_revoke_sql(table) for table in IMMUTABLE_TABLES)
    return tuple(statements)
