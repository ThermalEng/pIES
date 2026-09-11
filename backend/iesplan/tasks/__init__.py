"""任务域公开门面（快照/任务/尝试/租约/进度/诊断/槽位/不确定性表，归属 tasks）。

外部只允许经本门面消费 contract 与 repository 协议；不得导入本域
repository 实现（切片 5 落实）、`iesplan.models` 或 services。
"""

from __future__ import annotations

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
]
