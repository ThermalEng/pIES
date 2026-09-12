"""项目生命周期用例(Wave 2 W2-A: application/projects)。

项目增删改查/启用归档/状态设置/草稿修订编排（旧 ``services.project`` 已删除）：
访问控制、错误类型、序列化与 require_* 读取经 project 域公开门面
（``iesplan.project`` 唯一实现，本模块不保留复制）；版本编排见
``application.projects.versions``。

事务：写用例顶层函数拥有提交/回滚（``db.commit`` 收尾，失败 ``db.rollback``）；
内部步骤只 ``flush``，由顶层统一。读用例不提交事务。

调用方向：``api → application.projects.lifecycle → {project, identity,
tasks, audit} 域公开门面 + content_objects 用例``；不导入 ORM、不导入
其他域内部模块、不调用 ``services.*``。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy.orm import Session

from iesplan import audit as audit_domain
from iesplan import identity as identity_domain
from iesplan import project as project_domain
from iesplan import tasks as tasks_domain
from iesplan.application.projects.content_objects import (
    load_content_object,
    merge_patch,
    store_content_object,
)
from iesplan.core.contracts import ProjectBaseline, ProjectBaselineError
from iesplan.core.errors import ConflictError, ForbiddenError
from iesplan.core.jsonutil import jsonable
from iesplan.identity.contracts import UserRecord
from iesplan.project import (
    InvalidRequestError,
    OWNER_CAPABILITIES,
    draft_to_dict,
    ensure_access,
    get_role,
    project_to_dict,
    require_current_draft,
    require_project,
    version_to_dict,
)
from iesplan.project.contracts import (
    DraftRecord,
    ProjectConflictError,
    ProjectRecord,
)

# ---------------------------------------------------------------------------
# 访问控制（唯一实现归属 project 域 access 模块；本模块只消费公开门面）
# ---------------------------------------------------------------------------


def is_admin(db: Session, user: UserRecord) -> bool:
    """用户是否持有全局 admin 角色(经 identity 域公开门面判定，不直接查表)。

    供管理端整体视图入口使用（API 迁移 Wave 3 后直调 identity 域门面）。
    """
    return "admin" in identity_domain.user_roles(db, user.id)


# ---------------------------------------------------------------------------
# 项目用例: 生命周期（内部实现只 flush；公开写用例负责 commit/rollback）
# ---------------------------------------------------------------------------


def _create_project(
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
    if is_admin(db, user):
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
    """事务型创建项目；application 层统一提交或回滚。"""
    try:
        project = _create_project(
            db,
            user,
            name,
            currency,
            baseline_resolution=baseline_resolution,
            baseline_leap_year=baseline_leap_year,
            baseline_scenario_mode=baseline_scenario_mode,
            description=description,
            language=language,
        )
        db.commit()
        return project
    except Exception:
        db.rollback()
        raise


def get_project_view(db: Session, user: UserRecord, project_id: int) -> dict:
    """项目视图: 项目 + 草稿摘要(含内容) + 版本列表(domain-model §项目聚合)。"""
    ensure_access(db, user, project_id, "view")
    project = require_project(db, project_id)
    draft = require_current_draft(db, project)
    content = _load_draft_content(db, draft)
    content.pop("applied_commands", None)  # 命令簿记不外泄
    versions = project_domain.list_versions(db, project_id)
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
    if is_admin(db, user):
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


def _archive_project(db: Session, user: UserRecord, project_id: int) -> ProjectRecord:
    """归档项目(归档后不可编辑/提交计算, 只读，domain-model §项目聚合)。"""
    ensure_access(db, user, project_id, "manage_lifecycle")
    project = require_project(db, project_id)
    if project.status != "archived":
        project = project_domain.set_project_status(db, project_id, "archived")
        _audit(db, "project", project_id, "project.archived", user.id, after={"status": "archived"})
    return project


def archive_project(db: Session, user: UserRecord, project_id: int) -> ProjectRecord:
    """事务型归档项目；application 层统一提交或回滚。"""
    try:
        project = _archive_project(db, user, project_id)
        db.commit()
        return project
    except Exception:
        db.rollback()
        raise


def _unarchive_project(db: Session, user: UserRecord, project_id: int) -> ProjectRecord:
    """撤销归档(恢复为 active，domain-model §项目聚合)。"""
    ensure_access(db, user, project_id, "manage_lifecycle")
    project = require_project(db, project_id)
    if project.status != "active":
        project = project_domain.set_project_status(db, project_id, "active")
        _audit(db, "project", project_id, "project.unarchived", user.id, after={"status": "active"})
    return project


def unarchive_project(db: Session, user: UserRecord, project_id: int) -> ProjectRecord:
    """事务型撤销归档；application 层统一提交或回滚。"""
    try:
        project = _unarchive_project(db, user, project_id)
        db.commit()
        return project
    except Exception:
        db.rollback()
        raise


def _delete_project(
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
    project = require_project(db, project_id)
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
    # 取消排队/取消中的任务(删除协调；tasks 表归属 tasks 域，经 tasks 域公开门面)
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


def delete_project(
    db: Session,
    user: UserRecord,
    project_id: int,
    confirm: bool = False,
    name: str | None = None,
    reason: str | None = None,
) -> None:
    """事务型删除项目；application 层统一提交或回滚。"""
    try:
        _delete_project(db, user, project_id, confirm=confirm, name=name, reason=reason)
        db.commit()
    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------------------------
# 项目用例: 草稿修订（唯一写入：项目草稿）
# ---------------------------------------------------------------------------

#: 草稿命令类型 → 处理函数(语义命令，见架构宪法 §4.4/§12、domain-model §项目聚合)
_COMMAND_HANDLERS: dict[str, Any] = {}


def _update_draft(
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
    project = require_project(db, project_id)
    if project.status != "active":
        raise ConflictError(
            "项目已归档或已删除, 不能编辑",
            location={"object_type": "project", "object_id": project_id},
        )
    if not isinstance(commands, list):
        raise InvalidRequestError("commands 必须是数组", code="PROJ-CMD-001")
    draft = require_current_draft(db, project)
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


def update_draft(
    db: Session,
    user: UserRecord,
    project_id: int,
    commands: list[dict],
    expected_revision: int,
) -> dict:
    """事务型草稿修订；application 层统一提交或回滚。"""
    try:
        result = _update_draft(db, user, project_id, commands, expected_revision)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


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
    merge_patch(content["layout"], payload)
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
    merge_patch(content["calc_config"], payload)
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
    merge_patch(content["extensions"], ext)
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


# ---------------------------------------------------------------------------
# 内容读取（序列化唯一实现归属 project 域 versions 模块，经公开门面消费）
# ---------------------------------------------------------------------------


def get_current_draft_content(db: Session, project_id: int) -> dict:
    """读取当前草稿内容文档(命令簿记不外泄)。

    项目不存在抛 NotFoundError; 缺少当前草稿视为数据损坏。
    """
    project = require_project(db, project_id)
    draft = require_current_draft(db, project)
    content = _load_draft_content(db, draft)
    content.pop("applied_commands", None)
    return content


def _load_draft_content(db: Session, draft: DraftRecord) -> dict:
    """读取草稿内容文档(按草稿 content_object_id 取内容对象，经 content_objects 用例)。"""
    return load_content_object(db, draft.content_object_id)


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
    "OWNER_CAPABILITIES",
    "ensure_access",
    "get_role",
    "is_admin",
    "create_project",
    "get_project_view",
    "list_visible_projects",
    "list_all_projects",
    "project_count_by_owner",
    "archive_project",
    "unarchive_project",
    "delete_project",
    "update_draft",
    "project_to_dict",
    "draft_to_dict",
    "version_to_dict",
    "get_current_draft_content",
]
