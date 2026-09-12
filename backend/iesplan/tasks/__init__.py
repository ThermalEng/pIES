"""任务域公开门面（快照/任务/尝试/租约/进度/诊断/槽位/不确定性/队列，归属 tasks）。

外部只允许经本门面消费 contract、repository 协议、repository 实现函数与
队列可重建视图；不得导入 `iesplan.models`、services 或其他域的内部模块。
"""

from __future__ import annotations

from iesplan.tasks import persistence
from iesplan.tasks import queue as _queue
from iesplan.tasks.contracts import (
    IDEMPOTENCY_KEY_RE,
    CalcSnapshotRecord,
    ComputeSlotRecord,
    MaintenanceActionRecord,
    RetentionRuleRecord,
    SampleRecordRecord,
    SampleTaskRecord,
    TaskAttemptRecord,
    TaskConflictError,
    TaskDiagnosticRecord,
    TaskLeaseRecord,
    TaskNotFoundError,
    TaskProgressRecord,
    TaskRecord,
    UncertaintySnapshotRecord,
)
from iesplan.tasks.repository import TasksRepository

acquire_lease = persistence.acquire_lease
acquire_slot = persistence.acquire_slot
append_diagnostic = persistence.append_diagnostic
bind_slot_attempt = persistence.bind_slot_attempt
cancel_pending_tasks = persistence.cancel_pending_tasks
count_completed_samples = persistence.count_completed_samples
count_tasks_by_status = persistence.count_tasks_by_status
count_tasks_by_statuses = persistence.count_tasks_by_statuses
count_tasks_by_type = persistence.count_tasks_by_type
create_attempt = persistence.create_attempt
has_running_tasks = persistence.has_running_tasks
create_sample_row = persistence.create_sample_row
create_sample_task = persistence.create_sample_task
create_snapshot = persistence.create_snapshot
create_task = persistence.create_task
create_uncertainty_snapshot = persistence.create_uncertainty_snapshot
ensure_slots = persistence.ensure_slots
fence_release_lease = persistence.fence_release_lease
fence_renew_lease = persistence.fence_renew_lease
find_active_duplicate = persistence.find_active_duplicate
finish_attempt = persistence.finish_attempt
get_active_lease_for_attempt = persistence.get_active_lease_for_attempt
get_active_lease_for_task = persistence.get_active_lease_for_task
get_attempt = persistence.get_attempt
get_latest_attempt = persistence.get_latest_attempt
get_lease_by_token = persistence.get_lease_by_token
get_progress = persistence.get_progress
get_project_version_content_id = persistence.get_project_version_content_id
get_running_attempt = persistence.get_running_attempt
get_sample_task = persistence.get_sample_task
get_snapshot = persistence.get_snapshot
get_task = persistence.get_task
get_task_by_idempotency = persistence.get_task_by_idempotency
latest_diagnostic = persistence.latest_diagnostic
latest_progress_for_task = persistence.latest_progress_for_task
list_active_retention_rules = persistence.list_active_retention_rules
list_attempts = persistence.list_attempts
list_child_tasks = persistence.list_child_tasks
list_diagnostics = persistence.list_diagnostics
list_maintenance_actions = persistence.list_maintenance_actions
list_recent_failed_tasks = persistence.list_recent_failed_tasks
list_snapshots_for_version = persistence.list_snapshots_for_version
list_task_ids = persistence.list_task_ids
list_tasks = persistence.list_tasks
pool_has_free_slot = persistence.pool_has_free_slot
record_maintenance_action = persistence.record_maintenance_action
record_sample = persistence.record_sample
release_lease = persistence.release_lease
release_slot = persistence.release_slot
renew_lease = persistence.renew_lease
revoke_leases_for_attempts = persistence.revoke_leases_for_attempts
set_task_status = persistence.set_task_status
upsert_progress = persistence.upsert_progress

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
    "IDEMPOTENCY_KEY_RE",
    "CalcSnapshotRecord",
    "ComputeSlotRecord",
    "MaintenanceActionRecord",
    "RetentionRuleRecord",
    "SampleRecordRecord",
    "SampleTaskRecord",
    "TaskAttemptRecord",
    "TaskConflictError",
    "TaskDiagnosticRecord",
    "TaskLeaseRecord",
    "TaskNotFoundError",
    "TaskProgressRecord",
    "TaskRecord",
    "TasksRepository",
    "UncertaintySnapshotRecord",
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
    "get_project_version_content_id",
    "get_queue_progress",
    "get_running_attempt",
    "get_sample_task",
    "get_snapshot",
    "get_task",
    "get_task_by_idempotency",
    "latest_diagnostic",
    "latest_progress_for_task",
    "list_active_retention_rules",
    "list_attempts",
    "list_child_tasks",
    "list_diagnostics",
    "list_maintenance_actions",
    "list_recent_failed_tasks",
    "list_snapshots_for_version",
    "list_task_ids",
    "list_tasks",
    "pool_has_free_slot",
    "queue_position",
    "queue_status",
    "record_maintenance_action",
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
