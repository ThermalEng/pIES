"""项目与权限域模型(U02 权限 / U03 项目写入单元)。

含: projects / drafts / project_versions / version_refs / project_members /
ownership_transfers / admin_maintenance_actions。对应 01-db-schema.md 第2、3节。

注意: projects.current_draft_id -> drafts 与 current_version_id -> project_versions
存在循环外键, 用 ``use_alter=True`` 让 SQLAlchemy 在建表后补 ALTER 语句,
与 01 §11 迁移顺序第5步"先建表、后补指针列"一致。
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
    fixed_utc_offset_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa.text("480")
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("1"))
    # 管理员访问授权(所有者控制): false = 管理员不可查看项目细节/转移所有权(仅整体管理);
    # true = 管理员可查看细节并转移所有权。默认 false。
    admin_access: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa.text("false"))
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
        CheckConstraint("fixed_utc_offset_minutes BETWEEN -720 AND 840", name="ck_projects_utc_offset"),
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
    fixed_utc_offset_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str | None] = mapped_column(Text)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "fixed_utc_offset_minutes BETWEEN -720 AND 840", name="ck_project_versions_utc_offset"
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


class ProjectMember(Base):
    """项目成员(owner/viewer, 追加式授权, 01 §2.1)。"""

    __tablename__ = "project_members"

    id: Mapped[int] = bigint_pk()
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    granted_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("role IN ('owner','viewer')", name="ck_project_members_role"),
        UniqueConstraint("project_id", "user_id", "granted_at", name="uq_project_members_grant"),
        Index(
            "uq_project_members_current",
            "project_id",
            "user_id",
            unique=True,
            postgresql_where=sa.text("revoked_at IS NULL"),
            sqlite_where=sa.text("revoked_at IS NULL"),
        ),
        Index("idx_project_members_user", "user_id"),
        Index("idx_project_members_project", "project_id", "role"),
    )


class OwnershipTransfer(Base):
    """项目所有权转移审计(追加式, 01 §2.2)。"""

    __tablename__ = "ownership_transfers"

    id: Mapped[int] = bigint_pk()
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    from_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    to_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    transfer_version: Mapped[int] = mapped_column(Integer, nullable=False)
    proposed_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    proposed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed','accepted','completed','cancelled','rejected')",
            name="ck_ownership_transfers_status",
        ),
        CheckConstraint("from_user_id <> to_user_id", name="ck_ownership_transfers_distinct"),
        Index(
            "uq_ownership_transfers_open",
            "project_id",
            unique=True,
            postgresql_where=sa.text("status IN ('proposed','accepted')"),
            sqlite_where=sa.text("status IN ('proposed','accepted')"),
        ),
        Index("idx_ownership_transfers_to", "to_user_id", "status"),
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
