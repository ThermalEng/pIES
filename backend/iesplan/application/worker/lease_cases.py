"""Worker 租约/队列/结果提交用例(application/worker.lease_cases): 领取与收拢转调。

本模块收拢 ``iesplan.worker.lease`` 与 ``iesplan.worker.main`` 原先对
可重建视图与任务服务的直接调用, 只做转调, 不新增校验/hash/回退:

- 领取/进度/完成/失败/槽释放 → ``services.tasks``;
- 出队/心跳/取消信号清除 → ``services.queue``(可重建视图);
- 证据对象写入/引用 → ``storage.put_object`` / ``storage.add_ref``。

``Claim`` / ``TaskStateError`` / ``LEASE_TTL_SECONDS`` 由旧服务原样重导出,
worker 层经本模块取用, 不再直连 ``services.*``。

依赖方向: worker → application → (services/storage)。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from iesplan.core.diagnostics import SEVERITY_ERROR
from iesplan.services import queue as queue_service
from iesplan.services import tasks as tasks_service
from iesplan.storage import add_ref, put_object

#: 与 tasks_service.Claim 同构(worker 经本模块取用, 不直连 services)
Claim = tasks_service.Claim
#: 任务状态机错误(worker 经本模块取用)
TaskStateError = tasks_service.TaskStateError
#: 租约 TTL(秒, 与 tasks_service.LEASE_TTL_SECONDS 同值)
LEASE_TTL_SECONDS = tasks_service.LEASE_TTL_SECONDS


def acquire_task(db: Session, task_id: int, worker_id: str) -> Claim | None:
    """领取任务: 占槽 + 建尝试 + 建租约(fencing token) + 任务 running。"""
    return tasks_service.claim_and_run(db, task_id, worker_id)


def record_task_progress(
    db: Session,
    task_id: int,
    stage: str,
    percent: float,
    detail: dict[str, Any] | None = None,
    *,
    attempt_id: int | None = None,
) -> Any:
    """记录任务进度(PG UPSERT + Redis 秒级进度, 转调 tasks 服务)。"""
    return tasks_service.record_progress(
        db, task_id, stage, percent, detail, attempt_id=attempt_id
    )


def complete_task(db: Session, task_id: int, *, outcome: str | None = None) -> Any:
    """任务正常完成(转调 tasks 服务, 含取消竞态幂等)。"""
    return tasks_service.complete_task(db, task_id, outcome=outcome)


def fail_task(
    db: Session,
    task_id: int,
    *,
    code: str | None = None,
    message: str = "",
    stack_trace: str | None = None,
    level: str = SEVERITY_ERROR,
    outcome: str | None = None,
) -> Any:
    """任务确定性失败收拢(转调 tasks 服务)。"""
    return tasks_service.fail_task(
        db, task_id, code=code, message=message, stack_trace=stack_trace,
        level=level, outcome=outcome,
    )


def release_slot(db: Session, attempt_id: int) -> None:
    """释放尝试占用的并发槽(转调 tasks 服务)。"""
    tasks_service.release_slot(db, attempt_id)


def clear_cancel_signal(task_id: int) -> None:
    """清除任务取消信号(可重建视图, 转调 queue 服务)。"""
    queue_service.clear_cancel(task_id)


def store_result_blob(db: Session, blob: bytes, *, actor_id: int | None) -> int:
    """证据载荷写入对象存储, 返回对象 id(转调 storage 公开门面)。"""
    return put_object(
        db, blob, "application/json", source_category="evidence",
        purpose="evidence_package", actor_id=actor_id,
    ).id


def attach_result_ref(
    db: Session, object_id: int, evidence_id: int, *, actor_id: int | None
) -> Any:
    """证据包对象引用挂接(转调 storage 公开门面)。"""
    return add_ref(
        db, object_id, "evidence_package", evidence_id,
        purpose="evidence_package", actor_id=actor_id,
    )


def dequeue_task(pool: str) -> int | None:
    """按入队序(FIFO)领取一个任务, 返回 task_id; 队列空返回 None。"""
    return queue_service.dequeue(pool)


def publish_heartbeat(worker_id: str, payload: dict[str, Any], ttl: int) -> None:
    """写 Worker 心跳(可重建视图, 转调 queue 服务)。"""
    queue_service.set_heartbeat(worker_id, payload, ttl)
