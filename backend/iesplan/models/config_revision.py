"""财务三件套 revision 域模型(0.6.5 条目 1-2;替换旧单体 FinanceConfig 持久化)。

FinanceProfile(地区注册表) / FinanceOverrides(项目覆盖) /
EffectiveFinanceConfig(项目有效快照):

- ``finance_profiles``: 地区 Profile 注册表。Profile 是已注册、可复用的地区财务基准(宪法 4.6): 每次登记写入新的 YAML 对象与
  注册行; 项目经 Overrides 的 profile_ref{id} 引用。
- ``finance_overrides``: 项目 FinanceOverrides 不可变 revision 追加表
  (每次保存形成新 revision, 不覆盖历史); 引用 Profile id。
- ``effective_finance_revisions``: 合并器确定性产物不可变 revision 追加表。

projects 当前生效指针:
- ``finance_profile_id``: 项目当前引用的已注册 Profile 主键;
- ``overrides_revision``: 项目当前 Overrides revision(无覆盖时指针为空);
- ``effective_finance_revision``: 项目当前 Effective revision(装配/规划/
  财务计算唯一消费的快照)。

不可变性由 ``models.immutable_triggers.IMMUTABLE_TABLES`` 注册的禁
UPDATE/DELETE 触发器保证(Postgres), 应用层同样无任何更新入口。

领域归属与约束见 ARCHITECTURE_CONSTITUTION.md §4.6/§11 与
manual/developer-guide/zh-CN/formats/finance-yaml.md。
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from iesplan.db import Base
from iesplan.models.common import JSONB, bigint_pk

class FinanceProfile(Base):
    """地区 FinanceProfile 注册表(已注册、可复用的地区财务基准)。

    每次登记写入新行; content 存内容 JSON(FinanceProfile.to_dict 形态);
    object_id 指向 YAML 对象。文本文件只校验字头，不做内容摘要。
    """

    __tablename__ = "finance_profiles"

    id: Mapped[int] = bigint_pk()
    profile_id: Mapped[str] = mapped_column(Text, nullable=False)
    region: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"), nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        UniqueConstraint("profile_id", name="uq_finance_profiles_id"),
        Index("idx_finance_profiles_id", "profile_id"),
    )

class FinanceOverridesRevision(Base):
    """项目 FinanceOverrides 不可变 revision(仅 INSERT, 追加式)。"""

    __tablename__ = "finance_overrides"

    id: Mapped[int] = bigint_pk()
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    profile_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_finance_overrides_revision"),
        UniqueConstraint("project_id", "revision", name="uq_finance_overrides_revision"),
        Index("idx_finance_overrides_project", "project_id", sa.text("revision DESC")),
    )

class EffectiveFinanceRevision(Base):
    """项目 EffectiveFinanceConfig 不可变 revision(仅 INSERT, 合并器产物)。

    装配/规划/财务计算只消费当前 Effective revision 指向的快照。
    """

    __tablename__ = "effective_finance_revisions"

    id: Mapped[int] = bigint_pk()
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    profile_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_effective_finance_revisions_revision"),
        UniqueConstraint(
            "project_id", "revision", name="uq_effective_finance_revisions_revision"
        ),
        Index(
            "idx_effective_finance_revisions_project",
            "project_id", sa.text("revision DESC"),
        ),
    )

class PlanningConfigRevision(Base):
    """规划配置不可变 revision(仅 INSERT, 追加式)。

    规划与结果财务计算必须消费同一有效快照(宪法 4.6)。
    """

    __tablename__ = "planning_configs"

    id: Mapped[int] = bigint_pk()
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_planning_configs_revision"),
        UniqueConstraint("project_id", "revision", name="uq_planning_configs_revision"),
        Index("idx_planning_configs_project", "project_id", sa.text("revision DESC")),
    )
