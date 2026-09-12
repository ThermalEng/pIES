"""任务域公开门面（快照/任务/尝试/租约/进度/诊断/槽位/不确定性表，归属 tasks）。

外部只允许经本门面消费 contract、repository 协议与 repository 实现函数；
不得导入 `iesplan.models`、services 或其他域的内部模块。
"""

from __future__ import annotations

from iesplan.tasks import persistence
from iesplan.tasks.contracts import (
    CalcSnapshotRecord,
    ComputeSlotRecord,
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
count_tasks_by_statuses = persistence.count_tasks_by_statuses
create_attempt = persistence.create_attempt
create_sample_task = persistence.create_sample_task
get_sample_task = persistence.get_sample_task
create_snapshot = persistence.create_snapshot
create_task = persistence.create_task
create_uncertainty_snapshot = persistence.create_uncertainty_snapshot
ensure_slots = persistence.ensure_slots
find_active_duplicate = persistence.find_active_duplicate
finish_attempt = persistence.finish_attempt
get_active_lease_for_attempt = persistence.get_active_lease_for_attempt
get_active_lease_for_task = persistence.get_active_lease_for_task
get_attempt = persistence.get_attempt
get_latest_attempt = persistence.get_latest_attempt
get_lease_by_token = persistence.get_lease_by_token
get_progress = persistence.get_progress
get_running_attempt = persistence.get_running_attempt
get_snapshot = persistence.get_snapshot
get_task = persistence.get_task
get_task_by_idempotency = persistence.get_task_by_idempotency
latest_diagnostic = persistence.latest_diagnostic
latest_progress_for_task = persistence.latest_progress_for_task
list_attempts = persistence.list_attempts
list_child_tasks = persistence.list_child_tasks
list_task_ids = persistence.list_task_ids
list_diagnostics = persistence.list_diagnostics
list_snapshots_for_version = persistence.list_snapshots_for_version
list_tasks = persistence.list_tasks
record_sample = persistence.record_sample
release_lease = persistence.release_lease
release_slot = persistence.release_slot
renew_lease = persistence.renew_lease
set_task_status = persistence.set_task_status
upsert_progress = persistence.upsert_progress

__all__ = [
    "CalcSnapshotRecord",
    "ComputeSlotRecord",
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
    "count_tasks_by_statuses",
    "create_attempt",
    "create_sample_task",
    "create_snapshot",
    "create_task",
    "create_uncertainty_snapshot",
    "ensure_slots",
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
    "list_snapshots_for_version",
    "list_task_ids",
    "list_tasks",
    "record_sample",
    "release_lease",
    "release_slot",
    "renew_lease",
    "set_task_status",
    "upsert_progress",
]
