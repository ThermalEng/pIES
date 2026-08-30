"""项目与权限域模型(U02 权限 / U03 项目写入单元)。

含: projects / drafts / project_versions / version_refs / admin_maintenance_actions。
领域归属与约束见 manual/developer-guide/zh-CN/modules/persistence.md 与 ARCHITECTURE_CONSTITUTION.md §11 数据库与持久化。0.8.0 起剔除共享成员/所有权转移: 项目权限以
projects.owner_id 为唯一权威, 不再有 project_members / ownership_transfers 表。

注意: projects.current_draft_id -> drafts 与 current_version_id -> project_versions
存在循环外键, 用 ``use_alter=True`` 让 SQLAlchemy 在建表后补 ALTER 语句,
与 manual/developer-guide/zh-CN/modules/persistence.md §修改 schema 保持一致（先建表、后补指针列）。
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from iesplan.db import Base
from iesplan.models.common import HASH64_RE, JSONB, bigint_pk, regex_check


class Project(Base):
    """项目主表(生命周期状态, 01 §3.1)。"""

    __tablename__ = "projects"

    id: Mapped[int] = bigint_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default="CNY")
    # 项目计算基线(0.6.5 事项 1): 创建时一次性固定, 创建后不可修改
    # (宪法 7.5: 计算序列不使用时间戳或时区, 统一从 0 开始的连续 step)。
    baseline_resolution: Mapped[str] = mapped_column(Text, nullable=False)
    baseline_leap_year: Mapped[bool] = mapped_column(Boolean, nullable=False)
    baseline_scenario_mode: Mapped[str] = mapped_column(Text, nullable=False)
    baseline_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("1"))
    # 循环依赖指针: 先建表, 后补外键(use_alter)
    current_draft_id: Mapped[int | None] = mapped_column(ForeignKey("drafts.id", use_alter=True))
    current_version_id: Mapped[int | None] = mapped_column(ForeignKey("project_versions.id", use_alter=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    __table_args__ = (
        CheckConstraint("status IN ('active','archived','deleted')", name="ck_projects_status"),
        CheckConstraint("currency IN ('CNY','USD')", name="ck_projects_currency"),
        CheckConstraint(
            "baseline_resolution IN ('15min','30min','1h')",
            name="ck_projects_baseline_resolution",
        ),
        CheckConstraint(
            "baseline_scenario_mode IN ('single')",
            name="ck_projects_baseline_scenario",
        ),
        regex_check(
            f"baseline_sha256 ~ '{HASH64_RE}'",
            name="ck_projects_baseline_sha256",
        ),
        UniqueConstraint("name", name="uq_projects_name"),
        Index("idx_projects_status", "status"),
        Index("idx_projects_owner", "owner_id"),
    )


class Draft(Base):
    """工作草稿(综合修订号, 可改, 01 §3.2)。"""

    __tablename__ = "drafts"

    id: Mapped[int] = bigint_pk()
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    parent_draft_id: Mapped[int | None] = mapped_column(ForeignKey("drafts.id"))
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa.text("false"))
    updated_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        regex_check(f"content_hash ~ '{HASH64_RE}'", name="ck_drafts_content_hash"),
        UniqueConstraint("project_id", "revision", name="uq_drafts_revision"),
        Index(
            "uq_drafts_current",
            "project_id",
            unique=True,
            postgresql_where=sa.text("is_current"),
            sqlite_where=sa.text("is_current"),
        ),
        Index("idx_drafts_project", "project_id", sa.text("revision DESC")),
    )


class ProjectVersion(Base):
    """项目版本(不可变, 追加式, 01 §3.3)。"""

    __tablename__ = "project_versions"

    id: Mapped[int] = bigint_pk()
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    parent_version_id: Mapped[int | None] = mapped_column(ForeignKey("project_versions.id"))
    source_draft_id: Mapped[int | None] = mapped_column(ForeignKey("drafts.id"))
    source_draft_revision: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # 项目计算基线(版本固化, 自包含; 创建后不可修改)
    baseline_resolution: Mapped[str] = mapped_column(Text, nullable=False)
    baseline_leap_year: Mapped[bool] = mapped_column(Boolean, nullable=False)
    baseline_scenario_mode: Mapped[str] = mapped_column(Text, nullable=False)
    baseline_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str | None] = mapped_column(Text)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "baseline_resolution IN ('15min','30min','1h')",
            name="ck_project_versions_baseline_resolution",
        ),
        CheckConstraint(
            "baseline_scenario_mode IN ('single')",
            name="ck_project_versions_baseline_scenario",
        ),
        regex_check(
            f"baseline_sha256 ~ '{HASH64_RE}'",
            name="ck_project_versions_baseline_sha256",
        ),
        CheckConstraint("currency IS NULL OR currency IN ('CNY','USD')", name="ck_project_versions_currency"),
        regex_check(f"content_hash ~ '{HASH64_RE}'", name="ck_project_versions_content_hash"),
        UniqueConstraint("project_id", "version_no", name="uq_project_versions_version"),
        Index("idx_project_versions_parent", "parent_version_id"),
        Index("idx_project_versions_project", "project_id", sa.text("version_no DESC")),
    )


class VersionRef(Base):
    """版本引用清单(不可变, 版本自包含, 01 §3.4)。"""

    __tablename__ = "version_refs"

    id: Mapped[int] = bigint_pk()
    project_version_id: Mapped[int] = mapped_column(ForeignKey("project_versions.id"), nullable=False)
    ref_type: Mapped[str] = mapped_column(Text, nullable=False)
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"), nullable=False)
    ref_key: Mapped[str | None] = mapped_column(Text)
    ref_hash: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "ref_type IN ('dataset_version','system_graph','calc_config','calc_snapshot',"
            "'evidence_package','report','object')",
            name="ck_version_refs_type",
        ),
        regex_check("ref_hash IS NULL OR ref_hash ~ '^[0-9a-f]{64}$'", name="ck_version_refs_hash"),
        UniqueConstraint("project_version_id", "ref_type", "object_id", name="uq_version_refs_ref"),
        Index("idx_version_refs_object", "object_id"),
    )


class AdminMaintenanceAction(Base):
    """管理员维护操作审计(不可变, 01 §2.3)。"""

    __tablename__ = "admin_maintenance_actions"

    id: Mapped[int] = bigint_pk()
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    performed_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    params: Mapped[dict | None] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint(
            "action_type IN ('backup','restore','purge','reindex','config_change',"
            "'object_quota_change','retention_change','user_override')",
            name="ck_admin_actions_type",
        ),
        CheckConstraint(
            "status IN ('pending','running','succeeded','failed')", name="ck_admin_actions_status"
        ),
        Index("idx_admin_actions_time", sa.text("started_at DESC")),
        Index("idx_admin_actions_by", "performed_by"),
    )
