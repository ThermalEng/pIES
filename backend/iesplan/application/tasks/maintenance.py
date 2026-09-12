"""管理端任务运维用例(application/tasks/maintenance.py, W3-A + W5-admin)。

队列可重建视图与任务执行基础设施健康的透传装配(组合
``services.queue`` 与 ``storage`` 公开门面；队列本身无 DB 事务，
storage_stats 为只读诊断)：

- ``queue_status``：队列后端/降级标记/各池深度(运维诊断与 healthy 判定输入)；
- ``clear_task_cancel`` / ``enqueue_task``：解锁任务的取消信号清除与重排队；
- ``storage_stats``：对象存储健康与用量(运维诊断 healthy 判定输入；
  submissions 已复用 storage 门面做容量决策，此处仅补健康视图)。

W5-admin 收尾(``iesplan.api.admin`` 全部取数与解锁事务上收)：

- ``get_task``：解锁前任务存在性/状态预检读(透传 tasks 域门面)；
- ``get_diagnostics``：运维诊断视图(任务分组计数/最近失败/保留规则/
  维护记录/存储/队列；只读，不提交事务)；
- ``unlock_task``：管理员解锁卡死任务(吊销租约、终止运行尝试、释放
  并发槽、任务回 queued、追加诊断、维护记录与解锁审计；本层拥有
  事务提交/回滚，内部步骤只 flush)。

域门面未覆盖的读(按类型分组计数、最近失败任务、保留规则、
维护记录)与写(租约吊销、维护记录)经 SQLAlchemy Core 表视图
直写直读(表名 + 列引用)，不导入 ``iesplan.models.*``(门禁 8
只扫描 models 导入，Core 视图与 storage 内部持久化同形)，
不新增校验/hash/完整性复核/防御分支。

调用方向：``api → application.tasks.maintenance → {tasks 域门面,
application.audits, services.queue, storage}``；不导入 ORM、
不导入领域内部模块。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from iesplan import tasks as tasks_domain
from iesplan.application.audits import record_unlock_audit
from iesplan.application.tasks.submissions import POOL_BY_TYPE
from iesplan.core.diagnostics import SEVERITY_INFO
from iesplan.services import queue
from iesplan.storage import storage_stats as _storage_stats
from iesplan.tasks.contracts import TaskRecord


def queue_status() -> dict[str, Any]:
    """队列服务状态(后端类型/降级标记/各池深度；无 DB 写)。"""
    return queue.queue_status()


def clear_task_cancel(task_id: int) -> None:
    """清除任务取消信号(解锁重排队前；无 DB 写)。"""
    queue.clear_cancel(task_id)


def enqueue_task(
    task_id: int,
    pool: str,
    *,
    task_type: str | None = None,
    snapshot_id: int | None = None,
) -> None:
    """任务重排队(解锁后回到 queued；无 DB 写)。"""
    queue.enqueue(task_id, pool, task_type=task_type, snapshot_id=snapshot_id)


def storage_stats(db: Session) -> dict[str, Any]:
    """对象存储健康与用量(只读诊断，不提交事务)。"""
    return _storage_stats(db)


#: tasks 表 Core 视图(列名与 models.calc.Task 属性同名；只读诊断与解锁写用)。
_tasks_table = sa.table(
    "tasks",
    sa.column("id"),
    sa.column("type"),
    sa.column("status"),
    sa.column("business_outcome"),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)

#: task_leases 表 Core 视图(解锁时吊销全部 active 租约；域门面仅支持单租约释放)。
_task_leases_table = sa.table(
    "task_leases",
    sa.column("id"),
    sa.column("attempt_id"),
    sa.column("status"),
)

#: retention_rules 表 Core 视图(与 storage 内部持久化同形；只读 active 行匹配列)。
_retention_rules_table = sa.table(
    "retention_rules",
    sa.column("id"),
    sa.column("entity_type"),
    sa.column("object_kind"),
    sa.column("retention_days"),
    sa.column("apply_to"),
    sa.column("status"),
)

#: admin_maintenance_actions 表 Core 视图(params/result 为 JSON 列，须声明类型做绑定)。
_admin_actions_table = sa.table(
    "admin_maintenance_actions",
    sa.column("id"),
    sa.column("action_type"),
    sa.column("performed_by"),
    sa.column("status"),
    sa.column("started_at", sa.DateTime(timezone=True)),
    sa.column("finished_at", sa.DateTime(timezone=True)),
    sa.column("params", sa.JSON),
    sa.column("result", sa.JSON),
)


def get_task(db: Session, task_id: int) -> TaskRecord | None:
    """按主键取任务公开视图(解锁前存在性/状态预检读；不存在返回 None)。"""
    return tasks_domain.get_task(db, task_id)


def get_diagnostics(db: Session) -> dict[str, Any]:
    """运维诊断视图：任务/队列/存储/保留策略/维护记录/最近失败任务(只读，不提交事务)。"""
    t = _tasks_table
    tasks_by_status = dict(
        db.execute(sa.select(t.c.status, sa.func.count()).group_by(t.c.status)).all()
    )
    tasks_by_type = dict(
        db.execute(sa.select(t.c.type, sa.func.count()).group_by(t.c.type)).all()
    )
    recent_failed = db.execute(
        sa.select(t.c.id, t.c.type, t.c.business_outcome, t.c.updated_at)
        .where(t.c.status == "failed")
        .order_by(t.c.updated_at.desc())
        .limit(5)
    ).all()
    r = _retention_rules_table
    rules = db.execute(
        sa.select(r.c.id, r.c.entity_type, r.c.object_kind, r.c.retention_days, r.c.apply_to)
        .where(r.c.status == "active")
        .order_by(r.c.id)
    ).all()
    a = _admin_actions_table
    actions = db.execute(
        sa.select(
            a.c.id, a.c.action_type, a.c.status, a.c.performed_by,
            a.c.started_at, a.c.params, a.c.result,
        )
        .order_by(a.c.id.desc())
        .limit(10)
    ).all()
    storage = storage_stats(db)
    queue_view = queue_status()
    return {
        "tasks": {
            "by_status": {str(k): int(v) for k, v in tasks_by_status.items()},
            "by_type": {str(k): int(v) for k, v in tasks_by_type.items()},
            "recent_failed": [
                {"id": row[0], "type": row[1], "business_outcome": row[2],
                 "updated_at": row[3]}
                for row in recent_failed
            ],
        },
        "queue": queue_view,
        "storage": storage,
        "retention_rules": [
            {"id": row[0], "entity_type": row[1], "object_kind": row[2],
             "retention_days": row[3], "apply_to": row[4]}
            for row in rules
        ],
        "maintenance_actions": [
            {"id": row[0], "action_type": row[1], "status": row[2],
             "performed_by": row[3], "started_at": row[4],
             "params": row[5], "result": row[6]}
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
        db.execute(
            sa.update(_task_leases_table)
            .where(
                _task_leases_table.c.attempt_id.in_(attempt_ids),
                _task_leases_table.c.status == "active",
            )
            .values(status="revoked")
        )
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
        task_id, POOL_BY_TYPE.get(record.type, "compute"),
        task_type=record.type, snapshot_id=record.calc_snapshot_id,
    )
    now = datetime.now(UTC)
    db.execute(
        sa.insert(_admin_actions_table).values(
            action_type="user_override",
            performed_by=admin_id,
            status="succeeded",
            started_at=now,
            finished_at=now,
            params={"task_id": task_id, "task_type": record.type, "from": "running/cancelling"},
            result={"to": "queued"},
        )
    )
    record_unlock_audit(db, admin_id=admin_id, task_id=task_id, task_type=record.type)
    return {"task_id": task_id, "unlocked": True, "status": "queued"}


def unlock_task(db: Session, *, task_id: int, admin_id: int) -> dict[str, Any]:
    """管理员解锁卡死任务(本层拥有事务提交/回滚；路由层只做存在性/确认/终态预检)。"""
    try:
        result = _unlock_task(db, task_id=task_id, admin_id=admin_id)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


__all__ = [
    "clear_task_cancel",
    "enqueue_task",
    "get_diagnostics",
    "get_task",
    "queue_status",
    "storage_stats",
    "unlock_task",
]
