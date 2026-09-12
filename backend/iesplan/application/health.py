"""运维健康只读探针(application/health.py, W4-health)。

api/health.py 的取数门面: 存活 ping、任务/项目按状态计数、用户总数、
队列状态、存储用量与抽样校验, 全部只读透传领域/存储公开门面。
本层不导入 iesplan.models.*、不提交事务, 不新增校验/hash/回退。

调用方向: api.health → application.health → {tasks/project/identity
领域门面(含 tasks 域队列视图), storage}。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from iesplan import identity as identity_domain
from iesplan import project as project_domain
from iesplan import tasks as tasks_domain
from iesplan.storage import sample_verify as _sample_verify
from iesplan.storage import storage_stats as _storage_stats

#: 任务状态全集(models.calc.TASK_STATUSES, ck_tasks_status 约束保证无域外值;
#: application 层禁直引 models, 此处按约束复述。零计数状态不输出,
#: 与原 GROUP BY 查询同形)。
_TASK_STATUSES: tuple[str, ...] = (
    "queued",
    "running",
    "completed",
    "cancelling",
    "cancelled",
    "timed_out",
    "failed",
)


def check_db(db: Session) -> bool:
    """数据库存活 ping(健康检查只上报状态, 不抛错)。"""
    try:
        db.execute(select(1))
    except Exception:  # noqa: BLE001  (健康检查只上报状态, 不抛错)
        return False
    return True


def tasks_by_status(db: Session) -> dict[str, int]:
    """任务按状态计数(零计数状态省略, 与原 GROUP BY 同形)。"""
    counts: dict[str, int] = {}
    for status in _TASK_STATUSES:
        count = int(tasks_domain.count_tasks_by_statuses(db, [status]))
        if count:
            counts[status] = count
    return counts


def projects_by_status(db: Session) -> dict[str, int]:
    """项目按状态计数(全部分页枚举后分组, 含已删除, 与原 GROUP BY 同形)。"""
    counts: dict[str, int] = {}
    cursor: int | None = None
    while True:
        page = project_domain.list_projects(db, cursor=cursor)
        for project in page.items:
            status = str(project.status)
            counts[status] = counts.get(status, 0) + 1
        cursor = page.next_cursor
        if cursor is None:
            break
    return counts


def count_users(db: Session) -> int:
    """用户总数(含停用, 与原 COUNT 同口径)。"""
    return len(identity_domain.list_users(db))


def queue_status() -> dict[str, Any]:
    """队列服务状态(后端类型/降级标记/各池深度；无 DB 写)。"""
    return tasks_domain.queue_status()


def storage_stats(db: Session) -> dict[str, Any]:
    """对象存储健康与用量(只读诊断，不提交事务)。"""
    return _storage_stats(db)


def sample_verify(db: Session, limit: int = 10) -> dict[str, Any]:
    """对象抽样存在性巡检(只读诊断；limit 原样透传)。"""
    return _sample_verify(db, limit=limit)


__all__ = [
    "check_db",
    "count_users",
    "projects_by_status",
    "queue_status",
    "sample_verify",
    "storage_stats",
    "tasks_by_status",
]
