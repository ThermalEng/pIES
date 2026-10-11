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

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from iesplan import audit as audit_domain
from iesplan import configuration as configuration_domain
from iesplan import project as project_domain
from iesplan import results as results_domain
from iesplan import tasks as tasks_domain
from iesplan.application.configuration import (
    get_effective_finance_config as _read_effective,
    get_finance_profile_by_ref as _profile_by_ref,
    get_planning_config as _read_planning,
)
from iesplan.application.projects import ensure_access
from iesplan.application.projects.content_objects import (
    load_content_bytes as _load_content_bytes,
    load_content_object as _load_content_object,
    store_content_object as _store_content_object,
)
from iesplan.core.errors import AppError, ConflictError, NotFoundError
from iesplan.core.jsonutil import canonical_json
from iesplan.identity.contracts import UserRecord
from iesplan.project.contracts import DraftRecord, ProjectRecord, ProjectVersionRecord
from iesplan.results.contracts import SINGLE_SOLUTION_ID
from iesplan.storage import get_object as _get_object


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
        baseline_start_year=project.baseline_start_year,
        scenario_mode=project.baseline_scenario_mode,
        effective_profile_id=eff_id,
        overrides_revision=project.overrides_revision,
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


def _source_pins(
    db: Session, project_id: int, src_content: Mapping
) -> tuple[tuple[int, str, int, int] | None, int | None]:
    """历史版本冻结 lineage → ((profile行id, profile_id, overrides_rev, eff_rev)|None, plan_rev|None)。

    只读 source 版本已冻结的精确引用并解析真实依赖行；引用行缺失 →
    AppError(配置域 PROJ-FIN-003/PROJ-PLAN-004 同语义；Profile 行缺失由
    公开门面透传 404)，不 fallback，不改历史 revision 行。版本内容由
    可信构造器写入，不做字段形态防御。
    """
    eff_lineage: tuple[int, str, int, int] | None = None
    plan_rev: int | None = None
    eff = src_content.get("effective_finance")
    if eff is not None:
        profile_row, _ = _profile_by_ref(db, eff.get("profile_id"))
        ovr_row = configuration_domain.get_overrides_revision(
            db, project_id, eff.get("overrides_revision")
        )
        if ovr_row is None:
            raise AppError(
                "历史版本引用的财务覆盖不存在",
                code="PROJ-FIN-003",
                params={
                    "project_id": project_id,
                    "overrides_revision": eff.get("overrides_revision"),
                },
            )
        row = configuration_domain.get_effective_revision(db, project_id, eff.get("revision"))
        if row is None or row.profile_id != eff.get("profile_id"):
            raise AppError(
                "历史版本引用的 Effective 财务快照不存在",
                code="PROJ-FIN-003",
                params={
                    "project_id": project_id,
                    "profile_id": eff.get("profile_id"),
                    "revision": eff.get("revision"),
                },
            )
        eff_lineage = (profile_row.id, profile_row.profile_id, ovr_row.revision, row.revision)
    plan = src_content.get("planning_config")
    if plan is not None:
        row = configuration_domain.get_planning_revision(db, project_id, plan.get("revision"))
        if row is None:
            raise AppError(
                "历史版本引用的规划配置不存在",
                code="PROJ-PLAN-004",
                params={"project_id": project_id, "revision": plan.get("revision")},
            )
        plan_rev = row.revision
    return eff_lineage, plan_rev


def restore_version(
    db: Session,
    user: UserRecord,
    project_id: int,
    version_id: int,
    name: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """事务型恢复历史版本(新版本 + 新草稿，不倒写历史)；统一提交或回滚。

    新草稿与新版本保留 source 版本冻结的完整配置 lineage（财务三元组 +
    规划 revision，不重读项目当前指针）；项目四个当前指针
    (finance_profile_id/overrides/effective/planning)原子移动到恢复后的
    业务状态：source 缺 effective_finance 块则三个财务指针全空，缺
    planning_config 块则规划指针为空，不 fallback 到当前指针。
    历史 revision 行只读不改。
    """
    try:
        ensure_access(db, user, project_id, "edit")
        project = project_domain.require_project(db, project_id)
        if project.status != "active":
            raise ConflictError(
                "项目已归档或已删除, 不能恢复版本",
                location={"object_type": "project", "object_id": project_id},
            )
        source = project_domain.require_version(db, project_id, version_id)
        src_content = _load_content_object(db, source.content_object_id)
        eff_lineage, plan_rev = _source_pins(db, project_id, src_content)
        draft_content = {
            k: v for k, v in src_content.items() if k != "applied_commands"
        }
        content_object_id = _store_content_object(db, draft_content)
        new_draft = project_domain.create_draft(
            db,
            project_id=project.id,
            content_object_id=content_object_id,
            updated_by=user.id,
        )
        project_domain.update_revision_pointers(
            db,
            project.id,
            finance_profile_id=eff_lineage[0] if eff_lineage is not None else None,
            overrides_revision=eff_lineage[2] if eff_lineage is not None else None,
            effective_finance_revision=eff_lineage[3] if eff_lineage is not None else None,
            planning_revision=plan_rev,
        )
        version_name = (name or f"恢复: {source.name}").strip()
        if not version_name:
            raise project_domain.InvalidRequestError("版本名称不能为空", code="PROJ-CMD-001")
        version_content = project_domain.build_version_content(
            draft_content,
            currency=project.currency,
            baseline_resolution=project.baseline_resolution,
            baseline_start_year=project.baseline_start_year,
            scenario_mode=project.baseline_scenario_mode,
            effective_profile_id=eff_lineage[1] if eff_lineage is not None else None,
            overrides_revision=eff_lineage[2] if eff_lineage is not None else None,
            effective_revision=eff_lineage[3] if eff_lineage is not None else None,
            planning_revision=plan_rev,
        )
        version_object_id = _store_content_object(db, version_content)
        version = project_domain.create_version(
            db,
            project_id=project.id,
            name=version_name,
            reason="restore",
            created_by=user.id,
            content_object_id=version_object_id,
            source_draft_id=new_draft.id,
            source_draft_revision=new_draft.revision,
            description=description,
            parent_version_id=source.id,
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
                "name": version_name,
                "reason": "restore",
                "parent_version_id": version.parent_version_id,
                "source_draft_revision": new_draft.revision,
                "source_result_id": None,
            },
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


def _scheme_params(db: Session, package: Any, snapshot: Any) -> list[dict[str, Any]]:
    """应用方案参数投影(服务端权威解析): 封存解 × 同一快照冻结绑定。

    无 compute_result 块或块内无 solution(metrics-only)是业务缺席, 返回
    空表(调用方不创建空应用版本); 冻结字段直接消费, 残缺即正常错误
    路径可见。值与单位沿封存解原样, 不猜测、不重验。
    """
    raw = _get_object(db, package.object_id)
    inner = results_domain.evidence_inner(json.loads(raw.decode("utf-8")))
    compute_result = inner.get("compute_result")
    if compute_result is None:
        return []
    solution = compute_result["outputs"].get("solution")
    if solution is None:
        return []
    return results_domain.planning_solution_params(
        solution["variables"],
        snapshot.assembly_receipt["planning"]["variables"],
        json.loads(snapshot.canonical_assembly_text)["planning"]["variables"],
    )


def apply_scheme(
    db: Session,
    user: UserRecord,
    project_id: int,
    task_id: int,
    *,
    solution_id: int,
    name: str | None = None,
    description: str | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """事务型应用方案(真实规划参数 → 来源版本派生新草稿 + 新版本)；统一提交或回滚。

    基线严格取任务快照引用的计算来源版本：在其完整工作状态上写入封存
    规划解参数(同一快照实例/参数绑定，值原样；来源其他参数/设备/连接/
    配置完整保留)，派生新版本并切换为当前工作状态；不叠加当前草稿，
    不带入计算后编辑。配置指针按来源版本既有恢复语义移动；参数写入、
    新版本创建与状态切换同一事务完成，失败零部分状态。
    """
    try:
        ensure_access(db, user, project_id, "edit")
        project = project_domain.require_project(db, project_id)
        if project.status != "active":
            raise ConflictError(
                "项目已归档或已删除, 不能应用方案",
                location={"object_type": "project", "object_id": project_id},
            )
        if expected_revision is not None:
            draft_now = project_domain.require_current_draft(db, project)
            if draft_now.revision != expected_revision:
                raise ConflictError(
                    "项目草稿已被其他操作更新",
                    params={
                        "expected_revision": expected_revision,
                        "current_revision": draft_now.revision,
                    },
                    location={"object_type": "draft", "object_id": str(draft_now.id)},
                )
        task = tasks_domain.get_task(db, task_id)
        if task is None or task.project_id != project_id:
            raise NotFoundError(
                "任务不存在",
                params={"task_id": task_id, "project_id": project_id},
                location={"object_type": "task", "object_id": task_id},
            )
        if solution_id != SINGLE_SOLUTION_ID:
            raise NotFoundError(
                "所选解不存在",
                params={"task_id": task_id, "solution_id": solution_id},
                location={"object_type": "task", "object_id": task_id},
            )
        snapshot = tasks_domain.get_snapshot(db, task.calc_snapshot_id)
        source = project_domain.require_version(db, project_id, snapshot.project_version_id)
        package = results_domain.latest_evidence_for_task(db, task_id)
        if package is None:
            raise NotFoundError(
                "任务尚无证据包, 无法应用方案",
                params={"task_id": task_id},
                location={"object_type": "task", "object_id": task_id},
            )
        params = _scheme_params(db, package, snapshot)
        if not params:
            raise NotFoundError(
                "该任务结果无可用规划解, 不能应用方案",
                params={"task_id": task_id},
                location={"object_type": "task", "object_id": task_id},
            )
        src_content = _load_content_object(db, source.content_object_id)
        draft_content = {
            k: v for k, v in src_content.items() if k != "applied_commands"
        }
        devices = draft_content["model"]["devices"]
        for param in params:
            device = next(
                (d for d in devices if d.get("name") == param["device_ref"]), None
            )
            if device is None:
                raise NotFoundError(
                    "来源版本工作状态缺少设备",
                    params={"version_id": source.id, "device": param["device_ref"]},
                    location={"object_type": "project_version", "object_id": source.id},
                )
            merged = dict(device.get("params") or {})
            merged[param["parameter"]] = param["value"]
            device["params"] = merged
        eff_lineage, plan_rev = _source_pins(db, project_id, src_content)
        content_object_id = _store_content_object(db, draft_content)
        new_draft = project_domain.create_draft(
            db,
            project_id=project.id,
            content_object_id=content_object_id,
            updated_by=user.id,
        )
        project_domain.update_revision_pointers(
            db,
            project.id,
            finance_profile_id=eff_lineage[0] if eff_lineage is not None else None,
            overrides_revision=eff_lineage[2] if eff_lineage is not None else None,
            effective_finance_revision=eff_lineage[3] if eff_lineage is not None else None,
            planning_revision=plan_rev,
        )
        version_name = (name or f"应用: {source.name}").strip()
        if not version_name:
            raise project_domain.InvalidRequestError("版本名称不能为空", code="PROJ-CMD-001")
        version_content = project_domain.build_version_content(
            draft_content,
            currency=project.currency,
            baseline_resolution=project.baseline_resolution,
            baseline_start_year=project.baseline_start_year,
            scenario_mode=project.baseline_scenario_mode,
            effective_profile_id=eff_lineage[1] if eff_lineage is not None else None,
            overrides_revision=eff_lineage[2] if eff_lineage is not None else None,
            effective_revision=eff_lineage[3] if eff_lineage is not None else None,
            planning_revision=plan_rev,
        )
        version_object_id = _store_content_object(db, version_content)
        source_result_id = f"task:{task.id}/evidence:{package.id}/solution:{solution_id}"
        version = project_domain.create_version(
            db,
            project_id=project.id,
            name=version_name,
            reason="apply_result",
            created_by=user.id,
            content_object_id=version_object_id,
            source_draft_id=new_draft.id,
            source_draft_revision=new_draft.revision,
            description=description,
            parent_version_id=source.id,
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
                "name": version_name,
                "reason": "apply_result",
                "parent_version_id": version.parent_version_id,
                "source_draft_revision": new_draft.revision,
                "source_result_id": source_result_id,
            },
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
                "new_revision": new_draft.revision,
                "task_id": task.id,
                "evidence_package_id": package.id,
                "solution_id": solution_id,
                "source_result_id": source_result_id,
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
    "apply_scheme",
    "create_version",
    "current_version_matches_draft",
    "freeze_snapshot_version",
    "get_version",
    "list_versions",
    "replace_project_model_refs",
    "restore_version",
]
