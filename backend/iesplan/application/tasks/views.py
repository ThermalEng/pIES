"""任务面向 API 的薄封装用例(application/tasks): 查询 + 用户级写编排。

只做转调, 不新增校验/hash/回退:

- 查询(``task_summary``/``list_tasks``/``task_detail``)直调
  ``services.tasks`` 同名只读函数, 不拥有事务;
- 用户级写(``cancel_user_task``/``retry_user_task``)按路由原顺序组合已有
  公开用例(权限 → 归属 → 取消/重试), 事务由 ``submissions`` 顶层用例提交/
  回滚, 路由层不再提交。

依赖方向: api → application → (services/领域门面)。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from iesplan.application.tasks.submissions import (
    cancel_task,
    ensure_task_belongs,
    retry_task,
    submit_task,  # noqa: F401 (经本模块再导出, 调用方以 tasks_app.submit_task 取用)
)
from iesplan.identity.contracts import UserRecord
from iesplan.services import project as project_service
from iesplan.services import tasks as tasks_service
from iesplan.tasks.contracts import TaskRecord


def task_summary(db: Session, task: TaskRecord) -> dict[str, Any]:
    """任务列表项摘要(只读转调)。"""
    return tasks_service.task_summary(db, task)


def list_tasks(
    db: Session,
    user: UserRecord,
    project_id: int,
    *,
    task_type: str | None = None,
    status: str | None = None,
    outcome: str | None = None,
    cursor: int | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """任务列表(只读转调)。"""
    return tasks_service.list_tasks(
        db, user, project_id,
        task_type=task_type, status=status, outcome=outcome, cursor=cursor, limit=limit,
    )


def task_detail(
    db: Session, user: UserRecord, project_id: int, task_id: int
) -> dict[str, Any]:
    """任务详情(只读转调)。"""
    return tasks_service.task_detail(db, user, project_id, task_id)


def cancel_user_task(
    db: Session, user: UserRecord, project_id: int, task_id: int, reason: str = "user_cancel",
) -> TaskRecord:
    """取消任务(路由原顺序: edit 权限 → 归属 → 取消; 提交由用例层拥有)。"""
    project_service.ensure_access(db, user, project_id, "edit")
    ensure_task_belongs(db, project_id, task_id)
    return cancel_task(db, task_id, reason=reason, actor_id=user.id)


def retry_user_task(
    db: Session, user: UserRecord, project_id: int, task_id: int
) -> TaskRecord:
    """手动重试(路由原顺序: 归属 → 重试; 权限与提交由用例层拥有)。"""
    ensure_task_belongs(db, project_id, task_id)
    return retry_task(db, user, task_id)
