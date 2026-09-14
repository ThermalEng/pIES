"""项目版本用例(application/projects/versions, 纠偏 Wave 1 集成)。

版本/草稿编排唯一实现（旧 services.project 已删除；project 域仅保留
require_*/纯组装/序列化，存储 IO 与跨域编排归属本层）：

- 以项目模型清单推进草稿修订 / 创建版本 / 恢复版本 / 应用结果 /
  任务提交冻结固化 / 当前版本与草稿一致性判断；
- 只读：版本列表 / 版本详情。

事务：写用例顶层函数拥有提交/回滚；读用例不提交事务。本层不新增校验/hash/
完整性复核/防御分支。

调用方向：``api → application.projects.versions → (project/configuration/
audit 领域公开门面, storage 经 content_objects)``；不导入 ORM、不导入领域
内部模块、不调用 ``services.*``。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from iesplan import audit as audit_domain
from iesplan import configuration as configuration_domain
from iesplan import project as project_domain
from iesplan.application.configuration import (
    get_effective_finance_config as _read_effective,
    get_planning_config as _read_planning,
)
from iesplan.application.projects import ensure_access
from iesplan.application.projects.content_objects import (
    load_content_bytes as _load_content_bytes,
    load_content_object as _load_content_object,
    merge_patch,
    store_content_object as _store_content_object,
)
from iesplan.core.errors import AppError, ConflictError, NotFoundError
from iesplan.core.jsonutil import canonical_json
from iesplan.identity.contracts import UserRecord
from iesplan.project.contracts import DraftRecord, ProjectRecord, ProjectVersionRecord


def _audit(
    db: Session,
    entity_type: str,
    entity_id: int,
    action: str,
    actor_id: int,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    """审计事件写入(与业务写入同事务；只含脱敏元数据)。"""
    audit_domain.append_entry(
        db,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_type="user",
        before=before,
        extra=after,
    )


def _current_refs(db: Session, project_id: int) -> tuple[tuple[str, int] | None, int | None]:
    """当前生效财务/规划引用(无则省略, 不静默默认)。"""
    try:
        effective, effective_revision, _ = _read_effective(db, project_id)
        eff: tuple[str, int] | None = (effective.profile_id, effective_revision)
    except NotFoundError:
        eff = None
    try:
        _, planning_revision, _ = _read_planning(db, project_id)
    except NotFoundError:
        planning_revision = None
    return eff, planning_revision


def _assemble(
    db: Session,
    project: ProjectRecord,
    content: dict,
    *,
    pinned: bool,
) -> dict:
    """版本内容组装(纯规则经 project 域 build_version_content)。

    pinned=False 取当前生效引用(交互式版本操作)；pinned=True 取项目行指针
    引用(任务提交冻结)。指针损坏抛 AppError。
    """
    if pinned:
        eff_id = eff_rev = plan_rev = None
        if project.effective_finance_revision is not None:
            row = configuration_domain.get_effective_revision(
                db, project.id, project.effective_finance_revision
            )
            if row is None:
                raise AppError(
                    "项目 Effective 财务快照指针损坏(指向不存在的 revision)",
                    code="PROJ-FIN-003",
                    params={"project_id": project.id, "revision": project.effective_finance_revision},
                )
            eff_id, eff_rev = row.profile_id, row.revision
        if project.planning_revision is not None:
            row = configuration_domain.get_planning_revision(db, project.id, project.planning_revision)
            if row is None:
                raise AppError(
                    "项目规划配置指针损坏(指向不存在的 revision)",
                    code="PROJ-PLAN-004",
                    params={"project_id": project.id, "revision": project.planning_revision},
                )
            plan_rev = row.revision
    else:
        eff, plan_rev = _current_refs(db, project.id)
        eff_id, eff_rev = eff if eff is not None else (None, None)
    return project_domain.build_version_content(
        content,
        currency=project.currency,
        baseline_resolution=project.baseline_resolution,
        baseline_leap_year=project.baseline_leap_year,
        scenario_mode=project.baseline_scenario_mode,
        effective_profile_id=eff_id,
        effective_revision=eff_rev,
        planning_revision=plan_rev,
    )


def replace_project_model_refs(
    db: Session,
    user: UserRecord,
    project_id: int,
    expected_revision: int,
    refs: list[dict[str, object]],
) -> DraftRecord:
    """以项目模型清单的权威快照推进草稿修订(乐观锁；调用方拥有事务)。"""
    ensure_access(db, user, project_id, "edit")
    project = project_domain.require_project(db, project_id)
    draft = project_domain.require_current_draft(db, project)
    if draft.revision != expected_revision:
        raise ConflictError(
            "项目草稿已被其他操作更新",
            params={"expected_revision": expected_revision, "current_revision": draft.revision},
            location={"object_type": "draft", "object_id": str(draft.id)},
        )
    content = _load_content_object(db, draft.content_object_id)
    content["project_models"] = refs
    content_object_id = _store_content_object(db, content)
    new_draft = project_domain.create_draft(
        db,
        project_id=project.id,
        content_object_id=content_object_id,
        updated_by=user.id,
    )
    _audit(
        db,
        "project",
        project.id,
        "project.models_updated",
        user.id,
        after={"revision": new_draft.revision, "model_count": len(refs)},
    )
    db.flush()
    return new_draft


def _do_create_version(
    db: Session,
    user: UserRecord,
    project_id: int,
    name: str,
    description: str | None = None,
    reason: str = "manual_save",
    parent_version_id: int | None = None,
    source_result_id: str | None = None,
) -> ProjectVersionRecord:
    """创建版本内核(无提交；调用方拥有事务)：当前草稿 → 组装 → 版本行 + 审计。"""
    ensure_access(db, user, project_id, "edit")
    project = project_domain.require_project(db, project_id)
    if project.status != "active":
        raise ConflictError(
            "项目已归档或已删除, 不能创建版本",
            location={"object_type": "project", "object_id": project_id},
        )
    name = (name or "").strip()
    if not name:
        raise project_domain.InvalidRequestError("版本名称不能为空", code="PROJ-CMD-001")
    draft = project_domain.require_current_draft(db, project)
    content = _load_content_object(db, draft.content_object_id)
    version_content = _assemble(db, project, content, pinned=False)
    content_object_id = _store_content_object(db, version_content)
    if parent_version_id is not None:
        parent_id = project_domain.require_version(db, project_id, parent_version_id).id
    else:
        parent_id = None
    version = project_domain.create_version(
        db,
        project_id=project_id,
        name=name,
        reason=reason,
        created_by=user.id,
        content_object_id=content_object_id,
        source_draft_id=draft.id,
        source_draft_revision=draft.revision,
        description=description,
        parent_version_id=parent_id,
    )
    _audit(
        db,
        "project_version",
        version.id,
        "project.version_created",
        user.id,
        after={
            "project_id": project.id,
            "version_no": version.version_no,
            "name": name,
            "reason": reason,
            "parent_version_id": version.parent_version_id,
            "source_draft_revision": draft.revision,
            "source_result_id": source_result_id,
        },
    )
    return version


def create_version(
    db: Session,
    user: UserRecord,
    project_id: int,
    name: str,
    description: str | None = None,
    reason: str = "manual_save",
    parent_version_id: int | None = None,
    source_result_id: str | None = None,
) -> ProjectVersionRecord:
    """事务型从当前草稿创建不可变项目版本；统一提交或回滚。"""
    try:
        version = _do_create_version(
            db, user, project_id, name, description, reason, parent_version_id, source_result_id
        )
        db.commit()
        return version
    except Exception:
        db.rollback()
        raise


def current_version_matches_draft(db: Session, project: ProjectRecord) -> bool:
    """当前版本内容是否与当前草稿一致(任务提交冻结口径：行指针引用)。

    无当前版本返回 False(需固化)。草稿仅在命令簿记上推进而无领域变更时
    视为一致。
    """
    if project.current_version_id is None:
        return False
    version = project_domain.get_version(db, project.id, project.current_version_id)
    if version is None:
        return False
    draft = project_domain.require_current_draft(db, project)
    content = _load_content_object(db, draft.content_object_id)
    expected = canonical_json(_assemble(db, project, content, pinned=True)).encode("utf-8")
    stored = _load_content_bytes(db, version.content_object_id)
    return stored == expected


def freeze_snapshot_version(
    db: Session, actor: UserRecord, project: ProjectRecord, draft: DraftRecord
) -> ProjectVersionRecord:
    """为快照装配固化草稿为不可变版本(任务提交冻结口径：行指针引用)。"""
    if project.status != "active":
        raise ConflictError(
            "项目已归档或已删除, 不能创建版本",
            location={"object_type": "project", "object_id": project.id},
        )
    content = _load_content_object(db, draft.content_object_id)
    version_content = _assemble(db, project, content, pinned=True)
    content_object_id = _store_content_object(db, version_content)
    version = project_domain.create_version(
        db,
        project_id=project.id,
        name="计算任务自动固化",
        reason="snapshot_freeze",
        created_by=actor.id,
        content_object_id=content_object_id,
        source_draft_id=draft.id,
        source_draft_revision=draft.revision,
        description=None,
        parent_version_id=None,
    )
    _audit(
        db,
        "project_version",
        version.id,
        "project.version_created",
        actor.id,
        after={
            "project_id": project.id,
            "version_no": version.version_no,
            "name": "计算任务自动固化",
            "reason": "snapshot_freeze",
            "parent_version_id": version.parent_version_id,
            "source_draft_revision": draft.revision,
            "source_result_id": None,
        },
    )
    return version


def restore_version(
    db: Session,
    user: UserRecord,
    project_id: int,
    version_id: int,
    name: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """事务型恢复历史版本(新版本 + 新草稿，不倒写历史)；统一提交或回滚。"""
    try:
        ensure_access(db, user, project_id, "edit")
        project = project_domain.require_project(db, project_id)
        if project.status != "active":
            raise ConflictError(
                "项目已归档或已删除, 不能恢复版本",
                location={"object_type": "project", "object_id": project_id},
            )
        source = project_domain.require_version(db, project_id, version_id)
        content = _load_content_object(db, source.content_object_id)
        content.pop("applied_commands", None)
        content_object_id = _store_content_object(db, content)
        new_draft = project_domain.create_draft(
            db,
            project_id=project.id,
            content_object_id=content_object_id,
            updated_by=user.id,
        )
        version = _do_create_version(
            db,
            user,
            project_id,
            name=name or f"恢复: {source.name}",
            description=description,
            reason="restore",
            parent_version_id=source.id,
        )
        _audit(
            db,
            "project_version",
            version.id,
            "project.version_restored",
            user.id,
            after={
                "project_id": project.id,
                "from_version_no": source.version_no,
                "new_version_no": version.version_no,
                "new_revision": new_draft.revision,
            },
        )
        db.commit()
        return {
            "version": project_domain.version_to_dict(version),
            "draft": project_domain.draft_to_dict(new_draft),
        }
    except Exception:
        db.rollback()
        raise


def apply_result(
    db: Session,
    user: UserRecord,
    project_id: int,
    diff_patch: dict,
    *,
    version_id: int | None = None,
    name: str | None = None,
    description: str | None = None,
    source_result_id: str | None = None,
) -> dict[str, Any]:
    """事务型应用选定结果(参数差异补丁→新草稿+新版本)；统一提交或回滚。"""
    try:
        ensure_access(db, user, project_id, "edit")
        project = project_domain.require_project(db, project_id)
        if project.status != "active":
            raise ConflictError(
                "项目已归档或已删除, 不能应用结果",
                location={"object_type": "project", "object_id": project_id},
            )
        source = (
            project_domain.require_version(db, project_id, version_id) if version_id is not None else None
        )
        if source is None:
            if project.current_version_id is None:
                raise NotFoundError("项目尚无版本, 无法应用结果", params={"project_id": project_id})
            source = project_domain.require_version(db, project_id, project.current_version_id)
        if not isinstance(diff_patch, dict):
            raise project_domain.InvalidRequestError("diff_patch 必须是对象", code="PROJ-CMD-005")
        draft = project_domain.require_current_draft(db, project)
        content = _load_content_object(db, draft.content_object_id)
        inner = diff_patch.get("calc_config")
        patch = inner if isinstance(inner, dict) else diff_patch
        if not isinstance(patch, dict):
            raise project_domain.InvalidRequestError("diff_patch 内容非法", code="PROJ-CMD-005")
        merge_patch(content["calc_config"], patch)
        content.pop("applied_commands", None)
        content_object_id = _store_content_object(db, content)
        new_draft = project_domain.create_draft(
            db,
            project_id=project.id,
            content_object_id=content_object_id,
            updated_by=user.id,
        )
        version = _do_create_version(
            db,
            user,
            project_id,
            name=name or "应用结果",
            description=description,
            reason="apply_result",
            parent_version_id=source.id,
            source_result_id=source_result_id,
        )
        _audit(
            db,
            "project_version",
            version.id,
            "project.result_applied",
            user.id,
            after={
                "project_id": project.id,
                "source_version_no": source.version_no,
                "new_version_no": version.version_no,
                "source_result_id": source_result_id,
                "new_revision": new_draft.revision,
            },
        )
        db.commit()
        return {
            "version": project_domain.version_to_dict(version),
            "draft": project_domain.draft_to_dict(new_draft),
        }
    except Exception:
        db.rollback()
        raise


def list_versions(db: Session, user: UserRecord, project_id: int) -> list[ProjectVersionRecord]:
    """版本列表(新版本在前；含 view 授权，只读，不提交事务)。"""
    ensure_access(db, user, project_id, "view")
    return project_domain.list_versions(db, project_id)


def get_version(
    db: Session, user: UserRecord, project_id: int, version_id: int
) -> ProjectVersionRecord:
    """按 id 获取项目版本(须属于该项目，否则 404；含 view 授权，只读，不提交事务)。"""
    ensure_access(db, user, project_id, "view")
    return project_domain.require_version(db, project_id, version_id)


__all__ = [
    "apply_result",
    "create_version",
    "current_version_matches_draft",
    "freeze_snapshot_version",
    "get_version",
    "list_versions",
    "replace_project_model_refs",
    "restore_version",
]
