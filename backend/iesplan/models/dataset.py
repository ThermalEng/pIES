"""数据集域(U05 数据集写入单元): datasets / dataset_versions / dataset_files。

领域归属与约束见 manual/developer-guide/zh-CN/modules/persistence.md 与 ARCHITECTURE_CONSTITUTION.md §11 数据库与持久化。
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
from iesplan.models.common import HASH64_RE, JSONB, bigint_pk, regex_check


class Dataset(Base):
    """数据集元数据(01 §5.1)。"""

    __tablename__ = "datasets"

    id: Mapped[int] = bigint_pk()
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    default_license: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','published','deprecated')", name="ck_datasets_status"
        ),
        # 表达式唯一索引: 共享数据集(project_id NULL)按 (name) 全局去重
        Index(
            "uq_datasets_name",
            sa.func.coalesce(sa.literal_column("project_id"), 0),
            "name",
            unique=True,
        ),
        Index("idx_datasets_project", "project_id"),
    )


class DatasetVersion(Base):
    """数据集版本(不可变, 追加式, 01 §5.2)。"""

    __tablename__ = "dataset_versions"

    id: Mapped[int] = bigint_pk()
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    timeline: Mapped[str] = mapped_column(Text, nullable=False)
    resolution: Mapped[str | None] = mapped_column(Text)
    fixed_utc_offset_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    fields: Mapped[dict] = mapped_column(JSONB, nullable=False)
    units: Mapped[dict] = mapped_column(JSONB, nullable=False)
    quality_report: Mapped[dict | None] = mapped_column(JSONB)
    provenance: Mapped[dict | None] = mapped_column(JSONB)
    license: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    created_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "timeline IN ('hourly','quarter_hourly','daily','monthly','yearly','custom')",
            name="ck_dataset_versions_timeline",
        ),
        CheckConstraint(
            "fixed_utc_offset_minutes BETWEEN -720 AND 840", name="ck_dataset_versions_utc_offset"
        ),
        regex_check(f"content_hash ~ '{HASH64_RE}'", name="ck_dataset_versions_content_hash"),
        UniqueConstraint("dataset_id", "version_no", name="uq_dataset_versions_version"),
        Index("idx_dataset_versions_dataset", "dataset_id", sa.text("version_no DESC")),
    )


class DatasetFile(Base):
    """数据集版本文件(指向内容寻址对象, 不可变, 01 §5.3)。"""

    __tablename__ = "dataset_files"

    id: Mapped[int] = bigint_pk()
    dataset_version_id: Mapped[int] = mapped_column(ForeignKey("dataset_versions.id"), nullable=False)
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"), nullable=False)
    file_kind: Mapped[str] = mapped_column(Text, nullable=False)
    format: Mapped[str] = mapped_column(Text, nullable=False)
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=sa.text("0"))
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=sa.text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "file_kind IN ('data','header','manifest','metadata')", name="ck_dataset_files_kind"
        ),
        CheckConstraint("format IN ('parquet','csv','json')", name="ck_dataset_files_format"),
        CheckConstraint("row_count >= 0", name="ck_dataset_files_row_count"),
        CheckConstraint("size_bytes >= 0", name="ck_dataset_files_size"),
        UniqueConstraint("dataset_version_id", "object_id", name="uq_dataset_files_object"),
        Index("idx_dataset_files_object", "object_id"),
    )
