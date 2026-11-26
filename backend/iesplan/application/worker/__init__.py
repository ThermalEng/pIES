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
- 领取/进度/提交/失败/取消: ``acquire_attempt``/
  ``report_attempt_progress``/
  ``submit_attempt_result``/``fail_attempt``/``cancel_attempt``;
- 队列/槽/心跳/取消: ``dequeue_task``/``slot_available``/
  ``publish_heartbeat``/``cancel_requested``;
- 输入装配只读: ``get_task_record``/``get_snapshot_record``/
  ``get_project_content_id``/``load_version_content``/
  ``get_dataset_version_record``/``get_dataset_data_object``/
  ``load_dataset_blob``/``parse_dataset_csv``/``count_completed_samples``;
- 计算阶段: ``load_compute_inputs``/``predict_target_round``/
  ``generate_bundle``/``execute_bundle``/``adapt_result``(配套
  ``build_attempt_context``/``business_outcome_for``)/
  ``build_financial_evaluation``(同次解财务事实投影与封存评价);
- report 检查: ``locate_report_evidence``/``assess_report_stage``。

不可变结果与错误语义: ``Claim``/``SubmitReceipt``/``ReportCheckResult``/
``TaskRecord``/``CalcSnapshotRecord``/``BUSINESS_OUTCOMES``/
``LeaseRejectedError``/``ExecutionUnavailableError``/
``ComputationUnavailableError``（0.8 前用于明确拒绝计算 Worker 启动）。
"""

from __future__ import annotations

from iesplan import tasks as tasks_domain
from iesplan.application.worker.attempt_cases import (
    SubmitReceipt,
    acquire_attempt,
    cancel_attempt,
    fail_attempt,
    fail_resource_attempt,
    report_attempt_progress,
    report_task_process_exit,
    submit_attempt_result,
    timeout_attempt,
)
from iesplan.application.worker.checkpoints import (
    execute_saved_bundle,
    list_evaluator_retry_requests,
    load_algorithm_checkpoint,
    load_evaluation_checkpoint,
    load_evaluator_checkpoint,
    load_pending_evaluator_retry,
    load_prediction_checkpoint,
    load_restoration_checkpoint,
    publish_prediction_checkpoint_with_artifacts,
    restore_round,
    save_algorithm_checkpoint,
    save_bundle_checkpoint,
    save_evaluation_checkpoint,
    save_evaluator_checkpoint,
    save_evaluator_retry_request,
    save_prediction_checkpoint,
    save_prediction_group_checkpoint,
    save_restoration_checkpoint,
    save_stage_outcome_checkpoint,
)
from iesplan.application.worker.compute_cases import (
    HOURLY_OBJECT_PATH,
    ComputeExecutionPlane,
    EvaluatedStageOutput,
    FrozenSeriesInputs,
    StageExecutionError,
    attach_evidence_objects,
    build_attempt_context,
    build_financial_evaluation,
    build_hourly_files,
    business_outcome_for,
    complete_materialized,
    generate_bundle,
    hourly_files_from_payload,
    load_compute_inputs,
    predict_round_stage,
    restore_core_solution,
    run_algorithm_stage,
    run_evaluators_stage,
    run_single_evaluator,
    seal_output_objects,
)
from iesplan.application.worker.evidence_cases import (
    assess_check_evidence,
    create_sample_row,
    create_uncertainty_snapshot_record,
    get_evidence_record,
    get_latest_evidence_for_project,
    get_latest_evidence_for_task,
    load_retry_base_evidence,
    point_result_assessment,
    record_sample_value,
)
from iesplan.application.worker.lease_cases import (
    CalcSnapshotRecord,
    Claim,
    LeaseRejectedError,
    TaskRecord,
    attempt_stop_reason,
    dequeue_task,
    get_reconcilable_claim,
    get_task_pool,
    list_active_reconcilable_claims,
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
from iesplan.application.worker.transactions import run_worker_transaction
from iesplan.computation import ComputationUnavailableError, observe_local_resources
from iesplan.computation.runtime.stage_runner import ControlPlaneDetached
from iesplan.tasks import (
    BUSINESS_OUTCOMES,
    ExecutionUnavailableError,
)

__all__ = [
    "ControlPlaneDetached",
    "FrozenSeriesInputs",
    "get_task_pool",
    "list_active_reconcilable_claims",
    "list_evaluator_retry_requests",
    "load_algorithm_checkpoint",
    "load_evaluation_checkpoint",
    "load_evaluator_checkpoint",
    "load_pending_evaluator_retry",
    "load_restoration_checkpoint",
    "run_worker_transaction",
    "observe_local_resources",
    "fail_resource_attempt",
    "report_task_process_exit",
    "find_queued_task",
    "BUSINESS_OUTCOMES",
    "CalcSnapshotRecord",
    "Claim",
    "ComputationUnavailableError",
    "ComputeExecutionPlane",
    "EvaluatedStageOutput",
    "ExecutionUnavailableError",
    "HOURLY_OBJECT_PATH",
    "LeaseRejectedError",
    "ReportCheckResult",
    "StageExecutionError",
    "SubmitReceipt",
    "TaskRecord",
    "acquire_attempt",
    "assess_report_stage",
    "attach_evidence_objects",
    "build_attempt_context",
    "build_financial_evaluation",
    "build_hourly_files",
    "business_outcome_for",
    "cancel_attempt",
    "cancel_requested",
    "complete_materialized",
    "count_completed_samples",
    "dequeue_task",
    "execute_saved_bundle",
    "get_reconcilable_claim",
    "hourly_files_from_payload",
    "load_prediction_checkpoint",
    "publish_prediction_checkpoint_with_artifacts",
    "restore_core_solution",
    "restore_round",
    "save_algorithm_checkpoint",
    "save_bundle_checkpoint",
    "save_evaluation_checkpoint",
    "save_evaluator_checkpoint",
    "save_evaluator_retry_request",
    "save_prediction_checkpoint",
    "save_prediction_group_checkpoint",
    "save_restoration_checkpoint",
    "save_stage_outcome_checkpoint",
    "fail_attempt",
    "generate_bundle",
    "get_dataset_data_object",
    "get_dataset_version_record",
    "get_project_content_id",
    "get_snapshot_record",
    "get_task_record",
    "load_compute_inputs",
    "load_dataset_blob",
    "load_version_content",
    "locate_report_evidence",
    "parse_dataset_csv",
    "predict_round_stage",
    "run_algorithm_stage",
    "run_evaluators_stage",
    "run_single_evaluator",
    "publish_heartbeat",
    "report_attempt_progress",
    "seal_output_objects",
    "attempt_stop_reason",
    "slot_available",
    "submit_attempt_result",
    "timeout_attempt",
    "verify_lease",
]


def find_queued_task(db, pool: str) -> int | None:
    """队列提示丢失时，从任务权威状态发现可领取工作。"""
    return tasks_domain.find_queued_task(db, pool)


def cancel_requested(task_id: int) -> bool:
    """任务是否存在取消信号(可重建视图)。"""
    return tasks_domain.get_cancel(task_id) is not None
