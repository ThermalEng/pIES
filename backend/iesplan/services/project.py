"""项目访问控制(U02)与项目/草稿/版本服务(U03)。

对应 RPD 第 3.2/5/17.2/20 节与 01-db-schema.md 第 2、3 节。

设计约定:
- 草稿内容(模型/布局/数据集绑定/计算配置/语言/受控扩展清单)以规范化 JSON 文档
  表示, 按内容寻址落盘(settings.data_dir/objects/<oid[:2]>/<oid>.json),
  objects 表(StoredObject)登记元数据; drafts.content_hash /
  project_versions.content_hash 即内容校验值(sha256)。
  对象清理与配额维护属于 U11/U16 运维单元职责。
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
from decimal import Decimal
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from iesplan.config import settings
from iesplan.core.diagnostics import SEVERITY_ERROR, SYS_STORE_CORRUPT
from iesplan.core.errors import AppError, ConflictError, ForbiddenError, NotFoundError
from iesplan.core.idgen import sha256_hex
from iesplan.models.audit import AuditLog, StoredObject
from iesplan.models.calc import Task
from iesplan.models.identity import Role, User, UserRole
from iesplan.models.project import (
    Draft,
    OwnershipTransfer,
    Project,
    ProjectMember,
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

#: 能力矩阵: 项目角色 → 能力集合(RPD 3.2 / 20.2)
ROLE_CAPABILITIES: dict[str, set[str]] = {
    "owner": {
        "view", "edit", "manage_members", "manage_lifecycle", "transfer",
        "duplicate", "export_package", "export_excel",  # owner 覆盖 viewer 全部能力
    },
    "viewer": {"view", "export_excel"},
    # 管理员经维护入口只读访问(不含任何业务编辑能力)
    "maintenance_admin": {"view", "maintenance"},
}


def get_role(db: Session, user: User, project_id: int) -> str | None:
    """返回用户在项目中的当前角色: 'owner' | 'viewer' | None(非成员, 01 §2.1)。

    有效成员判定(M-01): revoked_at 为空 且 (expires_at 为空 或 未过期);
    临时授权到期后视同无权限。
    """
    member = _current_member(db, project_id, user.id)
    return member.role if member is not None else None


def _is_admin(db: Session, user: User) -> bool:
    """用户是否持有全局 admin 角色(经 user_roles 当前有效授权)。"""
    row = db.execute(
        select(Role.id)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(
            UserRole.user_id == user.id,
            UserRole.revoked_at.is_(None),
            Role.code == "admin",
        )
        .limit(1)
    ).first()
    return row is not None


def ensure_access(db: Session, user: User, project_id: int, *capabilities: str) -> None:
    """访问判定(U02, RPD 20.2): 用户必须同时具备全部请求能力, 否则 ForbiddenError。

    - owner 具备全部业务能力; viewer 只读; 管理员(全局 admin 角色)经维护入口
      只读访问(view + maintenance)。
    - 项目不存在或已删除一律按 NotFoundError(不泄露项目存在性细节)。
    """
    _get_project(db, project_id)  # 存在性检查: 不存在/已删除 → 404
    granted = set(ROLE_CAPABILITIES.get(get_role(db, user, project_id) or "", set()))
    if _is_admin(db, user):
        granted |= ROLE_CAPABILITIES["maintenance_admin"]
    missing = [cap for cap in capabilities if cap not in granted]
    if missing:
        raise ForbiddenError(
            "缺少所需项目权限",
            params={"required": list(capabilities), "missing": missing, "project_id": project_id},
            location={"object_type": "project", "object_id": project_id},
        )


def maintenance_access(db: Session, user: User, project_id: int) -> bool:
    """管理员维护只读访问判定(U02, RPD 3.2): 管理员只读查看, 不得业务编辑。"""
    return _is_admin(db, user)


def add_viewer(db: Session, user: User, project_id: int, target_user_id: int) -> ProjectMember:
    """添加查看者(仅所有者, 追加式授权, 01 §2.1)。"""
    ensure_access(db, user, project_id, "manage_members")
    _get_project(db, project_id)
    target = db.get(User, target_user_id)
    if target is None:
        raise NotFoundError("目标用户不存在", params={"user_id": target_user_id})
    existing = _current_member(db, project_id, target_user_id)
    if existing is not None:
        raise ConflictError("该用户已是项目成员", params={"user_id": target_user_id, "role": existing.role})
    auth_version = _next_auth_version(db, project_id)
    now = datetime.now(UTC)
    member = ProjectMember(
        project_id=project_id,
        user_id=target_user_id,
        role="viewer",
        auth_version=auth_version,
        granted_by=user.id,
        granted_at=now,
    )
    db.add(member)
    db.flush()
    _audit(
        db, "project", project_id, "project.viewer_added", user.id,
        after={"user_id": target_user_id, "role": "viewer", "auth_version": auth_version},
    )
    return member


def remove_viewer(db: Session, user: User, project_id: int, target_user_id: int) -> None:
    """移除查看者(仅所有者; 撤销置 revoked_at, 追加式, 01 §2.1)。"""
    ensure_access(db, user, project_id, "manage_members")
    member = _current_member(db, project_id, target_user_id)
    if member is None:
        raise NotFoundError("目标用户不是项目查看者", params={"user_id": target_user_id})
    if member.role == "owner":
        raise ConflictError("不能移除项目所有者", params={"user_id": target_user_id})
    member.revoked_at = datetime.now(UTC)
    member.revoked_by = user.id
    db.flush()
    _audit(
        db, "project", project_id, "project.viewer_removed", user.id,
        after={"user_id": target_user_id, "role": "viewer"},
    )


def transfer_ownership(db: Session, user: User, project_id: int, target_user_id: int) -> Project:
    """转移项目所有权(仅所有者, 原所有者默认成为查看者, RPD 3.2)。

    在 ownership_transfers 记录一次性 completed 转移, 同事务内:
    撤销原 owner → 授予新 owner → 原所有者追加 viewer 行; 项目始终至少一个 owner。
    """
    ensure_access(db, user, project_id, "transfer")
    project = _get_project(db, project_id)
    target = db.get(User, target_user_id)
    if target is None:
        raise NotFoundError("目标用户不存在", params={"user_id": target_user_id})
    if target.status != "active":
        raise ConflictError(
            "目标用户未启用, 不能接收所有权",
            params={"user_id": target_user_id, "status": target.status},
        )
    if target.is_system:
        raise ConflictError(
            "目标用户是系统账号, 不能接收所有权", params={"user_id": target_user_id}
        )
    if target_user_id == project.owner_id:
        raise ConflictError("目标用户已是项目所有者", params={"user_id": target_user_id})

    from_user_id = project.owner_id  # 原所有者(审计与撤销使用, 先于 owner_id 变更)
    auth_version = _next_auth_version(db, project_id)
    now = datetime.now(UTC)
    # 目标用户若原为查看者, 先撤销其查看者行, 再授予 owner
    target_member = _current_member(db, project_id, target_user_id)
    if target_member is not None:
        target_member.revoked_at = now
        target_member.revoked_by = user.id
    # 原所有者: 撤销 owner 行 + 追加 viewer 行
    owner_member = _current_member(db, project_id, from_user_id)
    if owner_member is not None:
        owner_member.revoked_at = now
        owner_member.revoked_by = user.id
    db.add(
        ProjectMember(
            project_id=project_id, user_id=target_user_id, role="owner",
            auth_version=auth_version, granted_by=user.id, granted_at=now,
        )
    )
    db.add(
        ProjectMember(
            project_id=project_id, user_id=from_user_id, role="viewer",
            auth_version=auth_version, granted_by=user.id, granted_at=now,
        )
    )
    # 转移审计记录(01 §2.2)
    db.add(
        OwnershipTransfer(
            project_id=project_id,
            from_user_id=from_user_id,
            to_user_id=target_user_id,
            status="completed",
            transfer_version=auth_version,
            proposed_by=user.id,
            proposed_at=now,
            decided_by=user.id,
            decided_at=now,
            completed_at=now,
        )
    )
    project.owner_id = target_user_id
    project.updated_at = now
    db.flush()
    _audit(
        db, "project", project_id, "project.transferred", user.id,
        before={"from_user_id": from_user_id},
        after={"to_user_id": target_user_id, "auth_version": auth_version},
    )
    return project


def list_members(db: Session, project_id: int) -> list[dict]:
    """当前有效成员清单(供成员管理界面展示)。"""
    rows = db.execute(
        select(ProjectMember)
        .where(ProjectMember.project_id == project_id, ProjectMember.revoked_at.is_(None))
        .order_by(ProjectMember.id)
    ).scalars().all()
    return [
        {
            "user_id": m.user_id,
            "role": m.role,
            "auth_version": m.auth_version,
            "granted_at": m.granted_at,
        }
        for m in rows
    ]


def _current_member(db: Session, project_id: int, user_id: int) -> ProjectMember | None:
    """当前有效成员行(M-01): revoked_at 为空 且 (expires_at 为空 或 未过期)。"""
    member = db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
            ProjectMember.revoked_at.is_(None),
        )
    ).scalar_one_or_none()
    if member is None:
        return None
    if member.expires_at is not None and _as_utc(member.expires_at) < datetime.now(UTC):
        # 临时授权已过期: 视同无成员(不修改行, 仅判定失效)
        return None
    return member


def _as_utc(dt: datetime | None) -> datetime | None:
    """将可能为 naive 的 datetime 按 UTC 解释(SQLite 测试环境回读为 naive)。"""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _next_auth_version(db: Session, project_id: int) -> int:
    """项目级授权版本递增: 角色变更/转移/成员增减时递增(01 §2.1)。"""
    max_av = db.execute(
        select(func.max(ProjectMember.auth_version)).where(ProjectMember.project_id == project_id)
    ).scalar()
    return (max_av or 0) + 1


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
    # 初始草稿(revision=1)与所有者成员行, 与项目创建同事务
    _new_draft_row(db, project, _initial_content(lang), user)
    db.add(
        ProjectMember(
            project_id=project.id, user_id=user.id, role="owner", auth_version=1, granted_by=user.id
        )
    )
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
    """我可见的项目列表(所有者 + 查看者, 不含已删除, RPD 3.2)。

    有效成员判定含 expires_at(M-01): 临时授权到期后不再可见。
    """
    now = datetime.now(UTC)
    rows = db.execute(
        select(Project, ProjectMember.role)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(
            ProjectMember.user_id == user.id,
            ProjectMember.revoked_at.is_(None),
            sa.or_(
                ProjectMember.expires_at.is_(None),
                ProjectMember.expires_at > now,
            ),
            Project.status != "deleted",
        )
        .order_by(Project.created_at.desc())
    ).all()
    return [{**project_to_dict(p), "my_role": role} for p, role in rows]


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


def delete_project(db: Session, user: User, project_id: int, confirm: bool = False) -> None:
    """删除项目(RPD 5.3: 确认 → 取消排队任务 → 一致性检查 → 硬删除)。

    - 必须显式确认(confirm=True), 否则 400;
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
    _audit(db, "project", project_id, "project.deleted", user.id, after={"status": "deleted"})


def duplicate_project(db: Session, user: User, project_id: int, name: str | None = None) -> Project:
    """复制项目为独立候选方案(REQ-PROJ-003): 复制者成为新项目所有者。

    仅复制当前草稿内容(候选方案从当前状态开始); 版本历史不复制。
    """
    ensure_access(db, user, project_id, "duplicate")
    source = _get_project(db, project_id)
    draft = _get_current_draft(db, source)
    content = _load_draft_content(db, draft)
    # 新项目名称去重(名称全局唯一)
    base = (name or "").strip() or f"{source.name} 副本"
    candidate = base
    index = 2
    while db.execute(select(Project.id).where(Project.name == candidate)).first() is not None:
        candidate = f"{base} ({index})"
        index += 1
    project = Project(
        name=candidate,
        description=source.description,
        status="active",
        owner_id=user.id,
        currency=source.currency,
        fixed_utc_offset_minutes=source.fixed_utc_offset_minutes,
        schema_version=source.schema_version,
        created_by=user.id,
    )
    db.add(project)
    try:
        db.flush()
    except IntegrityError as exc:
        raise ConflictError("已存在同名项目", params={"name": candidate}) from exc
    # 复制内容并重置命令簿记(独立候选从干净修订开始)
    content.pop("applied_commands", None)
    _new_draft_row(db, project, content, user)
    db.add(
        ProjectMember(
            project_id=project.id, user_id=user.id, role="owner", auth_version=1, granted_by=user.id
        )
    )
    _audit(
        db, "project", project.id, "project.duplicated", user.id,
        after={"source_project_id": source.id, "source_draft_revision": draft.revision, "name": candidate},
    )
    db.flush()
    return project


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
        applied[cid] = {"revision": draft.revision + 1, "result": _jsonable(result)}
        changed = True
        results.append(
            {
                "command_id": cid,
                "status": "applied",
                "revision": draft.revision + 1,
                "result": _jsonable(result),
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
            object_id=obj.id,
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
    raw = _canonical_json(_version_content(project, content))
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
# 内容寻址对象存储(草稿/版本内容载体)
# ---------------------------------------------------------------------------


def _object_root() -> Path:
    """对象存储根目录(settings.data_dir/objects)。"""
    return Path(settings.data_dir) / "objects"


def _object_rel_path(oid: str) -> str:
    """对象相对路径(按前缀分桶, 便于文件系统规模扩展)。"""
    return f"{oid[:2]}/{oid}.json"


def _store_content(db: Session, content: dict) -> str:
    """规范化 JSON → 内容寻址对象: 写对象文件 + objects 元数据行, 返回 content_hash。

    相同内容的重复写入按 oid 去重并递增引用计数(对象清理由 U11/U16 负责)。
    """
    raw = _canonical_json(content)
    content_hash = sha256_hex(raw.encode("utf-8"))
    obj = db.execute(select(StoredObject).where(StoredObject.oid == content_hash)).scalar_one_or_none()
    if obj is None:
        _write_object_file(content_hash, raw)
        db.add(
            StoredObject(
                oid=content_hash,
                sha256=content_hash,
                size_bytes=len(raw),
                storage_path=_object_rel_path(content_hash),
                media_type="application/json",
                status="stored",
                ref_count=1,
                quota_bytes=0,
            )
        )
    else:
        obj.ref_count += 1
    return content_hash


def _write_object_file(oid: str, raw: str) -> None:
    """原子写入对象文件(临时文件 + rename)。"""
    path = _object_root() / _object_rel_path(oid)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(raw, encoding="utf-8")
        tmp.rename(path)
    except OSError as exc:
        raise AppError(
            "内容对象写入失败(存储异常)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
        ) from exc


def _load_content_by_hash(db: Session, content_hash: str) -> dict:
    """按内容校验值读取内容对象并校验(对象缺失/哈希不符视为数据损坏)。"""
    obj = db.execute(
        select(StoredObject).where(StoredObject.oid == content_hash)
    ).scalar_one_or_none()
    if obj is None or not obj.storage_path:
        raise AppError(
            "内容对象缺失(数据损坏)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
            location={"object_type": "object", "object_id": content_hash},
        )
    path = _object_root() / obj.storage_path
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AppError(
            "内容对象读取失败(数据损坏)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
            location={"object_type": "object", "object_id": content_hash},
        ) from exc
    if sha256_hex(raw.encode("utf-8")) != content_hash:
        raise AppError(
            "内容校验失败(数据损坏)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
            location={"object_type": "object", "object_id": content_hash},
        )
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
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


def _get_object_by_oid(db: Session, oid: str) -> StoredObject:
    obj = db.execute(select(StoredObject).where(StoredObject.oid == oid)).scalar_one_or_none()
    if obj is None:
        raise AppError(
            "内容对象缺失(数据损坏)",
            code=SYS_STORE_CORRUPT,
            severity=SEVERITY_ERROR,
            message_key="ies.diag.store.corrupt",
            location={"object_type": "object", "object_id": oid},
        )
    return obj


def _canonical_json(content: dict) -> str:
    """规范化 JSON(键排序、紧凑分隔), 保证相同内容的哈希稳定。"""
    return json.dumps(_jsonable(content), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _jsonable(value: Any) -> Any:
    """递归转换为 JSON 安全值(datetime → ISO 字符串, Decimal → float)。"""
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


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
            before=_jsonable(before) if before else None,
            after=_jsonable(after) if after else None,
        )
    )


__all__ = [
    "InvalidRequestError",
    "ROLE_CAPABILITIES",
    "ensure_access",
    "get_role",
    "maintenance_access",
    "add_viewer",
    "remove_viewer",
    "transfer_ownership",
    "list_members",
    "create_project",
    "get_project_view",
    "list_visible_projects",
    "archive_project",
    "unarchive_project",
    "delete_project",
    "duplicate_project",
    "update_draft",
    "create_version",
    "get_version",
    "list_versions",
    "restore_version",
    "apply_result",
    "project_to_dict",
    "draft_to_dict",
    "version_to_dict",
]
