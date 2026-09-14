"""项目域 SQL repository 实现（projects/drafts/project_versions/version_refs）。

- 本模块是 `ProjectRepository` 协议的唯一实现，可经 `iesplan.project` 门面调用；
- 表真相收归本模块（Wave2A 由 iesplan.models.project 迁入）；绝不 commit/rollback（调用方事务拥有）；
- 唯一冲突转 `ProjectConflictError`；冲突后调用方须回滚会话（与旧 services
  契约一致；实测 SA 2.0 下 flush 失败后会话不可继续，savepoint 保不住可用性）；
- 时间以 `datetime.isoformat()` 原样映射为记录字符串（与 FastAPI 既有
  datetime 序列化输出一致，API 行为不变）；
- `get_project` 把“不存在或已软删”统一为 None（404 规则归属本域）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from iesplan.db import Base, JSONB, bigint_pk
from iesplan.project.contracts import (
    DraftRecord,
    MaintenanceActionRecord,
    ProjectConflictError,
    ProjectPage,
    ProjectRecord,
    ProjectVersionRecord,
    VersionRefRecord,
)


def _iso(value: datetime | None) -> str | None:
    """ORM 时间 → 记录字符串（原样 isoformat，不增减时区后缀）。"""
    return value.isoformat() if value is not None else None


def _now() -> datetime:
    return datetime.now(UTC)


def _row_to_project(row: Project) -> ProjectRecord:
    return ProjectRecord(
        id=row.id,
        name=row.name,
        description=row.description,
        status=row.status,
        owner_id=row.owner_id,
        currency=row.currency,
        baseline_resolution=row.baseline_resolution,
        baseline_leap_year=row.baseline_leap_year,
        baseline_scenario_mode=row.baseline_scenario_mode,
        schema_version=row.schema_version,
        finance_profile_id=row.finance_profile_id,
        overrides_revision=row.overrides_revision,
        effective_finance_revision=row.effective_finance_revision,
        planning_revision=row.planning_revision,
        current_draft_id=row.current_draft_id,
        current_version_id=row.current_version_id,
        created_by=row.created_by,
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


def _row_to_draft(row: Draft) -> DraftRecord:
    return DraftRecord(
        id=row.id,
        project_id=row.project_id,
        revision=row.revision,
        content_object_id=row.content_object_id,
        parent_draft_id=row.parent_draft_id,
        is_current=row.is_current,
        updated_by=row.updated_by,
        updated_at=_iso(row.updated_at),
    )


def _row_to_version(row: ProjectVersion) -> ProjectVersionRecord:
    return ProjectVersionRecord(
        id=row.id,
        project_id=row.project_id,
        version_no=row.version_no,
        name=row.name,
        description=row.description,
        created_by=row.created_by,
        parent_version_id=row.parent_version_id,
        source_draft_id=row.source_draft_id,
        source_draft_revision=row.source_draft_revision,
        reason=row.reason,
        baseline_resolution=row.baseline_resolution,
        baseline_leap_year=row.baseline_leap_year,
        baseline_scenario_mode=row.baseline_scenario_mode,
        currency=row.currency,
        schema_version=row.schema_version,
        content_object_id=row.content_object_id,
        created_at=_iso(row.created_at),
    )


def _row_to_ref(row: VersionRef) -> VersionRefRecord:
    return VersionRefRecord(
        id=row.id,
        project_version_id=row.project_version_id,
        ref_type=row.ref_type,
        object_id=row.object_id,
        ref_key=row.ref_key,
    )


def get_project(db: Session, project_id: int) -> ProjectRecord | None:
    """取项目主行；不存在或已软删返回 None。"""
    row = db.get(Project, project_id)
    if row is None or row.status == "deleted":
        return None
    return _row_to_project(row)


def project_name_exists(db: Session, name: str) -> bool:
    """项目名是否已被占用（含已软删行，与名称唯一约束口径一致，供导入命名去重）。"""
    return db.execute(select(Project.id).where(Project.name == name)).first() is not None


def list_projects(
    db: Session,
    *,
    owner_id: int | None = None,
    statuses: Sequence[str] | None = None,
    limit: int = 50,
    cursor: int | None = None,
) -> ProjectPage:
    """按显式条件列出项目（id 倒序，cursor 为末条 id）。"""
    stmt = select(Project)
    if owner_id is not None:
        stmt = stmt.where(Project.owner_id == owner_id)
    if statuses is not None:
        stmt = stmt.where(Project.status.in_(statuses))
    if cursor is not None:
        stmt = stmt.where(Project.id < cursor)
    rows = db.execute(stmt.order_by(Project.id.desc()).limit(limit + 1)).scalars().all()
    items = [_row_to_project(row) for row in rows[:limit]]
    next_cursor = items[-1].id if len(rows) > limit else None
    return ProjectPage(items=tuple(items), next_cursor=next_cursor)


def count_projects_by_owner(db: Session, owner_ids: Sequence[int]) -> dict[int, int]:
    """未删除项目数 read model（单条 GROUP BY，防 N+1）。"""
    if not owner_ids:
        return {}
    rows = db.execute(
        select(Project.owner_id, func.count(Project.id))
        .where(
            Project.owner_id.in_(owner_ids),
            Project.status != "deleted",
        )
        .group_by(Project.owner_id)
    ).all()
    return {owner_id: count for owner_id, count in rows}


def create_project(
    db: Session,
    *,
    name: str,
    owner_id: int,
    created_by: int,
    description: str | None = None,
    currency: str = "CNY",
    baseline_resolution: str = "1h",
    baseline_leap_year: bool = False,
    baseline_scenario_mode: str = "single",
    schema_version: int = 1,
) -> ProjectRecord:
    """创建项目裸行（不含初始草稿）；重名抛 ProjectConflictError。"""
    row = Project(
        name=name,
        description=description,
        status="active",
        owner_id=owner_id,
        currency=currency,
        baseline_resolution=baseline_resolution,
        baseline_leap_year=baseline_leap_year,
        baseline_scenario_mode=baseline_scenario_mode,
        schema_version=schema_version,
        created_by=created_by,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise ProjectConflictError("已存在同名项目", params={"name": name}) from exc
    return _row_to_project(row)


def set_project_status(db: Session, project_id: int, status: str) -> ProjectRecord:
    """切换项目状态（归档/撤销归档/软删统一入口）；项目缺失抛 ProjectConflictError。"""
    row = db.get(Project, project_id)
    if row is None:
        raise ProjectConflictError("项目不存在", params={"project_id": project_id})
    if row.status != status:
        row.status = status
        row.updated_at = _now()
        db.flush()
    return _row_to_project(row)


def update_revision_pointers(
    db: Session,
    project_id: int,
    *,
    finance_profile_id: int | None = None,
    overrides_revision: int | None = None,
    effective_finance_revision: int | None = None,
    planning_revision: int | None = None,
) -> ProjectRecord:
    """移动配置 revision 指针（revision 行只追加，指针可移动）。"""
    row = db.get(Project, project_id)
    if row is None:
        raise ProjectConflictError("项目不存在", params={"project_id": project_id})
    row.finance_profile_id = finance_profile_id
    row.overrides_revision = overrides_revision
    row.effective_finance_revision = effective_finance_revision
    row.planning_revision = planning_revision
    row.updated_at = _now()
    db.flush()
    return _row_to_project(row)


def get_current_draft(db: Session, project_id: int) -> DraftRecord | None:
    """取项目当前草稿；无草稿返回 None。"""
    row = db.execute(
        select(Draft)
        .where(Draft.project_id == project_id, Draft.is_current.is_(True))
        .order_by(Draft.revision.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _row_to_draft(row) if row is not None else None


def get_draft(db: Session, draft_id: int) -> DraftRecord | None:
    """按 id 取草稿行。"""
    row = db.get(Draft, draft_id)
    return _row_to_draft(row) if row is not None else None


def get_draft_revision(db: Session, project_id: int, revision: int) -> DraftRecord | None:
    """按综合修订号取草稿行。"""
    row = db.execute(
        select(Draft).where(Draft.project_id == project_id, Draft.revision == revision)
    ).scalar_one_or_none()
    return _row_to_draft(row) if row is not None else None


def create_draft(
    db: Session,
    *,
    project_id: int,
    content_object_id: int,
    updated_by: int,
    parent_draft_id: int | None = None,
    make_current: bool = True,
) -> DraftRecord:
    """追加草稿行（revision = max + 1；make_current 时切换 current 并移动指针）。"""
    old = db.execute(
        select(Draft)
        .where(Draft.project_id == project_id, Draft.is_current.is_(True))
        .order_by(Draft.revision.desc())
        .limit(1)
    ).scalar_one_or_none()
    max_revision = db.execute(select(func.max(Draft.revision)).where(Draft.project_id == project_id)).scalar()
    # 先翻转旧 current（uq_drafts_current 部分唯一索引要求同一项目同时至多一行 current）
    if make_current and old is not None:
        old.is_current = False
    row = Draft(
        project_id=project_id,
        revision=(max_revision or 0) + 1,
        content_object_id=content_object_id,
        parent_draft_id=parent_draft_id if parent_draft_id is not None else (old.id if old else None),
        is_current=make_current,
        updated_by=updated_by,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise ProjectConflictError(
            "草稿修订冲突(并发编辑), 请重新加载后再试",
            params={"revision": (max_revision or 0) + 1},
        ) from exc
    if make_current:
        project_row = db.get(Project, project_id)
        if project_row is not None:
            project_row.current_draft_id = row.id
            project_row.updated_at = _now()
        db.flush()
    return _row_to_draft(row)


def update_draft_content_ref(db: Session, draft_id: int, content_object_id: int) -> DraftRecord | None:
    """维护草稿内容指针；草稿缺失返回 None。"""
    row = db.get(Draft, draft_id)
    if row is None:
        return None
    row.content_object_id = content_object_id
    db.flush()
    return _row_to_draft(row)


def create_version(
    db: Session,
    *,
    project_id: int,
    name: str,
    reason: str,
    created_by: int,
    content_object_id: int,
    source_draft_id: int | None = None,
    source_draft_revision: int | None = None,
    description: str | None = None,
    parent_version_id: int | None = None,
) -> ProjectVersionRecord:
    """追加不可变版本（含 version_no 分配、内容引用行、current 指针移动）。"""
    project_row = db.get(Project, project_id)
    if project_row is None:
        raise ProjectConflictError("项目不存在", params={"project_id": project_id})
    max_no = db.execute(
        select(func.max(ProjectVersion.version_no)).where(ProjectVersion.project_id == project_id)
    ).scalar()
    row = ProjectVersion(
        project_id=project_id,
        version_no=(max_no or 0) + 1,
        name=name,
        description=description,
        created_by=created_by,
        parent_version_id=(
            parent_version_id if parent_version_id is not None else project_row.current_version_id
        ),
        source_draft_id=source_draft_id,
        source_draft_revision=source_draft_revision,
        reason=reason,
        baseline_resolution=project_row.baseline_resolution,
        baseline_leap_year=project_row.baseline_leap_year,
        baseline_scenario_mode=project_row.baseline_scenario_mode,
        currency=project_row.currency,
        schema_version=project_row.schema_version,
        content_object_id=content_object_id,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise ProjectConflictError(
            "版本创建冲突(并发编辑), 请重新加载后再试",
            params={"project_id": project_id},
        ) from exc
    db.add(
        VersionRef(
            project_version_id=row.id,
            ref_type="object",
            object_id=content_object_id,
            ref_key="project_version_content",
        )
    )
    project_row.current_version_id = row.id
    project_row.updated_at = _now()
    db.flush()
    return _row_to_version(row)


def list_versions(db: Session, project_id: int) -> list[ProjectVersionRecord]:
    """列出项目版本（version_no 倒序）。"""
    rows = (
        db.execute(
            select(ProjectVersion)
            .where(ProjectVersion.project_id == project_id)
            .order_by(ProjectVersion.version_no.desc())
        )
        .scalars()
        .all()
    )
    return [_row_to_version(row) for row in rows]


def get_version(db: Session, project_id: int, version_id: int) -> ProjectVersionRecord | None:
    """取单个版本（归属不符返回 None）。"""
    row = db.get(ProjectVersion, version_id)
    if row is None or row.project_id != project_id:
        return None
    return _row_to_version(row)


def add_version_ref(
    db: Session,
    *,
    project_version_id: int,
    ref_type: str,
    object_id: int,
    ref_key: str | None = None,
) -> VersionRefRecord:
    """追加版本引用行；重复引用抛 ProjectConflictError。"""
    row = VersionRef(
        project_version_id=project_version_id,
        ref_type=ref_type,
        object_id=object_id,
        ref_key=ref_key,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise ProjectConflictError(
            "版本引用已存在", params={"project_version_id": project_version_id}
        ) from exc
    return _row_to_ref(row)


def list_version_refs(db: Session, project_version_id: int) -> list[VersionRefRecord]:
    """列出版本引用清单。"""
    rows = (
        db.execute(select(VersionRef).where(VersionRef.project_version_id == project_version_id))
        .scalars()
        .all()
    )
    return [_row_to_ref(row) for row in rows]


def get_project_version_content_id(db: Session, version_id: int) -> int | None:
    """按主键取项目版本的内容对象 id；版本缺失返回 None。"""
    row = db.get(ProjectVersion, version_id)
    return int(row.content_object_id) if row is not None else None


def _row_to_maintenance_action(row: AdminMaintenanceAction) -> MaintenanceActionRecord:
    return MaintenanceActionRecord(
        id=row.id,
        action_type=row.action_type,
        performed_by=row.performed_by,
        status=row.status,
        started_at=_iso(row.started_at),
        finished_at=_iso(row.finished_at),
        params=row.params,
        result=row.result,
    )


def list_maintenance_actions(db: Session, limit: int = 10) -> list[MaintenanceActionRecord]:
    """维护记录（id 倒序；运维诊断消费，只读）。"""
    rows = (
        db.execute(select(AdminMaintenanceAction).order_by(AdminMaintenanceAction.id.desc()).limit(limit))
        .scalars()
        .all()
    )
    return [_row_to_maintenance_action(row) for row in rows]


def record_maintenance_action(
    db: Session,
    *,
    action_type: str,
    performed_by: int,
    status: str,
    params: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
) -> MaintenanceActionRecord:
    """记录管理员维护操作（不可变，只 INSERT）。"""
    now = _now()
    row = AdminMaintenanceAction(
        action_type=action_type,
        performed_by=performed_by,
        status=status,
        started_at=now,
        finished_at=now,
        params=params,
        result=result,
    )
    db.add(row)
    db.flush()
    return _row_to_maintenance_action(row)


# ---------------------------------------------------------------------------
# ORM 表定义: Wave2A 由 iesplan.models.project 迁入, 表真相归本域所有。
# ---------------------------------------------------------------------------

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
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("1"))
    # 当前生效财务三件套/规划配置 revision 指针(0.6.5 条目 1-2):
    # finance_profiles / finance_overrides / effective_finance_revisions /
    # planning_configs 均仅 INSERT, 指针指向当前生效 revision(指针可移动);
    # finance_profile_id 指向项目引用的已注册 Profile 主键,
    # overrides_revision 指向当前覆盖 revision(空 = 无覆盖, 空覆盖文档),
    # effective_finance_revision 指向合并器产出的有效快照 revision,
    # planning_revision 指向当前规划配置 revision。
    finance_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("finance_profiles.id")
    )
    overrides_revision: Mapped[int | None] = mapped_column(BigInteger)
    effective_finance_revision: Mapped[int | None] = mapped_column(BigInteger)
    planning_revision: Mapped[int | None] = mapped_column(BigInteger)
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
    content_object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"), nullable=False)
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
    currency: Mapped[str | None] = mapped_column(Text)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "baseline_resolution IN ('15min','30min','1h')",
            name="ck_project_versions_baseline_resolution",
        ),
        CheckConstraint(
            "baseline_scenario_mode IN ('single')",
            name="ck_project_versions_baseline_scenario",
        ),
        CheckConstraint("currency IS NULL OR currency IN ('CNY','USD')", name="ck_project_versions_currency"),
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "ref_type IN ('dataset_version','system_graph','calc_config','calc_snapshot',"
            "'evidence_package','report','object')",
            name="ck_version_refs_type",
        ),
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
