"""任务执行分派与状态机收拢(计算 Worker / I/O Worker 共用)。

职责:
- dispatch: 按 task.type 分派(calc/optimization/uncertainty/analysis 走
  application.worker 计算阶段网关; report 走证据包; io 任务走占位执行器);
- run_task: 一次尝试的完整执行闭环 —— 阶段网关读任务/快照记录 → 无事务
  执行 → 短事务提交(证据包/四维评估/结果索引/业务结局)或失败/取消收拢,
  全部经由 application.worker 阶段网关的 fencing 协议(03 §4.4: 迟到的写回
  永远不入权威库)。一次长时 attempt 不是一个事务: 任务/快照记录读完即
  关闭读取会话, 求解/分析/导出在无数据库事务状态下运行, 领取/进度/
  评估写入/提交/失败/取消各自使用新的短会话与 application.worker 短事务
  用例。

Worker 只按 application.worker 阶段网关结果驱动状态机, 不解释数据集
字段、不补缺省值、不做 SI 换算、不物化时间轴、不解释装配内容与任务
参数。计算任务走组合根装配的执行面真实链（快照恢复 → 生成 → 执行 →
适配）；执行面缺失的计算 Worker 在组合根阶段拒绝启动。

写入资格: 本模块不直接写任务状态/结果, 统一由 application.worker 的
submit_attempt_result / fail_attempt / cancel_attempt 带 token 完成
(03 §4.4 硬约束)。

行级读取全部经 application.worker 用例(db 会话 + id/参数进, 记录/id 出),
本模块不直接引用 ``iesplan.models.*`` 做查询(仅用例返回类型做注解)。
"""

from __future__ import annotations

import logging
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from sqlalchemy.orm import Session

from iesplan.application import worker as worker_app
from iesplan.core.diagnostics import (
    SEVERITY_BLOCKING,
    SEVERITY_ERROR,
    TASK_DATA_SNAPSHOT_MISSING,
    TASK_SOLVE_FAILED,
)
from iesplan.core.errors import AppError
from iesplan.worker import executors
from iesplan.worker.executors import (
    ControlPlaneDetached,
    EngineRunError,
    LeaseFenced,
    ResourceExhaustedError,
    RunContext,
    TaskCancelled,
    TaskTimedOut,
)

logger = logging.getLogger(__name__)

#: 计算类任务(必须绑定 calc_snapshot_id, 03 规格 2.1; 03 §9.7 增补 analysis)
COMPUTE_TASK_TYPES: tuple[str, ...] = ("calc", "optimization", "uncertainty", "analysis")
#: io 队列任务类型
IO_TASK_TYPES: tuple[str, ...] = ("report", "dataset_build", "export", "import")


class SnapshotInputError(AppError):
    """快照/数据集输入不可用(03 §6.3: TASK-DATA-001 blocking, 不可复现)。"""

    code = TASK_DATA_SNAPSHOT_MISSING
    severity = SEVERITY_BLOCKING
    message_key = "ies.diag.task.snapshot_missing"


class InvalidTaskTypeError(AppError):
    """未知任务类型(不落入任何执行器)。"""

    code = "TASK-REQ-002"
    message_key = "ies.diag.param.invalid"


# ---------------------------------------------------------------------------
# 分派
# ---------------------------------------------------------------------------


def _stop_probe(ctx: RunContext):
    return ctx.stop_reason


def _cleanup_checkpointed_stage(
    plane: Any,
    attempt_id: int | str,
    *,
    stage_id: str | None = None,
    stage_id_prefix: str | None = None,
) -> None:
    stage_runner = getattr(plane, "stage_runner", None)
    cleanup = getattr(stage_runner, "cleanup_completed_stages", None)
    if cleanup is not None:
        cleanup(attempt_id=str(attempt_id), stage_id=stage_id, stage_id_prefix=stage_id_prefix)


def _raise_for_receipt_status(receipt: Any) -> None:
    """回执分流：取消、超时、隔离各自独立；成功直接通过。

    租约隔离（fenced）绝不能走取消路径：不得报告取消，不得写终态。
    其他非成功回执走 EngineRunError（结构化失败，不伪装成功）。
    """
    if receipt.status == "cancelled":
        raise TaskCancelled("solve")
    if receipt.status == "fenced":
        raise LeaseFenced()
    if receipt.status == "timed_out":
        raise TaskTimedOut(
            f"求解执行超时(receipt={receipt.receipt_id}): {receipt.error}"
        )
    if receipt.status != "succeeded":
        if receipt.status == "process_failed" and receipt.termination_reason == "oom":
            raise ResourceExhaustedError(f"执行器确认 OOM：{receipt.error}")
        raise EngineRunError(
            f"求解执行未成功(status={receipt.status}, "
            f"receipt={receipt.receipt_id}): {receipt.error}"
        )


def _predict_round(
    ctx: RunContext, plane: Any, snapshot: Any,
) -> tuple[tuple, tuple]:
    """复用已保存预测，否则经容器单趟预测阶段执行并在清理前保存检查点。"""
    saved = worker_app.run_worker_transaction(
        ctx.session_factory, worker_app.load_prediction_checkpoint,
        ctx.claim, 1,
    )
    if saved is not None:
        _cleanup_checkpointed_stage(plane, ctx.claim.attempt_id, stage_id_prefix="predict-r1")
        ctx.prediction_artifacts[1] = saved[2]
        return saved[:2]
    series, predicted, artifacts = worker_app.predict_round_stage(
        plane=plane,
        task_project_id=ctx.task.project_id,
        snapshot=snapshot,
        claim=ctx.claim,
        number=1,
        session_factory=ctx.session_factory,
        save_checkpoint=worker_app.save_prediction_checkpoint,
        save_group_checkpoint=worker_app.save_prediction_group_checkpoint,
        load_group_checkpoint=worker_app.load_prediction_group_checkpoint,
        publish_combined_checkpoint=worker_app.publish_prediction_checkpoint_with_artifacts,
        stop=_stop_probe(ctx),
        detach=ctx.detach_event,
        on_stage_resources=ctx.update_stage_resources,
        cleanup_on_reuse=lambda sid: _cleanup_checkpointed_stage(
            plane, ctx.claim.attempt_id, stage_id=sid
        ),
    )
    ctx.prediction_artifacts[1] = artifacts
    return series, predicted


def _solve_round(
    ctx: RunContext, plane: Any, snapshot: Any, artifact: Any, models: Any,
    materialized: Any, finance: Any, config: Any, evaluation: Any, attempt: Any,
    number: int = 1,
) -> tuple:
    """单向流水线核心求解: 容器生成 → 容器求解 → spawn ResultAdapter 还原 → 业务结局。"""
    ctx.checkpoint("restore_round")
    saved = worker_app.run_worker_transaction(
        ctx.session_factory, worker_app.restore_round,
        ctx.claim, number,
    )
    if saved is None:
        worker_app.generate_bundle(
            plane, snapshot, artifact, models, materialized, finance, config, evaluation,
            attempt=attempt, claim=ctx.claim, number=number,
            session_factory=ctx.session_factory,
            save_checkpoint=worker_app.save_bundle_checkpoint,
            stop=_stop_probe(ctx),
            detach=ctx.detach_event,
            on_stage_resources=ctx.update_stage_resources,
        )
        saved = worker_app.run_worker_transaction(
            ctx.session_factory, worker_app.restore_round,
            ctx.claim, number,
        )
    else:
        _cleanup_checkpointed_stage(plane, ctx.claim.attempt_id, stage_id=f"generate-r{number}")
    bundle = saved.bundle
    generated_id = saved.generated_id
    bundle_refs = saved.files["bundle_files"]
    attempt = replace(attempt, bundle_id=saved.bundle_id)
    ctx.progress(40.0, "generate", None)
    if saved.receipt is not None:
        _cleanup_checkpointed_stage(plane, ctx.claim.attempt_id, stage_id=f"solve-r{number}")
        receipt, collected = saved.receipt, saved.outputs
        solution_refs = saved.files["solution"]
    else:
        assert config.solver is not None
        receipt, collected, solution_refs = worker_app.execute_saved_bundle(
            plane=plane,
            bundle=bundle,
            solver_ref=str(config.solver),
            time_limit_seconds=int(config.time_limit_seconds),
            seed=int(snapshot.random_seed),
            solver_options=config.slot_options("solver"),
            attempt=attempt,
            claim=ctx.claim,
            number=number,
            generated_id=generated_id,
            session_factory=ctx.session_factory,
            stop=_stop_probe(ctx),
            detach=ctx.detach_event,
        )
    ctx.update_stage_resources(receipt.resource_usage)
    _raise_for_receipt_status(receipt)
    ctx.progress(85.0, "execute", {"receipt_status": receipt.status})
    restored = worker_app.run_worker_transaction(
        ctx.session_factory, worker_app.load_restoration_checkpoint,
        ctx.claim, number,
    )
    if restored is None:
        restored = worker_app.restore_core_solution(
            bundle,
            receipt,
            collected,
            config=config,
            claim=ctx.claim,
            number=number,
            session_factory=ctx.session_factory,
            save_checkpoint=worker_app.save_restoration_checkpoint,
            stop=_stop_probe(ctx),
        )
    result = restored["compute_result"]
    outcome = worker_app.business_outcome_for(result)
    if outcome is None:
        raise EngineRunError(
            f"统一结果无法映射业务结局(execution={result.execution_status}, "
            f"math={result.math_status}, receipt={receipt.receipt_id})"
        )
    sealed: dict[str, Any] = {"bundle_files": bundle_refs, "solution": solution_refs}
    if restored.get("solution_ref") is not None:
        sealed["engineering_solution"] = {"solution.json": restored["solution_ref"]}
    if restored.get("hourly_ref") is not None:
        sealed["hourly"] = {worker_app.HOURLY_OBJECT_PATH: restored["hourly_ref"]}
    return bundle, receipt, collected, restored, outcome, sealed


def _predicted_series_csv(item: Any) -> bytes:
    """预测序列确定性 CSV 字节（证据封存用）。"""
    lines = [f"step,{item.interface}"]
    lines += [f"{index},{value!r}" for index, value in enumerate(item.values)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _execute_evaluator_retry(ctx: RunContext, retry_req: Mapping[str, Any]) -> tuple[dict, dict | None]:
    """Single-evaluator retry reusing frozen CalcSnapshot and sealed ``ies.engineering-solution@1.0.0``."""
    task = ctx.task
    snapshot = ctx.snapshot
    if snapshot is None:
        raise SnapshotInputError(
            "计算快照缺失", location={"object_type": "calc_snapshot"})
    plane = ctx.compute
    if plane is None:
        raise worker_app.ComputationUnavailableError(
            "计算执行面未装配，计算任务不可执行", reason="compute-plane-missing")
    artifact, _models, materialized, finance, config, _evaluation = worker_app.run_worker_transaction(
        ctx.session_factory, worker_app.load_compute_inputs,
        task.project_id, snapshot, materialize_series=False,
    )
    predicted_series: tuple = ()
    if config.predictions:
        saved_pred = worker_app.run_worker_transaction(
            ctx.session_factory, worker_app.load_prediction_checkpoint,
            ctx.claim, 1,
        )
        if saved_pred is not None:
            predicted_series = saved_pred[0]
    completed = worker_app.complete_materialized(materialized, predicted_series)
    attempt = worker_app.build_attempt_context(task, ctx.claim, snapshot)
    base_evidence_id = int(retry_req["base_evidence_id"])
    base_payload = worker_app.run_worker_transaction(
        ctx.session_factory, worker_app.load_retry_base_evidence, base_evidence_id,
    )
    eng_group = base_payload.get("engineering_solution") or {}
    solution_ref = eng_group.get("solution.json")
    if not isinstance(solution_ref, Mapping):
        raise EngineRunError(f"基线证据缺少密封工程解(evidence_id={base_evidence_id})")

    target_ref = str(retry_req["evaluator"])
    target_idx = 0
    target_params: dict[str, Any] = {}
    for idx, ev in enumerate(config.evaluators):
        if str(ev["evaluator"]) == target_ref:
            target_idx = idx
            target_params = dict(ev.get("parameters") or {})
            break

    ctx.progress(50.0, "evaluate_retry", {"evaluator": target_ref})
    ev_out = worker_app.run_single_evaluator(
        plane=plane,
        attempt=attempt,
        claim=ctx.claim,
        idx=target_idx,
        evaluator_ref=target_ref,
        parameters=target_params,
        solution_ref=solution_ref,
        snapshot=snapshot,
        artifact=artifact,
        finance=finance,
        materialized=completed,
        receipt_dict=base_payload.get("receipt"),
        session_factory=ctx.session_factory,
        load_checkpoint=worker_app.load_evaluator_checkpoint,
        save_checkpoint=worker_app.save_evaluator_checkpoint,
        stop=_stop_probe(ctx),
        detach=ctx.detach_event,
        cleanup_on_reuse=lambda sid: _cleanup_checkpointed_stage(
            plane, ctx.claim.attempt_id, stage_id=sid
        ),
    )

    new_entry = dict(ev_out["result_entry"])
    existing_results = [
        new_entry if item.get("evaluator") == target_ref else dict(item)
        for item in (base_payload.get("evaluator_results") or [])
    ]
    if not any(item.get("evaluator") == target_ref for item in existing_results):
        existing_results.append(new_entry)

    existing_reports = [
        dict(rep)
        for rep in (base_payload.get("evaluation_reports") or [])
        if rep.get("evaluator") != target_ref
    ]
    if ev_out.get("report") is not None:
        existing_reports.append(dict(ev_out["report"]))

    for group_key in ("bundle_files", "solution", "engineering_solution", "hourly", "prediction_files"):
        if isinstance(base_payload.get(group_key), Mapping):
            ctx.sealed_output_files[group_key] = dict(base_payload[group_key])

    merged_payload = dict(base_payload)
    merged_payload["evaluator_results"] = existing_results
    merged_payload["evaluation_reports"] = existing_reports
    if target_ref == "ies.evaluator.finance@1.0.0":
        if ev_out.get("financial_evaluation") is not None:
            merged_payload["financial_evaluation"] = dict(ev_out["financial_evaluation"])
        else:
            merged_payload.pop("financial_evaluation", None)
    return merged_payload, None


def _execute_compute(ctx: RunContext) -> tuple[dict, dict | None]:
    """1.2.0 单向计算流水线: 单趟预测 → 核心求解/还原 → 0..N 并联评价器 → 不可变证据。"""
    retry_req = worker_app.run_worker_transaction(
        ctx.session_factory, worker_app.load_pending_evaluator_retry, ctx.claim,
    )
    if retry_req is not None:
        return _execute_evaluator_retry(ctx, retry_req)

    task = ctx.task
    snapshot = ctx.snapshot
    if snapshot is None:
        raise SnapshotInputError(
            "计算快照缺失", location={"object_type": "calc_snapshot"})
    plane = ctx.compute
    if plane is None:
        raise worker_app.ComputationUnavailableError(
            "计算执行面未装配，计算任务不可执行", reason="compute-plane-missing")
    artifact, models, materialized, finance, config, evaluation = worker_app.run_worker_transaction(
        ctx.session_factory, worker_app.load_compute_inputs,
        task.project_id, snapshot, materialize_series=False,
    )
    ctx.checkpoint("materialize")
    ctx.progress(10.0, "materialize", None)
    predicted: tuple = ()
    predicted_series: tuple = ()
    if config.predictions:
        predicted_series, predicted = _predict_round(ctx, plane, snapshot)
    completed = worker_app.complete_materialized(materialized, predicted_series)
    attempt = worker_app.build_attempt_context(task, ctx.claim, snapshot)

    if config.algorithm is not None:
        evaluated = worker_app.run_worker_transaction(
            ctx.session_factory, worker_app.load_algorithm_checkpoint,
            ctx.claim,
        )
        if evaluated is None:
            evaluated = worker_app.run_algorithm_stage(
                plane=plane,
                attempt=attempt,
                claim=ctx.claim,
                snapshot=snapshot,
                artifact=artifact,
                materialized=completed,
                finance=finance,
                config=config,
                session_factory=ctx.session_factory,
                save_checkpoint=worker_app.save_algorithm_checkpoint,
                stop=_stop_probe(ctx),
                detach=ctx.detach_event,
            )
            if evaluated.solution_ref is None:
                loaded_algo = worker_app.run_worker_transaction(
                    ctx.session_factory, worker_app.load_algorithm_checkpoint, ctx.claim,
                )
                if loaded_algo is not None and loaded_algo.solution_ref is not None:
                    evaluated = loaded_algo
        else:
            _cleanup_checkpointed_stage(plane, ctx.claim.attempt_id, stage_id="algorithm-r1")
        ctx.update_stage_resources(evaluated.resources)
        result = evaluated.result
        outcome = worker_app.business_outcome_for(result)
        if outcome is None:
            raise EngineRunError(
                f"一体化结果无法映射业务结局(execution={result.execution_status}, "
                f"math={result.math_status})"
            )
        solution_ref = evaluated.solution_ref
        if solution_ref is not None:
            ctx.sealed_output_files["engineering_solution"] = {"solution.json": dict(solution_ref)}
        if evaluated.hourly_ref is not None:
            ctx.sealed_output_files["hourly"] = {
                worker_app.HOURLY_OBJECT_PATH: dict(evaluated.hourly_ref)
            }
        evaluator_results, evaluation_reports, eval_financial = worker_app.run_evaluators_stage(
            plane=plane,
            attempt=attempt,
            claim=ctx.claim,
            number=1,
            config=config,
            has_feasible_solution=(
                solution_ref is not None
                and bool(result.outputs["solver_summary"]["has_feasible_solution"])
            ),
            solution_ref=solution_ref,
            snapshot=snapshot,
            artifact=artifact,
            finance=finance,
            materialized=completed,
            receipt_dict=None,
            session_factory=ctx.session_factory,
            load_checkpoint=worker_app.load_evaluator_checkpoint,
            save_checkpoint=worker_app.save_evaluator_checkpoint,
            stop=_stop_probe(ctx),
            detach=ctx.detach_event,
            cleanup_on_reuse=lambda sid: _cleanup_checkpointed_stage(
                plane, ctx.claim.attempt_id, stage_id=sid
            ),
        )
        payload: dict[str, Any] = {
            "outcome": outcome,
            "compute_result": result.to_mapping(),
            "evaluator_results": evaluator_results,
            "evaluation_reports": evaluation_reports,
        }
        if evaluated.hourly_ref is not None and evaluated.hourly_meta is not None:
            payload["hourly_refs"] = [
                {"object_id": evaluated.hourly_ref["object_id"], **dict(evaluated.hourly_meta)}
            ]
        if eval_financial is not None:
            payload["financial_evaluation"] = dict(eval_financial)
        return payload, None

    bundle, receipt, _collected, restored, outcome, sealed = _solve_round(
        ctx, plane, snapshot, artifact, models, completed, finance, config, evaluation, attempt,
    )
    result = restored["compute_result"]
    solution_ref = restored.get("solution_ref")
    has_feasible = result.math_status in ("solved", "feasible") and solution_ref is not None
    evaluator_results, evaluation_reports, financial_evaluation = worker_app.run_evaluators_stage(
        plane=plane,
        attempt=attempt,
        claim=ctx.claim,
        number=1,
        config=config,
        has_feasible_solution=has_feasible,
        solution_ref=solution_ref,
        snapshot=snapshot,
        artifact=artifact,
        finance=finance,
        materialized=completed,
        receipt_dict=receipt.to_dict(),
        session_factory=ctx.session_factory,
        load_checkpoint=worker_app.load_evaluator_checkpoint,
        save_checkpoint=worker_app.save_evaluator_checkpoint,
        stop=_stop_probe(ctx),
        detach=ctx.detach_event,
        cleanup_on_reuse=lambda sid: _cleanup_checkpointed_stage(
            plane, ctx.claim.attempt_id, stage_id=sid
        ),
    )

    ctx.sealed_output_files.update(sealed)
    output_files = {}
    payload = {
        "outcome": outcome,
        "compute_result": result.to_mapping(),
        "receipt": receipt.to_dict(),
        "bundle_manifest": bundle.manifest.to_dict(),
        "evaluator_results": evaluator_results,
        "evaluation_reports": evaluation_reports,
    }
    if predicted:
        prediction_files = {}
        receipts = []
        for item in predicted:
            artifact_path = f"prediction/{item.device}/{item.interface}/training_artifact.json"
            ctx.sealed_output_files.setdefault("prediction_files", {})[artifact_path] = (
                ctx.prediction_artifacts[1][f"{item.device}/{item.interface}"])
            series_path = f"prediction/{item.device}/{item.interface}/series.csv"
            prediction_files[series_path] = (_predicted_series_csv(item), "text/csv")
            entry = dict(item.receipt)
            entry["artifact_path"] = artifact_path
            entry["series_path"] = series_path
            entry["training_artifact"] = None
            receipts.append(entry)
        output_files["prediction_files"] = prediction_files
        payload["prediction"] = {
            "predict_target_count": len(predicted),
            "prediction_rounds": 1,
            "solve_rounds": 1,
            "phase2_solve_rounds": 0,
            "receipts": receipts,
        }
    del bundle, _collected, materialized, completed, models, artifact
    if restored.get("hourly_ref") is not None and restored.get("hourly_meta") is not None:
        payload["hourly_refs"] = [
            {"object_id": restored["hourly_ref"]["object_id"], **dict(restored["hourly_meta"])}
        ]
    if financial_evaluation is not None:
        payload["financial_evaluation"] = dict(financial_evaluation)
    return payload, output_files


def dispatch(
    ctx: RunContext,
) -> tuple[dict, dict | None]:
    """按任务类型分派到执行器(返回结果 payload 与待封存输出文件, 含显式 outcome)。

    未实现的 I/O 执行器抛 tasks 域执行不可用错误(上抛, 不落成功);
    未知任务类型抛 InvalidTaskTypeError。计算类任务走真实链
    (``_execute_compute``): 任务/快照记录缺失即确定性失败, 不解释任何
    输入字段；计算阶段资产已通过公开网关持久化，新输出字节在payload
    之外返回，由最终提交单元与已有对象引用一起原子发布业务结果。
    Blob字节落盘不参与数据库原子提交，本层不直接写数据库。
    """
    task_type = ctx.task.type
    if task_type in COMPUTE_TASK_TYPES:
        return _execute_compute(ctx)
    if task_type == "report":
        return executors.execute_check(ctx), None
    if task_type == "dataset_build":
        return executors.execute_dataset_process(ctx), None
    if task_type == "export":
        return executors.execute_export(ctx), None
    if task_type == "import":
        return executors.execute_package_import(ctx), None
    raise InvalidTaskTypeError(
        "未知任务类型",
        params={"task_type": task_type, "allowed": list(COMPUTE_TASK_TYPES) + list(IO_TASK_TYPES)},
    )


# ---------------------------------------------------------------------------
# 一次尝试的完整执行闭环
# ---------------------------------------------------------------------------


def run_task(
    session_factory: Callable[[], Session],
    claim: worker_app.Claim,
    *,
    worker_id: str = "",
    stop_event: Any = None,
    cancel_event: Any = None,
    detach_event: Any = None,
    compute: Any = None,
) -> str:
    """执行已领取的任务并落终态(带 fencing 提交/失败/取消收拢)。

    参数:
        session_factory: 会话工厂(Worker 持有的唯一会话来源; 各阶段按需
            开短会话, 本函数不持有跨阶段的 Session)。
        claim: acquire_attempt 的领取结果(尝试 + 租约 + token)。
        stop_event: 取消/优雅退出事件(透传给执行器检查点与隔离子进程)。
        compute: 计算执行面(组合根装配; 计算任务必需, 缺失即明确不可用)。
    返回:
        终态状态: completed / failed / cancelled / lease_rejected / fenced。
        fenced 表示回执证明租约已失效：本次执行结果被丢弃，不写任何终态、
        不报取消。
    事务边界: 本函数不调用 commit/rollback, 不持有跨阶段的 Session, 不写
    ORM; 任务/快照读完即关闭读取会话, 求解/分析/导出在无数据库事务状态
    下运行, 领取/进度/评估写入/提交/失败/取消各自的新短会话短事务
    由 application.worker 用例拥有并提交(领取已在执行前提交, 故执行期
    回滚不会丢失租约; 失败收拢不再误判为 lease_rejected)。计算阶段产物
    经公开网关分段保存；最终提交复用已有对象并封存新输出，证据/评价/
    索引/终态同事务发布，失败不撤销先前检查点。本函数只编排和传参。
    """
    factory = session_factory

    # 输入加载: 短读会话, 读完即关(task/snapshot 为脱离会话的值对象)
    def load_task_inputs(db: Session):
        task = worker_app.get_task_record(db, claim.task_id)
        snapshot = (
            worker_app.get_snapshot_record(db, task.calc_snapshot_id)
            if task is not None and task.calc_snapshot_id else None
        )
        return task, snapshot

    task, snapshot = worker_app.run_worker_transaction(factory, load_task_inputs)
    if task is None:
        raise SnapshotInputError("任务不存在", params={"task_id": claim.task_id})

    def _report_progress(percent: float, stage: str, detail: dict | None) -> None:
        # 每次进度各自新短会话短事务, 提交后即刻对其他会话可见;
        # worker_resources 上报已观测到的阶段容器 cgroup v2 指标, 不伪造控制面进程指标。
        payload_detail = dict(detail or {})
        if ctx.stage_resources:
            payload_detail["worker_resources"] = dict(ctx.stage_resources)
        accepted = worker_app.run_worker_transaction(
            factory,
            lambda db: worker_app.report_attempt_progress(
                db, claim.attempt_id, claim.lease_token, task.id, percent, stage,
                payload_detail or None,
            ),
        )
        if not accepted:
            raise worker_app.LeaseRejectedError(
                "进度上报遭租约拒绝", params={"attempt_id": claim.attempt_id},
            )

    ctx = RunContext(
        task=task, claim=claim, session_factory=factory, worker_id=worker_id,
        stop_event=stop_event, cancel_event=cancel_event, detach_event=detach_event,
        snapshot=snapshot, progress_fn=_report_progress, compute=compute,
    )
    try:
        ctx.checkpoint("start")
        ctx.progress(0, "start")
        payload, output_files = dispatch(ctx)
        ctx.checkpoint("submit")
        # 完成路径要求显式合法 outcome: 缺字段默认成功已删除, 缺失/非法
        # 即确定性失败, 绝不落成功(合法集合归 tasks 域所有)。
        outcome = payload.get("outcome")
        if outcome not in worker_app.BUSINESS_OUTCOMES:
            raise EngineRunError(f"执行器未返回显式合法 outcome: {outcome!r}")
        worker_app.run_worker_transaction(
            factory,
            lambda db: worker_app.submit_attempt_result(
                db, claim, payload=payload, outcome=outcome,
                actor_id=task.requested_by, output_files=output_files,
                sealed_output_files=ctx.sealed_output_files,
            ),
        )
        return "completed"
    except ControlPlaneDetached:
        logger.info(
            "控制面关闭分离，保留活跃尝试与容器待重启接管: task=%s attempt=%s",
            task.id,
            claim.attempt_id,
        )
        return "detached"
    except LeaseFenced:
        logger.warning("租约隔离，丢弃本次执行结果: task=%s", task.id)
        return "fenced"
    except TaskCancelled as exc:
        return _handle_cancel(factory, ctx, claim, exc.stage)
    except TaskTimedOut as exc:
        message = str(exc)
        try:
            worker_app.run_worker_transaction(
                factory, worker_app.timeout_attempt, claim, message=message,
            )
        except worker_app.LeaseRejectedError:
            return "lease_rejected"
        return "timed_out"
    except worker_app.ComputationUnavailableError as exc:
        if exc.reason == "cancelled":
            return _handle_cancel(factory, ctx, claim, "generate")
        if exc.reason == "fenced":
            return "fenced"
        return _handle_failure(factory, ctx, claim, exc)
    except worker_app.StageExecutionError as exc:
        if exc.status == "cancelled":
            return _handle_cancel(factory, ctx, claim, exc.stage)
        if exc.status == "fenced":
            logger.warning("阶段租约隔离，丢弃本次执行结果: task=%s stage=%s", task.id, exc.stage)
            return "fenced"
        if exc.status == "timed_out":
            try:
                worker_app.run_worker_transaction(
                    factory, worker_app.timeout_attempt, claim, message=exc.detail,
                )
            except worker_app.LeaseRejectedError:
                return "lease_rejected"
            return "timed_out"
        if exc.status == "oom":
            try:
                worker_app.run_worker_transaction(
                    factory, worker_app.fail_resource_attempt, claim,
                    cause=exc.detail,
                    resources=executors.normalize_worker_resources(
                        exc.resources or ctx.stage_resources
                    ),
                )
            except worker_app.LeaseRejectedError:
                return "lease_rejected"
            return "failed"
        return _handle_failure(factory, ctx, claim, EngineRunError(exc.detail))
    except (
        worker_app.LeaseRejectedError, SnapshotInputError, EngineRunError,
        AppError,
    ) as exc:
        return _handle_failure(factory, ctx, claim, exc)
    except (MemoryError, ResourceExhaustedError) as exc:
        try:
            resources = (
                executors.normalize_worker_resources(ctx.stage_resources)
                if ctx.stage_resources
                else worker_app.observe_local_resources().to_dict()
            )
            worker_app.run_worker_transaction(
                factory, worker_app.fail_resource_attempt, claim,
                cause=type(exc).__name__ + (f": {exc}" if str(exc) else ""),
                resources=resources,
            )
        except worker_app.LeaseRejectedError:
            return "lease_rejected"
        return "failed"
    except Exception as exc:  # noqa: BLE001 - 尝试边界: 任何未预期异常落确定性失败
        logger.exception("任务执行内部错误: task=%s", task.id)
        return _handle_failure(factory, ctx, claim, RuntimeError(f"内部错误: {exc}"))


def _handle_cancel(
    factory: Callable[[], Session], ctx: RunContext, claim: worker_app.Claim, stage: str,
) -> str:
    """取消收拢(03 §6.1): 部分完成的批量子任务 → partial_batch。

    样本计数读与取消收拢写各自新短会话; 收拢事务由 application.worker
    用例提交, 本函数不调用 commit/rollback。
    """
    task = ctx.task
    logger.info("任务取消: task=%s stage=%s", task.id, stage)
    outcome = None
    if task.type == "uncertainty":
        completed = worker_app.run_worker_transaction(
            factory, worker_app.count_completed_samples,
            task.id,
        )
        if completed > 0:
            outcome = "partial_batch"
    for retry in range(6):
        try:
            worker_app.run_worker_transaction(
                factory, worker_app.cancel_attempt, claim, outcome=outcome,
            )
            return "cancelled"
        except worker_app.LeaseRejectedError:
            logger.warning("取消收拢遭租约隔离，停止写回: task=%s", task.id)
            return "lease_rejected"
        except AppError as exc:
            # 只有权威终态才可认定取消/完成竞态已落定；不得看到 Redis
            # 信号便返回 cancelled 而遗留 running attempt 与占用的槽位。
            latest = worker_app.run_worker_transaction(
                factory, worker_app.get_task_record, task.id,
            )
            if latest is not None and latest.status in (
                "completed", "failed", "cancelled", "timed_out",
            ):
                logger.info("取消与终态竞态: task=%s status=%s", task.id, latest.status)
                return latest.status
            if retry < 5:
                time.sleep(0.05)  # 仅异常路径短暂等候并发状态事务提交
                continue
            logger.error(
                "取消信号与权威任务状态不一致: task=%s status=%s error=%s",
                task.id, latest.status if latest is not None else "missing", exc,
            )
            return _handle_failure(
                factory, ctx, claim,
                EngineRunError("停止信号与权威取消状态不一致，执行已停止"),
            )
    raise AssertionError("取消收拢循环未返回")


def _handle_failure(
    factory: Callable[[], Session], ctx: RunContext, claim: worker_app.Claim, exc: Exception,
) -> str:
    """失败收拢(03 §6.3): 快照/输入问题 → blocking insufficient_evidence。

    每次收拢写各自新短会话; 收拢事务由 application.worker 用例提交, 本函数
    不调用 commit/rollback。返回失败终态; 真正的租约失效类错误不写终态
    执行失败按实际错误收拢。
    已提交的进度/评估不受本次收拢回滚影响(各短事务互不包揽)。
    """
    if isinstance(exc, worker_app.LeaseRejectedError):
        logger.warning("租约失效, 停止一切写回: task=%s (%s)", claim.task_id, exc)
        return "lease_rejected"
    if isinstance(exc, worker_app.ComputationUnavailableError):
        # 计算阶段网关结构化 unavailable(无可用 provider/能力延期):
        # 错误类型经 application.worker 门面消费(门禁 20: Worker 不直引
        # computation), 失败语义与 computation 边界同源。
        # 确定性失败落 failed + TASK-SOLVE-001, 保留原始原因(含 reason),
        # 不得包成"内部错误", 更不得误判为 lease_rejected。
        try:
            worker_app.run_worker_transaction(
                factory, worker_app.fail_attempt, claim, code=TASK_SOLVE_FAILED,
                message=f"{exc} (reason={exc.reason})",
                level=SEVERITY_ERROR,
                model_source=exc.model_source,
                stack_trace=traceback.format_exc(limit=10),
            )
        except worker_app.LeaseRejectedError:
            return "lease_rejected"
        return "failed"
    if isinstance(exc, SnapshotInputError):
        try:
            worker_app.run_worker_transaction(
                factory, worker_app.fail_attempt, claim,
                code=exc.code, message=str(exc), level=exc.severity,
                outcome="insufficient_evidence",
            )
        except worker_app.LeaseRejectedError:
            return "lease_rejected"
        return "failed"
    # 计算不可用/执行不可用/引擎/内部失败: 确定性失败落 failed, 不自动重试。
    # 错误码取结构化错误自带码(计算入口显式不可用为 TASK-SOLVE-001,
    # 未实现 I/O 执行入口为 TASK-EXEC-001), 未预期异常沿用 TASK-SOLVE-001
    # 包络; 原始原因保留在 message 中。
    code = exc.code if isinstance(exc, AppError) else TASK_SOLVE_FAILED
    try:
        worker_app.run_worker_transaction(
            factory, worker_app.fail_attempt, claim, code=code, message=str(exc),
            stack_trace=traceback.format_exc(limit=10),
        )
    except worker_app.LeaseRejectedError:
        return "lease_rejected"
    return "failed"
