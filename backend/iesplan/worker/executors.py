"""任务类型执行函数（计算 Worker 职责，见宪法 §4.5/§12 与 architecture §核心业务流）。

0.8 计算未实现：计算类任务（calc/optimization/uncertainty/analysis）入口显式
抛 NotImplementedError，经 runner 收拢为结构化可见失败（failed +
TASK-SOLVE-001），不伪造成功、不 fallback 旧引擎。旧 plan 装配、旧算法选择器、
旧命令注册与旧求解/评估代码已删除，等待 GeneratorProvider/Solver Bundle 接入。

I/O 任务（dataset_build/export/import）执行入口同样未实现：抛 tasks 域
明确执行不可用错误，经 runner 收拢为结构化失败（failed +
TASK-EXEC-001），不得借用 TASK-SOLVE-001 充数，更不伪造成功。

本模块仅保留：
- 任务执行上下文（进度/取消检查点，经 application.worker 用例）；
- 计算/I/O 入口的明确未实现错误；
- 结果检查（report）运行编排：按运行时序调用 application.worker
  report 阶段命令（只读定位 → 进度/检查点 → 原子评估 → 上报），证据
  定位/解析/评估/评分/outcome 与 payload 组装由 application 经
  results/analysis 公开能力与领域规则完成，本模块不解释证据、不复制
  评分规则，不把回调传入 application。
"""

from __future__ import annotations

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
    快照由 runner 装配, 均为脱离会话的不可变值对象); 本模块不直接引用
    ``iesplan.models.*``。需要权威状态读写时, 经 ``session_factory`` 开
    新短会话调用 application.worker 短事务用例, 长时运算期间不持有打开
    的会话或数据库事务。
    """

    task: worker_app.TaskRecord
    claim: Claim
    session_factory: Callable[[], Session]
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
    """结果检查运行编排: Worker 只安排定位 → 检查点 → 评估 → 上报时序。

    定位与评估均为 application.worker 阶段命令(短会话由本层开, 短事务由
    application 提交; 只读定位与原子评估写分离): ``locate_report_evidence``
    做证据定位读, ``assess_report_stage`` 做评估写/无证据口径并返回不可变
    阶段结果契约。本函数只调阶段命令、自有 progress/checkpoint、消费显式
    契约的 payload 上报; 证据选择优先级、无证据业务 outcome/payload、
    assessment DTO 组装全部归 application, 本层不复制。进度与取消回调不
    出本层, 不传入 application。

    任务参数(存储待 tasks.params 落地): io_params 支持
    evidence_package_id / task_id; 缺省检查本项目最新证据包。
    """
    msg_params = ctx.io_params or {}
    # 定位阶段: 短会话只读, 读完即关, 不跨越后续检查点与评估写入
    with ctx.session_factory() as db:
        evidence_id = worker_app.locate_report_evidence(
            db,
            project_id=ctx.task.project_id,
            evidence_package_id=msg_params.get("evidence_package_id"),
            task_id=msg_params.get("task_id"),
        )

    ctx.progress(20, "load_evidence", {"evidence_package_id": evidence_id})
    ctx.checkpoint("load_evidence")
    # 评估阶段: 独立短会话, 短事务由阶段命令提交后即关
    with ctx.session_factory() as db:
        result = worker_app.assess_report_stage(
            db, ctx.claim, evidence_id=evidence_id
        )
    if result.assessment is not None:
        ctx.progress(100, "done", {"assessment_id": result.assessment.id})
    return result.payload


# ---------------------------------------------------------------------------
# I/O 任务未实现入口(数据集处理/Excel 导出/项目包导入; 显式不可用)
# ---------------------------------------------------------------------------


def execute_dataset_process(ctx: RunContext) -> dict:
    """数据集处理(task_type=dataset_build, io 队列)未实现入口。

    真实实现(清洗/构建数据集版本)待后续波次接入; 本波次抛 tasks 域明确
    执行不可用错误(经 runner 收拢为结构化失败 failed + TASK-EXEC-001),
    不伪造成功。
    """
    raise worker_app.ExecutionUnavailableError(
        "dataset_build 执行入口未实现",
        params={"task_id": ctx.task.id, "task_type": ctx.task.type},
    )


def execute_export(ctx: RunContext) -> dict:
    """Excel/项目包导出(task_type=export, io 队列)未实现入口。

    export_kind(excel_report/raw_data/project_package)参数存储待 tasks.params
    落地; 抛 tasks 域明确执行不可用错误( failed + TASK-EXEC-001), 不伪造成功。
    """
    raise worker_app.ExecutionUnavailableError(
        "export 执行入口未实现",
        params={"task_id": ctx.task.id, "task_type": ctx.task.type},
    )


def execute_package_import(ctx: RunContext) -> dict:
    """项目包导入(task_type=import, io 队列)未实现入口。

    项目包导入与校验由后续实现负责; 抛 tasks 域明确执行不可用错误
    (failed + TASK-EXEC-001), 不伪造成功。
    """
    raise worker_app.ExecutionUnavailableError(
        "import 执行入口未实现",
        params={"task_id": ctx.task.id, "task_type": ctx.task.type},
    )
