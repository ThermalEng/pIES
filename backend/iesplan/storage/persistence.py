"""对象持久化(STO-05: StoredObject/ObjectRef repository, 模块内部)。

对象与引用 ORM 从 models/audit.py 迁出, 归属存储模块; 审计/导入提案/
保留规则不再与对象 ORM 混放。本模块只被 services/storage 内部使用,
业务模块不得导入(经 storage.__init__ 公开门面访问)。
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
from iesplan.models.common import HASH64_RE, bigint_pk, regex_check

#: 对象状态(01 §10.1 CHECK 枚举)
OBJ_STATUS_STORED = "stored"
OBJ_STATUS_ORPHANED = "orphaned"
OBJ_STATUS_PENDING_DELETION = "pending_deletion"
OBJ_STATUS_DELETED = "deleted"


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
    """对象引用清单(01 §10.2; STO-02: 引用清单为对象生命周期唯一权威)。

    ref_entity_id 为 Text: owner 标识是调用方声明的稳定字符串
    (整数主键或内容寻址 sha256 均可, STO-05), 存储不解析其语义。
    """

    __tablename__ = "object_refs"

    id: Mapped[int] = bigint_pk()
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"), nullable=False)
    ref_type: Mapped[str] = mapped_column(Text, nullable=False)
    ref_entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    ref_entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "object_id", "ref_type", "ref_entity_type", "ref_entity_id", name="uq_object_refs_ref"
        ),
        Index("idx_object_refs_entity", "ref_entity_type", "ref_entity_id"),
        Index("idx_object_refs_object", "object_id"),
    )
