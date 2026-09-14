"""Worker 边界用例包(application/worker): Worker 消费的阶段网关。

Worker(``iesplan.worker``)只经本包推进, 不再直连 ``services.*`` 与
``models.*``; 依赖方向: worker → application → (storage/领域门面)。

本包只导出 Worker 真正消费的阶段命令与不可变结果契约, 不导出实现
模块对象(``attempt_cases``/``lease_cases``/``evidence_cases``/
``report_cases``/``runner_cases``)与 repository 级原子操作(行级
领取/完成/失败/证据/索引/诊断原语)。各原子步骤短事务的提交/回滚由
阶段命令自己拥有(一次长时 attempt 不是一个事务; worker 层不
commit/rollback)。

阶段命令:
- 计算三段式: ``run_compute_stage``(经组合根注入的 computation 公开能力
  执行 generate → solve → adapt, 无可用能力即结构化 unavailable);
- 领取/续租/进度/提交/失败/取消: ``acquire_attempt``/
  ``renew_attempt_lease``/``report_attempt_progress``/
  ``submit_attempt_result``/``fail_attempt``/``cancel_attempt``;
- 队列/槽/心跳/取消: ``dequeue_task``/``slot_available``/
  ``publish_heartbeat``/``cancel_requested``;
- 输入装配只读: ``get_task_record``/``get_snapshot_record``/
  ``get_project_content_id``/``load_version_content``/
  ``get_dataset_version_record``/``get_dataset_data_object``/
  ``load_dataset_blob``/``parse_dataset_csv``/``count_completed_samples``;
- report 检查: ``locate_report_evidence``/``assess_report_stage``。

不可变结果与错误语义: ``Claim``/``SubmitReceipt``/``ReportCheckResult``/
``TaskRecord``/``CalcSnapshotRecord``/``BUSINESS_OUTCOMES``/
``LeaseRejectedError``/``ExecutionUnavailableError``/
``ComputationUnavailableError``(计算阶段网关 unavailable, Worker 经本门面
消费, 不直引 computation)。
"""

from __future__ import annotations

from iesplan import tasks as tasks_domain
from iesplan.application.worker.attempt_cases import (
    SubmitReceipt,
    acquire_attempt,
    cancel_attempt,
    fail_attempt,
    renew_attempt_lease,
    report_attempt_progress,
    submit_attempt_result,
)
from iesplan.application.worker.compute_cases import (
    ComputationUnavailableError,
    run_compute_stage,
)
from iesplan.application.worker.lease_cases import (
    CalcSnapshotRecord,
    Claim,
    LeaseRejectedError,
    TaskRecord,
    dequeue_task,
    publish_heartbeat,
    slot_available,
    verify_lease,
)
from iesplan.application.worker.report_cases import (
    ReportCheckResult,
    assess_report_stage,
    locate_report_evidence,
)
from iesplan.application.worker.runner_cases import (
    count_completed_samples,
    get_dataset_data_object,
    get_dataset_version_record,
    get_project_content_id,
    get_snapshot_record,
    get_task_record,
    load_dataset_blob,
    load_version_content,
    parse_dataset_csv,
)
from iesplan.tasks import (
    BUSINESS_OUTCOMES,
    ExecutionUnavailableError,
)

__all__ = [
    "BUSINESS_OUTCOMES",
    "CalcSnapshotRecord",
    "Claim",
    "ComputationUnavailableError",
    "ExecutionUnavailableError",
    "LeaseRejectedError",
    "ReportCheckResult",
    "SubmitReceipt",
    "TaskRecord",
    "acquire_attempt",
    "assess_report_stage",
    "cancel_attempt",
    "cancel_requested",
    "count_completed_samples",
    "dequeue_task",
    "fail_attempt",
    "get_dataset_data_object",
    "get_dataset_version_record",
    "get_project_content_id",
    "get_snapshot_record",
    "get_task_record",
    "load_dataset_blob",
    "load_version_content",
    "locate_report_evidence",
    "parse_dataset_csv",
    "publish_heartbeat",
    "renew_attempt_lease",
    "report_attempt_progress",
    "run_compute_stage",
    "slot_available",
    "submit_attempt_result",
    "verify_lease",
]


def cancel_requested(task_id: int) -> bool:
    """任务是否存在取消信号(可重建视图)。"""
    return tasks_domain.get_cancel(task_id) is not None
