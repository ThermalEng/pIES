"""任务域公开契约(快照/任务/尝试/租约/进度/诊断/槽位/不确定性表，归属 tasks)。

- 任务执行只消费不可变快照；快照一经创建不得修改；
- 0.7.0 前非规范 assembly_text 字段不在本契约中（旧快照审计残留，
  新快照不得写入/消费）；
- 只含不可变值对象与领域错误；不导入 ORM、Session、services 或 application。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from iesplan.core.errors import ConflictError, NotFoundError


class TaskNotFoundError(NotFoundError):
    """任务/快照/尝试/租约不存在（沿用基类诊断码，不新增码）。"""


class TaskConflictError(ConflictError):
    """任务状态机冲突或幂等/并发冲突（沿用基类诊断码，不新增码）。"""


@dataclass(frozen=True, slots=True)
class CalcSnapshotRecord:
    """计算快照（calc_snapshots 表公开视图，不可变，任务唯一输入）。"""

    id: int
    project_version_id: int
    dataset_version_ids: tuple[int, ...]
    calc_config_snapshot: dict[str, Any]
    random_seed: int
    program_version: str | None = None
    extension_versions: dict[str, Any] | None = None
    tolerances: dict[str, Any] | None = None
    canonical_assembly_text: str | None = None
    assembly_receipt: dict[str, Any] | None = None
    created_by: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "dataset_version_ids", tuple(self.dataset_version_ids))


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """任务行（tasks 表公开视图）。"""

    id: int
    project_id: int
    type: str
    status: str
    requested_by: int
    business_outcome: str | None = None
    idempotency_key: str | None = None
    calc_snapshot_id: int | None = None
    priority: int = 0
    deadline: str | None = None
    attempt_count: int = 0
    max_attempts: int = 3
    superseded_by_task_id: int | None = None
    requested_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class TaskAttemptRecord:
    """任务尝试行（task_attempts 表公开视图）。"""

    id: int
    task_id: int
    attempt_no: int
    status: str
    worker_id: str | None = None
    stop_reason: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass(frozen=True, slots=True)
class TaskLeaseRecord:
    """任务租约（task_leases 表公开视图；token 为不透明字符串）。"""

    id: int
    attempt_id: int
    lease_token: str
    status: str
    acquired_by: str | None = None
    expires_at: str | None = None
    renewed_at: str | None = None


@dataclass(frozen=True, slots=True)
class TaskProgressRecord:
    """任务进度（task_progress 表公开视图，每尝试至多一行）。"""

    id: int
    attempt_id: int
    progress_percent: float
    stage: str | None = None
    detail: dict[str, Any] | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class TaskDiagnosticRecord:
    """任务诊断（task_diagnostics 表公开视图，不可变）。"""

    id: int
    task_id: int
    level: str
    message: str
    attempt_id: int | None = None
    code: str | None = None
    stack_trace: str | None = None
    context: dict[str, Any] | None = None
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class ComputeSlotRecord:
    """计算并发槽（compute_slots 表公开视图）。"""

    id: int
    pool_name: str
    status: str
    capacity: int
    in_use: int
    current_attempt_id: int | None = None


@dataclass(frozen=True, slots=True)
class UncertaintySnapshotRecord:
    """不确定性快照（uncertainty_snapshots 表公开视图，不可变）。"""

    id: int
    calc_snapshot_id: int
    method: str
    n_samples: int
    random_seed: int
    distributions: dict[str, Any]
    created_by: int = 0


@dataclass(frozen=True, slots=True)
class SampleTaskRecord:
    """采样任务（sample_tasks 表公开视图）。"""

    id: int
    uncertainty_snapshot_id: int
    sample_index: int
    status: str
    parent_task_id: int | None = None
    parent_sample_id: int | None = None
    depth: int = 0
    params: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SampleRecordRecord:
    """样本记录（sample_records 表公开视图）。"""

    id: int
    sample_task_id: int
    variable_name: str
    value: float
    unit: str | None = None
