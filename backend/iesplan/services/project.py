"""项目访问控制与项目/草稿/版本服务。

依据架构宪法 §4.9/§12 与 domain-model §项目聚合（草稿/版本）及 §对象生命周期：

设计约定:
- 草稿内容(模型/布局/数据集绑定/计算配置/语言/受控扩展清单)以规范化 JSON 文档
  表示, 经 iesplan.storage 公开门面落盘为对象存储对象(按对象 id 寻址);
  drafts / project_versions 只持有明确的 content_object_id 外键(对象引用),
  历史定位使用稳定 ID + revision, 不存储、不比对内容摘要。
  对象清理与配额维护属于存储运维职责（架构宪法 §10）。
  (模型/数据集/配置的权威数据由对应领域模块持久化；本层以内容文档
  作为草稿阶段自包含的契约载体，跨模块提交由 application 编排层统一完成。)
- 草稿修订为追加式: 每次领域修改在同一事务内新建 revision+1 的 Draft 行
  (旧行置 is_current=false, domain-model §项目聚合)，内容写入与修订递增严格同事务。
- project_versions / version_refs 仅 INSERT（不可变版本，domain-model §项目聚合）。
- 审计事件(audit_log)与业务写入同事务写入（domain-model §身份、权限和审计）。
- 本层服务不主动 commit, 事务边界由 API 层(请求级)控制; 抛出
  IntegrityError(如并发修订冲突)后调用方须回滚会话。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy.orm import Session

from iesplan import audit as audit_domain
from iesplan import project as project_domain
from iesplan import tasks as tasks_domain
from iesplan.core.contracts import ProjectBaseline, ProjectBaselineError
from iesplan.core.diagnostics import SEVERITY_ERROR, SYS_STORE_CORRUPT
from iesplan.core.errors import AppError, ConflictError, ForbiddenError, NotFoundError
from iesplan.core.jsonutil import canonical_json, jsonable
from iesplan.identity.contracts import UserRecord
from iesplan.project.contracts import (
    DraftRecord,
    ProjectConflictError,
    ProjectRecord,
    ProjectVersionRecord,
)
from iesplan.storage import ObjectCorruptError, attach, get_object, put_object

# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class InvalidRequestError(AppError):
    """请求/草稿命令校验失败(HTTP 400)。

    code 为项目域内稳定标识(前端按 message_key 渲染文案，见 contracts §成功与错误)。
    """

    code = "PROJ-CMD-001"
    http_status = 400
    severity = SEVERITY_ERROR
    message_key = "ies.diag.param.invalid"


# ---------------------------------------------------------------------------
# 访问控制
# ---------------------------------------------------------------------------

#: 所有者能力集(项目权限以 projects.owner_id 为唯一权威；共享走“导出项目包 → 他人导入”流程，包内不携带账号权限，见 domain-model §项目聚合/§对象生命周期)。
OWNER_CAPABILITIES: frozenset[str] = frozenset(
    {"view", "edit", "manage_lifecycle", "export_package", "export_excel"}
)


def get_role(db: Session, user: UserRecord, project_id: int) -> str | None:
    """返回用户在项目中的角色: 'owner'(项目所有者) | None(非所有者)。

    项目权限以 owner_id 为唯一权威，非所有者无项目访问能力（管理员除外，见 ensure_access；依据 domain-model §身份、权限和审计、架构宪法 §16）。
    """
    project = project_domain.get_project(db, project_id)
    if project is None:
        return None
    return "owner" if project.owner_id == user.id else None


def _is_admin(db: Session, user: UserRecord) -> bool:
    """用户是否持有全局 admin 角色(委托 identity 的权威判定)。"""
    from iesplan.application import identity

    return identity.has_role(db, user, "admin")


def ensure_access(db: Session, user: UserRecord, project_id: int, *capabilities: str) -> None:
    """访问判定(架构宪法 §16、domain-model §身份、权限和审计)：用户必须同时具备全部请求能力，否则 ForbiddenError。

    - 仅项目所有者具备全部业务能力;
    - 管理员(全局 admin 角色)始终可查看项目细节与管理生命周期(删除/归档),
      不得业务编辑;
    - 项目不存在或已删除一律按 NotFoundError(不泄露项目存在性细节)。
    """
    _get_project(db, project_id)  # 存在性检查: 不存在/已删除 → 404
    granted = set(OWNER_CAPABILITIES) if get_role(db, user, project_id) == "owner" else set()
    if _is_admin(db, user):
        # 管理员始终可管理项目整体生命周期(删除/归档), 无需授权
        granted |= {"view", "manage_lifecycle"}
    missing = [cap for cap in capabilities if cap not in granted]
    if missing:
        raise ForbiddenError(
            "缺少所需项目权限",
            params={"required": list(capabilities), "missing": missing, "project_id": project_id},
            location={"object_type": "project", "object_id": project_id},
        )


# ---------------------------------------------------------------------------
# 项目服务: 生命周期
# ---------------------------------------------------------------------------


def create_project(
    db: Session,
    user: UserRecord,
    name: str,
    currency: str = "CNY",
    *,
    baseline_resolution: str,
    baseline_leap_year: bool,
    baseline_scenario_mode: str,
    description: str | None = None,
    language: str | None = None,
) -> ProjectRecord:
    """创建项目: 创建者即所有者, 同事务创建初始草稿(revision=1，domain-model §项目聚合)。

    业务规则: 管理员不持有业务项目(仅负责账号与系统管理), 创建一律拒绝
    (403, PERM-DENIED-001 标准信封); 普通工程师行为不变。
    名称全局唯一(uq_projects_name), 冲突抛 ConflictError。
    项目计算基线(0.6.5 事项 1): 创建时必须显式提供 resolution/leap_year/
    scenario_mode 三字段, 缺失或非法一律拒绝(PROJ-BASE-001), 不静默使用
    默认值; 摘要经 ``core.contracts.ProjectBaseline`` 确定性计算。默认值
    (1h/非闰年/single)只用于迁移对存量项目的回填, 不用于新项目创建。
    基线创建后无任何更新入口, 数据库层另有不可变触发器(Postgres)。
    """
    if _is_admin(db, user):
        raise ForbiddenError(
            "管理员不持有项目, 不可创建项目",
            params={"role": "admin"},
        )
    name = (name or "").strip()
    if not name:
        raise InvalidRequestError("项目名称不能为空", code="PROJ-CMD-001")
    if currency not in ("CNY", "USD"):
        raise InvalidRequestError("币种仅支持 CNY/USD", params={"currency": currency})
    if not isinstance(baseline_leap_year, bool):
        raise InvalidRequestError(
            "项目计算基线必须显式提供 leap_year(布尔值)",
            code="PROJ-BASE-001",
            params={"baseline_leap_year": baseline_leap_year},
        )
    try:
        baseline = ProjectBaseline(
            resolution=baseline_resolution,
            leap_year=baseline_leap_year,
            scenario_mode=baseline_scenario_mode,
        )
    except ProjectBaselineError as exc:
        raise InvalidRequestError(str(exc), code="PROJ-BASE-001", params={"baseline": str(exc)}) from exc
    lang = language or getattr(user, "locale", None) or "zh-CN"
    # 项目裸行经领域 repository 创建（重名抛 ProjectConflictError，系 ConflictError 子类）；
    # 初始草稿(revision=1)随后补建，与项目创建同事务(所有者以 projects.owner_id 记录)
    project = project_domain.create_project(
        db,
        name=name,
        owner_id=user.id,
        created_by=user.id,
        description=description,
        currency=currency,
        baseline_resolution=baseline.resolution,
        baseline_leap_year=baseline.leap_year,
        baseline_scenario_mode=baseline.scenario_mode,
    )
    content_object_id = store_content_object(db, project_domain.initial_content(lang))
    project_domain.create_draft(
        db,
        project_id=project.id,
        content_object_id=content_object_id,
        updated_by=user.id,
    )
    _audit(
        db,
        "project",
        project.id,
        "project.created",
        user.id,
        after={
            "name": name,
            "currency": currency,
            "project_baseline": baseline.to_dict(),
            "owner_id": user.id,
        },
    )
    db.flush()
    return project


def get_project_view(db: Session, user: UserRecord, project_id: int) -> dict:
    """项目视图: 项目 + 草稿摘要(含内容) + 版本列表(domain-model §项目聚合)。"""
    ensure_access(db, user, project_id, "view")
    project = _get_project(db, project_id)
    draft = _get_current_draft(db, project)
    content = load_content_object(db, draft.content_object_id)
    content.pop("applied_commands", None)  # 命令簿记不外泄
    versions = list_versions(db, project_id)
    return {
        "project": project_to_dict(project),
        "draft": {**draft_to_dict(draft), "content": content},
        "versions": [version_to_dict(v) for v in versions],
        "my_role": get_role(db, user, project_id),
    }


def list_visible_projects(db: Session, user: UserRecord, status: str | None = None) -> list[dict]:
    """我的项目列表(仅所有者, 不含已删除; 支持按状态筛选)。

    项目只属于所有者；共享通过项目包导出/导入完成（domain-model §项目聚合/§对象生命周期）。
    业务规则: 管理员不持有业务项目(仅负责账号与系统管理), 一律返回空列表;
    普通工程师可按状态筛选(status: 'active' 进行中 / 'archived' 已归档 /
    None 全部未删除), 筛选在数据库查询中完成。
    """
    if _is_admin(db, user):
        return []
    statuses = [status] if status in ("active", "archived") else ["active", "archived"]
    page = project_domain.list_projects(db, owner_id=user.id, statuses=statuses)
    return [{**project_to_dict(p), "my_role": "owner"} for p in page.items]


def list_all_projects(db: Session) -> list[dict]:
    """全部项目整体视图(管理员管理入口): 含已删除, 仅整体管理字段。

    不含草稿内容/版本等细节(管理员经维护入口只读访问)。
    """
    page = project_domain.list_projects(db)
    return [project_to_dict(p) for p in page.items]


def project_count_by_owner(db: Session, owner_ids: Sequence[int]) -> dict[int, int]:
    """项目数量 read model: owner_id -> 未删除项目数(admin 用户列表等跨领域消费)。

    统计口径(与账号管理展示一致): 该用户拥有的 active + archived 项目,
    deleted 一律排除; 无项目或缺省不在 owner_ids 中的用户返回 0。
    实现为一次 GROUP BY 聚合查询(单条 SQL, 防 N+1): 用户数量增加时
    查询数量保持 1 不变。
    数据库故障沿用统一错误处理(异常向上传播, 不在此吞掉转为 0)。
    """
    return project_domain.count_projects_by_owner(db, owner_ids)


def archive_project(db: Session, user: UserRecord, project_id: int) -> ProjectRecord:
    """归档项目(归档后不可编辑/提交计算, 只读，domain-model §项目聚合)。"""
    ensure_access(db, user, project_id, "manage_lifecycle")
    project = _get_project(db, project_id)
    if project.status != "archived":
        project = project_domain.set_project_status(db, project_id, "archived")
        _audit(db, "project", project_id, "project.archived", user.id, after={"status": "archived"})
    return project


def unarchive_project(db: Session, user: UserRecord, project_id: int) -> ProjectRecord:
    """撤销归档(恢复为 active，domain-model §项目聚合)。"""
    ensure_access(db, user, project_id, "manage_lifecycle")
    project = _get_project(db, project_id)
    if project.status != "active":
        project = project_domain.set_project_status(db, project_id, "active")
        _audit(db, "project", project_id, "project.unarchived", user.id, after={"status": "active"})
    return project


def delete_project(
    db: Session,
    user: UserRecord,
    project_id: int,
    confirm: bool = False,
    name: str | None = None,
    reason: str | None = None,
) -> None:
    """删除项目(确认 → 取消排队任务 → 一致性检查 → 归档/删除，domain-model §项目聚合/§对象生命周期)。

    - 误操作防护: 必须提供 ``name``(与项目名精确匹配)或 ``reason``
      (非空删除原因)之一; 单独 ``confirm: true`` 不足以确认(参数保留仅为
      兼容旧调用方, 不单独作为确认条件);
    - 排队/取消中任务置为 cancelled; 存在运行中任务时阻断删除(终止运行任务由
      任务单元负责, 本阶段以冲突提示要求先终止);
    - 项目置 status='deleted'(软删，无回收站语义); 不可变版本与审计
      记录保留, 对象清理由存储运维重试执行（架构宪法 §10/§12）。
    """
    ensure_access(db, user, project_id, "manage_lifecycle")
    project = _get_project(db, project_id)
    if not confirm:
        raise InvalidRequestError(
            "删除项目必须显式确认", code="PROJ-DEL-001", params={"project_id": project_id}
        )
    provided = (name or "").strip() or (reason or "").strip()
    if not provided:
        raise InvalidRequestError(
            "删除项目须输入项目名或删除原因",
            code="PROJ-DEL-002",
            params={"project_id": project_id},
        )
    if name is not None and (name or "").strip() != project.name:
        raise InvalidRequestError(
            "输入的项目名与待删除项目不一致",
            code="PROJ-DEL-003",
            params={"project_id": project_id},
        )
    # 取消排队/取消中的任务(删除协调；tasks 表归属 tasks 域)
    tasks_domain.cancel_pending_tasks(db, project_id)
    # 一致性检查: 运行中任务阻断删除
    if tasks_domain.has_running_tasks(db, project_id):
        raise ConflictError("项目存在运行中的计算任务, 无法删除", params={"project_id": project_id})
    # 置 deleted(软删，无回收站语义)
    project_domain.set_project_status(db, project_id, "deleted")
    _audit(
        db,
        "project",
        project_id,
        "project.deleted",
        user.id,
        after={
            "status": "deleted",
            "confirm": "name" if (name or "").strip() else "reason",
            "reason": (reason or "").strip()[:200] or None,
        },
    )


# ---------------------------------------------------------------------------
# 项目服务: 草稿修订
# ---------------------------------------------------------------------------

#: 草稿命令类型 → 处理函数(语义命令，见架构宪法 §4.4/§12、domain-model §项目聚合)
_COMMAND_HANDLERS: dict[str, Any] = {}


def update_draft(
    db: Session,
    user: UserRecord,
    project_id: int,
    commands: list[dict],
    expected_revision: int,
) -> dict:
    """应用草稿语义命令(架构宪法 §4.4 assembly/§12、domain-model §项目聚合，唯一写入：项目草稿)。

    每个命令至少包含: id(幂等命令标识)/project_id/expected_revision/session
    (发起窗口会话)/unit(唯一写入单元)/type/payload。
    语义:
    - 乐观锁: 当前修订 != expected_revision 且存在未应用命令 → ConflictError;
      全部命令已应用(整批重试) → 返回原结果且不再递增修订(幂等)。
    - 领域内容修改与新 Draft 行(revision+1)在同一事务内完成(domain-model §项目聚合)。
    返回 {"revision": 新修订号, "results": [每命令结果]}。
    """
    ensure_access(db, user, project_id, "edit")
    project = _get_project(db, project_id)
    if project.status != "active":
        raise ConflictError(
            "项目已归档或已删除, 不能编辑",
            location={"object_type": "project", "object_id": project_id},
        )
    if not isinstance(commands, list):
        raise InvalidRequestError("commands 必须是数组", code="PROJ-CMD-001")
    draft = _get_current_draft(db, project)
    content = load_content_object(db, draft.content_object_id)
    applied = content.setdefault("applied_commands", {})

    # 幂等重试: 当前修订已推进且整批命令均已应用 → 返回原结果
    if draft.revision != expected_revision:
        if commands and all(_already_applied(applied, cmd) for cmd in commands):
            results = [
                _idempotent_result(cmd["id"], applied[cmd["id"]])
                for cmd in commands
                if isinstance(cmd, dict) and isinstance(cmd.get("id"), str) and cmd["id"] in applied
            ]
            return {"revision": draft.revision, "results": results}
        raise ConflictError(
            "草稿修订冲突: 预期修订与当前修订不一致",
            params={"expected_revision": expected_revision, "current_revision": draft.revision},
            location={"object_type": "draft", "object_id": draft.id},
        )

    results: list[dict] = []
    changed = False
    for cmd in commands:
        if not isinstance(cmd, dict) or not isinstance(cmd.get("id"), str) or not cmd["id"]:
            raise InvalidRequestError("命令缺少幂等标识(id)", code="PROJ-CMD-004")
        cid = cmd["id"]
        if cid in applied:
            results.append(_idempotent_result(cid, applied[cid]))
            continue
        _validate_command_scope(cmd, project_id)
        result = _apply_command(content, cmd)
        applied[cid] = {"revision": draft.revision + 1, "result": jsonable(result)}
        changed = True
        results.append(
            {
                "command_id": cid,
                "status": "applied",
                "revision": draft.revision + 1,
                "result": jsonable(result),
            }
        )

    if not changed:
        return {"revision": draft.revision, "results": results}

    content_object_id = store_content_object(db, content)
    try:
        new_draft = project_domain.create_draft(
            db,
            project_id=project.id,
            content_object_id=content_object_id,
            updated_by=user.id,
        )
    except ProjectConflictError as exc:
        raise ConflictError(
            "草稿修订冲突(并发编辑), 请重新加载后再试",
            params={"revision": draft.revision + 1},
        ) from exc
    _audit(
        db,
        "project",
        project.id,
        "project.draft_updated",
        user.id,
        after={
            "revision": new_draft.revision,
            "previous_revision": draft.revision,
            "command_ids": [r["command_id"] for r in results if r["status"] == "applied"],
        },
    )
    return {"revision": new_draft.revision, "results": results}


def _already_applied(applied: dict, cmd: Any) -> bool:
    return isinstance(cmd, dict) and isinstance(cmd.get("id"), str) and cmd["id"] in applied


def _idempotent_result(cid: str, record: dict) -> dict:
    """幂等重试结果: 返回命令首次应用时的原始结果(相同命令重试返回原结果，domain-model §项目聚合)。"""
    return {
        "command_id": cid,
        "status": "idempotent",
        "revision": record.get("revision"),
        "result": record.get("result"),
    }


def _validate_command_scope(cmd: dict, project_id: int) -> None:
    """校验命令携带的项目标识与唯一写入单元(架构宪法 §4.9、domain-model §项目聚合)。"""
    pid = cmd.get("project_id")
    if pid is not None and pid != project_id:
        raise InvalidRequestError(
            "命令中的项目标识与目标项目不一致",
            code="PROJ-CMD-001",
            params={"project_id": pid, "expected": project_id},
        )
    if not isinstance(cmd.get("unit"), str) or not cmd["unit"]:
        raise InvalidRequestError("命令缺少唯一写入单元(unit)", code="PROJ-CMD-001")


def _apply_command(content: dict, cmd: dict) -> dict:
    """按命令类型分派并应用到草稿内容文档(返回 JSON 安全的结果摘要)。"""
    ctype = cmd.get("type")
    if not isinstance(ctype, str) or "." not in ctype:
        raise InvalidRequestError("命令类型非法", code="PROJ-CMD-002", params={"type": ctype})
    prefix, _rest = ctype.split(".", 1)
    if cmd.get("unit") != prefix:
        raise InvalidRequestError(
            "命令唯一写入单元与命令类型前缀不一致",
            code="PROJ-CMD-002",
            params={"unit": cmd.get("unit"), "type": ctype},
        )
    payload = cmd.get("payload")
    if not isinstance(payload, dict):
        raise InvalidRequestError("命令负载必须是对象", code="PROJ-CMD-005")
    handler = _COMMAND_HANDLERS.get(ctype)
    if handler is None:
        raise InvalidRequestError("不支持的命令类型", code="PROJ-CMD-003", params={"type": ctype})
    return handler(content, payload)


def _cmd_model_upsert_device(content: dict, payload: dict) -> dict:
    """upsert 设备实例(模型内容契约表示；布局位置入 layout 节，domain-model §项目聚合)。"""
    name = payload.get("name")
    if not isinstance(name, str) or not name:
        raise InvalidRequestError("model.upsert_device 缺少设备名称", code="PROJ-CMD-005")
    if payload.get("kind") is not None and payload["kind"] not in ("existing", "new"):
        raise InvalidRequestError("设备 kind 仅支持 existing/new", code="PROJ-CMD-005")
    devices = content["model"]["devices"]
    device = next((d for d in devices if d.get("name") == name), None)
    if device is None:
        device = {"name": name}
        devices.append(device)
    for field in ("device_type", "kind", "model_fidelity"):
        if field in payload:
            device[field] = payload[field]
    if isinstance(payload.get("params"), dict):
        device["params"] = {**(device.get("params") or {}), **payload["params"]}
    if "position" in payload:
        content["layout"].setdefault("positions", {})[name] = payload["position"]
    return {"device": name, "stored": True}


def _cmd_model_remove_device(content: dict, payload: dict) -> dict:
    """删除设备(级联移除关联连接与布局位置)。"""
    name = payload.get("name")
    devices = content["model"]["devices"]
    if not any(d.get("name") == name for d in devices):
        raise InvalidRequestError("设备不存在", code="PROJ-CMD-005", params={"name": name})
    content["model"]["devices"] = [d for d in devices if d.get("name") != name]
    content["model"]["connections"] = [
        c for c in content["model"]["connections"] if name not in (c.get("from_device"), c.get("to_device"))
    ]
    positions = content["layout"].get("positions")
    if isinstance(positions, dict) and name in positions:
        del positions[name]
    return {"device": name, "removed": True}


def _cmd_model_upsert_connection(content: dict, payload: dict) -> dict:
    """upsert 连接(端点引用设备与端口)。"""
    required = ("from_device", "from_port", "to_device", "to_port")
    if not all(isinstance(payload.get(k), str) and payload[k] for k in required):
        raise InvalidRequestError(
            "model.upsert_connection 需要 from_device/from_port/to_device/to_port",
            code="PROJ-CMD-005",
        )
    name = payload.get("name") or (
        f"{payload['from_device']}.{payload['from_port']}->{payload['to_device']}.{payload['to_port']}"
    )
    connections = content["model"]["connections"]
    conn = next((c for c in connections if c.get("name") == name), None)
    if conn is None:
        conn = {"name": name}
        connections.append(conn)
    for key in required + ("conn_type",):
        if key in payload:
            conn[key] = payload[key]
    for key in ("capacity", "loss_rate"):
        if key in payload:
            conn[key] = payload[key]
    return {"connection": name, "stored": True}


def _cmd_model_remove_connection(content: dict, payload: dict) -> dict:
    """删除连接。"""
    name = payload.get("name")
    connections = content["model"]["connections"]
    if not any(c.get("name") == name for c in connections):
        raise InvalidRequestError("连接不存在", code="PROJ-CMD-005", params={"name": name})
    content["model"]["connections"] = [c for c in connections if c.get("name") != name]
    return {"connection": name, "removed": True}


def _cmd_layout_patch(content: dict, payload: dict) -> dict:
    """布局补丁(布局是显示事实, 不改变工程语义，架构宪法 §4.4)。"""
    _deep_merge(content["layout"], payload)
    return {"stored": True}


def _cmd_dataset_bind(content: dict, payload: dict) -> dict:
    """绑定数据集版本(domain-model §数据集/§项目聚合的契约表示)。"""
    dvid = payload.get("dataset_version_id")
    if not isinstance(dvid, int):
        raise InvalidRequestError("dataset.bind 需要整数 dataset_version_id", code="PROJ-CMD-005")
    bindings = content["dataset_bindings"]
    if any(b.get("dataset_version_id") == dvid for b in bindings):
        return {"dataset_version_id": dvid, "bound": True, "duplicate": True}
    entry = {"dataset_version_id": dvid}
    for key in ("role", "note", "dataset_id"):
        if key in payload:
            entry[key] = payload[key]
    bindings.append(entry)
    return {"dataset_version_id": dvid, "bound": True}


def _cmd_dataset_unbind(content: dict, payload: dict) -> dict:
    """解除数据集版本绑定。"""
    dvid = payload.get("dataset_version_id")
    bindings = content["dataset_bindings"]
    if not any(b.get("dataset_version_id") == dvid for b in bindings):
        raise InvalidRequestError(
            "数据集绑定不存在", code="PROJ-CMD-005", params={"dataset_version_id": dvid}
        )
    content["dataset_bindings"] = [b for b in bindings if b.get("dataset_version_id") != dvid]
    return {"dataset_version_id": dvid, "unbound": True}


def _cmd_config_patch(content: dict, payload: dict) -> dict:
    """计算配置补丁(参数/目标/约束/容差等，domain-model §规划、财务与计算配置)。"""
    _deep_merge(content["calc_config"], payload)
    return {"stored": True}


def _cmd_config_set_variable(content: dict, payload: dict) -> dict:
    """upsert 规划变量。"""
    name = payload.get("name")
    if not isinstance(name, str) or not name:
        raise InvalidRequestError("config.set_variable 缺少变量名", code="PROJ-CMD-005")
    variables = content["calc_config"]["variables"]
    variable = next((v for v in variables if v.get("name") == name), None)
    if variable is None:
        variable = {"name": name}
        variables.append(variable)
    variable.update({k: v for k, v in payload.items() if k != "name"})
    return {"variable": name, "stored": True}


def _cmd_project_set_language(content: dict, payload: dict) -> dict:
    """设置项目语言(版本固化语言，domain-model §项目聚合)。"""
    lang = payload.get("language")
    if lang not in ("zh-CN", "en"):
        raise InvalidRequestError("project.set_language 仅支持 zh-CN/en", code="PROJ-CMD-005")
    content["language"] = lang
    return {"language": lang, "stored": True}


def _cmd_project_set_extensions(content: dict, payload: dict) -> dict:
    """更新受控扩展清单(扩展校验由安全域负责, 此处仅登记声明，架构宪法 §16)。"""
    ext = payload.get("extensions")
    if not isinstance(ext, dict):
        raise InvalidRequestError("project.set_extensions 需要 extensions 对象", code="PROJ-CMD-005")
    _deep_merge(content["extensions"], ext)
    return {"stored": True}


_COMMAND_HANDLERS.update(
    {
        "model.upsert_device": _cmd_model_upsert_device,
        "model.remove_device": _cmd_model_remove_device,
        "model.upsert_connection": _cmd_model_upsert_connection,
        "model.remove_connection": _cmd_model_remove_connection,
        "layout.patch": _cmd_layout_patch,
        "dataset.bind": _cmd_dataset_bind,
        "dataset.unbind": _cmd_dataset_unbind,
        "config.patch": _cmd_config_patch,
        "config.set_variable": _cmd_config_set_variable,
        "project.set_language": _cmd_project_set_language,
        "project.set_extensions": _cmd_project_set_extensions,
    }
)


def _deep_merge(base: dict, patch: dict) -> None:
    """递归合并补丁到基础字典(值为 dict 时继续下钻, 其余覆盖)。"""
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


# ---------------------------------------------------------------------------
# 项目服务: 版本
# ---------------------------------------------------------------------------


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
    """从当前草稿创建不可变项目版本(domain-model §项目聚合/§快照、任务和结果)。

    快照内容: 模型/布局/数据集绑定/计算配置/语言/币种/UTC 偏移/受控扩展清单/
    模式版本/内容校验; 父版本缺省取项目当前版本; source_result_id(应用结果的
    来源结果标识)记入审计。project_versions 仅 INSERT(不可变，domain-model §项目聚合)。
    """
    ensure_access(db, user, project_id, "edit")
    project = _get_project(db, project_id)
    if project.status != "active":
        raise ConflictError(
            "项目已归档或已删除, 不能创建版本",
            location={"object_type": "project", "object_id": project_id},
        )
    name = (name or "").strip()
    if not name:
        raise InvalidRequestError("版本名称不能为空", code="PROJ-CMD-001")
    draft = _get_current_draft(db, project)
    content = load_content_object(db, draft.content_object_id)
    version_content = _version_content(db, project, content)
    content_object_id = store_content_object(db, version_content)

    if parent_version_id is not None:
        parent_id = get_version(db, project_id, parent_version_id).id
    else:
        parent_id = None
    # 版本行 + 内容引用行 + current 指针移动经领域 repository 同事务完成
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


def current_version_matches_draft(db: Session, project: ProjectRecord) -> bool:
    """当前版本内容是否与当前草稿一致(按版本固化规则比较，domain-model §项目聚合)。

    版本内容 = 草稿领域内容(去命令簿记) + 项目固化字段;
    草稿仅在命令簿记(applied_commands)上推进而无领域变更时视为一致。
    无当前版本返回 False(需固化)。用于任务提交时判断是否需重新固化,
    避免草稿已修改而任务仍运行旧版本输入。
    """
    if project.current_version_id is None:
        return False
    version = project_domain.get_version(db, project.id, project.current_version_id)
    if version is None:
        return False
    draft = _get_current_draft(db, project)
    content = load_content_object(db, draft.content_object_id)
    expected = canonical_json(_version_content(db, project, content)).encode("utf-8")
    stored = _load_content_bytes(db, version.content_object_id)
    return stored == expected


def get_version(db: Session, project_id: int, version_id: int) -> ProjectVersionRecord:
    """按 id 获取项目版本(须属于该项目, 否则 404)。"""
    version = project_domain.get_version(db, project_id, version_id)
    if version is None:
        raise NotFoundError(
            "版本不存在",
            params={"project_id": project_id, "version_id": version_id},
            location={"object_type": "project_version", "object_id": version_id},
        )
    return version


def list_versions(db: Session, project_id: int) -> list[ProjectVersionRecord]:
    """版本列表(新版本在前)。"""
    return project_domain.list_versions(db, project_id)


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
    project = _get_project(db, project_id)
    if project.status != "active":
        raise ConflictError(
            "项目已归档或已删除, 不能恢复版本",
            location={"object_type": "project", "object_id": project_id},
        )
    source = get_version(db, project_id, version_id)
    content = load_content_object(db, source.content_object_id)
    # 恢复内容中的命令簿记清空(新修订从干净状态开始; 与版本内容保持一致)
    content.pop("applied_commands", None)
    content_object_id = store_content_object(db, content)
    new_draft = project_domain.create_draft(
        db,
        project_id=project.id,
        content_object_id=content_object_id,
        updated_by=user.id,
    )
    version = create_version(
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
    project = _get_project(db, project_id)
    if project.status != "active":
        raise ConflictError(
            "项目已归档或已删除, 不能应用结果",
            location={"object_type": "project", "object_id": project_id},
        )
    source = get_version(db, project_id, version_id) if version_id is not None else None
    if source is None:
        if project.current_version_id is None:
            raise NotFoundError("项目尚无版本, 无法应用结果", params={"project_id": project_id})
        source = get_version(db, project_id, project.current_version_id)
    if not isinstance(diff_patch, dict):
        raise InvalidRequestError("diff_patch 必须是对象", code="PROJ-CMD-005")
    draft = _get_current_draft(db, project)
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
    new_draft = project_domain.create_draft(
        db,
        project_id=project.id,
        content_object_id=content_object_id,
        updated_by=user.id,
    )
    version = create_version(
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
# 序列化与内部工具
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

    项目不存在抛 NotFoundError; 缺少当前草稿视为数据损坏(与 _get_current_draft 一致)。
    """
    project = _get_project(db, project_id)
    draft = _get_current_draft(db, project)
    content = load_content_object(db, draft.content_object_id)
    content.pop("applied_commands", None)
    return content


def initial_content(language: str = "zh-CN") -> dict:
    """初始草稿内容骨架(实现归属 project 域，此处为调用方兼容委托)。

    供校验/模型等只读或写入方初始化内容文档。
    """
    return project_domain.initial_content(language)


def _load_content_bytes(db: Session, content_object_id: int) -> bytes:
    """按对象 id 读取内容字节(对象缺失/损坏抛 AppError，供字节级一致性比对)。

    IO 经 storage 公开门面，错误构造经 project 域纯函数。
    """
    try:
        return get_object(db, content_object_id)
    except NotFoundError as exc:
        raise project_domain.corrupt_error(
            "内容对象缺失(数据损坏)", object_id=content_object_id
        ) from exc
    except ObjectCorruptError as exc:
        raise project_domain.corrupt_error(
            "内容对象读取失败(数据损坏)", object_id=content_object_id
        ) from exc


def store_content_object(db: Session, content: dict) -> int:
    """内容字典 → 对象存储对象, 返回对象 id(草稿内容写入方的统一入口)。

    每次写入新建对象行(无内容去重), 对象清理由存储运维负责。
    IO 经 storage 公开门面（put_object+attach），编码经 project 域纯函数。
    """
    handle = put_object(
        db,
        project_domain.content_to_bytes(content),
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
    return project_domain.parse_content_object(_load_content_bytes(db, content_object_id))


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


def require_project(db: Session, project_id: int) -> ProjectRecord:
    """按 id 取项目; 不存在或已删除(软删)一律 404(无回收站语义)。"""
    return _get_project(db, project_id)


def _get_project(db: Session, project_id: int) -> ProjectRecord:
    """按 id 取项目; 不存在或已删除(软删)一律 404(无回收站语义)。"""
    project = project_domain.get_project(db, project_id)
    if project is None:
        raise NotFoundError(
            "项目不存在",
            params={"project_id": project_id},
            location={"object_type": "project", "object_id": project_id},
        )
    return project


def get_current_draft(db: Session, project: ProjectRecord) -> DraftRecord:
    """取项目当前草稿(is_current=true 且修订最大者); 缺失视为数据损坏。"""
    return _get_current_draft(db, project)


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
    project = _get_project(db, project_id)
    draft = _get_current_draft(db, project)
    if draft.revision != expected_revision:
        raise ConflictError(
            "项目草稿已被其他操作更新",
            params={"expected_revision": expected_revision, "current_revision": draft.revision},
            location={"object_type": "draft", "object_id": str(draft.id)},
        )
    content = load_content_object(db, draft.content_object_id)
    content["project_models"] = refs
    content_object_id = store_content_object(db, content)
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


def _get_current_draft(db: Session, project: ProjectRecord) -> DraftRecord:
    """取项目当前草稿(is_current=true 且修订最大者); 缺失视为数据损坏。"""
    draft = project_domain.get_current_draft(db, project.id)
    if draft is None:
        raise AppError(
            "项目缺少当前草稿(数据损坏)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
            location={"object_type": "project", "object_id": project.id},
        )
    return draft


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
    from iesplan.services.config_revisions import get_effective_finance_config
    from iesplan.core.errors import NotFoundError

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


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 对象存储写入(草稿/版本内容载体; 实现经 iesplan.storage 公开门面，架构宪法 §10)
# ---------------------------------------------------------------------------


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
    "InvalidRequestError",
    "ensure_access",
    "get_role",
    "create_project",
    "get_project_view",
    "list_visible_projects",
    "archive_project",
    "unarchive_project",
    "delete_project",
    "update_draft",
    "create_version",
    "get_version",
    "list_versions",
    "restore_version",
    "apply_result",
    "project_to_dict",
    "draft_to_dict",
    "replace_project_model_refs",
    "version_to_dict",
    "list_all_projects",
    "project_count_by_owner",
]
