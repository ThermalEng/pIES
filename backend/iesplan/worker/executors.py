"""任务类型执行函数（计算 Worker 职责，见宪法 §4.5/§12 与 architecture §核心业务流）。

0.8 计算未实现：计算类任务（calc/optimization/uncertainty/analysis）入口显式
抛 NotImplementedError，经 runner 收拢为结构化可见失败（failed +
TASK-SOLVE-001），不伪造成功、不 fallback 旧引擎。旧 plan 装配、旧算法选择器、
旧命令注册与旧求解/评估代码已删除，等待 GeneratorProvider/Solver Bundle 接入。

本模块仅保留：
- 任务执行上下文（进度/取消检查点，经 application.worker 用例）；
- 计算入口的明确未实现错误；
- 结果检查（report）与 I/O 占位执行器（分派 + 占位，不驱动计算引擎）。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from iesplan.application import worker as worker_app
from iesplan.worker.lease import Claim


class TaskCancelled(Exception):
    """取消检查点触发（Worker 轮询到取消信号后终止执行，见宪法 §12）。"""

    def __init__(self, stage: str = "") -> None:
        self.stage = stage
        super().__init__(f"任务已取消(阶段: {stage})")


class EngineRunError(Exception):
    """引擎执行失败(含隔离子进程超时/异常)。"""


@dataclass(slots=True)
class RunContext:
    """一次任务执行上下文(执行器/进度/取消检查共用)。

    task/snapshot 为 application.worker 用例返回的公开记录(计算类任务
    快照由 runner 装配); 本模块不直接引用 ``iesplan.models.*``。
    """

    db: Session
    task: worker_app.TaskRecord
    claim: Claim
    worker_id: str = ""
    isolate: bool = True
    stop_event: threading.Event | None = None
    progress_fn: Callable[[float, str, dict | None], None] | None = None
    snapshot: worker_app.CalcSnapshotRecord | None = None  # 计算类任务快照; runner 装配
    axis_resolution: str = "1h"
    axis_n: int = 8760
    io_params: dict[str, Any] = field(default_factory=dict)  # io 任务参数(队列消息扩展)

    def progress(self, percent: float, stage: str, detail: dict | None = None) -> None:
        """阶段化进度报告（PG 持久进度 + Redis 秒级进度，见 contracts §快照与异步契约）。"""
        if self.progress_fn is not None:
            self.progress_fn(percent, stage, detail)

    def checkpoint(self, stage: str) -> None:
        """取消检查点: 每阶段检查任务是否 cancelling（见宪法 §12）。

        取消信号(Redis cancel 键)或停止事件置位 → 抛 TaskCancelled。
        """
        if self.stop_event is not None and self.stop_event.is_set():
            raise TaskCancelled(stage)
        if _cancel_signal(self.task.id):
            raise TaskCancelled(stage)


def _cancel_signal(task_id: int) -> bool:
    """读取取消信号(Redis cancel:{task_id}, 可重建视图; 经 application.worker 用例)。"""
    return worker_app.cancel_requested(task_id)


# ---------------------------------------------------------------------------
# 计算类任务入口（0.8 未实现，显式失败；旧计算链已删除）
# ---------------------------------------------------------------------------


def execute_calc(ctx: RunContext, content: dict, data: dict, axis: Any, options: dict | None = None) -> dict:
    """旧计算原型已删除；待 0.8 GeneratorProvider 接入。"""
    raise NotImplementedError("旧计算执行链已删除，等待 GeneratorProvider/Solver Bundle")


def execute_plan(ctx: RunContext, content: dict, data: dict, axis: Any, options: dict | None = None) -> dict:
    """旧规划链已删除；待 0.8 GeneratorProvider/Solver Bundle 接入。"""
    raise NotImplementedError("旧规划执行链已删除，等待 GeneratorProvider/Solver Bundle")


def execute_uncertainty(
    ctx: RunContext, content: dict, data: dict, axis: Any, options: dict | None = None,
) -> dict:
    """旧不确定性分析链已删除；待 0.8 GeneratorProvider/Solver Bundle 接入。"""
    raise NotImplementedError("旧不确定性执行链已删除，等待 GeneratorProvider/Solver Bundle")


def execute_analysis(
    ctx: RunContext, content: dict, data: dict, axis: Any, options: dict | None = None
) -> dict:
    """旧批量分析链已删除；待 0.8 GeneratorProvider/Solver Bundle 接入。"""
    raise NotImplementedError("旧批量分析执行链已删除，等待 GeneratorProvider/Solver Bundle")


# ---------------------------------------------------------------------------
# 结果检查（task_type=report，四维评估见 domain-model §快照任务和结果）
# ---------------------------------------------------------------------------


def execute_check(ctx: RunContext) -> dict:
    """结果检查: 对证据包执行四维检查(追加评估记录, 不覆盖原记录)。

    任务参数(存储待 tasks.params 落地): io_params 支持
    evidence_package_id / task_id; 缺省检查本项目最新证据包。
    """
    evidence_id: int | None = None
    msg_params = ctx.io_params or {}
    if msg_params.get("evidence_package_id"):
        evidence_id = int(msg_params["evidence_package_id"])
    elif msg_params.get("task_id"):
        package_row = worker_app.get_latest_evidence_for_task(
            ctx.db, int(msg_params["task_id"])
        )
        evidence_id = package_row.id if package_row else None
    if evidence_id is None:
        package_row = worker_app.get_latest_evidence_for_project(
            ctx.db, ctx.task.project_id
        )
        evidence_id = package_row.id if package_row else None

    ctx.progress(20, "load_evidence", {"evidence_package_id": evidence_id})
    ctx.checkpoint("load_evidence")
    if evidence_id is None:
        return {
            "schema_version": 1, "result_kind": "assessment_report", "task_type": "report",
            "status": "no_evidence", "evidence_package_id": None, "assessment": {},
            "outcome": "insufficient_evidence",
            "summary": {"assessed": False, "reason": "项目无证据包可检查"},
        }
    package = worker_app.get_evidence_record(ctx.db, evidence_id)
    payload = _load_evidence_payload(ctx.db, package)
    assessment = _assess_payload(payload)

    # 追加评估记录(assessor='system', 不覆盖原记录)并挂接最新评估引用
    assessment_id = worker_app.append_check_assessment(
        ctx.db, evidence_package_id=package.id, assessment=assessment
    )
    has_fail = any(assessment[d] == "fail" for d in (
        "dimension_physical", "dimension_optimality", "dimension_financial", "dimension_reliability"))
    ctx.progress(100, "done", {"assessment_id": assessment_id, "has_fail": has_fail})
    return {
        "schema_version": 1,
        "result_kind": "assessment_report",
        "task_type": "report",
        "status": "assessed",
        "evidence_package_id": package.id,
        "assessment": assessment,
        "outcome": "insufficient_evidence" if has_fail else "normal_completion",
        "summary": {"assessed": True, "evidence_package_id": package.id,
                    "assessment_id": assessment_id},
    }


def _load_evidence_payload(db: Session, package: worker_app.EvidencePackageRecord) -> dict:
    """读取证据包对象内容并解析（按对象 id 读取，解析失败抛错）。"""
    raw = worker_app.load_worker_object(db, package.object_id)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise EngineRunError(f"证据包内容解析失败: {exc}") from exc
    return payload if isinstance(payload, dict) else {}


def _assess_payload(payload: dict) -> dict:
    """对既有证据包重新派生四维评估（与提交时同口径，可追溯不覆盖）。"""
    raw = payload.get("assessment")
    assessment = dict(raw) if isinstance(raw, dict) else {}
    dims = ["dimension_physical", "dimension_optimality", "dimension_financial", "dimension_reliability"]
    for dim in dims:
        if dim not in assessment:
            assessment[dim] = "unknown"
    if "overall_score" not in assessment:
        assessment["overall_score"] = _overall_score(
            assessment[dims[0]], assessment[dims[1]], assessment[dims[2]], assessment[dims[3]])
    assessment.setdefault("comment", "结果检查(系统评估)")
    assessment.setdefault("detail", {"source_kind": payload.get("result_kind")})
    return assessment


# ---------------------------------------------------------------------------
# I/O 任务占位执行器(数据集处理/Excel/项目包; 项目包功能在另一 agent)
# ---------------------------------------------------------------------------


def execute_dataset_process(ctx: RunContext) -> dict:
    """数据集处理(task_type=dataset_build, io 队列)占位执行器。

    真实实现(清洗/构建数据集版本)由后续波次接入; 本波次只报告进度并落
    占位结果, 保证任务生命周期完整闭环。
    """
    return _io_placeholder(ctx, io_kind="dataset_build", stage_hint="清洗/构建数据集版本")


def execute_export(ctx: RunContext) -> dict:
    """Excel/项目包导出(task_type=export, io 队列)占位执行器。

    export_kind(excel_report/raw_data/project_package)参数存储待 tasks.params
    落地; 项目包导出功能由另一 agent 实现。
    """
    return _io_placeholder(ctx, io_kind="export", stage_hint="Excel 报告/项目包导出")


def execute_package_import(ctx: RunContext) -> dict:
    """项目包导入(task_type=import, io 队列)占位执行器。

    项目包导入与校验由另一实现负责；本波次只留分派与占位。
    """
    return _io_placeholder(ctx, io_kind="package_import", stage_hint="项目包导入与校验")


def _io_placeholder(ctx: RunContext, *, io_kind: str, stage_hint: str) -> dict:
    """I/O 占位执行器通用流程: 进度 + 取消检查点 + 占位结果。"""
    ctx.progress(30, "io_prepare", {"io_kind": io_kind})
    ctx.checkpoint("io_run")
    ctx.progress(80, "io_run", {"io_kind": io_kind, "hint": stage_hint})
    ctx.checkpoint("io_finish")
    ctx.progress(100, "done", {"io_kind": io_kind})
    return {
        "schema_version": 1,
        "result_kind": "io_placeholder",
        "task_type": ctx.task.type,
        "status": "placeholder",
        "io_kind": io_kind,
        "outcome": "normal_completion",
        "summary": {"placeholder": True, "hint": f"{stage_hint}: 占位执行器(后续波次实现)"},
        "meta": {"engine": "io-placeholder@0.1.0"},
    }


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _overall_score(physical: str, optimality: str, financial: str, reliability: str) -> float:
    """四维 → 综合得分(0-100; fail 扣 40, unknown 扣 15, 通过 100)。"""
    dims = [physical, optimality, financial, reliability]
    score = 100.0 - 40.0 * dims.count("fail") - 15.0 * dims.count("unknown")
    return max(score, 0.0)
