"""不确定性域(U10 不确定性写入单元): uncertainty_snapshots / sample_tasks / sample_records。

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
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from iesplan.db import Base
from iesplan.models.calc import TASK_STATUSES
from iesplan.models.common import HASH64_RE, JSONB, bigint_pk, regex_check


class UncertaintySnapshot(Base):
    """不确定性快照(不可变, 01 §9.1)。"""

    __tablename__ = "uncertainty_snapshots"

    id: Mapped[int] = bigint_pk()
    calc_snapshot_id: Mapped[int] = mapped_column(ForeignKey("calc_snapshots.id"), nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    n_samples: Mapped[int] = mapped_column(Integer, nullable=False)
    random_seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    distributions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "method IN ('monte_carlo','lhs','scenario','robust')", name="ck_uncertainty_method"
        ),
        CheckConstraint("n_samples BETWEEN 1 AND 1000000", name="ck_uncertainty_n_samples"),
        regex_check(f"content_hash ~ '{HASH64_RE}'", name="ck_uncertainty_content_hash"),
        Index("idx_uncertainty_snapshots_calc", "calc_snapshot_id"),
    )


class SampleTask(Base):
    """采样任务(父子树形分解, 01 §9.2)。"""

    __tablename__ = "sample_tasks"

    id: Mapped[int] = bigint_pk()
    uncertainty_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("uncertainty_snapshots.id"), nullable=False
    )
    parent_task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"))
    parent_sample_id: Mapped[int | None] = mapped_column(ForeignKey("sample_tasks.id"))
    sample_index: Mapped[int] = mapped_column(Integer, nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("0"))
    params: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint("depth BETWEEN 0 AND 10", name="ck_sample_tasks_depth"),
        CheckConstraint(f"status IN {TASK_STATUSES!r}", name="ck_sample_tasks_status"),
        Index(
            "uq_sample_tasks_top",
            "uncertainty_snapshot_id",
            "sample_index",
            unique=True,
            postgresql_where=sa.text("parent_sample_id IS NULL"),
            sqlite_where=sa.text("parent_sample_id IS NULL"),
        ),
        Index(
            "uq_sample_tasks_child",
            "parent_sample_id",
            "sample_index",
            unique=True,
            postgresql_where=sa.text("parent_sample_id IS NOT NULL"),
            sqlite_where=sa.text("parent_sample_id IS NOT NULL"),
        ),
        Index("idx_sample_tasks_snapshot", "uncertainty_snapshot_id", "status"),
        Index("idx_sample_tasks_parent", "parent_sample_id"),
    )


class SampleRecord(Base):
    """样本记录(01 §9.3)。"""

    __tablename__ = "sample_records"

    id: Mapped[int] = bigint_pk()
    sample_task_id: Mapped[int] = mapped_column(ForeignKey("sample_tasks.id"), nullable=False)
    variable_name: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    unit: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("sample_task_id", "variable_name", name="uq_sample_records_variable"),
        Index("idx_sample_records_task", "sample_task_id"),
    )
