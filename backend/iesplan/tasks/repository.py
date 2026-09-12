"""任务域 repository 协议（快照/任务/尝试/租约/进度/诊断/槽位/不确定性表）。

实现规则（切片 5 落实）：
- 只做查询、写入、flush；租约/槽位等并发竞争用 savepoint 化为读取
  或明确冲突；绝不 commit/rollback；
- 状态机转换合法性由领域服务判定后传入，非法转换抛 TaskConflictError；
- 幂等：按 (project_id, idempotency_key) 查询已有任务，创建重复键抛
  TaskConflictError（由 application 转为返回已有任务，切片 6）。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import datetime
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
        self,
        db: Session,
        project_id: int,
        *,
        task_type: str | None = None,
        status: str | None = None,
        outcome: str | None = None,
        limit: int = 50,
        cursor: int | None = None,
    ) -> list[TaskRecord]:
        """任务列表（过滤 + 游标分页，id 倒序；limit 原样透传，调用方传 limit+1 探下一页）。"""
        ...

    def find_active_duplicate(
        self, db: Session, project_id: int, task_type: str, snapshot_id: int
    ) -> TaskRecord | None:
        """同（项目，类型，快照）的最新非终态任务；无返回 None（重复提交去重）。"""
        ...

    def count_tasks_by_statuses(self, db: Session, statuses: Collection[str]) -> int:
        """按状态集合计数任务（清理建议等聚合读）。"""
        ...

    def list_child_tasks(self, db: Session, parent_task_id: int) -> list[TaskRecord]:
        """批量父任务的子任务（经 sample_tasks 关联；调用方按状态过滤）。"""
        ...

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
        deadline: datetime | None = None,
    ) -> TaskRecord: ...

    def set_task_status(
        self,
        db: Session,
        task_id: int,
        status: str,
        *,
        business_outcome: str | None = None,
    ) -> TaskRecord:
        """推进任务状态并刷新 updated_at；缺失抛 TaskNotFoundError。

        business_outcome=None 表示清空（与直接赋值语义一致）。
        """
        ...

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

    def list_snapshots_for_version(self, db: Session, project_version_id: int) -> list[CalcSnapshotRecord]:
        """取某项目版本的快照（id 倒序；快照输入复用扫描用）。"""
        ...

    def create_attempt(self, db: Session, *, task_id: int, worker_id: str | None = None) -> TaskAttemptRecord:
        """追加尝试行（含 attempt_no 分配与 task.attempt_count 推进）。"""
        ...

    def finish_attempt(
        self, db: Session, attempt_id: int, status: str, *, stop_reason: str | None = None
    ) -> TaskAttemptRecord: ...

    def get_attempt(self, db: Session, attempt_id: int) -> TaskAttemptRecord | None:
        """按主键取尝试；不存在返回 None。"""
        ...

    def get_latest_attempt(
        self, db: Session, task_id: int, attempt_id: int | None = None
    ) -> TaskAttemptRecord | None:
        """取任务最新尝试（attempt_no 最大；指定 attempt_id 则限定该行）；无返回 None。"""
        ...

    def get_running_attempt(self, db: Session, task_id: int) -> TaskAttemptRecord | None:
        """取任务当前 running 尝试（attempt_no 最大）；无返回 None。"""
        ...

    def list_attempts(self, db: Session, task_id: int) -> list[TaskAttemptRecord]:
        """取任务全部尝试（attempt_no 升序；详情展示用）。"""
        ...

    def acquire_lease(
        self,
        db: Session,
        *,
        attempt_id: int,
        lease_token: str,
        acquired_by: str | None = None,
        ttl_seconds: int = 60,
    ) -> TaskLeaseRecord:
        """领取租约；同 attempt 已有 active 租约抛 TaskConflictError。"""
        ...

    def renew_lease(self, db: Session, attempt_id: int, lease_token: str) -> TaskLeaseRecord:
        """续租（token 不符抛 TaskConflictError）。"""
        ...

    def release_lease(
        self, db: Session, attempt_id: int, lease_token: str, *, status: str = "released"
    ) -> TaskLeaseRecord:
        """释放租约（终态默认 released，失败收拢传 revoked）；token 失配或非 active 抛 TaskConflictError。"""
        ...

    def get_active_lease_for_attempt(self, db: Session, attempt_id: int) -> TaskLeaseRecord | None:
        """取尝试的 active 租约；无返回 None。"""
        ...

    def get_active_lease_for_task(self, db: Session, task_id: int) -> TaskLeaseRecord | None:
        """取任务各尝试中最新的 active 租约；无返回 None（详情展示用）。"""
        ...

    def get_lease_by_token(self, db: Session, lease_token: str) -> TaskLeaseRecord | None:
        """按 token 取租约（字符串比对）；无返回 None（fencing 校验用）。"""
        ...

    def get_progress(self, db: Session, attempt_id: int) -> TaskProgressRecord | None:
        """取尝试进度行；无返回 None。"""
        ...

    def latest_progress_for_task(self, db: Session, task_id: int) -> TaskProgressRecord | None:
        """取任务最新尝试的进度行（attempt_no 最大）；无返回 None。"""
        ...

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
        stack_trace: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> TaskDiagnosticRecord: ...

    def list_diagnostics(self, db: Session, task_id: int) -> list[TaskDiagnosticRecord]: ...

    def latest_diagnostic(self, db: Session, task_id: int, code: str) -> TaskDiagnosticRecord | None:
        """取任务某 code 最新诊断；无返回 None（trace 关联等单点读）。"""
        ...

    def ensure_slots(self, db: Session, pools: Mapping[str, int]) -> None:
        """惰性初始化槽表（每池按并发额度建 capacity 行，幂等）。"""
        ...

    def acquire_slot(self, db: Session, pool_name: str) -> ComputeSlotRecord | None:
        """取可用槽位并标记占用（行锁，in_use+1，不绑定尝试）；无可用返回 None（不抛错）。"""
        ...

    def bind_slot_attempt(self, db: Session, slot_id: int, attempt_id: int) -> ComputeSlotRecord:
        """槽绑定尝试（领取成功后）；缺失抛 TaskNotFoundError。"""
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

    def get_sample_task(self, db: Session, sample_id: int) -> SampleTaskRecord | None:
        """按主键取采样任务；不存在返回 None（批量父子关系查询用）。"""
        ...

    def record_sample(
        self,
        db: Session,
        *,
        sample_task_id: int,
        variable_name: str,
        value: float,
        unit: str | None = None,
    ) -> SampleRecordRecord: ...
