"""结果域(U09 结果 / U13 报告写入单元)。

含: evidence_packages / result_assessments / result_index / result_selections /
reports。领域归属与约束见 manual/developer-guide/zh-CN/modules/persistence.md 与 ARCHITECTURE_CONSTITUTION.md §11 数据库与持久化。
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column

from iesplan.db import Base
from iesplan.models.common import HASH64_RE, JSONB, bigint_pk, regex_check

#: 评估维度取值(01 §8.2)
DIMENSION_VALUES: tuple[str, ...] = ("pass", "fail", "unknown")


class EvidencePackage(Base):
    """证据包(不可变, 01 §8.1)。"""

    __tablename__ = "evidence_packages"

    id: Mapped[int] = bigint_pk()
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    attempt_id: Mapped[int | None] = mapped_column(ForeignKey("task_attempts.id"))
    calc_snapshot_id: Mapped[int] = mapped_column(ForeignKey("calc_snapshots.id"), nullable=False)
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"), nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('complete','partial','invalid')", name="ck_evidence_packages_status"
        ),
        regex_check(f"content_hash ~ '{HASH64_RE}'", name="ck_evidence_packages_content_hash"),
        Index("idx_evidence_packages_task", "task_id"),
        Index("idx_evidence_packages_snapshot", "calc_snapshot_id"),
    )


class ResultAssessment(Base):
    """结果评估(四维, 不可变, 01 §8.2)。"""

    __tablename__ = "result_assessments"

    id: Mapped[int] = bigint_pk()
    evidence_package_id: Mapped[int] = mapped_column(
        ForeignKey("evidence_packages.id"), nullable=False
    )
    assessor: Mapped[str] = mapped_column(Text, nullable=False)
    assessed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    dimension_physical: Mapped[str] = mapped_column(Text, nullable=False)
    dimension_optimality: Mapped[str] = mapped_column(Text, nullable=False)
    dimension_financial: Mapped[str] = mapped_column(Text, nullable=False)
    dimension_reliability: Mapped[str] = mapped_column(Text, nullable=False)
    overall_score: Mapped[float | None] = mapped_column(Numeric(5, 2))
    comment: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint("assessor IN ('system','human')", name="ck_result_assessments_assessor"),
        CheckConstraint(
            f"dimension_physical IN {DIMENSION_VALUES!r}", name="ck_result_assessments_physical"
        ),
        CheckConstraint(
            f"dimension_optimality IN {DIMENSION_VALUES!r}", name="ck_result_assessments_optimality"
        ),
        CheckConstraint(
            f"dimension_financial IN {DIMENSION_VALUES!r}", name="ck_result_assessments_financial"
        ),
        CheckConstraint(
            f"dimension_reliability IN {DIMENSION_VALUES!r}", name="ck_result_assessments_reliability"
        ),
        CheckConstraint(
            "overall_score IS NULL OR overall_score BETWEEN 0 AND 100",
            name="ck_result_assessments_score",
        ),
        Index(
            "idx_result_assessments_evidence",
            "evidence_package_id",
            sa.text("created_at DESC"),
        ),
    )


class ResultIndex(Base):
    """结果索引(仅最新评估引用, 01 §8.3)。"""

    __tablename__ = "result_index"

    id: Mapped[int] = bigint_pk()
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    project_version_id: Mapped[int] = mapped_column(ForeignKey("project_versions.id"), nullable=False)
    evidence_package_id: Mapped[int] = mapped_column(ForeignKey("evidence_packages.id"), nullable=False)
    assessment_id: Mapped[int | None] = mapped_column(ForeignKey("result_assessments.id"))
    result_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_latest: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa.text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        regex_check(f"result_hash ~ '{HASH64_RE}'", name="ck_result_index_result_hash"),
        Index(
            "uq_result_index_latest",
            "project_version_id",
            unique=True,
            postgresql_where=sa.text("is_latest"),
            sqlite_where=sa.text("is_latest"),
        ),
        Index("idx_result_index_project", "project_id", sa.text("project_version_id DESC")),
    )


class ResultSelection(Base):
    """结果选中(业务索引, 追加式, 01 §8.4)。"""

    __tablename__ = "result_selections"

    id: Mapped[int] = bigint_pk()
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    result_index_id: Mapped[int] = mapped_column(ForeignKey("result_index.id"), nullable=False)
    selected_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    selected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    reason: Mapped[str | None] = mapped_column(Text)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa.text("true"))

    __table_args__ = (
        Index(
            "uq_result_selections_current",
            "project_id",
            unique=True,
            postgresql_where=sa.text("is_current"),
            sqlite_where=sa.text("is_current"),
        ),
        Index("idx_result_selections_project", "project_id", sa.text("selected_at DESC")),
    )


class Report(Base):
    """报告(Excel 等报告对象引用, 01 §8.5)。"""

    __tablename__ = "reports"

    id: Mapped[int] = bigint_pk()
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    report_type: Mapped[str] = mapped_column(Text, nullable=False)
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"), nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    generated_by_task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))
    generated_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("report_type IN ('excel','pdf','html')", name="ck_reports_type"),
        regex_check(f"content_hash ~ '{HASH64_RE}'", name="ck_reports_content_hash"),
        CheckConstraint("status IN ('generating','ready','failed')", name="ck_reports_status"),
        Index("idx_reports_project", "project_id", sa.text("generated_at DESC")),
    )
