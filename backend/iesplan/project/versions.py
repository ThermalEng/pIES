"""项目域版本/草稿编排（版本固化与恢复、模型清单引用推进，归属 project）。

收敛自 ``services.project``（纠偏 Wave 1 切片 D）：实现与旧服务逐行同义，
仅做收敛必需的机械调整——

- 与 repository 同名的 raising 变体改名（repository 原名与语义冻结，
  既有调用方不可触碰）：``get_current_draft(记录版)`` → ``require_current_draft``、
  ``get_version(404 版)`` → ``require_version``、``create_version(编排版)`` →
  ``create_project_version``；只读 ``list_versions`` 无需搬移（调用方直用
  repository 同名函数）；
- 内部 ``_get_project`` 并入公开 ``require_project``；
- 内容对象读写经 storage 公开门面 + 本域 ``content`` 纯函数；
- 审计经 audit 域公开门面；``_version_content`` 的财务/规划引用仍经
  ``services.config_revisions``（延迟导入，归属 config 切片后续收敛）。

本模块不主动 commit/rollback（调用方事务拥有）；外部经 ``iesplan.project``
门面消费；不得导入 ``iesplan.models``、services（延迟的 config 编排除外）
或其他域的内部模块。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from iesplan import audit as audit_domain
from iesplan.core.diagnostics import SEVERITY_ERROR, SYS_STORE_CORRUPT
from iesplan.core.errors import AppError, ConflictError, NotFoundError
from iesplan.core.jsonutil import canonical_json
from iesplan.identity.contracts import UserRecord
from iesplan.project import persistence
from iesplan.project.access import InvalidRequestError, ensure_access
from iesplan.project.content import content_to_bytes, corrupt_error, parse_content_object
from iesplan.project.contracts import DraftRecord, ProjectRecord, ProjectVersionRecord
from iesplan.storage import ObjectCorruptError, attach, get_object, put_object


def require_project(db: Session, project_id: int) -> ProjectRecord:
    """按 id 取项目; 不存在或已删除(软删)一律 404(无回收站语义)。"""
    project = persistence.get_project(db, project_id)
    if project is None:
        raise NotFoundError(
            "项目不存在",
            params={"project_id": project_id},
            location={"object_type": "project", "object_id": project_id},
        )
    return project


def require_current_draft(db: Session, project: ProjectRecord) -> DraftRecord:
    """取项目当前草稿(is_current=true 且修订最大者); 缺失视为数据损坏。"""
    draft = persistence.get_current_draft(db, project.id)
    if draft is None:
        raise AppError(
            "项目缺少当前草稿(数据损坏)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
            location={"object_type": "project", "object_id": project.id},
        )
    return draft


def require_version(db: Session, project_id: int, version_id: int) -> ProjectVersionRecord:
    """按 id 获取项目版本(须属于该项目, 否则 404)。"""
    version = persistence.get_version(db, project_id, version_id)
    if version is None:
        raise NotFoundError(
            "版本不存在",
            params={"project_id": project_id, "version_id": version_id},
            location={"object_type": "project_version", "object_id": version_id},
        )
    return version


def replace_project_model_refs(
    db: Session,
    user: UserRecord,
    project_id: int,
    expected_revision: int,
    refs: list[dict[str, object]],
) -> DraftRecord:
    """以项目模型清单的权威快照推进草稿修订。

    项目模型文件与清单由 application/projects 用例原子保存；本函数只拥有项目
    草稿事实，执行乐观锁并把项目模型清单引用(不透明模型 ID、device_id、
    revision)写入新草稿。调用方与本函数共享同一数据库事务。
    """
    ensure_access(db, user, project_id, "edit")
    project = require_project(db, project_id)
    draft = require_current_draft(db, project)
    if draft.revision != expected_revision:
        raise ConflictError(
            "项目草稿已被其他操作更新",
            params={"expected_revision": expected_revision, "current_revision": draft.revision},
            location={"object_type": "draft", "object_id": str(draft.id)},
        )
    content = load_content_object(db, draft.content_object_id)
    content["project_models"] = refs
    content_object_id = store_content_object(db, content)
    new_draft = persistence.create_draft(
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


def create_project_version(
    db: Session,
    user: UserRecord,
    project_id: int,
    name: str,
    description: str | None = None,
    reason: str = "manual_save",
    parent_version_id: int | None = None,
    source_result_id: str | None = None,
) -> ProjectVersionRecord:
    """从当前草稿创建不可变项目版本(domain-model §项目聚合/§快照、任务和结果)。

    快照内容: 模型/布局/数据集绑定/计算配置/语言/币种/UTC 偏移/受控扩展清单/
    模式版本/内容校验; 父版本缺省取项目当前版本; source_result_id(应用结果的
    来源结果标识)记入审计。project_versions 仅 INSERT(不可变，domain-model §项目聚合)。
    """
    ensure_access(db, user, project_id, "edit")
    project = require_project(db, project_id)
    if project.status != "active":
        raise ConflictError(
            "项目已归档或已删除, 不能创建版本",
            location={"object_type": "project", "object_id": project_id},
        )
    name = (name or "").strip()
    if not name:
        raise InvalidRequestError("版本名称不能为空", code="PROJ-CMD-001")
    draft = require_current_draft(db, project)
    content = load_content_object(db, draft.content_object_id)
    version_content = _version_content(db, project, content)
    content_object_id = store_content_object(db, version_content)

    if parent_version_id is not None:
        parent_id = require_version(db, project_id, parent_version_id).id
    else:
        parent_id = None
    # 版本行 + 内容引用行 + current 指针移动经领域 repository 同事务完成
    version = persistence.create_version(
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


def current_version_matches_draft(db: Session, project: ProjectRecord) -> bool:
    """当前版本内容是否与当前草稿一致(按版本固化规则比较，domain-model §项目聚合)。

    版本内容 = 草稿领域内容(去命令簿记) + 项目固化字段;
    草稿仅在命令簿记(applied_commands)上推进而无领域变更时视为一致。
    无当前版本返回 False(需固化)。用于任务提交时判断是否需重新固化,
    避免草稿已修改而任务仍运行旧版本输入。
    """
    if project.current_version_id is None:
        return False
    version = persistence.get_version(db, project.id, project.current_version_id)
    if version is None:
        return False
    draft = require_current_draft(db, project)
    content = load_content_object(db, draft.content_object_id)
    expected = canonical_json(_version_content(db, project, content)).encode("utf-8")
    stored = _load_content_bytes(db, version.content_object_id)
    return stored == expected


def restore_version(
    db: Session,
    user: UserRecord,
    project_id: int,
    version_id: int,
    name: str | None = None,
    description: str | None = None,
) -> dict:
    """恢复历史版本: 创建新版本 + 新草稿, 不倒写历史(domain-model §项目聚合)。

    恢复后的新草稿内容与目标版本一致; 新版本 parent 指向被恢复的版本。
    返回 {"version": 新版本, "draft": 新草稿}。
    """
    ensure_access(db, user, project_id, "edit")
    project = require_project(db, project_id)
    if project.status != "active":
        raise ConflictError(
            "项目已归档或已删除, 不能恢复版本",
            location={"object_type": "project", "object_id": project_id},
        )
    source = require_version(db, project_id, version_id)
    content = load_content_object(db, source.content_object_id)
    # 恢复内容中的命令簿记清空(新修订从干净状态开始; 与版本内容保持一致)
    content.pop("applied_commands", None)
    content_object_id = store_content_object(db, content)
    new_draft = persistence.create_draft(
        db,
        project_id=project.id,
        content_object_id=content_object_id,
        updated_by=user.id,
    )
    version = create_project_version(
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
    return {"version": version_to_dict(version), "draft": draft_to_dict(new_draft)}


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
) -> dict:
    """应用选定规划结果(domain-model §项目聚合/§快照、任务和结果)。

    参数差异补丁(diff_patch)应用到新草稿, 创建新版本; 结果来源版本保持不变。
    - diff_patch 直接作用于 calc_config 节(如 {"params": {...}});
      若含 "calc_config" 键则取其值作为补丁。
    返回 {"version": 新版本, "draft": 新草稿}。
    """
    ensure_access(db, user, project_id, "edit")
    project = require_project(db, project_id)
    if project.status != "active":
        raise ConflictError(
            "项目已归档或已删除, 不能应用结果",
            location={"object_type": "project", "object_id": project_id},
        )
    source = require_version(db, project_id, version_id) if version_id is not None else None
    if source is None:
        if project.current_version_id is None:
            raise NotFoundError("项目尚无版本, 无法应用结果", params={"project_id": project_id})
        source = require_version(db, project_id, project.current_version_id)
    if not isinstance(diff_patch, dict):
        raise InvalidRequestError("diff_patch 必须是对象", code="PROJ-CMD-005")
    draft = require_current_draft(db, project)
    content = load_content_object(db, draft.content_object_id)
    # 参数差异补丁应用到新草稿内容(原版本不变);
    # diff_patch 直接作用于 calc_config 节, 含 "calc_config" 键时取其值
    inner = diff_patch.get("calc_config")
    patch = inner if isinstance(inner, dict) else diff_patch
    if not isinstance(patch, dict):
        raise InvalidRequestError("diff_patch 内容非法", code="PROJ-CMD-005")
    _deep_merge(content["calc_config"], patch)
    content.pop("applied_commands", None)
    content_object_id = store_content_object(db, content)
    new_draft = persistence.create_draft(
        db,
        project_id=project.id,
        content_object_id=content_object_id,
        updated_by=user.id,
    )
    version = create_project_version(
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
    return {"version": version_to_dict(version), "draft": draft_to_dict(new_draft)}


# ---------------------------------------------------------------------------
# 序列化与内容载体
# ---------------------------------------------------------------------------


def project_to_dict(project: ProjectRecord) -> dict:
    """项目序列化(API 展示；时间已为 ISO 字符串，与既有 JSON 输出一致)。"""
    return {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "status": project.status,
        "owner_id": project.owner_id,
        "currency": project.currency,
        "project_baseline": {
            "resolution": project.baseline_resolution,
            "leap_year": project.baseline_leap_year,
            "scenario_mode": project.baseline_scenario_mode,
        },
        "schema_version": project.schema_version,
        "current_draft_id": project.current_draft_id,
        "current_version_id": project.current_version_id,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "created_by": project.created_by,
    }


def draft_to_dict(draft: DraftRecord) -> dict:
    """草稿摘要序列化。"""
    return {
        "id": draft.id,
        "revision": draft.revision,
        "content_object_id": draft.content_object_id,
        "parent_draft_id": draft.parent_draft_id,
        "updated_by": draft.updated_by,
        "updated_at": draft.updated_at,
        "created_at": draft.created_at,
    }


def get_current_draft_content(db: Session, project_id: int) -> dict:
    """读取当前草稿内容文档(命令簿记不外泄), 供只读聚合单元(校验/快照)复用。

    项目不存在抛 NotFoundError; 缺少当前草稿视为数据损坏(与 require_current_draft 一致)。
    """
    project = require_project(db, project_id)
    draft = require_current_draft(db, project)
    content = load_content_object(db, draft.content_object_id)
    content.pop("applied_commands", None)
    return content


def _load_content_bytes(db: Session, content_object_id: int) -> bytes:
    """按对象 id 读取内容字节(对象缺失/损坏抛 AppError，供字节级一致性比对)。

    IO 经 storage 公开门面，错误构造经 project 域纯函数。
    """
    try:
        return get_object(db, content_object_id)
    except NotFoundError as exc:
        raise corrupt_error(
            "内容对象缺失(数据损坏)", object_id=content_object_id
        ) from exc
    except ObjectCorruptError as exc:
        raise corrupt_error(
            "内容对象读取失败(数据损坏)", object_id=content_object_id
        ) from exc


def store_content_object(db: Session, content: dict) -> int:
    """内容字典 → 对象存储对象, 返回对象 id(草稿内容写入方的统一入口)。

    每次写入新建对象行(无内容去重), 对象清理由存储运维负责。
    IO 经 storage 公开门面（put_object+attach），编码经 project 域纯函数。
    """
    handle = put_object(
        db,
        content_to_bytes(content),
        "application/json",
        source_category="project_content",
    )
    attach(
        db,
        handle.id,
        "draft_content",
        handle.id,
        ref_entity_type="drafts",
        purpose="草稿内容文档",
    )
    return handle.id


def load_content_object(db: Session, content_object_id: int) -> dict:
    """按对象 id 读取内容对象(对象缺失/损坏抛 AppError)。

    IO 经 storage 公开门面，解析与错误构造经 project 域纯函数。
    """
    return parse_content_object(_load_content_bytes(db, content_object_id))


def version_to_dict(version: ProjectVersionRecord) -> dict:
    """版本序列化(API 展示；时间已为 ISO 字符串，与既有 JSON 输出一致)。"""
    return {
        "id": version.id,
        "project_id": version.project_id,
        "version_no": version.version_no,
        "name": version.name,
        "description": version.description,
        "created_by": version.created_by,
        "created_at": version.created_at,
        "parent_version_id": version.parent_version_id,
        "source_draft_id": version.source_draft_id,
        "source_draft_revision": version.source_draft_revision,
        "reason": version.reason,
        "project_baseline": {
            "resolution": version.baseline_resolution,
            "leap_year": version.baseline_leap_year,
            "scenario_mode": version.baseline_scenario_mode,
        },
        "currency": version.currency,
        "schema_version": version.schema_version,
        "content_object_id": version.content_object_id,
    }


def _deep_merge(base: dict, patch: dict) -> None:
    """递归合并补丁到基础字典(值为 dict 时继续下钻, 其余覆盖)。"""
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _version_content(db: Session, project: ProjectRecord, content: dict) -> dict:
    """版本内容 = 草稿领域内容(去命令簿记) + 项目固化字段(domain-model §项目聚合)。

    固化字段: 币种、项目计算基线、以及**财务三件套/规划配置引用**(0.6.5
    条目 2, 目标四闭合): 版本自包含 Effective 与规划配置引用(profile_id /
    revision), 不存业务文本摘要, 历史版本不随当前配置解释;
    项目未生成有效财务快照时 finance 块省略
    (与无配置项目包同语义, 不静默默认)。
    """
    version_content = {k: v for k, v in content.items() if k != "applied_commands"}
    version_content["currency"] = project.currency
    version_content["project_baseline"] = {
        "resolution": project.baseline_resolution,
        "leap_year": project.baseline_leap_year,
        "scenario_mode": project.baseline_scenario_mode,
    }
    # 财务三件套引用闭合: 版本固化当前 Effective 血缘(profile_id +
    # Effective revision 显式引用), 装配/财务计算只消费该快照。
    from iesplan.core.errors import NotFoundError
    from iesplan.services.config_revisions import get_effective_finance_config

    try:
        effective, effective_revision, _ = get_effective_finance_config(db, project.id)
    except NotFoundError:
        effective = None
        effective_revision = None
    if effective is not None:
        version_content["effective_finance"] = {
            "profile_id": effective.profile_id,
            "revision": effective_revision,
        }
    # 规划配置引用闭合: 版本固化当前规划配置 revision(规划行本身
    # 已指向被固化 Effective)。文本只校验字头, 不存业务文本摘要。
    from iesplan.services.config_revisions import get_planning_config

    try:
        _, planning_revision, _ = get_planning_config(db, project.id)
    except NotFoundError:
        planning_revision = None
    if planning_revision is not None:
        version_content["planning_config"] = {
            "revision": planning_revision,
        }
    return version_content


def _audit(
    db: Session,
    entity_type: str,
    entity_id: int,
    action: str,
    actor_id: int,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    """审计事件写入(与业务写入同事务，架构宪法 §16/domain-model §身份、权限和审计；只含脱敏元数据)。"""
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


__all__ = [
    "apply_result",
    "create_project_version",
    "current_version_matches_draft",
    "draft_to_dict",
    "get_current_draft_content",
    "load_content_object",
    "project_to_dict",
    "replace_project_model_refs",
    "require_current_draft",
    "require_project",
    "require_version",
    "restore_version",
    "store_content_object",
    "version_to_dict",
]
