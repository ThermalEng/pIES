"""项目域 SQL repository 实现（projects/drafts/project_versions/version_refs）。

- 本模块是 `ProjectRepository` 协议的唯一实现，可经 `iesplan.project` 门面调用；
- 只访问 `iesplan.models.project` 的表；绝不 commit/rollback（调用方事务拥有）；
- 唯一冲突转 `ProjectConflictError`；冲突后调用方须回滚会话（与旧 services
  契约一致；实测 SA 2.0 下 flush 失败后会话不可继续，savepoint 保不住可用性）；
- 时间以 `datetime.isoformat()` 原样映射为记录字符串（与 FastAPI 既有
  datetime 序列化输出一致，API 行为不变）；
- `get_project` 把“不存在或已软删”统一为 None（404 规则归属本域）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from iesplan.models.project import Draft, Project, ProjectVersion, VersionRef
from iesplan.project.contracts import (
    DraftRecord,
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
