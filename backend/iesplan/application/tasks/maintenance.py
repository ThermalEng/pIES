"""管理端任务运维用例(application/tasks/maintenance.py)。

队列可重建视图与任务执行基础设施健康的透传装配(组合
tasks 域队列视图与 ``storage`` 公开门面；队列本身无 DB 事务，
storage_stats 为只读诊断):

- ``queue_status``：队列后端/降级标记/各池深度(运维诊断与 healthy 判定输入)；
- ``clear_task_cancel`` / ``enqueue_task``：解锁任务的取消信号清除与重排队；
- ``storage_stats``：对象存储健康与用量(运维诊断 healthy 判定输入；
  submissions 已复用 storage 门面做容量决策，此处仅补健康视图)。

管理端收尾(``iesplan.api.admin`` 全部取数与解锁事务上收):

- ``get_diagnostics``：运维诊断视图(任务分组计数/最近失败/保留规则/
  维护记录/存储/队列；只读，不提交事务)；
- ``unlock_task_case``：解锁完整用例(存在性 → 危险确认 → 终态/已排队
  预检 → 解锁执行；返回 UnlockDecision，错误/响应映射归 API)。

数据访问只经领域公开门面(tasks 域、application.audits、storage)，
不新增校验/hash/完整性复核/防御分支。

调用方向：``api → application.tasks.maintenance → {tasks/audit/project 域门面,
application.audits, storage}``；不导入 ORM、不导入领域内部模块。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.orm import Session

from iesplan import audit as audit_domain
from iesplan import project as project_domain
from iesplan import tasks as tasks_domain
from iesplan.application.audits import record_unlock_audit
from iesplan.core.diagnostics import SEVERITY_INFO
from iesplan.storage import storage_stats as _storage_stats
from iesplan.tasks.contracts import TaskRecord


def queue_status() -> dict[str, Any]:
    """队列服务状态(后端类型/降级标记/各池深度；无 DB 写)。"""
    return tasks_domain.queue_status()


def clear_task_cancel(task_id: int) -> None:
    """清除任务取消信号(解锁重排队前；无 DB 写)。"""
    tasks_domain.clear_cancel(task_id)


def enqueue_task(
    task_id: int,
    pool: str,
    *,
    task_type: str | None = None,
    snapshot_id: int | None = None,
) -> None:
    """任务重排队(解锁后回到 queued；无 DB 写)。"""
    tasks_domain.enqueue(task_id, pool, task_type=task_type, snapshot_id=snapshot_id)


def storage_stats(db: Session) -> dict[str, Any]:
    """对象存储健康与用量(只读诊断，不提交事务)。"""
    return _storage_stats(db)


def get_diagnostics(db: Session) -> dict[str, Any]:
    """运维诊断视图：任务/队列/存储/保留策略/维护记录/最近失败任务(只读，不提交事务)。"""
    tasks_by_status = tasks_domain.count_tasks_by_status(db)
    tasks_by_type = tasks_domain.count_tasks_by_type(db)
    recent_failed = tasks_domain.list_recent_failed_tasks(db, limit=5)
    rules = audit_domain.list_active_retention_rules(db)
    actions = project_domain.list_maintenance_actions(db, limit=10)
    storage = storage_stats(db)
    queue_view = queue_status()
    return {
        "tasks": {
            "by_status": {str(k): int(v) for k, v in tasks_by_status.items()},
            "by_type": {str(k): int(v) for k, v in tasks_by_type.items()},
            "recent_failed": [
                {"id": row.id, "type": row.type, "business_outcome": row.business_outcome,
                 "updated_at": row.updated_at}
                for row in recent_failed
            ],
        },
        "queue": queue_view,
        "storage": storage,
        "retention_rules": [
            {"id": row.id, "entity_type": row.entity_type, "object_kind": row.object_kind,
             "retention_days": row.retention_days, "apply_to": row.apply_to}
            for row in rules
        ],
        "maintenance_actions": [
            {"id": row.id, "action_type": row.action_type, "status": row.status,
             "performed_by": row.performed_by, "started_at": row.started_at,
             "params": row.params, "result": row.result}
            for row in actions
        ],
        # 队列为可重建视图(PG 为权威事实), 降级只提示不影响健康判定
        "healthy": storage["healthy"] and not queue_view["degraded"],
    }


def _unlock_task(db: Session, *, task_id: int, admin_id: int) -> dict[str, Any]:
    """解锁步骤(只 flush，不提交；事务由 ``unlock_task`` 拥有)。"""
    # 吊销租约 + 终止运行尝试 + 释放并发槽(running/cancelling → queued)
    attempts = tasks_domain.list_attempts(db, task_id)
    attempt_ids = [a.id for a in attempts]
    if attempt_ids:
        tasks_domain.revoke_leases_for_attempts(db, attempt_ids)
    for attempt in attempts:
        if attempt.status == "running":
            tasks_domain.finish_attempt(db, attempt.id, "stopped", stop_reason="admin_unlock")
    for attempt in attempts:
        tasks_domain.release_slot(db, attempt.id)

    record = tasks_domain.set_task_status(db, task_id, "queued", business_outcome=None)
    tasks_domain.append_diagnostic(
        db, task_id=task_id, level=SEVERITY_INFO, code="TASK-ADMIN-001",
        message="管理员解锁, 任务重新排队",
        context={"unlocked_by": admin_id},
    )
    clear_task_cancel(task_id)
    enqueue_task(
        task_id, tasks_domain.POOL_BY_TYPE.get(record.type, "compute"),
        task_type=record.type, snapshot_id=record.calc_snapshot_id,
    )
    project_domain.record_maintenance_action(
        db,
        action_type="user_override",
        performed_by=admin_id,
        status="succeeded",
        params={"task_id": task_id, "task_type": record.type, "from": "running/cancelling"},
        result={"to": "queued"},
    )
    record_unlock_audit(db, admin_id=admin_id, task_id=task_id, task_type=record.type)
    return {"task_id": task_id, "unlocked": True, "status": "queued"}


#: 解锁预检与执行结局: not_found/confirm_required/terminal 由 API 映射为
#: 404/409 错误, already_queued/unlocked 映射为 200 响应
UnlockOutcome = Literal["not_found", "confirm_required", "terminal", "already_queued", "unlocked"]


@dataclass(frozen=True)
class UnlockDecision:
    """解锁完整用例结果(与 HTTP 无关): 状态规则由本层拥有, 错误/响应映射归 API。"""

    outcome: UnlockOutcome
    task_id: int
    status: str | None = None


def unlock_task_case(
    db: Session, *, task_id: int, confirm: bool, admin_id: int
) -> UnlockDecision:
    """管理员解锁完整用例: 存在性 → 危险确认 → 终态/已排队预检 → 解锁执行。

    原 ``api/admin.py::unlock_task_endpoint`` 的业务顺序整体下收:
    不存在的任务 → not_found(404, 优先于 confirm, 避免泄露);
    未确认 → confirm_required(409); 终态 → terminal(409);
    已在排队 → already_queued(200 短路, 不推进状态);
    其余 running/cancelling 卡死任务执行解锁并提交事务。
    """
    task = tasks_domain.get_task(db, task_id)
    if task is None:
        return UnlockDecision("not_found", task_id)
    if not confirm:
        return UnlockDecision("confirm_required", task.id, task.status)
    if task.status in tasks_domain.TERMINAL_STATUSES:
        return UnlockDecision("terminal", task.id, task.status)
    if task.status == "queued":
        return UnlockDecision("already_queued", task.id, "queued")
    try:
        _unlock_task(db, task_id=task.id, admin_id=admin_id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return UnlockDecision("unlocked", task.id, "queued")


__all__ = [
    "UnlockDecision",
    "UnlockOutcome",
    "clear_task_cancel",
    "enqueue_task",
    "get_diagnostics",
    "queue_status",
    "storage_stats",
    "unlock_task_case",
]
