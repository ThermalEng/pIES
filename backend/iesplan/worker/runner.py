"""任务执行分派与状态机收拢(计算 Worker / I/O Worker 共用)。

职责:
- dispatch: 按 task.type 分派(calc/optimization/uncertainty/analysis 走
  application.worker 计算阶段网关; report 走证据包; io 任务走占位执行器);
- run_task: 一次尝试的完整执行闭环 —— 阶段网关读任务/快照记录 → 无事务
  执行 → 短事务提交(证据包/四维评估/结果索引/业务结局)或失败/取消收拢,
  全部经由 application.worker 阶段网关的 fencing 协议(03 §4.4: 迟到的写回
  永远不入权威库)。一次长时 attempt 不是一个事务: 任务/快照记录读完即
  关闭读取会话, 求解/分析/导出在无数据库事务状态下运行, 领取/进度/续租/
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
import traceback
from collections.abc import Callable, Mapping
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
from iesplan.worker.executors import EngineRunError, LeaseFenced, RunContext, TaskCancelled

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
    """停止探针: 未停止 → None；用户取消 → cancelled；其余停止 → fenced。

    其余停止只可能是租约失效（续租失败置位）或进程退出：租约隔离必须
    记 fenced，不得记 cancelled。探针内取消查询走可重建队列视图，不持
    有数据库会话。
    """

    def probe():
        if ctx.stop_event is None or not ctx.stop_event.is_set():
            return None
        if worker_app.cancel_requested(ctx.task.id):
            return "cancelled"
        return "fenced"

    return probe


def _raise_for_receipt_status(receipt: Any) -> None:
    """回执状态分流：取消 → TaskCancelled；隔离 → LeaseFenced；成功 → 通过。

    租约隔离（fenced）绝不能走取消路径：不得报告取消，不得写终态。
    非成功非取消非隔离一律 EngineRunError（结构化失败，不伪装成功）。
    """
    if receipt.status == "cancelled":
        raise TaskCancelled("solve")
    if receipt.status == "fenced":
        raise LeaseFenced()
    if receipt.status != "succeeded":
        raise EngineRunError(
            f"求解执行未成功(status={receipt.status}, "
            f"receipt={receipt.receipt_id}): {receipt.error}"
        )


def _predict_round(
    ctx: RunContext, plane: Any, snapshot: Any,
    previous: Mapping[str, object] | None,
) -> tuple[tuple, tuple]:
    """预测阶段短读会话(读快照与固定资源, 无数据库写入)。"""
    with ctx.session_factory() as db:
        return worker_app.predict_target_round(
            db, plane, ctx.task.project_id, snapshot, previous=previous,
        )


def _solve_round(
    ctx: RunContext, plane: Any, snapshot: Any, artifact: Any, models: Any,
    materialized: Any, finance: Any, config: Any, attempt: Any,
) -> tuple[Any, Any, Any, Any]:
    """单轮求解: 生成 → 执行(含回执分流) → 适配 → 业务结局。

    每轮回执独立判定: cancelled → TaskCancelled, fenced → LeaseFenced
    (不写终态), 非成功/不可映射 → EngineRunError。生成后与回执判定后
    分别写进度（判定前不写 execute 进度）。返回
    (bundle, receipt, collected, result, outcome)。
    """
    bundle = worker_app.generate_bundle(
        plane, snapshot, artifact, models, materialized, finance, config)
    ctx.progress(40.0, "generate", None)
    receipt, collected = worker_app.execute_bundle(
        plane=plane, bundle=bundle, attempt=attempt, stop=_stop_probe(ctx),
    )
    _raise_for_receipt_status(receipt)
    ctx.progress(85.0, "execute", {"receipt_status": receipt.status})
    result = worker_app.adapt_result(plane, bundle, receipt, collected)
    outcome = worker_app.business_outcome_for(result)
    if outcome is None:
        raise EngineRunError(
            f"统一结果无法映射业务结局(execution={result.execution_status}, "
            f"math={result.math_status}, receipt={receipt.receipt_id})"
        )
    return bundle, receipt, collected, result, outcome


def _predicted_series_csv(item: Any) -> bytes:
    """最终轮预测序列确定性 CSV 字节（证据封存用）。

    按索引连续排列全周期值（``repr`` 逐行），只如实序列化本轮预测
    器产出的值，不做任何校验、补值或重算。
    """
    lines = [f"step,{item.interface}"]
    lines += [f"{index},{value!r}" for index, value in enumerate(item.values)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _execute_compute(ctx: RunContext) -> tuple[dict, dict]:
    """计算任务真实链: 快照恢复 → 预测 → 阶段一求解 → [阶段二迭代] → 证据。

    全程只按 application.worker 阶段网关结果驱动，不解释任何输入字段：
    冻结缺件即实际结构错误可见失败；预测算法未注册/目标类型不支持即
    明确不可用（不 stub、不回退）；取消收拢为 TaskCancelled；租约隔离
    收拢为 LeaseFenced（不写终态）；非成功回执与不可映射结果收拢为
    EngineRunError（结构化失败，不伪装成功）。回执状态判定后才写执行
    进度：fenced/cancelled/失败不产生 execute 进度与终态写回。
    阶段二（data_predict 多于 1 个时必进，不可选）：每轮用上一轮统一
    结果重建预测目标→重建模型→真实求解，再以相邻两轮预测序列最大
    绝对差按配置容差收敛或达最大迭代数；收敛判定不能跳过本轮求解。
    轮数上限为总求解轮数（阶段一计第 1 轮；多目标时提交门要求上限
    至少为 2，不存在跳过阶段二求解的上限）。prediction 载荷中
    prediction_rounds 与 solve_rounds 恒等（每阶段二迭代都真实求解），
    phase2_solve_rounds = solve_rounds - 1（阶段一计 1 轮）。
    多轮证据（solve_rounds > 1 时）：阶段一与非最终中间轮的 Bundle
    文件、回执、声明原始输出、训练产物与预测序列字节进入
    output_files["round_history"]（按 round-<n>/ 前缀区分）；最终轮
    不重复封存，其 rounds 条目直接引用顶层最终组
   （bundle_files/solution/prediction_files）的同一对象描述子。
    载荷 "rounds" 按轮次列出回执/manifest/文件引用（附各节所在
    密封组）并标明最终权威轮；最终轮各组即权威结果；仍由最终
    提交单元一次性封存提交，无中间轮终态。
    返回 (payload, output_files)：payload
    只含 JSON 兼容字段；output_files 为待封存原始字节，由提交单元随
    提交在同一数据库事务内建对象元数据/引用并注入描述子（Blob 字节落
    盘不参与原子提交），本函数不写数据库。
    """
    task = ctx.task
    snapshot = ctx.snapshot
    if snapshot is None:
        raise SnapshotInputError(
            "计算快照缺失", location={"object_type": "calc_snapshot"})
    plane = ctx.compute
    if plane is None:
        raise worker_app.ComputationUnavailableError(
            "计算执行面未装配，计算任务不可执行", reason="compute-plane-missing")
    with ctx.session_factory() as db:
        artifact, models, materialized, finance, config = worker_app.load_compute_inputs(
            db, task.project_id, snapshot
        )
    ctx.progress(10.0, "materialize", None)
    predicted: tuple = ()
    predicted_series: tuple = ()
    if config.predictions:
        predicted_series, predicted = _predict_round(ctx, plane, snapshot, None)
    # 完成态集合：预测阶段交付全部 pending（提交闸门 TASK-REQ-010/011 保证
    # 配置与绑定双向覆盖），由应用层合并构成，不复核。
    completed = worker_app.complete_materialized(materialized, predicted_series)
    attempt = worker_app.build_attempt_context(task, ctx.claim, snapshot)
    rounds_log: list[dict] = []
    bundle, receipt, collected, result, outcome = _solve_round(
        ctx, plane, snapshot, artifact, models, completed, finance, config, attempt)
    rounds_log.append({
        "round": 1, "phase": "phase1",
        "bundle": bundle, "receipt": receipt, "collected": collected,
        "predicted": predicted,
    })
    prediction_rounds = 1 if predicted else 0
    solve_rounds = 1
    converged = True
    final_delta: float | None = None
    if len(predicted) > 1:
        convergence = config.convergence or {}
        tolerance = float(convergence["tolerance"])
        max_iterations = int(convergence["max_iterations"])
        converged = False
        previous_values = {(p.device, p.interface): p.values for p in predicted}
        while prediction_rounds < max_iterations:
            new_series, new_predicted = _predict_round(
                ctx, plane, snapshot, result.outputs)
            prediction_rounds += 1
            completed = worker_app.complete_materialized(materialized, new_series)
            bundle, receipt, collected, result, outcome = _solve_round(
                ctx, plane, snapshot, artifact, models, completed,
                finance, config, attempt)
            solve_rounds += 1
            rounds_log.append({
                "round": prediction_rounds, "phase": "phase2",
                "bundle": bundle, "receipt": receipt, "collected": collected,
                "predicted": new_predicted,
            })
            ctx.progress(90.0, "phase2", {"round": solve_rounds})
            new_values = {(p.device, p.interface): p.values for p in new_predicted}
            final_delta = max(
                worker_app.prediction_delta(previous_values[key], new_values[key])
                for key in previous_values
            )
            previous_values = new_values
            predicted = new_predicted
            if final_delta <= tolerance:
                converged = True
                break
    bundle_media = {
        entry["path"]: entry["media_type"]
        for entry in (*bundle.manifest.inputs, *bundle.manifest.outputs)
    }
    solution_media = {
        entry["path"]: entry["media_type"] for entry in receipt.outputs
    }
    output_files = {
        "bundle_files": {
            path: (data, bundle_media[path]) for path, data in bundle.files.items()
        },
        "solution": {
            path: (collected[path], solution_media[path]) for path in collected
        },
    }
    payload = {
        "outcome": outcome,
        "compute_result": result.to_dict(),
        "receipt": receipt.to_dict(),
        "bundle_manifest": bundle.manifest.to_dict(),
    }
    if predicted:
        prediction_files = {}
        receipts = []
        for item in predicted:
            artifact_path = f"prediction/{item.device}/{item.interface}/training_artifact.json"
            prediction_files[artifact_path] = (item.artifact, item.artifact_media_type)
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
            "prediction_rounds": prediction_rounds,
            "solve_rounds": solve_rounds,
            "phase2_solve_rounds": solve_rounds - 1,
            "converged": converged,
            "final_delta": final_delta,
            "receipts": receipts,
        }
    if len(rounds_log) > 1:
        history: dict[str, tuple[bytes, str]] = {}
        rounds: list[dict] = []
        for record in rounds_log[:-1]:
            prefix = f"round-{record['round']}"
            round_bundle = record["bundle"]
            round_receipt = record["receipt"]
            round_collected = record["collected"]
            round_bundle_media = {
                entry["path"]: entry["media_type"]
                for entry in (*round_bundle.manifest.inputs, *round_bundle.manifest.outputs)
            }
            round_solution_media = {
                entry["path"]: entry["media_type"] for entry in round_receipt.outputs
            }
            bundle_paths = []
            for path, data in round_bundle.files.items():
                key = f"{prefix}/bundle/{path}"
                history[key] = (data, round_bundle_media[path])
                bundle_paths.append(key)
            solution_paths = []
            for path in round_collected:
                key = f"{prefix}/solution/{path}"
                history[key] = (round_collected[path], round_solution_media[path])
                solution_paths.append(key)
            prediction_paths = []
            for item in record["predicted"]:
                artifact_key = (
                    f"{prefix}/prediction/{item.device}/{item.interface}"
                    "/training_artifact.json"
                )
                history[artifact_key] = (item.artifact, item.artifact_media_type)
                series_key = (
                    f"{prefix}/prediction/{item.device}/{item.interface}/series.csv"
                )
                history[series_key] = (_predicted_series_csv(item), "text/csv")
                prediction_paths += [artifact_key, series_key]
            rounds.append({
                "round": record["round"],
                "phase": record["phase"],
                "authoritative": False,
                "receipt": round_receipt.to_dict(),
                "bundle_manifest": round_bundle.manifest.to_dict(),
                "sealed_in": {
                    "bundle": "round_history",
                    "solution": "round_history",
                    "prediction": "round_history",
                },
                "files": {
                    "bundle": sorted(bundle_paths),
                    "solution": sorted(solution_paths),
                    "prediction": sorted(prediction_paths),
                },
            })
        final_record = rounds_log[-1]
        rounds.append({
            "round": solve_rounds,
            "phase": final_record["phase"],
            "authoritative": True,
            "receipt": receipt.to_dict(),
            "bundle_manifest": bundle.manifest.to_dict(),
            "sealed_in": {
                "bundle": "bundle_files",
                "solution": "solution",
                "prediction": "prediction_files",
            },
            "files": {
                "bundle": sorted(f"bundle/{path}" for path in bundle.files),
                "solution": sorted(f"solution/{path}" for path in collected),
                "prediction": sorted(prediction_files),
            },
        })
        output_files["round_history"] = history
        payload["rounds"] = rounds
        payload["authoritative_round"] = solve_rounds
    # 评价时间轴现金流与指标(0.12-T2): 只对最终轮已求解结果计算, 随
    # payload 进同一证据封存/提交单元；未求解或缺 solution 输出返回
    # None（不写财务块，不伪造）；冻结缺件即实际错误可见失败。
    financial = worker_app.build_financial_evaluation(
        snapshot, finance, artifact, models, completed, result
    )
    if financial is not None:
        payload["financial_evaluation"] = financial
    return payload, output_files


def dispatch(
    ctx: RunContext,
) -> tuple[dict, dict | None]:
    """按任务类型分派到执行器(返回结果 payload 与待封存输出文件, 含显式 outcome)。

    未实现的 I/O 执行器抛 tasks 域执行不可用错误(上抛, 不落成功);
    未知任务类型抛 InvalidTaskTypeError。计算类任务走真实链
    (``_execute_compute``): 任务/快照记录缺失即确定性失败, 不解释任何
    输入字段；输出文件原始字节随 payload 之外返回，由提交单元随提交
    在同一数据库事务内建元数据/引用（Blob 字节落盘不参与原子提交，
    本函数不写数据库）。
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
        不报取消（过期租约由领取循环自然回收）。
    事务边界: 本函数不调用 commit/rollback, 不持有跨阶段的 Session, 不写
    ORM; 任务/快照读完即关闭读取会话, 求解/分析/导出在无数据库事务状态
    下运行, 领取/进度/续租/评估写入/提交/失败/取消各自的新短会话短事务
    由 application.worker 用例拥有并提交(领取已在执行前提交, 故执行期
    回滚不会丢失租约; 失败收拢不再误判为 lease_rejected)。计算输出字节
    由提交单元随提交在同一数据库事务内建元数据/引用(封存/证据/引用/
    完成一次提交; Blob 字节落盘不参与原子提交), 本函数只传参。
    """
    factory = session_factory
    # 输入加载: 短读会话, 读完即关(task/snapshot 为脱离会话的值对象)
    with factory() as db:
        task = worker_app.get_task_record(db, claim.task_id)
        snapshot = (
            worker_app.get_snapshot_record(db, task.calc_snapshot_id)
            if task is not None and task.calc_snapshot_id else None
        )
    if task is None:
        raise SnapshotInputError("任务不存在", params={"task_id": claim.task_id})

    def _report_progress(percent: float, stage: str, detail: dict | None) -> None:
        # 每次进度各自新短会话短事务, 提交后即刻对其他会话可见
        with factory() as progress_db:
            worker_app.report_attempt_progress(
                progress_db, claim.attempt_id, claim.lease_token,
                task.id, percent, stage, detail,
            )

    ctx = RunContext(
        task=task, claim=claim, session_factory=factory, worker_id=worker_id,
        stop_event=stop_event, snapshot=snapshot,
        progress_fn=_report_progress, compute=compute,
    )
    try:
        payload, output_files = dispatch(ctx)
        # 完成路径要求显式合法 outcome: 缺字段默认成功已删除, 缺失/非法
        # 即确定性失败, 绝不落成功(合法集合归 tasks 域所有)。
        outcome = payload.get("outcome")
        if outcome not in worker_app.BUSINESS_OUTCOMES:
            raise EngineRunError(f"执行器未返回显式合法 outcome: {outcome!r}")
        with factory() as db:
            worker_app.submit_attempt_result(
                db, claim, payload=payload, outcome=outcome,
                actor_id=task.requested_by, output_files=output_files)
        return "completed"
    except LeaseFenced:
        logger.warning("租约隔离，丢弃本次执行结果: task=%s", task.id)
        return "fenced"
    except TaskCancelled as exc:
        return _handle_cancel(factory, ctx, claim, exc.stage)
    except (
        worker_app.LeaseRejectedError, SnapshotInputError, EngineRunError,
        AppError, worker_app.ComputationUnavailableError,
    ) as exc:
        return _handle_failure(factory, ctx, claim, exc)
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
        with factory() as db:
            completed = worker_app.count_completed_samples(db, task.id)
        if completed > 0:
            outcome = "partial_batch"
    try:
        with factory() as db:
            worker_app.cancel_attempt(db, claim, outcome=outcome)
        return "cancelled"
    except AppError as exc:
        # 取消竞态: 任务已终态(以先落终态者为准, 03 §6.1 规则 4)
        logger.info("取消竞态忽略: task=%s (%s)", task.id, exc)
        return "cancelled"


def _handle_failure(
    factory: Callable[[], Session], ctx: RunContext, claim: worker_app.Claim, exc: Exception,
) -> str:
    """失败收拢(03 §6.3): 快照/输入问题 → blocking insufficient_evidence。

    每次收拢写各自新短会话; 收拢事务由 application.worker 用例提交, 本函数
    不调用 commit/rollback。返回失败终态; 真正的租约失效类错误不写终态
    (由调度器守护回收, 03 §4.3), 执行失败本身绝不误判为 lease_rejected。
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
            with factory() as db:
                worker_app.fail_attempt(
                    db, claim, code=TASK_SOLVE_FAILED,
                    message=f"{exc} (reason={exc.reason})",
                    level=SEVERITY_ERROR,
                    stack_trace=traceback.format_exc(limit=10),
                )
        except worker_app.LeaseRejectedError:
            return "lease_rejected"
        return "failed"
    if isinstance(exc, SnapshotInputError):
        try:
            with factory() as db:
                worker_app.fail_attempt(
                    db, claim, code=exc.code, message=str(exc), level=exc.severity,
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
        with factory() as db:
            worker_app.fail_attempt(
                db, claim, code=code, message=str(exc),
                stack_trace=traceback.format_exc(limit=10),
            )
    except worker_app.LeaseRejectedError:
        return "lease_rejected"
    return "failed"
