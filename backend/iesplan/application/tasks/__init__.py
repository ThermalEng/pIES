"""任务用例族(application/tasks): 提交/取消/重试/租约。

复制自 ``services.tasks`` + ``services.queue`` 的编排, 旧服务只读保留。
顶层用例拥有事务提交/回滚, 内部步骤只 flush。
"""

from iesplan.application.tasks.submissions import (
    COMPUTE_TYPES,
    IO_SLOT_CAPACITY,
    LEASE_TTL_SECONDS,
    POOL_BY_TYPE,
    TASK_TYPES,
    TERMINAL_STATUSES,
    VALID_TRANSITIONS,
    CancelDeniedError,
    Claim,
    InvalidRequestError,
    StorageEstimate,
    StorageQuotaError,
    TaskStateError,
    acknowledge_cancel,
    acquire_slot,
    cancel_task,
    claim_task,
    ensure_task_belongs,
    estimate_storage,
    list_cleanup_suggestions,
    release_slot,
    retry_task,
    submit_task,
)

__all__ = [
    "COMPUTE_TYPES",
    "IO_SLOT_CAPACITY",
    "LEASE_TTL_SECONDS",
    "POOL_BY_TYPE",
    "TASK_TYPES",
    "TERMINAL_STATUSES",
    "VALID_TRANSITIONS",
    "CancelDeniedError",
    "Claim",
    "InvalidRequestError",
    "StorageEstimate",
    "StorageQuotaError",
    "TaskStateError",
    "acknowledge_cancel",
    "acquire_slot",
    "cancel_task",
    "claim_task",
    "ensure_task_belongs",
    "estimate_storage",
    "list_cleanup_suggestions",
    "release_slot",
    "retry_task",
    "submit_task",
]
