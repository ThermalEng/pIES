"""项目访问控制(U02)与项目/草稿/版本服务(U03)。

对应 RPD 第 3.2/5/17.2/20 节与 01-db-schema.md 第 2、3 节。

设计约定:
- 草稿内容(模型/布局/数据集绑定/计算配置/语言/受控扩展清单)以规范化 JSON 文档
  表示, 按内容寻址落盘(settings.data_dir/objects/<oid[:2]>/<oid>.json),
  objects 表(经 iesplan.storage 公开门面)登记元数据; drafts.content_hash /
  project_versions.content_hash 即内容校验值(sha256)。
  对象清理与配额维护属于存储运维单元职责。
  (完整集成后, 模型/数据集/配置的权威数据在 U04/U05/U06 表中; 本实现以内容文档
  作为 U03 阶段自包含的契约载体, 跨单元提交由编排层统一完成。)
- 草稿修订为追加式: 每次领域修改在同一事务内新建 revision+1 的 Draft 行
  (旧行置 is_current=false, 01 §3.2), 内容写入与修订递增严格同事务(21.4)。
- project_versions / version_refs 仅 INSERT(不可变, 01 §3.3/§3.4)。
- 审计事件(audit_log)与业务写入同事务写入(21.4)。
- 本层服务不主动 commit, 事务边界由 API 层(请求级)控制; 抛出
  IntegrityError(如并发修订冲突)后调用方须回滚会话。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from iesplan.core.diagnostics import SEVERITY_ERROR, SYS_STORE_CORRUPT
from iesplan.core.errors import AppError, ConflictError, ForbiddenError, NotFoundError
from iesplan.core.idgen import sha256_hex
from iesplan.core.jsonutil import canonical_json, jsonable
from iesplan.models.audit import AuditLog
from iesplan.models.calc import Task
from iesplan.models.identity import User
from iesplan.models.project import (
    Draft,
    Project,
    ProjectVersion,
    VersionRef,
)

# ---------------------------------------------------------------------------
# 错误类型
# ---------------------------------------------------------------------------


class InvalidRequestError(AppError):
    """请求/草稿命令校验失败(HTTP 400)。

    code 为 U03 域内稳定标识(不入全局诊断目录, 前端按 message_key 渲染文案)。
    """

    code = "PROJ-CMD-001"
    http_status = 400
    severity = SEVERITY_ERROR
    message_key = "ies.diag.param.invalid"


# ---------------------------------------------------------------------------
# 访问控制(U02)
# ---------------------------------------------------------------------------

#: 所有者能力集(0.8.0: 剔除共享成员/转移所有权后, 项目权限以 projects.owner_id
#: 为唯一权威; 共享走"导出项目包 → 他人导入"流程, 包内不携带账号权限)。
OWNER_CAPABILITIES: frozenset[str] = frozenset(
    {"view", "edit", "manage_lifecycle", "export_package", "export_excel"}
)


def get_role(db: Session, user: User, project_id: int) -> str | None:
    """返回用户在项目中的角色: 'owner'(项目所有者) | None(非所有者)。

    0.8.0 起不存在 viewer/成员授权: 非所有者一律无项目访问能力
    (管理员除外, 见 ensure_access)。
    """
    project = db.get(Project, project_id)
    if project is None:
        return None
    return "owner" if project.owner_id == user.id else None


def _is_admin(db: Session, user: User) -> bool:
    """用户是否持有全局 admin 角色(委托 identity 的权威判定)。"""
    from iesplan.services import identity

    return identity.has_role(db, user, "admin")


def ensure_access(db: Session, user: User, project_id: int, *capabilities: str) -> None:
    """访问判定(U02, RPD 20.2): 用户必须同时具备全部请求能力, 否则 ForbiddenError。

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
# 项目服务(U03): 生命周期
# ---------------------------------------------------------------------------


def create_project(
    db: Session,
    user: User,
    name: str,
    currency: str = "CNY",
    utc_offset_minutes: int = 480,
    description: str | None = None,
    language: str | None = None,
) -> Project:
    """创建项目: 创建者即所有者, 同事务创建初始草稿(revision=1, 01 §3.1/§3.2)。

    名称全局唯一(01 §3.1 uq_projects_name), 冲突抛 ConflictError。
    """
    name = (name or "").strip()
    if not name:
        raise InvalidRequestError("项目名称不能为空", code="PROJ-CMD-001")
    if currency not in ("CNY", "USD"):
        raise InvalidRequestError("币种仅支持 CNY/USD", params={"currency": currency})
    if not -720 <= utc_offset_minutes <= 840:
        raise InvalidRequestError(
            "UTC 偏移必须在 -720~840 分钟之间", params={"utc_offset_minutes": utc_offset_minutes}
        )
    lang = language or getattr(user, "locale", None) or "zh-CN"
    project = Project(
        name=name,
        description=description,
        status="active",
        owner_id=user.id,
        currency=currency,
        fixed_utc_offset_minutes=utc_offset_minutes,
        schema_version=1,
        created_by=user.id,
    )
    db.add(project)
    try:
        db.flush()
    except IntegrityError as exc:
        raise ConflictError("已存在同名项目", params={"name": name}) from exc
    # 初始草稿(revision=1), 与项目创建同事务(所有者以 projects.owner_id 记录)
    _new_draft_row(db, project, _initial_content(lang), user)
    _audit(
        db, "project", project.id, "project.created", user.id,
        after={
            "name": name, "currency": currency, "utc_offset_minutes": utc_offset_minutes,
            "owner_id": user.id,
        },
    )
    db.flush()
    return project


def get_project_view(db: Session, user: User, project_id: int) -> dict:
    """项目视图: 项目 + 草稿摘要(含内容) + 版本列表(RPD 5.1)。"""
    ensure_access(db, user, project_id, "view")
    project = _get_project(db, project_id)
    draft = _get_current_draft(db, project)
    content = _load_draft_content(db, draft)
    content.pop("applied_commands", None)  # 命令簿记不外泄
    versions = list_versions(db, project_id)
    return {
        "project": project_to_dict(project),
        "draft": {**draft_to_dict(draft), "content": content},
        "versions": [version_to_dict(v) for v in versions],
        "my_role": get_role(db, user, project_id),
    }


def list_visible_projects(db: Session, user: User) -> list[dict]:
    """我的项目列表(仅所有者, 不含已删除)。

    0.8.0 剔除共享成员: 项目只属于所有者; 共享通过项目包导出/导入完成。
    """
    rows = db.execute(
        select(Project)
        .where(
            Project.owner_id == user.id,
            Project.status != "deleted",
        )
        .order_by(Project.created_at.desc())
    ).scalars().all()
    return [{**project_to_dict(p), "my_role": "owner"} for p in rows]


def list_all_projects(db: Session) -> list[dict]:
    """全部项目整体视图(管理员管理入口): 含已删除, 仅整体管理字段。

    不含草稿内容/版本等细节(管理员经维护入口只读访问)。
    """
    projects = db.execute(
        select(Project).order_by(Project.created_at.desc())
    ).scalars().all()
    return [project_to_dict(p) for p in projects]


def archive_project(db: Session, user: User, project_id: int) -> Project:
    """归档项目(归档后不可编辑/提交计算, 只读, RPD 5.3)。"""
    ensure_access(db, user, project_id, "manage_lifecycle")
    project = _get_project(db, project_id)
    if project.status != "archived":
        project.status = "archived"
        project.updated_at = datetime.now(UTC)
        db.flush()
        _audit(db, "project", project_id, "project.archived", user.id, after={"status": "archived"})
    return project


def unarchive_project(db: Session, user: User, project_id: int) -> Project:
    """撤销归档(恢复为 active, RPD 5.3)。"""
    ensure_access(db, user, project_id, "manage_lifecycle")
    project = _get_project(db, project_id)
    if project.status != "active":
        project.status = "active"
        project.updated_at = datetime.now(UTC)
        db.flush()
        _audit(db, "project", project_id, "project.unarchived", user.id, after={"status": "active"})
    return project


def delete_project(
    db: Session,
    user: User,
    project_id: int,
    confirm: bool = False,
    name: str | None = None,
    reason: str | None = None,
) -> None:
    """删除项目(RPD 5.3: 确认 → 取消排队任务 → 一致性检查 → 硬删除)。

    - 0.2.0 B4 误操作防护: 必须提供 ``name``(与项目名精确匹配)或 ``reason``
      (非空删除原因)之一; 单独 ``confirm: true`` 不足以确认(参数保留仅为
      兼容旧调用方, 不单独作为确认条件);
    - 排队/取消中任务置为 cancelled; 存在运行中任务时阻断删除(终止运行任务由
      U07 任务单元负责, 本阶段以冲突提示要求先终止);
    - 项目置 status='deleted'(01 §3.1 软删), 无回收站语义; 不可变版本与审计
      记录保留, 对象清理由 U16 运维单元重试执行。
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
            "删除项目须输入项目名或删除原因", code="PROJ-DEL-002",
            params={"project_id": project_id},
        )
    if name is not None and (name or "").strip() != project.name:
        raise InvalidRequestError(
            "输入的项目名与待删除项目不一致", code="PROJ-DEL-003",
            params={"project_id": project_id},
        )
    now = datetime.now(UTC)
    # 取消排队/取消中的任务(删除协调, RPD 5.3 第 2 步)
    cancellable = db.execute(
        select(Task).where(
            Task.project_id == project_id,
            Task.status.in_(("queued", "cancelling")),
        )
    ).scalars().all()
    for task in cancellable:
        task.status = "cancelled"
        task.updated_at = now
    # 一致性检查: 运行中任务阻断删除(第 3 步)
    running = db.execute(
        select(Task.id).where(Task.project_id == project_id, Task.status == "running").limit(1)
    ).first()
    if running is not None:
        raise ConflictError("项目存在运行中的计算任务, 无法删除", params={"project_id": project_id})
    # 硬删除: 置 deleted(第 5 步)
    project.status = "deleted"
    project.updated_at = now
    db.flush()
    _audit(
        db, "project", project_id, "project.deleted", user.id,
        after={
            "status": "deleted",
            "confirm": "name" if (name or "").strip() else "reason",
            "reason": (reason or "").strip()[:200] or None,
        },
    )


# ---------------------------------------------------------------------------
# 项目服务(U03): 草稿修订
# ---------------------------------------------------------------------------

#: 草稿命令类型 → 处理函数(语义命令, RPD 20.3)
_COMMAND_HANDLERS: dict[str, Any] = {}


def update_draft(
    db: Session,
    user: User,
    project_id: int,
    commands: list[dict],
    expected_revision: int,
) -> dict:
    """应用草稿语义命令(RPD 20.3, 唯一写入单元 U03)。

    每个命令至少包含: id(幂等命令标识)/project_id/expected_revision/session
    (发起窗口会话, 校验由 U01 负责)/unit(唯一写入单元)/type/payload。
    语义:
    - 乐观锁: 当前修订 != expected_revision 且存在未应用命令 → ConflictError;
      全部命令已应用(整批重试) → 返回原结果且不再递增修订(幂等)。
    - 领域内容修改与新 Draft 行(revision+1)在同一事务内完成(01 §3.2)。
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
    content = _load_draft_content(db, draft)
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

    try:
        new_draft = _new_draft_row(db, project, content, user)
    except IntegrityError as exc:
        raise ConflictError(
            "草稿修订冲突(并发编辑), 请重新加载后再试",
            params={"revision": draft.revision + 1},
        ) from exc
    _audit(
        db, "project", project.id, "project.draft_updated", user.id,
        after={
            "revision": new_draft.revision,
            "previous_revision": draft.revision,
            "command_ids": [r["command_id"] for r in results if r["status"] == "applied"],
        },
    )
    return {"revision": new_draft.revision, "results": results}


def _already_applied(applied: dict, cmd: Any) -> bool:
    return (
        isinstance(cmd, dict)
        and isinstance(cmd.get("id"), str)
        and cmd["id"] in applied
    )


def _idempotent_result(cid: str, record: dict) -> dict:
    """幂等重试结果: 返回命令首次应用时的原始结果(20.3 相同命令重试返回原结果)。"""
    return {
        "command_id": cid,
        "status": "idempotent",
        "revision": record.get("revision"),
        "result": record.get("result"),
    }


def _validate_command_scope(cmd: dict, project_id: int) -> None:
    """校验命令携带的项目标识与唯一写入单元(20.3)。"""
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
    """upsert 设备实例(模型内容, U04 内容的契约表示; 布局位置入 layout 节)。"""
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
        c for c in content["model"]["connections"]
        if name not in (c.get("from_device"), c.get("to_device"))
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
    """布局补丁(布局是显示事实, 不改变工程语义, RPD 20.5)。"""
    _deep_merge(content["layout"], payload)
    return {"stored": True}


def _cmd_dataset_bind(content: dict, payload: dict) -> dict:
    """绑定数据集版本(U05 内容的契约表示)。"""
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
    """计算配置补丁(参数/目标/约束/容差等, U06 内容的契约表示)。"""
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
    """设置项目语言(版本固化语言, RPD 20.4)。"""
    lang = payload.get("language")
    if lang not in ("zh-CN", "en"):
        raise InvalidRequestError("project.set_language 仅支持 zh-CN/en", code="PROJ-CMD-005")
    content["language"] = lang
    return {"language": lang, "stored": True}


def _cmd_project_set_extensions(content: dict, payload: dict) -> dict:
    """更新受控扩展清单(扩展校验由 SEC 域负责, 此处仅登记声明)。"""
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
# 项目服务(U03): 版本
# ---------------------------------------------------------------------------


def create_version(
    db: Session,
    user: User,
    project_id: int,
    name: str,
    description: str | None = None,
    reason: str = "manual_save",
    parent_version_id: int | None = None,
    source_result_id: str | None = None,
) -> ProjectVersion:
    """从当前草稿创建不可变项目版本(RPD 20.4 / REQ-PROJ-002)。

    快照内容: 模型/布局/数据集绑定/计算配置/语言/币种/UTC 偏移/受控扩展清单/
    模式版本/内容校验; 父版本缺省取项目当前版本; source_result_id(应用结果的
    来源结果标识)记入审计。project_versions 仅 INSERT(不可变)。
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
    content = _load_draft_content(db, draft)
    version_content = _version_content(project, content)
    content_hash = _store_content(db, version_content)

    if parent_version_id is not None:
        parent_id = get_version(db, project_id, parent_version_id).id
    else:
        parent_id = project.current_version_id
    version_no = _next_version_no(db, project_id)
    version = ProjectVersion(
        project_id=project_id,
        version_no=version_no,
        name=name,
        description=description,
        created_by=user.id,
        parent_version_id=parent_id,
        source_draft_id=draft.id,
        source_draft_revision=draft.revision,
        reason=reason,
        fixed_utc_offset_minutes=project.fixed_utc_offset_minutes,
        currency=project.currency,
        schema_version=project.schema_version,
        content_hash=content_hash,
    )
    db.add(version)
    db.flush()
    # 版本引用清单: 内容对象引用(版本自包含, 01 §3.4)
    obj = _get_object_by_oid(db, content_hash)
    db.add(
        VersionRef(
            project_version_id=version.id,
            ref_type="object",
            object_id=obj["id"],
            ref_key="project_version_content",
            ref_hash=content_hash,
        )
    )
    project.current_version_id = version.id
    project.updated_at = datetime.now(UTC)
    db.flush()
    _audit(
        db, "project_version", version.id, "project.version_created", user.id,
        after={
            "project_id": project.id,
            "version_no": version_no,
            "name": name,
            "reason": reason,
            "parent_version_id": parent_id,
            "source_draft_revision": draft.revision,
            "source_result_id": source_result_id,
        },
    )
    return version


def current_version_matches_draft(db: Session, project: Project) -> bool:
    """当前版本内容是否与当前草稿一致(按版本固化规则比较)。

    版本内容 = 草稿领域内容(去命令簿记) + 项目固化字段(RPD 20.4);
    草稿仅在命令簿记(applied_commands)上推进而无领域变更时视为一致。
    无当前版本返回 False(需固化)。用于任务提交时判断是否需重新固化,
    避免草稿已修改而任务仍运行旧版本输入。
    """
    if project.current_version_id is None:
        return False
    version = db.get(ProjectVersion, project.current_version_id)
    if version is None:
        return False
    draft = _get_current_draft(db, project)
    content = _load_draft_content(db, draft)
    raw = canonical_json(_version_content(project, content))
    return sha256_hex(raw.encode("utf-8")) == version.content_hash


def get_version(db: Session, project_id: int, version_id: int) -> ProjectVersion:
    """按 id 获取项目版本(须属于该项目, 否则 404)。"""
    version = db.get(ProjectVersion, version_id)
    if version is None or version.project_id != project_id:
        raise NotFoundError(
            "版本不存在",
            params={"project_id": project_id, "version_id": version_id},
            location={"object_type": "project_version", "object_id": version_id},
        )
    return version


def list_versions(db: Session, project_id: int) -> list[ProjectVersion]:
    """版本列表(新版本在前)。"""
    return db.execute(
        select(ProjectVersion)
        .where(ProjectVersion.project_id == project_id)
        .order_by(ProjectVersion.version_no.desc())
    ).scalars().all()


def restore_version(
    db: Session,
    user: User,
    project_id: int,
    version_id: int,
    name: str | None = None,
    description: str | None = None,
) -> dict:
    """恢复历史版本: 创建新版本 + 新草稿, 不倒写历史(REQ-PROJ-002)。

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
    content = _load_content_by_hash(db, source.content_hash)
    # 恢复内容中的命令簿记清空(新修订从干净状态开始; 与版本内容保持一致)
    content.pop("applied_commands", None)
    new_draft = _new_draft_row(db, project, content, user)
    version = create_version(
        db, user, project_id,
        name=name or f"恢复: {source.name}",
        description=description,
        reason="restore",
        parent_version_id=source.id,
    )
    _audit(
        db, "project_version", version.id, "project.version_restored", user.id,
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
    user: User,
    project_id: int,
    diff_patch: dict,
    *,
    version_id: int | None = None,
    name: str | None = None,
    description: str | None = None,
    source_result_id: str | None = None,
) -> dict:
    """应用选定规划结果(RPD 10.1 / 20.12 / REQ-PROJ-001)。

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
    content = _load_draft_content(db, draft)
    # 参数差异补丁应用到新草稿内容(原版本不变);
    # diff_patch 直接作用于 calc_config 节, 含 "calc_config" 键时取其值
    inner = diff_patch.get("calc_config")
    patch = inner if isinstance(inner, dict) else diff_patch
    if not isinstance(patch, dict):
        raise InvalidRequestError("diff_patch 内容非法", code="PROJ-CMD-005")
    _deep_merge(content["calc_config"], patch)
    content.pop("applied_commands", None)
    new_draft = _new_draft_row(db, project, content, user)
    version = create_version(
        db, user, project_id,
        name=name or "应用结果",
        description=description,
        reason="apply_result",
        parent_version_id=source.id,
        source_result_id=source_result_id,
    )
    _audit(
        db, "project_version", version.id, "project.result_applied", user.id,
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


def project_to_dict(project: Project) -> dict:
    """项目序列化(API 展示)。"""
    return {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "status": project.status,
        "owner_id": project.owner_id,
        "currency": project.currency,
        "fixed_utc_offset_minutes": project.fixed_utc_offset_minutes,
        "schema_version": project.schema_version,
        "current_draft_id": project.current_draft_id,
        "current_version_id": project.current_version_id,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "created_by": project.created_by,
    }


def draft_to_dict(draft: Draft) -> dict:
    """草稿摘要序列化。"""
    return {
        "id": draft.id,
        "revision": draft.revision,
        "content_hash": draft.content_hash,
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
    content = _load_draft_content(db, draft)
    content.pop("applied_commands", None)
    return content


def initial_content(language: str = "zh-CN") -> dict:
    """初始草稿内容骨架(空模型/布局/绑定/配置 + 空受控扩展清单)。

    供校验/模型等只读或写入方初始化内容文档(与 _initial_content 同构)。
    """
    return _initial_content(language)


def store_content_object(db: Session, content: dict) -> str:
    """内容字典 → 内容寻址对象, 返回 content_hash(草稿内容写入方的统一入口)。

    相同内容的重复写入按 oid 去重并递增引用计数(对象清理由 U11/U16 负责);
    与 _store_content 一致, 供校验/模型等单元复用, 避免各写入方自行落盘。
    """
    return _store_content(db, content)


def load_content_object(db: Session, content_hash: str) -> dict:
    """按内容校验值读取内容对象(对象缺失/哈希不符抛 AppError)。"""
    return _load_content_by_hash(db, content_hash)


def version_to_dict(version: ProjectVersion) -> dict:
    """版本序列化(API 展示)。"""
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
        "fixed_utc_offset_minutes": version.fixed_utc_offset_minutes,
        "currency": version.currency,
        "schema_version": version.schema_version,
        "content_hash": version.content_hash,
    }


def require_project(db: Session, project_id: int) -> Project:
    """按 id 取项目; 不存在或已删除(软删)一律 404(无回收站语义)。"""
    return _get_project(db, project_id)


def _get_project(db: Session, project_id: int) -> Project:
    """按 id 取项目; 不存在或已删除(软删)一律 404(无回收站语义)。"""
    project = db.get(Project, project_id)
    if project is None or project.status == "deleted":
        raise NotFoundError(
            "项目不存在",
            params={"project_id": project_id},
            location={"object_type": "project", "object_id": project_id},
        )
    return project


def get_current_draft(db: Session, project: Project) -> Draft:
    """取项目当前草稿(is_current=true 且修订最大者); 缺失视为数据损坏。"""
    return _get_current_draft(db, project)


def _get_current_draft(db: Session, project: Project) -> Draft:
    """取项目当前草稿(is_current=true 且修订最大者)。"""
    draft = db.execute(
        select(Draft)
        .where(Draft.project_id == project.id, Draft.is_current.is_(True))
        .order_by(Draft.revision.desc())
        .limit(1)
    ).scalar_one_or_none()
    if draft is None:
        raise AppError(
            "项目缺少当前草稿(数据损坏)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
            location={"object_type": "project", "object_id": project.id},
        )
    return draft


def _new_draft_row(db: Session, project: Project, content: dict, user: User) -> Draft:
    """追加新草稿行(修订 = max(revision)+1, 与内容写入同一事务, 01 §3.2)。

    旧当前草稿置 is_current=false; 更新项目 current_draft_id 指针。
    """
    content_hash = _store_content(db, content)
    old = db.execute(
        select(Draft)
        .where(Draft.project_id == project.id, Draft.is_current.is_(True))
        .order_by(Draft.revision.desc())
        .limit(1)
    ).scalar_one_or_none()
    if old is not None:
        old.is_current = False
    max_revision = db.execute(
        select(func.max(Draft.revision)).where(Draft.project_id == project.id)
    ).scalar()
    draft = Draft(
        project_id=project.id,
        revision=(max_revision or 0) + 1,
        content_hash=content_hash,
        parent_draft_id=old.id if old is not None else None,
        is_current=True,
        updated_by=user.id,
    )
    db.add(draft)
    db.flush()
    project.current_draft_id = draft.id
    project.updated_at = datetime.now(UTC)
    return draft


def _next_version_no(db: Session, project_id: int) -> int:
    """项目内版本号单调递增(01 §3.3)。"""
    max_no = db.execute(
        select(func.max(ProjectVersion.version_no)).where(ProjectVersion.project_id == project_id)
    ).scalar()
    return (max_no or 0) + 1


def _initial_content(language: str = "zh-CN") -> dict:
    """初始草稿内容(空模型/布局/绑定/配置 + 空受控扩展清单)。"""
    return {
        "schema_version": 1,
        "language": language,
        "unit_system": "si",
        "extensions": {},
        "model": {"devices": [], "ports": [], "connections": []},
        "layout": {},
        "dataset_bindings": [],
        "calc_config": {
            "params": {},
            "variables": [],
            "objectives": [],
            "constraints": [],
            "algorithm": None,
            "solver": None,
            "tolerances": {},
            "random_seed": None,
        },
        "applied_commands": {},
    }


def _version_content(project: Project, content: dict) -> dict:
    """版本内容 = 草稿领域内容(去命令簿记) + 项目固化字段(RPD 20.4)。"""
    version_content = {k: v for k, v in content.items() if k != "applied_commands"}
    version_content["currency"] = project.currency
    version_content["fixed_utc_offset_minutes"] = project.fixed_utc_offset_minutes
    return version_content


# ---------------------------------------------------------------------------
# 内容寻址对象存储(草稿/版本内容载体; 实现经 iesplan.storage 公开门面, STO-01)
# ---------------------------------------------------------------------------


def _store_content(db: Session, content: dict) -> str:
    """规范化 JSON → 内容寻址对象(STO-01: 经 storage 公开门面), 返回 content_hash。

    相同内容的重复写入按 sha256 去重(对象行复用, owner 引用仍单独建立)。
    storage_path 的解释/分桶/临时文件全部由 iesplan.storage 内部实现,
    本模块不拼路径、不导入 StoredObject ORM。
    """
    from iesplan.storage import put_object

    raw = canonical_json(content)
    content_hash = sha256_hex(raw.encode("utf-8"))
    put_object(
        db, raw.encode("utf-8"), "application/json",
        source_category="project_content",
        ref_type="draft_content", ref_id=content_hash, ref_entity_type="drafts",
        purpose="草稿内容文档(内容寻址)",
    )
    return content_hash


def _load_content_by_hash(db: Session, content_hash: str) -> dict:
    """按内容校验值读取内容对象并校验(对象缺失/哈希不符视为数据损坏)。

    STO-01: 经 storage 公开门面读取(读取时校验大小 + sha256)。
    """
    from iesplan.storage import ObjectCorruptError, get_object

    try:
        raw = get_object(db, content_hash)
    except NotFoundError as exc:
        raise AppError(
            "内容对象缺失(数据损坏)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
            location={"object_type": "object", "object_id": content_hash},
        ) from exc
    except ObjectCorruptError as exc:
        raise AppError(
            "内容对象读取失败(数据损坏)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
            location={"object_type": "object", "object_id": content_hash},
        ) from exc
    if sha256_hex(raw) != content_hash:
        raise AppError(
            "内容校验失败(数据损坏)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
            location={"object_type": "object", "object_id": content_hash},
        )
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AppError(
            "内容对象解析失败(数据损坏)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
        ) from exc
    if not isinstance(parsed, dict):
        raise AppError(
            "内容对象结构非法(数据损坏)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
        )
    return parsed


def _load_draft_content(db: Session, draft: Draft) -> dict:
    """读取草稿内容文档(按草稿 content_hash 取内容对象)。"""
    return _load_content_by_hash(db, draft.content_hash)


def _get_object_by_oid(db: Session, oid: str) -> dict:
    """按内容校验值解析对象元数据(STO-01: 经 storage 公开门面, 返回句柄 dict)。"""
    from iesplan.storage import object_info

    handle = object_info(db, oid)
    if handle is None:
        raise AppError(
            "内容对象缺失(数据损坏)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
            location={"object_type": "object", "object_id": oid},
        )
    return handle


def _audit(
    db: Session,
    entity_type: str,
    entity_id: int,
    action: str,
    actor_id: int,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    """审计事件写入(与业务写入同事务, 21.4; 只含脱敏元数据, 13.2)。"""
    db.add(
        AuditLog(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            actor_id=actor_id,
            actor_type="user",
            before=jsonable(before) if before else None,
            after=jsonable(after) if after else None,
        )
    )


__all__ = [
    "InvalidRequestError",
    "ROLE_CAPABILITIES",
    "ensure_access",
    "get_role",
    "maintenance_access",
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
    "version_to_dict",
    "list_all_projects",
]
