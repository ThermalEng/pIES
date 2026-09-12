"""管理端任务运维薄封装(application/tasks/maintenance.py, W3-A)。

队列可重建视图与任务执行基础设施健康的透传装配(组合
``services.queue`` 与 ``storage`` 公开门面；队列本身无 DB 事务，
storage_stats 为只读诊断)：

- ``queue_status``：队列后端/降级标记/各池深度(运维诊断与 healthy 判定输入)；
- ``clear_task_cancel`` / ``enqueue_task``：解锁任务的取消信号清除与重排队；
- ``storage_stats``：对象存储健康与用量(运维诊断 healthy 判定输入；
  submissions 已复用 storage 门面做容量决策，此处仅补健康视图)。

本层不新增校验/hash/完整性复核/防御分支。

调用方向：``api → application.tasks.maintenance → {services.queue,
storage}``；不导入 ORM、不导入领域内部模块。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from iesplan.services import queue
from iesplan.storage import storage_stats as _storage_stats


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


__all__ = [
    "clear_task_cancel",
    "enqueue_task",
    "queue_status",
    "storage_stats",
]
