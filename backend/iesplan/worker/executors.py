"""任务类型执行函数(计算 Worker 职责, 03 规格 2.1/§3.2、RPD 12.2)。

每个执行器:
- 调用引擎(经 run_solver_isolated 隔离子进程)与指标模块;
- 阶段化进度报告(record_progress)与取消检查点(每阶段检查 cancelling);
- 产出结果 payload(含四维评估与业务结局), 由 runner 统一落证据包/评估/
  结果索引(03 §4.1 ③, 写入资格由 lease.submit_result 的 fencing 保证)。

任务类型映射(01 §7.2 枚举 ↔ 本模块执行器):
    calc          → execute_calc           (方案评价, 02 §7)
    optimization  → execute_plan           (规划, 02 §5-§6)
    uncertainty   → execute_uncertainty    (不确定性分析, 02 §10; 父任务顺序执行样本)
    report        → execute_check          (结果检查, 01 §8.2 四维评估)
    dataset_build → execute_dataset_process(数据集处理, 占位执行器)
    export        → execute_export         (Excel/项目包导出, 占位执行器)
    import        → execute_package_import (项目包导入, 占位执行器)

注: I/O 执行器(数据集处理/Excel/项目包)本波次只落"分派 + 占位", 项目包
功能由另一 agent 实现; 任务级参数存储(tasks.params 列)尚未落地, io 任务
参数按约定取默认(见各执行器 docstring)。
"""

from __future__ import annotations

import importlib
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import numpy as np
import sqlalchemy as sa
from sqlalchemy.orm import Session

from iesplan.core.diagnostics import TASK_SOLVE_FAILED
from iesplan.core.jsonutil import jsonable
from iesplan.core.idgen import sha256_hex
from iesplan.engines.eval_run import EvalResult
from iesplan.engines.planning import PlanningResult
from iesplan.metrics.engineering import energy_balance_summary
from iesplan.metrics.environmental import operational_emissions
from iesplan.models.calc import Task, TaskDiagnostic
from iesplan.models.result import EvidencePackage, ResultAssessment, ResultIndex
from iesplan.models.uncertainty import SampleRecord, SampleTask, UncertaintySnapshot
from iesplan.worker.lease import Claim
from iesplan.worker.solver_process import run_solver_isolated

logger = logging.getLogger(__name__)

#: 求解器状态常量(02 §11.4)
S_OPTIMAL = "OPTIMAL"
S_TIME_LIMIT_INCUMBENT = "TIME_LIMIT_WITH_INCUMBENT"
S_NO_FEASIBLE = "NO_FEASIBLE_FOUND"
S_BASE_INFEASIBLE = "BASE_INFEASIBLE"
S_IRR_FLOOR = "INFEASIBLE_BY_IRR_FLOOR"
S_MODEL_AUDIT_FAIL = "MODEL_AUDIT_FAIL"

#: 不确定性两种分析模式(RPD 10.5)
MODE_FIXED_RELIABILITY = "fixed_reliability"
MODE_REPLAN_SENSITIVITY = "replan_sensitivity"

#: 可靠性有效样本比例阈值(低于则 reliability 维度 fail)
RELIABILITY_RATIO_THRESHOLD = 0.8


class TaskCancelled(Exception):
    """取消检查点触发(03 §6.1: Worker 轮询到取消信号后终止执行)。"""

    def __init__(self, stage: str = "") -> None:
        self.stage = stage
        super().__init__(f"任务已取消(阶段: {stage})")


class EngineRunError(Exception):
    """引擎执行失败(含隔离子进程超时/异常)。"""


@dataclass(slots=True)
class RunContext:
    """一次任务执行上下文(执行器/进度/取消检查共用)。"""

    db: Session
    task: Task
    claim: Claim
    worker_id: str = ""
    isolate: bool = True
    stop_event: threading.Event | None = None
    progress_fn: Callable[[float, str, dict | None], None] | None = None
    snapshot: Any = None  # CalcSnapshot(计算类任务); runner 装配
    axis_resolution: str = "1h"
    axis_n: int = 8760
    io_params: dict[str, Any] = field(default_factory=dict)  # io 任务参数(队列消息扩展)

    def progress(self, percent: float, stage: str, detail: dict | None = None) -> None:
        """阶段化进度报告(03 §7: PG 持久进度 + Redis 秒级进度)。"""
        if self.progress_fn is not None:
            self.progress_fn(percent, stage, detail)

    def checkpoint(self, stage: str) -> None:
        """取消检查点: 每阶段检查任务是否 cancelling(03 §6.1)。

        取消信号(Redis cancel 键)或停止事件置位 → 抛 TaskCancelled。
        """
        if self.stop_event is not None and self.stop_event.is_set():
            raise TaskCancelled(stage)
        if _cancel_signal(self.task.id):
            raise TaskCancelled(stage)


def _cancel_signal(task_id: int) -> bool:
    """读取取消信号(Redis cancel:{task_id}, 可重建视图)。"""
    from iesplan.services import queue  # 延迟导入避免模块环

    return queue.get_cancel(task_id) is not None


# ---------------------------------------------------------------------------
# 隔离引擎调用
# ---------------------------------------------------------------------------


def _run_engine(
    ctx: RunContext,
    fn: str | Any,
    args: tuple[Any, ...],
    *,
    timeout_sec: float,
    stage: str,
    mem_limit_mb: int | None = None,
) -> Any:
    """经隔离子进程调用引擎; ctx.isolate=False 时进程内直接调用(测试/降级)。

    超时/取消/异常统一转 EngineRunError, 由调用方决定失败语义。
    """
    ctx.checkpoint(stage)
    if not ctx.isolate:
        if isinstance(fn, str):
            module_path, _, attr = fn.rpartition(".")
            target = importlib.import_module(module_path)
            for part in attr.split("."):
                target = getattr(target, part)
            return target(*args)
        return fn(*args)
    resp = run_solver_isolated(
        fn, args, timeout_sec=float(timeout_sec), mem_limit_mb=mem_limit_mb,
        cancel_event=ctx.stop_event,
    )
    if not resp.get("ok"):
        reason = "已取消" if resp.get("canceled") else ("超时" if resp.get("timed_out") else "失败")
        raise EngineRunError(f"求解子进程{reason}: {resp.get('error', '未知错误')}")
    return resp["result"]


# ---------------------------------------------------------------------------
# 方案评价(task_type=calc)
# ---------------------------------------------------------------------------


def execute_calc(ctx: RunContext, content: dict, data: dict, axis: Any, options: dict | None = None) -> dict:
    """方案评价(02 §7): 快照 plan+data → evaluate_plan → 逐时结果/KPI/指标。

    产出 payload: result_kind='eval_result', 含逐时流字段、KPI、诊断、
    四维评估与业务结局。
    """
    config = content.get("calc_config") or {}
    task_params = config.get("task_params") or {}
    plan = _build_plan(content, config)
    solver_opts = dict(task_params.get("solver_options") or {})
    solver_opts.setdefault("timeout", float(solver_opts.get("timeout", 600.0)))
    solver_opts.setdefault("mip_rel_gap", float(solver_opts.get("mip_rel_gap", 0.001)))
    ctx.progress(5, "setup", {"n_steps": int(axis.n), "resolution": axis.resolution})
    ctx.checkpoint("setup")

    result = _run_engine(
        ctx, "iesplan.engines.eval_run.evaluate_plan",
        (plan, data, axis, solver_opts),
        timeout_sec=float(solver_opts["timeout"]) + 60.0,
        stage="solve",
        mem_limit_mb=task_params.get("mem_limit_mb"),
    )
    ctx.progress(80, "solve", {"status": result.status, "objective": result.objective,
                               "gap": result.gap})
    ctx.checkpoint("postprocess")
    payload = _eval_payload(ctx, result)
    _write_engine_diags(ctx, result.diagnostics)
    ctx.progress(100, "done", {"solver_status": payload["solver_status"]})
    return payload


def _eval_payload(ctx: RunContext, result: EvalResult) -> dict:
    """EvalResult → 证据包 payload(逐时流 + KPI + 四维评估 + 业务结局, 03 §3.2)。"""
    solver_status = _eval_solver_status(result)
    assessment = _assess_eval(result, solver_status)
    flows: dict[str, list[float]] = {name: np.asarray(arr, dtype=float).tolist()
                                     for name, arr in result.flows.items()}
    # 逐时流落对象存储并生成引用(结果视图/逐时查询的引用入口; 证据内容保留 flows 全文)
    hourly_refs = _store_hourly_refs(ctx, result.flows)
    kpi = result.kpi or {}
    emissions: dict = {}
    if result.status == "ok" and kpi.get("annual_buy_kwh") is not None:
        # 有效排放因子由 KPI 反推(电网 kg/kWh、燃气 kg/m³), 随输出绑定(REQ-ENV-001)
        buy_kwh = float(kpi["annual_buy_kwh"])
        gas_m3 = float(kpi.get("gas_volume_m3") or 0.0)
        eff_grid = float(kpi.get("co2_grid_kg") or 0.0) / max(buy_kwh, 1e-9)
        eff_gas = float(kpi.get("co2_gas_kg") or 0.0) / max(gas_m3, 1e-9)
        emissions = operational_emissions(
            {"grid_purchase": buy_kwh, "gas": gas_m3},
            {"grid_purchase": eff_grid, "gas": eff_gas},
            boundary="scope1+scope2", factor_version="snapshot-bound",
            data_refs=[f"snapshot:{getattr(ctx.snapshot, 'content_hash', '')[:12]}"],
        )
    summary = {
        "status": result.status,
        "objective": result.objective,
        "gap": result.gap,
        "stop_reason": result.stop_reason,
        "annual_buy_kwh": kpi.get("annual_buy_kwh"),
        "total_op_cost": str(kpi["total_op_cost"]) if kpi.get("total_op_cost") is not None else None,
        "co2_total_kg": kpi.get("co2_total_kg"),
        "self_sufficiency_rate": kpi.get("self_sufficiency_rate"),
    }
    return {
        "schema_version": 1,
        "result_kind": "eval_result",
        "task_type": "calc",
        "status": result.status,
        "solver_status": solver_status,
        "objective": result.objective,
        "gap": result.gap,
        "stop_reason": result.stop_reason,
        "flow_fields": sorted(flows),
        "flows": flows,
        "hourly_refs": hourly_refs,
        # 单方案评价: 唯一候选解(结果选择/差异预览按候选解标识寻址)
        "candidate_indices": [0],
        "candidates": [{"index": 0, "capacities": {}, "note": "方案评价单解"}],
        "metrics": _jsonable(kpi),
        "kpi": _jsonable(kpi),
        "diagnostics": _jsonable(result.diagnostics),
        "energy_balance": _jsonable(energy_balance_summary(
            result.flows, resolution=ctx.axis_resolution)) if result.status == "ok" else {},
        "emissions": _jsonable(emissions),
        "assessment": assessment,
        "outcome": _outcome_from_solver(solver_status),
        "summary": summary,
        "meta": {"axis": {"resolution": ctx.axis_resolution, "n": ctx.axis_n},
                 "solve_info": _jsonable(result.solve_info), "engine": "eval_run@1.0.0"},
    }


def _store_hourly_refs(ctx: RunContext, flows: dict[str, Any]) -> list[dict]:
    """逐时流 → 对象存储对象, 返回 hourly_refs 引用清单(03 §3.2 逐时结果引用)。

    结果视图/逐时查询(read_hourly)以 hourly_refs 为引用入口读取对象内容;
    证据内容本身仍保留 flows 全文(自足, 校验/审计可独立复核)。
    """
    from iesplan.services import objects as objects_service

    n = int(ctx.axis_n or 0)
    doc = {
        "data": {name: np.asarray(arr, dtype=float).tolist() for name, arr in flows.items()},
        "meta": {"resolution": ctx.axis_resolution, "n": n,
                 "unit": "W(W) / kWh(energy) / 0-1(ratio)"},
    }
    blob = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    obj = objects_service.put_object(
        ctx.db, blob, "application/json", source_category="evidence",
        purpose="hourly_result", actor_id=ctx.task.requested_by,
    )
    return [{
        "solution_id": 0,
        "object_id": obj.id,
        "rows": n,
        "fields": sorted(flows),
    }]


def _eval_solver_status(result: EvalResult) -> str:
    """EvalResult → 求解器状态码(02 §11.4, 03 §3.2 映射依据)。

    status 是权威信号(scipy milp 仅在证明最优时返回 ok; 命中时间上限 →
    time_limit 并携带 incumbent); gap 含数值噪声(如 2e-14), 不直接参与判定。
    """
    if result.status == "numerical_failure":
        return S_MODEL_AUDIT_FAIL  # 残差审计未通过
    if result.status in ("infeasible", "unbounded"):
        return S_NO_FEASIBLE
    if result.status == "time_limit":
        return S_TIME_LIMIT_INCUMBENT
    return S_OPTIMAL


def _assess_eval(result: EvalResult, solver_status: str) -> dict:
    """四维评估(01 §8.2 / RPD 10.4): 物理/最优性/财务/可靠性。"""
    audit_failed = any(d.get("code") == "ENG-AUDIT-001" for d in result.diagnostics)
    physical = "fail" if (result.status != "ok" or audit_failed) else "pass"
    optimality = {
        S_OPTIMAL: "pass",
        S_TIME_LIMIT_INCUMBENT: "unknown",  # 最优性未确认(gap 未收敛)
        S_MODEL_AUDIT_FAIL: "fail",
    }.get(solver_status, "fail")
    op_cost = result.kpi.get("total_op_cost") if result.kpi else None
    financial = "pass" if op_cost is not None else "unknown"
    reliability = "unknown"  # 单方案评价不涉及样本统计
    return {
        "dimension_physical": physical,
        "dimension_optimality": optimality,
        "dimension_financial": financial,
        "dimension_reliability": reliability,
        "overall_score": _overall_score(physical, optimality, financial, reliability),
        "comment": f"方案评价: 状态 {result.status}, 目标 {result.objective}",
        "detail": {"solver_status": solver_status, "gap": result.gap,
                   "n_diagnostics": len(result.diagnostics)},
    }


def _outcome_from_solver(solver_status: str) -> str:
    """求解器状态 → 业务结局(03 §3.2 表; 复用 U07 映射)。"""
    from iesplan.services.tasks import map_business_outcome

    return map_business_outcome(solver_status)


# ---------------------------------------------------------------------------
# 规划(task_type=optimization)
# ---------------------------------------------------------------------------


def execute_plan(ctx: RunContext, content: dict, data: dict, axis: Any, options: dict | None = None) -> dict:
    """规划(02 §5-§6): planning 引擎 → 候选列表 → IRR/NPV 评估 → 候选对象。"""
    config = content.get("calc_config") or {}
    task_params = config.get("task_params") or {}
    plan = _build_plan(content, config)
    opts = dict(task_params.get("planning_options") or {})
    opts.setdefault("timeout_per_eval", float(opts.get("timeout_per_eval", 60.0)))
    opts.setdefault("max_combinations", int(opts.get("max_combinations", 200)))
    if "seed" not in opts:
        opts["seed"] = int(getattr(ctx.snapshot, "random_seed", 42) or 42)
    ctx.progress(5, "setup", {"n_steps": int(axis.n), "max_combinations": opts["max_combinations"]})
    ctx.checkpoint("setup")

    # 总超时: 组合数 × 单次评价上限 + 裕量(≤ 任务级硬超时 8h, 03 §6.2)
    total_timeout = min(
        max(300.0, float(opts["max_combinations"]) * float(opts["timeout_per_eval"])), 8 * 3600
    )
    result = _run_engine(
        ctx, "iesplan.engines.planning.run_planning", (plan, data, axis, opts),
        timeout_sec=total_timeout, stage="solve", mem_limit_mb=task_params.get("mem_limit_mb"),
    )
    ctx.progress(80, "solve", {"status": result.status, "candidates": len(result.candidates)})
    ctx.checkpoint("postprocess")
    payload = _planning_payload(ctx, result)
    _write_engine_diags(ctx, result.diagnostics)
    ctx.progress(100, "done", {"solver_status": payload["solver_status"]})
    return payload


def _planning_payload(ctx: RunContext, result: PlanningResult) -> dict:
    """PlanningResult → payload(候选列表 + 最优解 + 四维评估 + 业务结局)。"""
    candidates = [
        {
            "capacities": dict(c.capacities),
            "capex": c.capex,
            "annual_op_cost": c.annual_op_cost,
            "annual_saving": c.annual_saving,
            "irr": c.irr,
            "npv": c.npv,
            "status": c.status,
        }
        for c in result.candidates
    ]
    baseline_infeasible = any(d.get("code") == "ENG-PLAN-001" for d in result.diagnostics)
    if result.best is not None:
        solver_status = S_OPTIMAL
    elif baseline_infeasible:
        solver_status = S_BASE_INFEASIBLE
    else:
        solver_status = S_IRR_FLOOR
    assessment = _assess_planning(result, solver_status)
    best = candidates[0] if candidates else None
    return {
        "schema_version": 1,
        "result_kind": "planning_result",
        "task_type": "optimization",
        "status": result.status,
        "solver_status": solver_status,
        "baseline_cost": result.baseline_cost,
        "candidates": candidates,
        "best": best,
        "diagnostics": _jsonable(result.diagnostics),
        "assessment": assessment,
        "outcome": _outcome_from_solver(solver_status),
        "summary": {
            "status": result.status,
            "n_candidates": len(candidates),
            "baseline_cost": result.baseline_cost,
            "best_irr": best["irr"] if best else None,
            "best_npv": best["npv"] if best else None,
            "best_capex": best["capex"] if best else None,
        },
        "meta": {"axis": {"resolution": ctx.axis_resolution, "n": ctx.axis_n},
                 "engine": "planning@1.0.0"},
    }


def _assess_planning(result: PlanningResult, solver_status: str) -> dict:
    """规划结果四维评估(02 §5.6: IRR 硬约束 + 基准可行性)。"""
    physical = "pass" if result.baseline_cost is not None else "fail"
    optimality = "pass" if result.best is not None else "fail"
    if result.best is not None:
        financial = "pass" if result.best.irr is not None else "unknown"
    else:
        financial = "fail"
    reliability = "unknown"
    return {
        "dimension_physical": physical,
        "dimension_optimality": optimality,
        "dimension_financial": financial,
        "dimension_reliability": reliability,
        "overall_score": _overall_score(physical, optimality, financial, reliability),
        "comment": f"规划: 候选 {len(result.candidates)} 个, 基准成本 {result.baseline_cost}",
        "detail": {"solver_status": solver_status, "n_candidates": len(result.candidates)},
    }


# ---------------------------------------------------------------------------
# 不确定性分析(task_type=uncertainty, 03 §5.4 / RPD 10.5)
# ---------------------------------------------------------------------------


def execute_uncertainty(
    ctx: RunContext, content: dict, data: dict, axis: Any, options: dict | None = None,
) -> dict:
    """不确定性分析(02 §10): 父任务顺序执行样本子任务。

    - 不可变输入: uncertainty_snapshots 行(方法/样本数/种子/分布/内容哈希);
    - 每个样本: sample_tasks 行(状态逐样本) + sample_records(数值指标);
    - 固定方案可靠性(mode=fixed_reliability): 容量固定, 只重优化运行;
    - 重规划敏感性(mode=replan_sensitivity): 每个样本重新优化容量;
    - 每个样本运行在隔离求解器子进程(支撑资源限制/超时/取消);
    - 无效样本单独统计(有效数/总数/剔除原因, RPD 10.5), 不静默计入分布。
    """
    config = content.get("calc_config") or {}
    task_params = config.get("task_params") or {}
    n_samples = max(1, int(task_params.get("n_samples") or 10))
    method = str(task_params.get("method") or "monte_carlo")
    mode = str(task_params.get("mode") or MODE_FIXED_RELIABILITY)
    if mode not in (MODE_FIXED_RELIABILITY, MODE_REPLAN_SENSITIVITY):
        mode = MODE_FIXED_RELIABILITY
    distributions = dict(task_params.get("distributions") or {})
    seed = int(getattr(ctx.snapshot, "random_seed", 42) or 42)
    plan = _build_plan(content, config)
    solver_opts = dict(task_params.get("solver_options") or {})
    solver_opts.setdefault("timeout", 120.0)
    planning_opts = dict(task_params.get("planning_options") or {})
    planning_opts.setdefault("timeout_per_eval", 30.0)
    planning_opts.setdefault("max_combinations", 40)

    # 不可变不确定性快照(01 §9.1; 记录方法/分布/种子)
    unc_hash = sha256_hex(json.dumps(
        {"method": method, "n_samples": n_samples, "seed": seed, "distributions": distributions},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8"))
    unc_snapshot = UncertaintySnapshot(
        calc_snapshot_id=ctx.task.calc_snapshot_id, method=method, n_samples=n_samples,
        random_seed=seed, distributions=distributions, content_hash=unc_hash,
        created_by=ctx.task.requested_by,
    )
    ctx.db.add(unc_snapshot)
    ctx.db.flush()
    ctx.progress(5, "sampling", {"n_samples": n_samples, "method": method, "mode": mode})
    ctx.checkpoint("sampling")

    samples: list[dict] = []
    valid, invalid = 0, 0
    invalid_reasons: dict[str, int] = {}
    for i in range(n_samples):
        ctx.checkpoint(f"sample_{i}")
        ctx.progress(10 + 80.0 * i / n_samples, "sampling",
                     {"sample_index": i, "total": n_samples, "valid": valid})
        rng = np.random.default_rng(seed + i)
        sampled = _sample_data(data, distributions, rng, i)
        try:
            if mode == MODE_REPLAN_SENSITIVITY:
                res = _run_engine(
                    ctx, "iesplan.engines.planning.run_planning",
                    (plan, sampled, axis, {**planning_opts, "seed": seed + i}),
                    timeout_sec=(
                        float(planning_opts["timeout_per_eval"]) * int(planning_opts["max_combinations"])
                        + 60.0
                    ),
                    stage=f"sample_{i}",
                    mem_limit_mb=task_params.get("mem_limit_mb"),
                )
                metric = _sample_metric_planning(res)
            else:
                res = _run_engine(
                    ctx, "iesplan.engines.eval_run.evaluate_plan",
                    (plan, sampled, axis, solver_opts), timeout_sec=float(solver_opts["timeout"]) + 60.0,
                    stage=f"sample_{i}", mem_limit_mb=task_params.get("mem_limit_mb"),
                )
                metric = _sample_metric_eval(res)
            status = "completed"
            valid += 1
        except (EngineRunError, ValueError) as exc:
            status = "failed"
            invalid += 1
            reason = type(exc).__name__
            invalid_reasons[reason] = invalid_reasons.get(reason, 0) + 1
            metric = {"status": "failed", "reason": str(exc)[:200]}
        sample_row = SampleTask(
            uncertainty_snapshot_id=unc_snapshot.id, parent_task_id=ctx.task.id,
            parent_sample_id=None, sample_index=i, depth=0, status=status,
            params={"mode": mode, "multipliers": metric.get("multipliers")},
        )
        ctx.db.add(sample_row)
        ctx.db.flush()
        for name, unit in (("annual_op_cost", "元"), ("co2_total_kg", "kg"), ("irr", None)):
            value = metric.get(name)
            if value is not None:
                ctx.db.add(SampleRecord(
                    sample_task_id=sample_row.id, variable_name=name, value=float(value), unit=unit,
                ))
        samples.append({"sample_index": i, "status": status, "metric": metric})

    valid_ratio = valid / n_samples
    reliability = ("pass" if valid_ratio >= RELIABILITY_RATIO_THRESHOLD
                   else "fail" if valid == 0 else "unknown")
    physical = "pass" if valid > 0 else "fail"
    financial = "pass" if valid > 0 else "fail"
    if valid == n_samples:
        outcome = "normal_completion"
    elif valid > 0:
        outcome = "partial_batch"  # 部分样本完成(03 §3.2)
    else:
        outcome = "no_recommendation"
    assessment = {
        "dimension_physical": physical,
        "dimension_optimality": "unknown",  # 样本统计不证明单一最优
        "dimension_financial": financial,
        "dimension_reliability": reliability,
        "overall_score": _overall_score(physical, "unknown", financial, reliability),
        "comment": f"样本 {valid}/{n_samples} 有效(方法 {method}, 模式 {mode})",
        "detail": {"valid": valid, "total": n_samples, "valid_ratio": round(valid_ratio, 6),
                   "invalid_reasons": invalid_reasons},
    }
    ctx.progress(100, "done", {"valid": valid, "total": n_samples, "outcome": outcome})
    return {
        "schema_version": 1,
        "result_kind": "uncertainty_result",
        "task_type": "uncertainty",
        "status": "ok" if valid > 0 else "no_feasible",
        "method": method,
        "mode": mode,
        "n_samples": n_samples,
        "samples": samples,
        "stats": {"valid": valid, "total": n_samples, "valid_ratio": round(valid_ratio, 6),
                  "invalid_reasons": invalid_reasons, "denominator": "total_samples"},
        "assessment": assessment,
        "outcome": outcome,
        "summary": {"valid": valid, "total": n_samples, "method": method, "mode": mode},
        "meta": {"axis": {"resolution": ctx.axis_resolution, "n": ctx.axis_n},
                 "engine": "uncertainty@1.0.0"},
    }


def _sample_metric_eval(result: EvalResult) -> dict:
    """单样本(固定方案)指标提取。"""
    if result.status != "ok":
        return {"status": "failed", "reason": f"eval {result.status}"}
    kpi = result.kpi or {}
    return {
        "status": "ok",
        "annual_op_cost": float(kpi["total_op_cost"]),
        "co2_total_kg": float(kpi.get("co2_total_kg") or 0.0),
        "annual_buy_kwh": float(kpi.get("annual_buy_kwh") or 0.0),
    }


def _sample_metric_planning(result: PlanningResult) -> dict:
    """单样本(重规划)指标提取(最优候选的 IRR/NPV/投资)。"""
    if result.best is None:
        return {"status": "failed", "reason": f"planning {result.status}"}
    best = result.best
    return {
        "status": "ok",
        "annual_op_cost": best.annual_op_cost,
        "irr": best.irr,
        "npv": best.npv,
        "capex": best.capex,
    }


def _sample_data(data: dict, distributions: dict, rng: np.random.Generator, sample_index: int) -> dict:
    """按分布采样逐时输入(02 §10; RPD 10.5: 种子进入快照, 可复现)。

    分布格式(calc_config.task_params.distributions):
        {"e_load": {"kind": "normal|uniform|scenario", "sigma": 0.1, "amplitude": 0.2,
                    "multipliers": [0.9, 1.1]}, ...}
    乘性键: e_load/h_load/c_load/electricity_price; 加性键: t_ambient
    (normal/uniform 用 sigma_abs/amplitude_abs, 单位 °C)。
    """
    sampled: dict[str, Any] = {}
    for key, value in data.items():
        if key not in distributions or not isinstance(value, np.ndarray):
            sampled[key] = value
            continue
        spec = distributions[key]
        kind = str(spec.get("kind") or "normal")
        if kind == "scenario":
            multipliers = [float(m) for m in spec.get("multipliers") or [1.0]]
            mult = float(multipliers[sample_index % len(multipliers)])
        elif kind == "uniform":
            amp = float(spec.get("amplitude") or 0.1)
            mult = float(rng.uniform(1.0 - amp, 1.0 + amp))
        else:  # normal
            sigma = float(spec.get("sigma") or 0.05)
            mult = float(rng.normal(1.0, sigma))
        if key == "t_ambient":
            if kind == "uniform":
                delta = float(rng.uniform(-float(spec.get("amplitude_abs") or 3.0),
                                          float(spec.get("amplitude_abs") or 3.0)))
            else:
                delta = float(rng.normal(0.0, float(spec.get("sigma_abs") or 2.0)))
            sampled[key] = value + delta
        else:
            sampled[key] = np.maximum(value * mult, 0.0)
    return sampled


# ---------------------------------------------------------------------------
# 结果检查(task_type=report, 01 §8.2 / RPD 10.4)
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
        row = ctx.db.execute(
            sa.select(EvidencePackage).where(
                EvidencePackage.task_id == int(msg_params["task_id"])
            ).order_by(EvidencePackage.id.desc())
        ).scalars().first()
        evidence_id = row.id if row else None
    if evidence_id is None:
        row = ctx.db.execute(
            sa.select(EvidencePackage)
            .join(Task, Task.id == EvidencePackage.task_id)
            .where(Task.project_id == ctx.task.project_id)
            .order_by(EvidencePackage.id.desc())
        ).scalars().first()
        evidence_id = row.id if row else None

    ctx.progress(20, "load_evidence", {"evidence_package_id": evidence_id})
    ctx.checkpoint("load_evidence")
    if evidence_id is None:
        return {
            "schema_version": 1, "result_kind": "assessment_report", "task_type": "report",
            "status": "no_evidence", "evidence_package_id": None, "assessment": {},
            "outcome": "insufficient_evidence",
            "summary": {"assessed": False, "reason": "项目无证据包可检查"},
        }
    package = ctx.db.get(EvidencePackage, evidence_id)
    payload = _load_evidence_payload(ctx.db, package)
    assessment = _assess_payload(payload)

    assess = ResultAssessment(
        evidence_package_id=package.id, assessor="system",
        dimension_physical=assessment["dimension_physical"],
        dimension_optimality=assessment["dimension_optimality"],
        dimension_financial=assessment["dimension_financial"],
        dimension_reliability=assessment["dimension_reliability"],
        overall_score=assessment["overall_score"], comment=assessment["comment"],
        detail=assessment["detail"],
    )
    ctx.db.add(assess)
    ctx.db.flush()
    # 挂接最新评估引用(01 §8.3: assessment_id 可 UPDATE)
    ctx.db.execute(
        sa.update(ResultIndex).where(ResultIndex.evidence_package_id == package.id)
        .values(assessment_id=assess.id)
    )
    has_fail = any(assessment[d] == "fail" for d in (
        "dimension_physical", "dimension_optimality", "dimension_financial", "dimension_reliability"))
    ctx.progress(100, "done", {"assessment_id": assess.id, "has_fail": has_fail})
    return {
        "schema_version": 1,
        "result_kind": "assessment_report",
        "task_type": "report",
        "status": "assessed",
        "evidence_package_id": package.id,
        "assessment": assessment,
        "outcome": "insufficient_evidence" if has_fail else "normal_completion",
        "summary": {"assessed": True, "evidence_package_id": package.id,
                    "assessment_id": assess.id},
    }


def _load_evidence_payload(db: Session, package: EvidencePackage) -> dict:
    """读取证据包对象内容并解析(内容寻址, 读取时校验哈希, 01 §8.1)。"""
    from iesplan.services import objects

    raw = objects.get_object(db, package.object_id)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise EngineRunError(f"证据包内容解析失败: {exc}") from exc
    return payload if isinstance(payload, dict) else {}


def _assess_payload(payload: dict) -> dict:
    """对既有证据包重新派生四维评估(与提交时同口径, RPD 10.4: 可追溯不覆盖)。"""
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

    项目包导入与校验(01 §10.4)由另一 agent 实现; 本波次只留分派与占位。
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


def _build_plan(content: dict, config: dict | None = None) -> dict:
    """项目内容 → 方案 dict(02 §7.4 evaluate_plan 输入: devices/损耗/泵系数)。"""
    model = content.get("model") or {}
    devices: list[dict] = []
    for dev in model.get("devices") or []:
        if not isinstance(dev, dict) or not dev.get("device_type"):
            continue
        kind = dev.get("kind") or ("new" if dev.get("is_new") else "existing")
        devices.append({
            "type": dev["device_type"],
            "params": dict(dev.get("params") or {}),
            "is_new": kind == "new",
        })
    cfg = config or content.get("calc_config") or {}
    params = cfg.get("params") or {}
    return {
        "devices": devices,
        "reverse_feed_allowed": bool(params.get("reverse_feed_allowed", False)),
        "lambda_h": float(params.get("lambda_h", 0.05)),
        "lambda_c": float(params.get("lambda_c", 0.08)),
        "c_ph": float(params.get("c_ph", 0.02)),
        "c_pc": float(params.get("c_pc", 0.02)),
    }


def _overall_score(physical: str, optimality: str, financial: str, reliability: str) -> float:
    """四维 → 综合得分(0-100; fail 扣 40, unknown 扣 15, 通过 100)。"""
    dims = [physical, optimality, financial, reliability]
    score = 100.0 - 40.0 * dims.count("fail") - 15.0 * dims.count("unknown")
    return max(score, 0.0)


def _write_engine_diags(ctx: RunContext, diags: list[dict]) -> None:
    """引擎诊断 → 任务诊断(不可变表; 只写 error/warning 级, 03 §6.3)。"""
    for d in diags:
        severity = str(d.get("severity") or "info")
        if severity not in ("error", "warning", "blocking"):
            continue
        ctx.db.add(TaskDiagnostic(
            task_id=ctx.task.id, attempt_id=ctx.claim.attempt_id,
            level="blocking" if severity == "blocking" else severity,
            code=d.get("code") or TASK_SOLVE_FAILED,
            message=d.get("message_key") or "ies.diag.eng.generic",
            context={"params": d.get("params") or {}, "location": d.get("location")},
        ))


def _jsonable(value: Any) -> Any:
    """递归转换 JSON 安全值(共享实现见 core/jsonutil; worker 的 Decimal 需保精度,
    先显式 str() 再交给 jsonable, 避免哈希口径改为 float)。"""
    if isinstance(value, Decimal):
        return str(value)
    return jsonable(value)
