"""不可变草稿历史与模板主记录指针（任务书 §四）。

每次持久化形成新 revision，不覆盖、不 detach 旧 revision。
模板主记录只保存当前草稿 revision 指针与最新发布 revision 指针。
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from iesplan.db import Base
from iesplan.models.common import HASH64_RE, bigint_pk, regex_check


class ModelTemplateDraftRevision(Base):
    """不可变草稿 revision（每次保存草稿新增一行，永不覆盖）。

    - entry_id：模板主表行
    - revision：严格递增（1 开始）
    - yaml_object_id：规范 YAML 对象（内容寻址）
    - canonical_sha256：规范字节 SHA-256
    - inputs_sha256：inputs 树摘要
    - source：创建来源（form/yaml_editor/upload/derived/migration）
    - created_by/created_at：创建者与时间
    - diagnostics_object_id：校验报告/诊断对象引用
    """

    __tablename__ = "model_template_draft_revisions"

    id: Mapped[int] = bigint_pk()
    entry_id: Mapped[int] = mapped_column(ForeignKey("model_templates.id"), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    yaml_object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"), nullable=False)
    canonical_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    inputs_sha256: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    diagnostics_object_id: Mapped[int | None] = mapped_column(ForeignKey("objects.id"))

    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_mtdr_revision"),
        regex_check(f"canonical_sha256 ~ '{HASH64_RE}'", name="ck_mtdr_canonical_sha256"),
        regex_check(
            f"inputs_sha256 IS NULL OR inputs_sha256 ~ '{HASH64_RE}'",
            name="ck_mtdr_inputs_sha256",
        ),
        CheckConstraint(
            "source IN ('form','yaml_editor','upload','derived','migration')",
            name="ck_mtdr_source",
        ),
        UniqueConstraint("entry_id", "revision", name="uq_mtdr_entry_revision"),
        Index("idx_mtdr_entry", "entry_id"),
    )


class TemplateMigrationReceipt(Base):
    """离线迁移回执（旧 ID → 新 ID 映射、摘要、回执）。"""

    __tablename__ = "template_migration_receipts"

    id: Mapped[int] = bigint_pk()
    old_template_id: Mapped[str] = mapped_column(Text, nullable=False)
    new_template_id: Mapped[str] = mapped_column(Text, nullable=False)
    entry_id: Mapped[int] = mapped_column(ForeignKey("model_templates.id"), nullable=False)
    old_content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    new_content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    migrated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    migrated_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    __table_args__ = (
        regex_check(f"old_content_sha256 ~ '{HASH64_RE}'", name="ck_tmr_old_sha256"),
        regex_check(f"new_content_sha256 ~ '{HASH64_RE}'", name="ck_tmr_new_sha256"),
        UniqueConstraint("old_template_id", name="uq_tmr_old_id"),
        UniqueConstraint("new_template_id", name="uq_tmr_new_id"),
    )
