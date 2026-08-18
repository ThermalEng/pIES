"""审计与对象域(U11 对象 / U12 审计 / U14 导入 / U15 保留策略写入单元)。

含: objects / object_refs / audit_log / import_proposals / retention_rules。
对应 01-db-schema.md 第10节。
"""

from __future__ import annotations

from datetime import datetime

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
)
from sqlalchemy.orm import Mapped, mapped_column

from iesplan.db import Base
from iesplan.models.common import HASH64_RE, JSONB, InetType, bigint_pk, regex_check


class StoredObject(Base):
    """内容寻址对象(元数据、引用计数、配额, 01 §10.1)。"""

    __tablename__ = "objects"

    id: Mapped[int] = bigint_pk()
    oid: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_path: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="stored")
    ref_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    quota_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=sa.text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    last_referenced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        regex_check(f"oid ~ '{HASH64_RE}'", name="ck_objects_oid"),
        regex_check(f"sha256 ~ '{HASH64_RE}'", name="ck_objects_sha256"),
        CheckConstraint("size_bytes >= 0", name="ck_objects_size"),
        CheckConstraint(
            "status IN ('stored','orphaned','pending_deletion','deleted')", name="ck_objects_status"
        ),
        CheckConstraint("ref_count >= 0", name="ck_objects_ref_count"),
        CheckConstraint("quota_bytes >= 0", name="ck_objects_quota"),
        UniqueConstraint("oid", name="uq_objects_oid"),
        UniqueConstraint("sha256", name="uq_objects_sha256"),
        Index("idx_objects_status", "status", "last_referenced_at"),
        Index("idx_objects_path", "storage_path"),
    )


class ObjectRef(Base):
    """对象引用清单(01 §10.2)。"""

    __tablename__ = "object_refs"

    id: Mapped[int] = bigint_pk()
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"), nullable=False)
    ref_type: Mapped[str] = mapped_column(Text, nullable=False)
    ref_entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    ref_entity_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    purpose: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "object_id", "ref_type", "ref_entity_type", "ref_entity_id", name="uq_object_refs_ref"
        ),
        Index("idx_object_refs_entity", "ref_entity_type", "ref_entity_id"),
    )


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


class ImportProposal(Base):
    """导入提议(外部数据入库前的评审记录, 01 §10.4)。"""

    __tablename__ = "import_proposals"

    id: Mapped[int] = bigint_pk()
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    proposer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_hash: Mapped[str] = mapped_column(Text, nullable=False)
    source_path: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="proposed")
    review_summary: Mapped[dict | None] = mapped_column(JSONB)
    review_errors: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "source_type IN ('excel','csv','json','dxf','gis','other')",
            name="ck_import_proposals_source_type",
        ),
        regex_check(f"source_hash ~ '{HASH64_RE}'", name="ck_import_proposals_source_hash"),
        CheckConstraint(
            "status IN ('proposed','validated','approved','rejected','applied')",
            name="ck_import_proposals_status",
        ),
        Index("idx_import_proposals_project", "project_id", "status"),
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
