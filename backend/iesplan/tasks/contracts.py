"""任务域公开契约(快照/任务/尝试/租约/进度/诊断/槽位/不确定性表，归属 tasks)。

- 任务执行只消费不可变快照；快照一经创建不得修改；
- 0.7.0 前非规范 assembly_text 字段不在本契约中（旧快照审计残留，
  新快照不得写入/消费）；
- 只含不可变值对象、领域错误与无状态纯规则（类型/状态机/结局映射）；
  不导入 ORM、Session、services 或 application。
- 任务状态、业务结局映射与任务错误唯一权威归 tasks 域；
  application.tasks 只做提交/幂等/快照/事务编排，直接复用本模块。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from iesplan.core.diagnostics import (
    SEVERITY_BLOCKING,
    SEVERITY_ERROR,
    SYS_STORE_QUOTA_EXCEEDED,
)
from iesplan.core.errors import AppError, ConflictError, NotFoundError
from iesplan.core.patterns import IDEMPOTENCY_KEY_RE as IDEMPOTENCY_KEY_RE


class TaskNotFoundError(NotFoundError):
    """任务/快照/尝试/租约不存在（沿用基类诊断码，不新增码）。"""


class TaskConflictError(ConflictError):
    """任务状态机冲突或幂等/并发冲突（沿用基类诊断码，不新增码）。"""


class InvalidRequestError(AppError):
    """任务请求/参数校验失败(HTTP 400)。"""

    code = "TASK-REQ-001"
    http_status = 400
    severity = SEVERITY_ERROR
    message_key = "ies.diag.param.invalid"


class TaskStateError(AppError):
    """任务状态机非法迁移(HTTP 409)。"""

    code = "TASK-STATE-001"
    http_status = 409
    severity = SEVERITY_ERROR
    message_key = "ies.diag.task.state_conflict"


class CancelDeniedError(AppError):
    """终态任务不可取消(HTTP 409)。"""

    code = "TASK-CANCEL-001"
    http_status = 409
    severity = SEVERITY_ERROR
    message_key = "ies.diag.task.cancel_denied"


class StorageQuotaError(AppError):
    """任务提交存储门禁未通过(HTTP 409; blocking 级 SYS-STORE-003)。"""

    code = SYS_STORE_QUOTA_EXCEEDED
    http_status = 409
    severity = SEVERITY_BLOCKING
    message_key = "ies.diag.store.quota_exceeded"


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


# ---------------------------------------------------------------------------
# 任务类型 / 队列池 / 状态机（单领域规则唯一权威；application 只复用）
# ---------------------------------------------------------------------------

#: 全部任务类型
TASK_TYPES: tuple[str, ...] = (
    "calc",
    "optimization",
    "uncertainty",
    "analysis",
    "import",
    "export",
    "report",
    "dataset_build",
)
#: 计算类任务(必须绑定 calc_snapshot_id)
COMPUTE_TYPES: tuple[str, ...] = ("calc", "optimization", "uncertainty", "analysis")
#: 任务类型 → 队列池
POOL_BY_TYPE: dict[str, str] = {
    "calc": "compute",
    "optimization": "compute",
    "uncertainty": "compute",
    "analysis": "compute",
    "report": "io",
    "dataset_build": "io",
    "export": "io",
    "import": "io",
}
#: 终态(终态不可再迁移)
TERMINAL_STATUSES: tuple[str, ...] = ("completed", "cancelled", "timed_out", "failed")
#: 状态机合法迁移
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"completed", "cancelling", "queued", "timed_out", "failed"}),
    # cancelling → cancelled 由确认取消完成; → completed 为取消竞态
    # 下先落终态者为准的异常路径
    "cancelling": frozenset({"cancelled", "completed", "timed_out", "failed"}),
}
#: 租约 TTL(秒, 默认 60 s)
LEASE_TTL_SECONDS = 60
#: io 池默认并发槽
IO_SLOT_CAPACITY = 2


def check_transition(task: TaskRecord, new_status: str) -> None:
    """状态机校验: 终态不可迁移; 非法跳转抛 TaskStateError。"""
    if task.status in TERMINAL_STATUSES:
        raise TaskStateError(
            "终态任务不可迁移状态",
            code="TASK-STATE-002",
            params={"task_id": task.id, "status": task.status},
            location={"object_type": "task", "object_id": task.id},
        )
    if new_status not in VALID_TRANSITIONS.get(task.status, frozenset()):
        raise TaskStateError(
            "非法状态迁移",
            code="TASK-STATE-003",
            params={"task_id": task.id, "from": task.status, "to": new_status},
            location={"object_type": "task", "object_id": task.id},
        )


#: 求解器状态 → 业务结局映射
_SOLVER_OUTCOME: dict[str, str] = {
    "OPTIMAL": "normal_completion",
    "TIME_LIMIT_WITH_INCUMBENT": "restricted_results",
    "NO_FEASIBLE_FOUND": "no_recommendation",
    "INFEASIBLE_BY_IRR_FLOOR": "no_recommendation",
    "BASE_INFEASIBLE": "no_recommendation",
    "MODEL_AUDIT_FAIL": "insufficient_evidence",
    "NO_PARETO_FEASIBLE": "no_feasible_multi_objective",
    "PARTIAL_BATCH": "partial_batch",
}


def map_business_outcome(solver_status: str) -> str:
    """求解器状态 → 业务结局(未知状态保守视为 normal_completion)。"""
    return _SOLVER_OUTCOME.get(solver_status, "normal_completion")
