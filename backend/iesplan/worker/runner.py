"""任务执行分派与快照输入装配(计算 Worker / I/O Worker 共用)。

职责:
- load_inputs: 从不可变 calc_snapshots 装配输入(03 §2.2/§9.4: 项目版本内容 +
  绑定数据集逐时数据 + 时间轴), 重试复用同一快照, 输入含义不变;
- dispatch: 按 task.type 分派到 executors(calc/optimization/uncertainty 走
  快照输入; report 走证据包; io 任务走占位执行器);
- run_task: 一次尝试的完整执行闭环 —— 装配输入 → 执行 → 提交(证据包/
  四维评估/结果索引/业务结局)或失败/取消收拢, 全部经由 lease 的 fencing
  协议(03 §4.4: 迟到的写回永远不入权威库)。

写入资格: 本模块不直接写任务状态/结果, 统一由 lease.submit_result /
fail_attempt / cancel_attempt 带 token 完成(03 §4.4 硬约束)。
"""

from __future__ import annotations

import logging
import traceback
from datetime import UTC, datetime
from typing import Any

import numpy as np
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from iesplan.assembly import AssemblyValidationError, ValidatedAssemblyArtifact
from iesplan.core.diagnostics import SEVERITY_BLOCKING, TASK_DATA_SNAPSHOT_MISSING
from iesplan.core.errors import AppError
from iesplan.core.timeaxis import RESOLUTIONS, TimeAxis, build_axis
from iesplan.models.calc import CalcSnapshot, Task
from iesplan.models.dataset import DatasetFile, DatasetVersion
from iesplan.models.project import ProjectVersion
from iesplan.services import dataset as dataset_service
from iesplan.services import project as project_service
from iesplan.storage import get_object
from iesplan.worker import executors, lease
from iesplan.worker.executors import EngineRunError, RunContext, TaskCancelled

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
# 输入装配(快照不可变: 版本内容 + 数据集逐时数据 + 时间轴)
# ---------------------------------------------------------------------------


def load_inputs(db: Session, snapshot: CalcSnapshot) -> tuple[dict, dict, TimeAxis]:
    """装配计算输入(03 §2.2): (项目版本内容, 逐时 data dict, 时间轴)。

    输入全部来自不可变快照: 项目版本 content_hash 指向内容对象(读取时校验
    哈希, 01 §10.1); 数据集版本文件经 parse_csv 解析为逐时数组; 时间轴按
    数据集分辨率/固定偏移构建(行数 = 标准年步数时按标准日历, 迷你数据按
    实际行数, 供测试与分段算例使用)。
    """
    if snapshot is None:
        raise SnapshotInputError("计算快照缺失", location={"object_type": "calc_snapshot"})
    _verify_snapshot_assembly(snapshot)
    version = db.get(ProjectVersion, snapshot.project_version_id)
    if version is None:
        raise SnapshotInputError(
            "快照绑定的项目版本缺失",
            params={"calc_snapshot_id": snapshot.id},
            location={"object_type": "project_versions", "object_id": snapshot.project_version_id},
        )
    try:
        content = project_service.load_content_object(db, version.content_hash)
    except AppError as exc:
        raise SnapshotInputError(
            f"项目版本内容不可用: {exc}",
            params={"calc_snapshot_id": snapshot.id, "content_hash": version.content_hash},
        ) from exc

    # 任务级参数权威来源 = 快照 calc_config_snapshot.task_params(03 规格 2.2:
    # 任务创建时任务级 config 并入快照哈希, 版本内容不含任务参数)
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



def _verify_snapshot_assembly(snapshot: CalcSnapshot) -> ValidatedAssemblyArtifact:
    """恢复并校验快照中的规范装配三件套；旧/畸形快照禁止进入计算。"""
    text = snapshot.canonical_assembly_text
    digest = snapshot.assembly_sha256
    receipt = snapshot.assembly_receipt
    if (
        not isinstance(text, str)
        or not text
        or not isinstance(digest, str)
        or not isinstance(receipt, dict)
    ):
        raise SnapshotInputError(
            "计算快照缺少规范装配产物三件套",
            params={"calc_snapshot_id": snapshot.id, "reason": "assembly_artifact_missing"},
            location={"object_type": "calc_snapshot", "object_id": snapshot.id},
        )
    try:
        return ValidatedAssemblyArtifact.from_persisted(text, digest, receipt)
    except (AssemblyValidationError, TypeError, ValueError) as exc:
        raise SnapshotInputError(
            "计算快照规范装配产物不一致",
            params={"calc_snapshot_id": snapshot.id, "reason": "assembly_artifact_invalid"},
            location={"object_type": "calc_snapshot", "object_id": snapshot.id},
        ) from exc


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
        version = db.get(DatasetVersion, dvid)
        if version is None:
            raise SnapshotInputError("快照绑定的数据集版本缺失", params={"dataset_version_id": dvid})
        resolution = version.resolution or resolution
        utc_offset = version.fixed_utc_offset_minutes
        data_file = db.execute(
            select(DatasetFile)
            .where(DatasetFile.dataset_version_id == dvid, DatasetFile.file_kind == "data")
            .order_by(DatasetFile.id)
        ).scalars().first()
        if data_file is None:
            continue
        raw = get_object(db, data_file.object_id)
        rows, diags = dataset_service.parse_csv(raw, resolution)
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
    """按任务类型分派到执行器(返回结果 payload, 含 outcome/assessment)。"""
    task_type = ctx.task.type
    if task_type in COMPUTE_TASK_TYPES:
        content, data, axis = load_inputs(ctx.db, ctx.snapshot)
        ctx.axis_resolution = axis.resolution
        ctx.axis_n = int(axis.n)
        if task_type == "calc":
            return executors.execute_calc(ctx, content, data, axis)
        if task_type == "optimization":
            return executors.execute_plan(ctx, content, data, axis)
        if task_type == "analysis":
            return executors.execute_analysis(ctx, content, data, axis)
        return executors.execute_uncertainty(ctx, content, data, axis)
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
    db: Session,
    claim: lease.Claim,
    *,
    worker_id: str = "",
    isolate: bool = True,
    stop_event: Any = None,
) -> str:
    """执行已领取的任务并落终态(带 fencing 提交/失败/取消收拢)。

    参数:
        claim: acquire_attempt 的领取结果(尝试 + 租约 + token)。
        isolate: 计算引擎是否运行在隔离子进程(生产 True; 测试可关闭)。
        stop_event: 取消/优雅退出事件(透传给执行器检查点与隔离子进程)。
    返回:
        终态状态: completed / failed / cancelled / lease_rejected。
    本函数负责提交事务(run_task 是尝试的单一事务边界)。
    """
    task = db.get(Task, claim.task_id)
    if task is None:
        raise SnapshotInputError("任务不存在", params={"task_id": claim.task_id})
    snapshot = db.get(CalcSnapshot, task.calc_snapshot_id) if task.calc_snapshot_id else None
    ctx = RunContext(
        db=db, task=task, claim=claim, worker_id=worker_id, isolate=isolate,
        stop_event=stop_event, snapshot=snapshot,
        progress_fn=lambda p, s, d: lease.report_progress(
            db, claim.attempt_id, claim.lease_token, task.id, p, s, d,
        ),
    )
    try:
        payload = dispatch(ctx)
        outcome = str(payload.get("outcome") or "normal_completion")
        receipt = lease.submit_result(db, claim, payload=payload, outcome=outcome,
                                      actor_id=task.requested_by)
        db.commit()
        logger.info("任务完成: task=%s outcome=%s evidence=%s",
                    task.id, outcome, receipt.evidence_package_id)
        return "completed"
    except TaskCancelled as exc:
        db.rollback()
        return _handle_cancel(db, ctx, claim, task, exc.stage)
    except (lease.LeaseRejectedError, SnapshotInputError, EngineRunError, AppError) as exc:
        db.rollback()
        return _handle_failure(db, ctx, claim, exc)
    except Exception as exc:  # noqa: BLE001 - 尝试边界: 任何未预期异常落确定性失败
        db.rollback()
        logger.exception("任务执行内部错误: task=%s", task.id)
        return _handle_failure(db, ctx, claim, RuntimeError(f"内部错误: {exc}"))


def _handle_cancel(db: Session, ctx: RunContext, claim: lease.Claim, task: Task, stage: str) -> str:
    """取消收拢(03 §6.1): 部分完成的批量子任务 → partial_batch。"""
    logger.info("任务取消: task=%s stage=%s", task.id, stage)
    outcome = None
    if task.type == "uncertainty":
        from iesplan.models.uncertainty import SampleTask

        completed = db.execute(
            sa.select(sa.func.count()).select_from(SampleTask)
            .where(SampleTask.parent_task_id == task.id, SampleTask.status == "completed")
        ).scalar() or 0
        if completed > 0:
            outcome = "partial_batch"
    try:
        lease.cancel_attempt(db, claim, outcome=outcome)
        db.commit()
        return "cancelled"
    except AppError as exc:
        # 取消竞态: 任务已终态(以先落终态者为准, 03 §6.1 规则 4)
        db.rollback()
        logger.info("取消竞态忽略: task=%s (%s)", task.id, exc)
        return "cancelled"


def _handle_failure(db: Session, ctx: RunContext, claim: lease.Claim, exc: Exception) -> str:
    """失败收拢(03 §6.3): 快照/输入问题 → blocking insufficient_evidence。

    返回失败终态; 租约失效类错误不写终态(由调度器守护回收, 03 §4.3)。
    """
    if isinstance(exc, lease.LeaseRejectedError):
        logger.warning("租约失效, 停止一切写回: task=%s (%s)", claim.task_id, exc)
        return "lease_rejected"
    if isinstance(exc, SnapshotInputError):
        try:
            lease.fail_attempt(
                db, claim, code=exc.code, message=str(exc), level=exc.severity,
                outcome="insufficient_evidence",
            )
            db.commit()
        except lease.LeaseRejectedError:
            db.rollback()
            return "lease_rejected"
        return "failed"
    # 引擎/内部失败: 确定性失败落 failed(TASK-SOLVE-001), 不自动重试
    try:
        lease.fail_attempt(
            db, claim, code="TASK-SOLVE-001", message=str(exc),
            stack_trace=traceback.format_exc(limit=10),
        )
        db.commit()
    except lease.LeaseRejectedError:
        db.rollback()
        return "lease_rejected"
    return "failed"
