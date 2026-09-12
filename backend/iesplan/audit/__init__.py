"""审计域公开门面（audit_log，归属 audit）。

外部只允许经本门面消费 contract、repository 协议、repository 实现函数
与审计能力（事件清单、统一写入入口、查询）；不得导入 `iesplan.models`、
services 或其他域的内部模块。

审计能力由 services/audit.py 收敛至此（纠偏 Wave 1 切片 C）：
audit_log 不可变（只 INSERT，不提供任何修改/删除路径）；
审计事件与业务写入同事务提交，事务边界由调用方（用例层）控制。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from iesplan.audit import persistence
from iesplan.audit.contracts import AuditError, AuditRecord
from iesplan.audit.repository import AuditRepository

if TYPE_CHECKING:
    from iesplan.identity.contracts import UserRecord

append_entry = persistence.append_entry
list_entries = persistence.list_entries

__all__ = [
    "AUDIT_ACTION_CATALOG",
    "AUDIT_AUTH_ACCOUNT_DISABLED",
    "AUDIT_AUTH_ACCOUNT_REACTIVATED",
    "AUDIT_AUTH_LOGIN",
    "AUDIT_AUTH_LOGIN_FAILED",
    "AUDIT_AUTH_LOGOUT",
    "AUDIT_AUTH_PASSWORD_CHANGED",
    "AUDIT_AUTH_PASSWORD_RESET",
    "AUDIT_AUTH_PERMISSION_CHANGED",
    "AUDIT_AUTH_SESSION_TAKEOVER",
    "AUDIT_CONFIG_SAVED",
    "AUDIT_DRAFT_SNAPSHOT_CHANGED",
    "AUDIT_MAINTENANCE",
    "AUDIT_MAINTENANCE_OBJECT_CLEANUP",
    "AUDIT_MAINTENANCE_UNLOCK_TASK",
    "AUDIT_PROJECT_ARCHIVED",
    "AUDIT_PROJECT_CREATED",
    "AUDIT_PROJECT_DELETED",
    "AUDIT_PROJECT_EXPORTED",
    "AUDIT_PROJECT_IMPORTED",
    "AUDIT_PROJECT_IMPORT_PROPOSED",
    "AUDIT_RESULT_APPLIED",
    "AUDIT_TASK_CANCELLED",
    "AUDIT_TASK_SUBMITTED",
    "AUDIT_TASK_TERMINATED",
    "AUDIT_VERSION_CREATED",
    "AuditError",
    "AuditRecord",
    "AuditRepository",
    "append_entry",
    "audit",
    "audit_user_action",
    "entry_to_dict",
    "list_actions",
    "list_entries",
    "query_audit",
    "utcnow",
]

# ---------------------------------------------------------------------------
# 审计事件清单(审计范围, 宪法 §16 + 领域模型 §身份、权限和审计; 命名约定 <域>.<动作>)
# ---------------------------------------------------------------------------

#: 身份与认证(U01 域, 与 auth_events 互补)
AUDIT_AUTH_LOGIN = "auth.login"  # 登录成功
AUDIT_AUTH_LOGOUT = "auth.logout"  # 登出
AUDIT_AUTH_LOGIN_FAILED = "auth.login_failed"  # 登录失败
AUDIT_AUTH_PASSWORD_CHANGED = "auth.password_changed"  # 修改密码
AUDIT_AUTH_PASSWORD_RESET = "auth.password_reset"  # 管理员重置密码
AUDIT_AUTH_ACCOUNT_DISABLED = "auth.account_disabled"  # 停用账号
AUDIT_AUTH_ACCOUNT_REACTIVATED = "auth.account_reactivated"  # 重新启用账号
AUDIT_AUTH_SESSION_TAKEOVER = "auth.session_takeover"  # 窗口接管
AUDIT_AUTH_PERMISSION_CHANGED = "auth.permission_changed"  # 角色/权限变化

#: 项目生命周期(U02/U03 域)
AUDIT_PROJECT_CREATED = "project.created"
AUDIT_PROJECT_ARCHIVED = "project.archived"
AUDIT_PROJECT_DELETED = "project.deleted"
AUDIT_PROJECT_IMPORTED = "project.imported"  # 导入完成(新项目身份)
AUDIT_PROJECT_IMPORT_PROPOSED = "import.proposal_created"  # 导入提案
AUDIT_PROJECT_EXPORTED = "project.exported"  # 项目包/Excel 导出

#: 草稿/版本/快照变化
AUDIT_DRAFT_SNAPSHOT_CHANGED = "draft.snapshot_changed"  # 草稿修订(快照变化)
AUDIT_VERSION_CREATED = "project_version.created"

#: 任务生命周期(U07/U08 域)
AUDIT_TASK_SUBMITTED = "task.submitted"
AUDIT_TASK_CANCELLED = "task.cancelled"
AUDIT_TASK_TERMINATED = "task.terminated"  # 超时/失败终止

#: 结果应用(U03/U09 域)
AUDIT_RESULT_APPLIED = "result.applied"

#: 计算配置(U06 域, 0.2.0 B4: 宪法 §16 关键变更审计)
AUDIT_CONFIG_SAVED = "config.saved"

#: 维护操作(U16 域)
AUDIT_MAINTENANCE = "maintenance.operation"  # 通用维护操作
AUDIT_MAINTENANCE_UNLOCK_TASK = "maintenance.unlock_task"  # 管理员解锁任务
AUDIT_MAINTENANCE_OBJECT_CLEANUP = "maintenance.object_cleanup"  # 对象清理

#: 审计事件目录(供管理端按 action 过滤; 非强制校验, 其他单元可按域扩展)
AUDIT_ACTION_CATALOG: frozenset[str] = frozenset(
    {
        AUDIT_AUTH_LOGIN, AUDIT_AUTH_LOGOUT, AUDIT_AUTH_LOGIN_FAILED,
        AUDIT_AUTH_PASSWORD_CHANGED, AUDIT_AUTH_PASSWORD_RESET,
        AUDIT_AUTH_ACCOUNT_DISABLED, AUDIT_AUTH_ACCOUNT_REACTIVATED,
        AUDIT_AUTH_SESSION_TAKEOVER, AUDIT_AUTH_PERMISSION_CHANGED,
        AUDIT_PROJECT_CREATED, AUDIT_PROJECT_ARCHIVED,
        AUDIT_PROJECT_DELETED, AUDIT_PROJECT_IMPORTED, AUDIT_PROJECT_IMPORT_PROPOSED,
        AUDIT_PROJECT_EXPORTED, AUDIT_DRAFT_SNAPSHOT_CHANGED, AUDIT_VERSION_CREATED,
        AUDIT_TASK_SUBMITTED, AUDIT_TASK_CANCELLED, AUDIT_TASK_TERMINATED,
        AUDIT_RESULT_APPLIED, AUDIT_CONFIG_SAVED, AUDIT_MAINTENANCE, AUDIT_MAINTENANCE_UNLOCK_TASK,
        AUDIT_MAINTENANCE_OBJECT_CLEANUP,
    }
)


def utcnow() -> datetime:
    """当前 UTC 时间。"""
    return datetime.now(UTC)


def audit(
    db: Session,
    actor_id: int | None,
    action: str,
    object_type: str,
    object_id: int,
    *,
    revision: int | None = None,
    result: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    before: dict[str, Any] | None = None,
    actor_type: str = "user",
    ip: str | None = None,
    request_id: str | None = None,
    trace_id: str | None = None,
) -> AuditRecord:
    """统一审计入口(写不可变 audit_log, 只 INSERT, 宪法 §16 + 领域模型 §对象生命周期 /
    领域模型 §身份、权限和审计)。

    只保存身份/时间/动作/对象标识/修订号/结果; 不复制密码、
    令牌、完整模型、完整数据集或原始求解日志(敏感与大体量内容一概不入库, 宪法 §16)。

    参数:
        db: 数据库会话(本函数只经本域 persistence 写入, 提交由调用方控制)。
        actor_id: 操作者用户 id; 系统动作传 None。
        action: 审计动作(建议取自 AUDIT_ACTION_CATALOG)。
        object_type: 对象类型(实体表名, 如 project/task/object)。
        object_id: 对象标识(实体 id)。
        revision: 修订号(草稿/版本场景, 记入 after.revision)。
        result: 结果摘要(如 {status: 'ok', package_object_id: 12})。
        extra: 其他脱敏元数据(合并进 after)。
        before: 变更前关键字段(如 {from_user_id: ...}); 无变更前状态传 None。
        actor_type: 操作者类型 user/system/admin(领域模型 §对象生命周期 CHECK)。
        ip/request_id/trace_id: 请求追踪信息。
    返回:
        AuditRecord 记录(未提交)。
    """
    return append_entry(
        db,
        actor_id=actor_id,
        action=action,
        entity_type=object_type,
        entity_id=object_id,
        revision=revision,
        result=result,
        extra=extra,
        before=before,
        actor_type=actor_type,
        ip=ip,
        request_id=request_id,
        trace_id=trace_id,
    )


def audit_user_action(
    db: Session,
    user: UserRecord,
    action: str,
    object_type: str,
    object_id: int,
    **kwargs: Any,
) -> AuditRecord:
    """便捷入口: 以用户身份写审计(actor_type 自动取 user)。"""
    return audit(db, user.id, action, object_type, object_id, **kwargs)


def entry_to_dict(row: AuditRecord) -> dict[str, Any]:
    """审计记录序列化(管理端展示)。"""
    return {
        "id": row.id,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "action": row.action,
        "actor_id": row.actor_id,
        "actor_type": row.actor_type,
        "occurred_at": row.occurred_at,
        "ip": row.ip,
        "before": row.before,
        "after": row.after,
        "request_id": row.request_id,
        "trace_id": row.trace_id,
    }


def query_audit(
    db: Session,
    *,
    entity_type: str | None = None,
    entity_id: int | None = None,
    action: str | None = None,
    actor_id: int | None = None,
    actor_type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    cursor: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """审计查询(管理端, 过滤 + 游标分页, 按发生时间倒序)。

    参数:
        entity_type/entity_id/action/actor_id/actor_type: 精确过滤。
        since/until: 时间范围过滤(UTC)。
        cursor: 上一页末条 id(游标分页)。
        limit: 每页条数(1..200)。
    返回:
        {"items": [...], "next_cursor": id|None}。
    """
    limit = min(max(int(limit), 1), 200)
    rows = list_entries(
        db,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        actor_id=actor_id,
        actor_type=actor_type,
        since=since,
        until=until,
        cursor=cursor,
        limit=limit + 1,
    )
    items = [entry_to_dict(row) for row in rows[:limit]]
    next_cursor = rows[-1].id if len(rows) > limit else None
    return {"items": items, "next_cursor": next_cursor}


def list_actions() -> list[str]:
    """审计事件目录(管理端 UI 可用)。"""
    return sorted(AUDIT_ACTION_CATALOG)
