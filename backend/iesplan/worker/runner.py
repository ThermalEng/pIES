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
from collections.abc import Callable
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


def _execute_compute(ctx: RunContext) -> tuple[dict, dict]:
    """计算任务真实链: 快照恢复 → 生成 → 执行 → 适配 → 证据 payload 与输出文件。

    全程只按 application.worker 阶段网关结果驱动，不解释任何输入字段：
    冻结缺件即实际结构错误可见失败；预测需求明确拒绝（不 stub、不回退）；
    取消收拢为 TaskCancelled；租约隔离收拢为 LeaseFenced（不写终态）；
    非成功回执与不可映射结果收拢为 EngineRunError（结构化失败，不伪装
    成功）。回执状态判定后才写执行进度：fenced/cancelled/失败不产生
    execute 进度与终态写回。返回 (payload, output_files)：payload 只含
    JSON 兼容字段；output_files 为待封存原始字节（路径 → (字节, 声明
    媒体类型)，媒体类型取 manifest/回执声明值，原样传递不做文本校验），
    由提交单元同事务封存并注入描述子，本函数不写数据库。
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
        artifact, models, series, finance, config = worker_app.load_compute_inputs(
            db, task.project_id, snapshot
        )
    ctx.progress(10.0, "materialize", None)
    bundle = worker_app.generate_bundle(plane, snapshot, artifact, models, series, finance, config)
    ctx.progress(40.0, "generate", None)
    attempt = worker_app.build_attempt_context(task, ctx.claim, snapshot)
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
    return {
        "outcome": outcome,
        "compute_result": result.to_dict(),
        "receipt": receipt.to_dict(),
        "bundle_manifest": bundle.manifest.to_dict(),
    }, output_files


def dispatch(
    ctx: RunContext,
) -> tuple[dict, dict | None]:
    """按任务类型分派到执行器(返回结果 payload 与待封存输出文件, 含显式 outcome)。

    未实现的 I/O 执行器抛 tasks 域执行不可用错误(上抛, 不落成功);
    未知任务类型抛 InvalidTaskTypeError。计算类任务走真实链
    (``_execute_compute``): 任务/快照记录缺失即确定性失败, 不解释任何
    输入字段；输出文件原始字节随 payload 之外返回，由提交单元同事务
    封存（本函数不写数据库）。
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
    由提交单元同事务封存(封存/证据/引用/完成一次提交), 本函数只传参。
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
