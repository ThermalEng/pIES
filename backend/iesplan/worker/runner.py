"""任务执行分派与快照输入装配(计算 Worker / I/O Worker 共用)。

职责:
- load_inputs: 从不可变 calc_snapshots 装配输入(03 §2.2/§9.4: 项目版本内容 +
  绑定数据集逐时数据 + 时间轴), 重试复用同一快照, 输入含义不变;
- dispatch: 按 task.type 分派(calc/optimization/uncertainty 走快照输入 +
  计算阶段网关; report 走证据包; io 任务走占位执行器);
- run_task: 一次尝试的完整执行闭环 —— 短会话装配输入 → 无事务执行 →
  短事务提交(证据包/四维评估/结果索引/业务结局)或失败/取消收拢, 全部经由
  application.worker 阶段网关的 fencing 协议(03 §4.4: 迟到的写回永远不入
  权威库)。一次长时 attempt 不是一个事务: 输入加载完即关闭读取会话,
  求解/分析/导出在无数据库事务状态下运行, 领取/进度/续租/评估写入/
  提交/失败/取消各自使用新的短会话与 application.worker 短事务用例。

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
from datetime import UTC, datetime
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from iesplan.application import worker as worker_app
from iesplan.core.diagnostics import (
    SEVERITY_BLOCKING,
    SEVERITY_ERROR,
    TASK_DATA_SNAPSHOT_MISSING,
    TASK_SOLVE_FAILED,
)
from iesplan.core.errors import AppError
from iesplan.core.timeaxis import RESOLUTIONS, TimeAxis, build_axis
from iesplan.worker import executors
from iesplan.worker.executors import EngineRunError, RunContext, TaskCancelled

logger = logging.getLogger(__name__)

#: 计算类任务(必须绑定 calc_snapshot_id, 03 规格 2.1; 03 §9.7 增补 analysis)
COMPUTE_TASK_TYPES: tuple[str, ...] = ("calc", "optimization", "uncertainty", "analysis")
#: io 队列任务类型
IO_TASK_TYPES: tuple[str, ...] = ("report", "dataset_build", "export", "import")

#: 计算执行网关(Worker 消费的计算阶段边界; Wave4 computation 公开协议交接点)。
#: None = 无可用 provider → 显式不可用失败(ComputeUnavailableError →
#: failed + TASK-SOLVE-001)，不伪造成功、不回退旧引擎。测试经此公开属性
#: 注入假网关；生产由 Wave4 computation provider 接管。
compute_gateway: Callable[[RunContext, dict, dict, Any], dict] | None = None


class SnapshotInputError(AppError):
    """快照/数据集输入不可用(03 §6.3: TASK-DATA-001 blocking, 不可复现)。"""

    code = TASK_DATA_SNAPSHOT_MISSING
    severity = SEVERITY_BLOCKING
    message_key = "ies.diag.task.snapshot_missing"


class InvalidTaskTypeError(AppError):
    """未知任务类型(不落入任何执行器)。"""

    code = "TASK-REQ-002"
    message_key = "ies.diag.param.invalid"


class ComputeUnavailableError(AppError):
    """计算执行入口显式不可用(旧计算链已删除, 0.8 计算未实现)。

    确定性失败: 经失败收拢落 failed + TASK-SOLVE-001, 保留执行器原始原因,
    不得包成"内部错误", 更不得误判为 lease_rejected。
    """

    code = TASK_SOLVE_FAILED
    severity = SEVERITY_ERROR
    message_key = "ies.diag.task.solve_failed"


# ---------------------------------------------------------------------------
# 输入装配(快照不可变: 版本内容 + 数据集逐时数据 + 时间轴)
# ---------------------------------------------------------------------------


def load_inputs(
    db: Session, snapshot: worker_app.CalcSnapshotRecord
) -> tuple[dict, dict, TimeAxis]:
    """装配计算输入(03 §2.2): (项目版本内容, 逐时 data dict, 时间轴)。

    输入全部来自不可变快照: 项目版本内容对象、数据集逐时数据与时间轴。
    """
    if snapshot is None:
        raise SnapshotInputError("计算快照缺失", location={"object_type": "calc_snapshot"})
    content_object_id = worker_app.get_project_content_id(db, snapshot.project_version_id)
    if content_object_id is None:
        raise SnapshotInputError(
            "快照绑定的项目版本缺失",
            params={"calc_snapshot_id": snapshot.id},
            location={"object_type": "project_versions", "object_id": snapshot.project_version_id},
        )
    try:
        content = worker_app.load_version_content(db, content_object_id)
    except AppError as exc:
        raise SnapshotInputError(
            f"项目版本内容不可用: {exc}",
            params={"calc_snapshot_id": snapshot.id,
                    "content_object_id": content_object_id},
        ) from exc

    # 任务级参数权威来源 = 快照 calc_config_snapshot.task_params(03 规格 2.2:
    # 任务创建时任务级 config 并入快照输入, 版本内容不含任务参数)
    snapshot_config = snapshot.calc_config_snapshot or {}
    task_params = dict(snapshot_config.get("task_params") or {})
    cfg = content.setdefault("calc_config", {})
    cfg["task_params"] = task_params
    resolution = str(task_params.get("resolution") or "1h")
    if resolution not in RESOLUTIONS:
        raise SnapshotInputError(f"非法时间分辨率: {resolution!r}", params={"resolution": resolution})

    data, diags, actual_resolution, utc_offset = _load_dataset_data(
        db, list(snapshot.dataset_version_ids or []), resolution
    )
    if not data:
        raise SnapshotInputError(
            "快照绑定的数据集缺失或为空(输入不可复现)",
            params={"calc_snapshot_id": snapshot.id, "dataset_version_ids": snapshot.dataset_version_ids},
        )
    _data_to_si(data, actual_resolution)  # 声明单位 → SI(唯一换算边界, 01 §5.2)
    axis = _build_axis(actual_resolution, utc_offset, data)
    return content, data, axis






def _load_dataset_data(
    db: Session, dataset_version_ids: list[int], fallback_resolution: str,
) -> tuple[dict, list[dict], str, int]:
    """装配绑定数据集的逐时数据(多版本按序合并, 先到先得)。

    返回 (data dict, 数据集诊断列表, 实际分辨率, 固定 UTC 偏移分钟)。
    列名映射(数据集标准字段 → 引擎字段, 单位换算 kWh/步 → W):
        e_load/h_load/c_load → 功率 W(= kWh × 1000 / 步长小时);
        t_ambient → temperature(°C); ghi → ghi(W/m²);
        electricity_price → tariff_buy(元/kWh); grid_emission_factor → kg/kWh。
    """
    data: dict[str, np.ndarray] = {}
    diagnostics: list[dict] = []
    resolution = fallback_resolution
    utc_offset = 480
    for dvid in dataset_version_ids:
        version = worker_app.get_dataset_version_record(db, dvid)
        if version is None:
            raise SnapshotInputError("快照绑定的数据集版本缺失", params={"dataset_version_id": dvid})
        resolution = version.resolution or resolution
        utc_offset = version.fixed_utc_offset_minutes
        data_object_id = worker_app.get_dataset_data_object(db, dvid)
        if data_object_id is None:
            continue
        raw = worker_app.load_dataset_blob(db, data_object_id)
        rows, diags = worker_app.parse_dataset_csv(raw, resolution)
        diags_dicts = [d.to_dict() for d in diags]
        diagnostics.extend(diags_dicts)
        if any(d.get("blocking") for d in diags_dicts):
            raise SnapshotInputError(
                "数据集解析存在阻断性错误(输入不可用)",
                params={"dataset_version_id": dvid, "blocking": len(diags_dicts)},
            )
        if not rows:
            continue
        _merge_rows(data, rows, resolution)
    return data, diagnostics, resolution, utc_offset


def _merge_rows(data: dict[str, np.ndarray], rows: list[dict], resolution: str) -> None:
    """数据行列表 → 引擎字段数组(缺失字段置 0; 多版本只补空缺, 先到先得)。

    单位换算统一在计算边界完成(_data_to_si): 本函数只做"声明单位数值 →
    引擎字段"的搬运与缺失补零, 不在解析层做 kWh→W 等手写换算(01 §5.3)。
    """
    n = len(rows)
    mapping = {
        "e_load": "e_load", "h_load": "h_load", "c_load": "c_load",
        "t_ambient": "temperature", "ghi": "ghi",
        "electricity_price": "tariff_buy", "grid_emission_factor": "emission_factor_grid",
    }
    for col, engine_key in mapping.items():
        if engine_key in data:
            continue  # 已有版本提供该字段
        values = [row.get(col) for row in rows]
        if all(v is None for v in values):
            continue
        arr = np.asarray([0.0 if v is None else float(v) for v in values], dtype=np.float64)
        if arr.size != n:
            raise SnapshotInputError("数据集行数不一致", params={"field": col, "rows": arr.size})
        if col == "grid_emission_factor":
            # 引擎约定: 排放因子为标量(kg/kWh); 逐时列取均值(缺省 0.581)
            data[engine_key] = float(np.mean(arr))
        else:
            data[engine_key] = arr
    # 缺失的负荷字段置 0(引擎约定: 热/冷缺省为 0)
    for key in ("e_load", "h_load", "c_load"):
        data.setdefault(key, np.zeros(n, dtype=np.float64))


def _data_to_si(data: dict[str, np.ndarray], resolution: str) -> None:
    """引擎输入逐时数据 → SI 功率边界(01 §5.2 data_to_si 语义, 唯一换算点)。

    本波次只换算能量型字段: e_load/h_load/c_load 声明 kWh/步 → 引擎功率 W
    (= J/步长秒 = kWh × 3.6e6 / (step_min × 60)), 去除 runner 内手写
    `*1000.0/step_hours`(01 §4.1: 换算经 core/units, 禁止自建换算表)。
    温度/电价/排放因子保持引擎声明单位(°C / CNY/kWh / kg/kWh), 配套引擎
    SI 化(P4)不在本波次范围。
    """
    from iesplan.core.units import to_si

    step_seconds = RESOLUTIONS[resolution][1] * 60.0
    for key in ("e_load", "h_load", "c_load"):
        arr = data.get(key)
        if isinstance(arr, np.ndarray):
            # kWh/步 → J/步 → W(引擎约定逐时功率)
            data[key] = arr * to_si(1.0, "kWh") / step_seconds


def _build_axis(resolution: str, utc_offset: int, data: dict) -> TimeAxis:
    """按数据集构建时间轴: 标准年步数 → 标准日历; 迷你行数 → 按行数构造。"""
    n_expected = RESOLUTIONS[resolution][0]
    first = next(iter(data.values()))
    n = int(first.size)
    if n == n_expected:
        return build_axis(resolution, utc_offset_minutes=utc_offset)
    # 非标准步数(迷你/分段算例): 复用标准非闰年日历的月/季节表
    step_min = RESOLUTIONS[resolution][1]
    idx = np.arange(n, dtype=np.int64)
    day_of_year = idx // (1440 // step_min)
    # 每月起始的年内天偏移(非闰年, 0 基; 与 iesplan.core.timeaxis 同表)
    month_start = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
    season = np.asarray([
        0 if m in (12, 1, 2) else 1 if m in (3, 4, 5) else 2 if m in (6, 7, 8) else 3
        for d in day_of_year
        for m in (max(m_i for m_i, start in enumerate(month_start, 1) if d >= start),)
    ], dtype=np.int64)
    return TimeAxis(
        resolution=resolution, n=n, step_minutes=step_min,
        utc_offset_minutes=utc_offset,
        t0_utc=datetime(2025, 1, 1, tzinfo=UTC),
        hour_of_year=idx // (60 // step_min), day_of_year=day_of_year, season=season,
    )


# ---------------------------------------------------------------------------
# 分派
# ---------------------------------------------------------------------------


def dispatch(ctx: RunContext) -> dict:
    """按任务类型分派到执行器(返回结果 payload, 含显式 outcome)。

    未实现的 I/O 执行器抛 tasks 域执行不可用错误(上抛, 不落成功);
    未知任务类型抛 InvalidTaskTypeError。计算类输入在短读会话内装配,
    会话关闭后才进入执行器(执行期无打开的数据库事务)。
    """
    task_type = ctx.task.type
    if task_type in COMPUTE_TASK_TYPES:
        with ctx.session_factory() as db:
            content, data, axis = load_inputs(db, ctx.snapshot)
        ctx.axis_resolution = axis.resolution
        ctx.axis_n = int(axis.n)
        # 计算执行经可注入的阶段网关(输入会话已关闭, 执行期无打开会话)。
        gateway = compute_gateway
        if gateway is None:
            raise ComputeUnavailableError(
                "计算执行网关无可用 provider, 等待 GeneratorProvider/Solver Bundle 接入"
            )
        return gateway(ctx, content, data, axis)
    if task_type == "report":
        return executors.execute_check(ctx)
    if task_type == "dataset_build":
        return executors.execute_dataset_process(ctx)
    if task_type == "export":
        return executors.execute_export(ctx)
    if task_type == "import":
        return executors.execute_package_import(ctx)
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
    isolate: bool = True,
    stop_event: Any = None,
) -> str:
    """执行已领取的任务并落终态(带 fencing 提交/失败/取消收拢)。

    参数:
        session_factory: 会话工厂(Worker 持有的唯一会话来源; 各阶段按需
            开短会话, 本函数不持有跨阶段的 Session)。
        claim: acquire_attempt 的领取结果(尝试 + 租约 + token)。
        isolate: 计算引擎是否运行在隔离子进程(生产 True; 测试可关闭)。
        stop_event: 取消/优雅退出事件(透传给执行器检查点与隔离子进程)。
    返回:
        终态状态: completed / failed / cancelled / lease_rejected。
    事务边界: 本函数不调用 commit/rollback, 不持有跨阶段的 Session; 任务/
    快照读完即关闭读取会话, 求解/分析/导出在无数据库事务状态下运行,
    领取/进度/续租/评估写入/提交/失败/取消各自的新短会话短事务由
    application.worker 用例拥有并提交(领取已在执行前提交, 故执行期回滚
    不会丢失租约; 失败收拢不再误判为 lease_rejected)。
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
        isolate=isolate, stop_event=stop_event, snapshot=snapshot,
        progress_fn=_report_progress,
    )
    try:
        payload = dispatch(ctx)
        # 完成路径要求显式合法 outcome: 缺字段默认成功已删除, 缺失/非法
        # 即确定性失败, 绝不落成功(合法集合归 tasks 域所有)。
        outcome = payload.get("outcome")
        if outcome not in worker_app.BUSINESS_OUTCOMES:
            raise EngineRunError(f"执行器未返回显式合法 outcome: {outcome!r}")
        with factory() as db:
            worker_app.submit_attempt_result(db, claim, payload=payload, outcome=outcome,
                                             actor_id=task.requested_by)
        return "completed"
    except TaskCancelled as exc:
        return _handle_cancel(factory, ctx, claim, exc.stage)
    except (worker_app.LeaseRejectedError, SnapshotInputError, EngineRunError, AppError) as exc:
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
