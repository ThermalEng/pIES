"""任务域 repository 协议（快照/任务/尝试/租约/进度/诊断/槽位/不确定性表）。

实现规则（切片 5 落实）：
- 只做查询、写入、flush；租约/槽位等并发竞争用 savepoint 化为读取
  或明确冲突；绝不 commit/rollback；
- 状态机转换合法性由领域服务判定后传入，非法转换抛 TaskConflictError；
- 幂等：按 (project_id, idempotency_key) 查询已有任务，创建重复键抛
  TaskConflictError（由 application 转为返回已有任务，切片 6）。
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy.orm import Session

from iesplan.tasks.contracts import (
    CalcSnapshotRecord,
    ComputeSlotRecord,
    SampleRecordRecord,
    SampleTaskRecord,
    TaskAttemptRecord,
    TaskDiagnosticRecord,
    TaskLeaseRecord,
    TaskProgressRecord,
    TaskRecord,
    UncertaintySnapshotRecord,
)


class TasksRepository(Protocol):
    """任务聚合 repository 协议（无状态方法组，db 由调用方事务拥有）。"""

    def get_task(self, db: Session, task_id: int) -> TaskRecord | None: ...

    def get_task_by_idempotency(
        self, db: Session, project_id: int, idempotency_key: str
    ) -> TaskRecord | None: ...

    def list_tasks(
        self, db: Session, project_id: int, *, limit: int = 50, cursor: int | None = None
    ) -> list[TaskRecord]: ...

    def create_task(
        self,
        db: Session,
        *,
        project_id: int,
        type: str,
        requested_by: int,
        idempotency_key: str | None = None,
        calc_snapshot_id: int | None = None,
        priority: int = 0,
        max_attempts: int = 3,
    ) -> TaskRecord: ...

    def set_task_status(
        self,
        db: Session,
        task_id: int,
        status: str,
        *,
        business_outcome: str | None = None,
        stop_reason: str | None = None,
    ) -> TaskRecord: ...

    def create_snapshot(
        self,
        db: Session,
        *,
        project_version_id: int,
        dataset_version_ids: list[int],
        calc_config_snapshot: dict[str, Any],
        random_seed: int,
        created_by: int,
        program_version: str | None = None,
        extension_versions: dict[str, Any] | None = None,
        tolerances: dict[str, Any] | None = None,
        canonical_assembly_text: str | None = None,
        assembly_receipt: dict[str, Any] | None = None,
    ) -> CalcSnapshotRecord:
        """创建不可变快照（缺 canonical 二件套的新快照由 Worker 拒绝，不在本层）。"""
        ...

    def get_snapshot(self, db: Session, snapshot_id: int) -> CalcSnapshotRecord | None: ...

    def create_attempt(self, db: Session, *, task_id: int, worker_id: str | None = None) -> TaskAttemptRecord:
        """追加尝试行（含 attempt_no 分配与 task.attempt_count 推进）。"""
        ...

    def finish_attempt(
        self, db: Session, attempt_id: int, status: str, *, stop_reason: str | None = None
    ) -> TaskAttemptRecord: ...

    def acquire_lease(
        self, db: Session, *, attempt_id: int, lease_token: str, acquired_by: str | None = None
    ) -> TaskLeaseRecord:
        """领取租约；同 attempt 已有 active 租约抛 TaskConflictError。"""
        ...

    def renew_lease(self, db: Session, attempt_id: int, lease_token: str) -> TaskLeaseRecord:
        """续租（token 不符抛 TaskConflictError）。"""
        ...

    def release_lease(self, db: Session, attempt_id: int, lease_token: str) -> TaskLeaseRecord: ...

    def upsert_progress(
        self,
        db: Session,
        *,
        attempt_id: int,
        progress_percent: float,
        stage: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> TaskProgressRecord: ...

    def append_diagnostic(
        self,
        db: Session,
        *,
        task_id: int,
        level: str,
        message: str,
        attempt_id: int | None = None,
        code: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> TaskDiagnosticRecord: ...

    def list_diagnostics(self, db: Session, task_id: int) -> list[TaskDiagnosticRecord]: ...

    def acquire_slot(self, db: Session, pool_name: str) -> ComputeSlotRecord | None:
        """取可用槽位并标记占用；无可用返回 None（不抛错）。"""
        ...

    def release_slot(self, db: Session, attempt_id: int) -> None: ...

    def create_uncertainty_snapshot(
        self,
        db: Session,
        *,
        calc_snapshot_id: int,
        method: str,
        n_samples: int,
        random_seed: int,
        distributions: dict[str, Any],
        created_by: int,
    ) -> UncertaintySnapshotRecord: ...

    def create_sample_task(
        self,
        db: Session,
        *,
        uncertainty_snapshot_id: int,
        sample_index: int,
        parent_task_id: int | None = None,
        parent_sample_id: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> SampleTaskRecord: ...

    def record_sample(
        self,
        db: Session,
        *,
        sample_task_id: int,
        variable_name: str,
        value: float,
        unit: str | None = None,
    ) -> SampleRecordRecord: ...
