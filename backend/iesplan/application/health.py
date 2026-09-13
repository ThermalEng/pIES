"""运维健康只读探针(application/health.py, W4-health)。

api/health.py 的取数门面: 存活 ping、任务/项目按状态计数、用户总数、
队列状态、存储用量与抽样校验, 全部只读透传领域/存储公开门面。
本层不导入 iesplan.models.*、不提交事务, 不新增校验/hash/回退。

``health_view`` 是运维健康端点的唯一完整 handler: 原 api/health.py 本地
helper 的全部健康门面调用收进本用例, 端点只转交一次。

调用方向: api.health → application.health → {tasks/project/identity
领域门面(含 tasks 域队列视图), storage}。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from iesplan import __version__
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


def health_view(db: Session) -> dict[str, Any]:
    """运维健康完整视图(架构宪法 §13 故障与健康语义): 存活/就绪/任务指标/队列指标/存储容量。

    原 api/health.py 本地 helper 的 6 次健康门面调用
    (check_db/tasks_by_status/projects_by_status/count_users/queue_status +
    storage_stats/sample_verify 组成存储视图)并入本完整 handler;
    存储健康取 storage_stats/sample_verify(容量 + 抽样校验), 队列取
    queue_status。健康判定: 存活 + 就绪 + 存储门禁; 队列为可重建视图
    (Redis 可重建), 其降级状态在 queue 节单独上报, 不影响整体状态。
    只读, 不提交事务。端点只转交本用例一次。
    """
    db_ok = check_db(db)
    task_counts = tasks_by_status(db)
    project_counts = projects_by_status(db)
    users_count = count_users(db)
    stats = storage_stats(db)
    verify = sample_verify(db, limit=10)
    storage = {
        "capacity": stats["capacity"],
        "corrupt_count": len(verify["failed"]),
        "orphan_count": stats["objects"]["orphan_count"],
        "object_count": stats["objects"]["count"],
        "ok": stats["healthy"] and len(verify["failed"]) == 0,
        "verify": {
            "checked": verify["checked"],
            "ok_count": verify["ok_count"],
            "failed": verify["failed"],
        },
    }
    queue_view = queue_status()
    healthy = db_ok and storage["capacity"]["ok"]
    return {
        "status": "ok" if healthy else "degraded",
        "service": "iesplan",
        "version": __version__,
        "time": datetime.now(UTC).isoformat(),
        "liveness": {"ok": True, "process": "alive"},
        "readiness": {"db": db_ok},
        "metrics": {
            "tasks_by_status": {str(k): int(v) for k, v in task_counts.items()},
            "projects_by_status": {str(k): int(v) for k, v in project_counts.items()},
            "users": int(users_count),
        },
        "queue": queue_view,
        "storage": storage,
    }


__all__ = [
    "check_db",
    "count_users",
    "health_view",
    "projects_by_status",
    "queue_status",
    "sample_verify",
    "storage_stats",
    "tasks_by_status",
]
