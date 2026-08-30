"""规划/财务配置 revision 域模型(0.6.5 事项 3)。

finance_configs / planning_configs: 仅 INSERT 的不可变 revision 追加表
(每次保存形成新的 revision, 不覆盖历史); 不可变性由
``models.immutable_triggers.IMMUTABLE_TABLES`` 注册的禁 UPDATE/DELETE
触发器保证(Postgres), 应用层同样无任何更新入口。

领域归属与约束见 ARCHITECTURE_CONSTITUTION.md §4.6/§11。
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from iesplan.db import Base
from iesplan.models.common import HASH64_RE, JSONB, bigint_pk, regex_check


class FinanceConfigRevision(Base):
    """公共财务配置不可变 revision(仅 INSERT, 追加式)。"""

    __tablename__ = "finance_configs"

    id: Mapped[int] = bigint_pk()
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: 规范内容 JSON(FinanceConfig.to_dict, 不含 revision 字段本身)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_finance_configs_revision"),
        regex_check(
            f"content_sha256 ~ '{HASH64_RE}'",
            name="ck_finance_configs_sha256",
        ),
        UniqueConstraint("project_id", "revision", name="uq_finance_configs_revision"),
        Index("idx_finance_configs_project", "project_id", sa.text("revision DESC")),
    )


class PlanningConfigRevision(Base):
    """规划配置不可变 revision(仅 INSERT, 追加式)。

    finance_revision 列固定本规划配置引用的 FinanceConfig revision
    (规划与结果财务计算必须消费同一 revision, 宪法 4.6)。
    """

    __tablename__ = "planning_configs"

    id: Mapped[int] = bigint_pk()
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    finance_revision: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_planning_configs_revision"),
        regex_check(
            f"content_sha256 ~ '{HASH64_RE}'",
            name="ck_planning_configs_sha256",
        ),
        regex_check(
            f"finance_revision ~ '{HASH64_RE}'",
            name="ck_planning_configs_finance_revision",
        ),
        UniqueConstraint("project_id", "revision", name="uq_planning_configs_revision"),
        Index("idx_planning_configs_project", "project_id", sa.text("revision DESC")),
    )
