"""任务面向 API 的薄封装用例(application/tasks): 查询 + 用户级写编排。

任务查询视图的唯一实现(已收敛原 ``services.tasks`` 同名只读函数语义):

- 查询(``task_summary``/``list_tasks``/``task_detail``)只读装配 tasks/results
  域门面与队列可重建视图, 不拥有事务;
- 用户级写(``cancel_user_task``/``retry_user_task``)按路由原顺序组合已有
  公开用例(权限 → 归属 → 取消/重试), 事务由 ``submissions`` 顶层用例提交/
  回滚, 路由层不再提交。

依赖方向: api → application → 领域门面。不新增校验/hash/回退。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from iesplan import identity as identity_domain
from iesplan import results as results_domain
from iesplan import tasks as tasks_domain
from iesplan.application.tasks.submissions import (
    cancel_task,
    ensure_project_access,
    ensure_task_belongs,
    require_project,
    retry_task,
    submit_task,  # noqa: F401 (经本模块再导出, 调用方以 tasks_app.submit_task 取用)
)
from iesplan.core.diagnostics import TASK_QUEUED
from iesplan.identity.contracts import UserRecord
from iesplan.tasks.contracts import TaskRecord


def _task_trace_id(db: Session, task: TaskRecord) -> str | None:
    """任务 trace_id(取自入队诊断 context)。"""
    diag = tasks_domain.latest_diagnostic(db, task.id, TASK_QUEUED)
    if diag is not None and diag.context:
        return diag.context.get("trace_id")
    return None


def _progress_summary(
    db: Session, task: TaskRecord
) -> tuple[int | None, float, str | None, dict[str, Any] | None]:
    """当前进度摘要 (attempt_no, percent, stage, detail): running 优先读队列秒级进度。"""
    attempt = tasks_domain.get_latest_attempt(db, task.id)
    if attempt is None:
        return None, 0.0, "queued" if task.status == "queued" else None, None
    percent: float = 0.0
    stage: str | None = None
    detail: dict[str, Any] | None = None
    if task.status == "running":
        live = tasks_domain.get_queue_progress(task.id, attempt.attempt_no)
        if live is not None:
            try:
                percent = float(live.get("percent", 0.0))
            except (TypeError, ValueError):
                percent = 0.0
            stage = live.get("stage")
            detail = live.get("detail")
    if stage is None:
        row = tasks_domain.get_progress(db, attempt.id)
        if row is not None:
            percent = float(row.progress_percent)
            stage = row.stage
            detail = row.detail
    if task.status in tasks_domain.TERMINAL_STATUSES and stage is None:
        # 终态无进度行: 完成定格 100, 其余 0
        percent = 100.0 if task.status == "completed" else percent
        stage = "done" if task.status == "completed" else None
    return attempt.attempt_no, percent, stage, detail


def _evidence_available(db: Session, task: TaskRecord) -> bool:
    """结果可用性: 只有任务 completed 且证据包状态为 complete/partial 才算可用。

    invalid 证据不可展示, queued/running/failed 等状态即使有残留证据行也不算。
    """
    if task.status != "completed":
        return False
    evidence = results_domain.latest_evidence_for_task(db, task.id)
    return evidence is not None and evidence.status in ("complete", "partial")


def task_summary(db: Session, task: TaskRecord) -> dict[str, Any]:
    """任务列表项摘要(只读装配)。"""
    attempt_no, percent, stage, _detail = _progress_summary(db, task)
    queue_position: int | None = None
    if task.status == "queued":
        queue_position = tasks_domain.queue_position(task.id, tasks_domain.POOL_BY_TYPE[task.type])
    evidence_exists = _evidence_available(db, task)
    summary: dict[str, Any] = {
        "id": task.id,
        "type": task.type,
        "status": task.status,
        "business_outcome": task.business_outcome,
        "priority": task.priority,
        "calc_snapshot_id": task.calc_snapshot_id,
        "requested_by": task.requested_by,
        "requested_at": task.requested_at,
        "attempt_count": task.attempt_count,
        "max_attempts": task.max_attempts,
        "idempotency_key": task.idempotency_key,
        "superseded_by_task_id": task.superseded_by_task_id,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "result_available": evidence_exists,
        "summary": {
            "attempt_no": attempt_no,
            "percent": percent,
            "stage": stage,
            "queue_position": queue_position,
        },
    }
    trace_id = _task_trace_id(db, task)
    if trace_id is not None:
        summary["trace_id"] = trace_id
    return summary


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
    """任务列表(状态/结局过滤 + 游标分页)。"""
    ensure_project_access(db, user, project_id, "view")
    require_project(db, project_id)
    rows = tasks_domain.list_tasks(
        db,
        project_id,
        task_type=task_type,
        status=status,
        outcome=outcome,
        cursor=cursor,
        limit=limit + 1,
    )
    page_tasks = list(rows[:limit])
    # result_available 批量预取(避免逐任务 N+1 查询证据包): 仅 completed 任务
    # 需要判定, 一次批量取回。
    if page_tasks:
        completed_ids = [t.id for t in page_tasks if t.status == "completed"]
        available_ids: set[int] = set()
        if completed_ids:
            statuses = results_domain.evidence_statuses_for_tasks(db, completed_ids)
            available_ids = {tid for tid, st in statuses.items() if st in ("complete", "partial")}
        items = [_task_summary_prefetched(db, task, available_ids) for task in page_tasks]
    else:
        items = []
    next_cursor = rows[-1].id if len(rows) > limit else None
    return {"items": items, "next_cursor": next_cursor}


def _task_summary_prefetched(db: Session, task: TaskRecord, available_ids: set[int]) -> dict[str, Any]:
    """列表项摘要: 复用 task_summary, 但结果可用性来自批量预取集合。"""
    summary = task_summary(db, task)
    if task.status == "completed":
        summary["result_available"] = task.id in available_ids
    return summary


#: 任务诊断 context 白名单: 仅这些内部字段可向普通项目成员展示,
#: 其余(路径/对象 id/求解器参数等)仅管理员可见或置空
_DIAG_CONTEXT_ALLOWLIST: frozenset[str] = frozenset(
    {"trace_id", "queue", "snapshot_id", "business_outcome", "outcome"}
)


def _sanitize_diag_context(context: dict[str, Any] | None) -> dict[str, Any] | None:
    """诊断 context 脱敏: 白名单字段保留, 其余剔除。"""
    if not isinstance(context, dict):
        return None
    return {k: v for k, v in context.items() if k in _DIAG_CONTEXT_ALLOWLIST} or None


def task_detail(
    db: Session, user: UserRecord, project_id: int, task_id: int
) -> dict[str, Any]:
    """任务详情(尝试/租约/进度/诊断/快照/批量关系; 不暴露 lease_token)。"""
    ensure_project_access(db, user, project_id, "view")
    task = ensure_task_belongs(db, project_id, task_id)
    detail = task_summary(db, task)
    if task.calc_snapshot_id is not None:
        snapshot = tasks_domain.get_snapshot(db, task.calc_snapshot_id)
        detail["calc_snapshot"] = (
            {"id": snapshot.id, "random_seed": snapshot.random_seed} if snapshot is not None else None
        )
    else:
        detail["calc_snapshot"] = None

    attempts = tasks_domain.list_attempts(db, task.id)
    detail["attempts"] = [
        {
            "id": a.id,
            "attempt_no": a.attempt_no,
            "status": a.status,
            "worker_id": a.worker_id,
            "stop_reason": a.stop_reason,
            "started_at": a.started_at,
            "finished_at": a.finished_at,
        }
        for a in attempts
    ]
    lease = tasks_domain.get_active_lease_for_task(db, task.id)
    detail["current_lease"] = None
    if lease is not None:
        # 只暴露 acquired_by/renewed_at/expires_at, 不暴露 lease_token
        attempt = tasks_domain.get_attempt(db, lease.attempt_id)
        detail["current_lease"] = {
            "attempt_no": attempt.attempt_no if attempt else None,
            "acquired_by": lease.acquired_by,
            "renewed_at": lease.renewed_at,
            "expires_at": lease.expires_at,
        }

    attempt_no, percent, stage, detail_json = _progress_summary(db, task)
    detail["progress"] = {
        "attempt_no": attempt_no,
        "percent": percent,
        "stage": stage,
        "detail": detail_json,
        "updated_at": None,
        "source": "pg",
    }
    progress_row = tasks_domain.latest_progress_for_task(db, task.id)
    if progress_row is not None:
        detail["progress"]["updated_at"] = progress_row.updated_at

    diagnostics = tasks_domain.list_diagnostics(db, task.id)
    # stack_trace 与完整 context 仅对全局管理员返回(受控审计视角);
    # viewer/owner 一律返回 stack_trace=null, context 仅保留白名单字段
    is_admin = "admin" in identity_domain.user_roles(db, user.id)
    detail["diagnostics"] = [
        {
            "id": d.id,
            "level": d.level,
            "code": d.code,
            "message": d.message,
            "stack_trace": d.stack_trace if is_admin else None,
            "context": d.context if is_admin else _sanitize_diag_context(d.context),
            "attempt_id": d.attempt_id,
            "created_at": d.created_at,
        }
        for d in diagnostics
    ]

    child_rows = tasks_domain.list_child_tasks(db, task.id)
    children = [{"id": c.id, "status": c.status} for c in child_rows]
    parent_sample = tasks_domain.get_sample_task(db, task.id)
    parent_id = parent_sample.parent_task_id if parent_sample is not None else None
    detail["batch"] = {"parent_task_id": parent_id, "child_task_count": len(children), "children": children}
    return detail


def cancel_user_task(
    db: Session, user: UserRecord, project_id: int, task_id: int, reason: str = "user_cancel",
) -> TaskRecord:
    """取消任务(路由原顺序: edit 权限 → 归属 → 取消; 提交由用例层拥有)。"""
    ensure_project_access(db, user, project_id, "edit")
    ensure_task_belongs(db, project_id, task_id)
    return cancel_task(db, task_id, reason=reason, actor_id=user.id)


def retry_user_task(
    db: Session, user: UserRecord, project_id: int, task_id: int
) -> TaskRecord:
    """手动重试(路由原顺序: 归属 → 重试; 权限与提交由用例层拥有)。"""
    ensure_task_belongs(db, project_id, task_id)
    return retry_task(db, user, task_id)
