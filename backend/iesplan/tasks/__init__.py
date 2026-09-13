"""任务域公开门面（快照/任务/尝试/租约/进度/诊断/槽位/不确定性/队列，归属 tasks）。

外部只允许经本门面消费 contract、repository 协议、repository 实现函数与
队列可重建视图；不得导入 `iesplan.models`、services 或其他域的内部模块。
"""

from __future__ import annotations

#: persistence 延迟导出名: 本包 __init__ 不得在导入期装载 persistence
#: (persistence 直连 models, eager 导入会与 ORM 初始化形成循环;
#: 幂等键格式的唯一权威在 contracts, DDL 经本包引用)。
#: 首次属性访问时装载, 此后常驻 sys.modules。
_PERSISTENCE_EXPORTS: frozenset[str] = frozenset({
    "acquire_lease",
    "acquire_slot",
    "append_diagnostic",
    "bind_slot_attempt",
    "cancel_pending_tasks",
    "count_completed_samples",
    "count_tasks_by_status",
    "count_tasks_by_statuses",
    "count_tasks_by_type",
    "create_attempt",
    "has_running_tasks",
    "create_sample_row",
    "create_sample_task",
    "create_snapshot",
    "create_task",
    "create_uncertainty_snapshot",
    "ensure_slots",
    "fence_release_lease",
    "fence_renew_lease",
    "find_active_duplicate",
    "finish_attempt",
    "get_active_lease_for_attempt",
    "get_active_lease_for_task",
    "get_attempt",
    "get_latest_attempt",
    "get_lease_by_token",
    "get_progress",
    "get_running_attempt",
    "get_sample_task",
    "get_snapshot",
    "get_task",
    "get_task_by_idempotency",
    "latest_diagnostic",
    "latest_progress_for_task",
    "list_attempts",
    "list_child_tasks",
    "list_diagnostics",
    "list_recent_failed_tasks",
    "list_snapshots_for_version",
    "list_task_ids",
    "list_tasks",
    "pool_has_free_slot",
    "record_sample",
    "release_lease",
    "release_slot",
    "renew_lease",
    "revoke_leases_for_attempts",
    "set_task_status",
    "upsert_progress",
})


def __getattr__(name: str) -> object:
    """延迟导出 persistence 函数(首次访问时装载, 打破 models 初始化循环)。"""
    if name in _PERSISTENCE_EXPORTS:
        from iesplan.tasks import persistence
        return getattr(persistence, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


from iesplan.tasks import queue as _queue
from iesplan.tasks.contracts import (
    BUSINESS_OUTCOMES,
    COMPUTE_TYPES,
    IDEMPOTENCY_KEY_RE,
    IO_SLOT_CAPACITY,
    LEASE_TTL_SECONDS,
    POOL_BY_TYPE,
    TASK_TYPES,
    TERMINAL_STATUSES,
    VALID_TRANSITIONS,
    CalcSnapshotRecord,
    CancelDeniedError,
    ComputeSlotRecord,
    ExecutionUnavailableError,
    InvalidRequestError,
    SampleRecordRecord,
    SampleTaskRecord,
    StorageQuotaError,
    TaskAttemptRecord,
    TaskConflictError,
    TaskDiagnosticRecord,
    TaskLeaseRecord,
    TaskNotFoundError,
    TaskProgressRecord,
    TaskRecord,
    TaskStateError,
    UncertaintySnapshotRecord,
    check_transition,
    map_business_outcome,
)


QUEUE_COMPUTE = _queue.QUEUE_COMPUTE
QUEUE_IO = _queue.QUEUE_IO
clear_cancel = _queue.clear_cancel
dequeue = _queue.dequeue
enqueue = _queue.enqueue
force_memory = _queue.force_memory
get_cancel = _queue.get_cancel
get_heartbeat = _queue.get_heartbeat
get_queue_progress = _queue.get_progress
queue_position = _queue.queue_position
queue_status = _queue.queue_status
remove = _queue.remove
requeue = _queue.requeue
set_cancel = _queue.set_cancel
set_heartbeat = _queue.set_heartbeat
set_queue_progress = _queue.set_progress

__all__ = [
    "QUEUE_COMPUTE",
    "QUEUE_IO",
    "BUSINESS_OUTCOMES",
    "COMPUTE_TYPES",
    "IDEMPOTENCY_KEY_RE",
    "IO_SLOT_CAPACITY",
    "LEASE_TTL_SECONDS",
    "POOL_BY_TYPE",
    "TASK_TYPES",
    "TERMINAL_STATUSES",
    "VALID_TRANSITIONS",
    "CalcSnapshotRecord",
    "CancelDeniedError",
    "ComputeSlotRecord",
    "ExecutionUnavailableError",
    "InvalidRequestError",
    "SampleRecordRecord",
    "SampleTaskRecord",
    "StorageQuotaError",
    "TaskAttemptRecord",
    "TaskConflictError",
    "TaskDiagnosticRecord",
    "TaskLeaseRecord",
    "TaskNotFoundError",
    "TaskProgressRecord",
    "TaskRecord",
    "TaskStateError",
    "UncertaintySnapshotRecord",
    "check_transition",
    "map_business_outcome",
    "acquire_lease",
    "acquire_slot",
    "append_diagnostic",
    "bind_slot_attempt",
    "cancel_pending_tasks",
    "clear_cancel",
    "count_completed_samples",
    "count_tasks_by_status",
    "count_tasks_by_statuses",
    "count_tasks_by_type",
    "create_attempt",
    "has_running_tasks",
    "create_sample_row",
    "create_sample_task",
    "create_snapshot",
    "create_task",
    "create_uncertainty_snapshot",
    "dequeue",
    "enqueue",
    "ensure_slots",
    "fence_release_lease",
    "fence_renew_lease",
    "find_active_duplicate",
    "finish_attempt",
    "force_memory",
    "get_active_lease_for_attempt",
    "get_active_lease_for_task",
    "get_attempt",
    "get_cancel",
    "get_heartbeat",
    "get_latest_attempt",
    "get_lease_by_token",
    "get_progress",
    "get_queue_progress",
    "get_running_attempt",
    "get_sample_task",
    "get_snapshot",
    "get_task",
    "get_task_by_idempotency",
    "latest_diagnostic",
    "latest_progress_for_task",
    "list_attempts",
    "list_child_tasks",
    "list_diagnostics",
    "list_recent_failed_tasks",
    "list_snapshots_for_version",
    "list_task_ids",
    "list_tasks",
    "pool_has_free_slot",
    "queue_position",
    "queue_status",
    "record_sample",
    "release_lease",
    "release_slot",
    "remove",
    "renew_lease",
    "requeue",
    "revoke_leases_for_attempts",
    "set_cancel",
    "set_heartbeat",
    "set_queue_progress",
    "set_task_status",
    "upsert_progress",
]
