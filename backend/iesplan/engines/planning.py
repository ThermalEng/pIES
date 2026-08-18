"""规划引擎(简版,02 §5.3-§5.6、§6.2)。

策略(02 §5.6 阶段 1 代理目标 + IRR 硬约束,当前单目标 IRR 最大化):
1. 对每个新增设备(plan 中 is_new=True 的设备),容量离散网格枚举
   np.arange(0, max_cap+step, step)(含 0 = 不建设);
2. 容量组合做笛卡尔积;组合数超上限时按项目优先级抽样(可复现种子 42);
3. 对每个组合调用 evaluate_plan 求年运行成本(容量固定,02 §7);
4. 基准方案 = 全部容量为 0 的组合(02 §5.3);基准不可行 → 直接返回 no_feasible;
5. 现金流(02 §5.4 税后口径):CAPEX = Σ c_i·C_i(固定费 F_i 默认 0),
   ATCF 由 metrics.financial.build_project_cashflows 构建;IRR/NPV 同样来自该模块;
6. 硬约束:最低税后项目 IRR(irr_floor,默认 8%,02 附录 B);IRR < 下限者过滤,
   与 02 §6.2"IRR 硬约束不可被任何权重抵消"一致(过滤数量进入 diagnostics);
7. 候选按 IRR 降序排序,best 取首个。

输出 PlanningResult:candidates(list[PlanCandidate])、best、status(ok/no_feasible)、
diagnostics;基准年运行成本 baseline_cost。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from decimal import Decimal

import numpy as np

from iesplan.core.timeaxis import TimeAxis
from iesplan.engines.eval_run import (
    CAPACITY_PARAM,
    MAX_CAPACITY_PARAM,
    _param,
    evaluate_plan,
)
from iesplan.metrics.financial import (
    IRRStatus,
    build_project_cashflows,
    cashflow_irr,
    project_npv,
)

#: 默认最低税后项目 IRR(02 附录 B:最低 IRR ρ_min = 8%)
DEFAULT_IRR_FLOOR = 0.08
#: 默认贴现率(02 附录 B:r = 8%)
DEFAULT_DISCOUNT_RATE = 0.08
#: 默认组合数上限(防组合爆炸,超出按项目优先级抽样)
DEFAULT_MAX_COMBINATIONS = 200
#: 随机种子(02 §9.4 可复现性)
DEFAULT_SEED = 42


def _diag(code: str, severity: str, message_key: str, params: dict | None = None) -> dict:
    """规划引擎诊断 dict(结构同 eval_run,来源 solve.planning)。"""
    return {
        "code": code,
        "severity": severity,
        "blocking": severity == "blocking",
        "message_key": message_key,
        "params": params or {},
        "location": None,
        "source": "solve.planning",
        "ref_ids": [],
    }


@dataclass(slots=True)
class PlanCandidate:
    """规划候选解(02 §5.6/§6.2)。

    属性:
        capacities: 新增设备 type_id → 容量(kWp/kWh/kW,按 04 §3 容量参数)。
        capex: 总投资(元,CAPEX_0,02 §5.4)。
        annual_op_cost: 年运行成本(元,含购电-售电+燃气;None = 该组合不可行)。
        annual_saving: 相对基准的年运行节省(元,None = 不可行)。
        irr: 税后项目 IRR(通过 irr_floor 硬约束者才进入候选列表)。
        npv: 税后 NPV(按贴现率 r,元)。
        status: 'ok' | 'infeasible'(不可行者不进入列表,仅在 diagnostics 计数)。
    """

    capacities: dict[str, float]
    capex: float
    annual_op_cost: float | None
    annual_saving: float | None
    irr: float | None
    npv: float | None
    status: str = "ok"


@dataclass(slots=True)
class PlanningResult:
    """规划结果(02 §5.6/§6.2 单目标 IRR 最大化)。

    属性:
        status: ok(存在通过硬约束的候选)/ no_feasible(无候选)。
        candidates: 按 IRR 降序的候选列表(已过滤 IRR 下限)。
        best: 最优候选(candidates[0]);无候选时为 None。
        baseline_cost: 基准年运行成本(元,02 §5.3);基准不可行为 None。
        diagnostics: 诊断列表(过滤/不可行组合计数、基准不可行等)。
    """

    status: str
    candidates: list[PlanCandidate]
    best: PlanCandidate | None
    diagnostics: list[dict]
    baseline_cost: float | None = None


def _capacity_grid(type_id: str, dev: dict, step: float | None) -> np.ndarray:
    """单个新增设备的容量离散网格(含 0 = 不建设)。"""
    max_cap = _param(dev, MAX_CAPACITY_PARAM[type_id], 0.0)
    if max_cap <= 0:
        return np.array([0.0])
    if step is None or step <= 0:
        step = max_cap / 5.0  # 默认每轴约 6 个点(含 0)
    points = np.arange(0.0, max_cap + step * 0.5, step)
    return np.unique(points)


def _sample_combinations(
    grids: list[np.ndarray],
    weights: list[float],
    max_combinations: int,
    seed: int,
) -> list[tuple[int, ...]]:
    """组合枚举或抽样:总数 ≤ 上限时全枚举,否则按设备权重加权抽样(02 §5.8 规模控制)。"""
    total = int(np.prod([len(g) for g in grids]))
    all_idx = list(itertools.product(*[range(len(g)) for g in grids]))
    if total <= max_combinations:
        return all_idx
    # 组合权重 = 组合内各设备(容量>0)权重之和;全部等权兜底
    w = np.ones(total, dtype=np.float64)
    if weights:
        w = np.array([sum(weights[d] for d, idx in enumerate(idx) if idx > 0) for idx in all_idx])
    w = np.maximum(w, 1e-9)
    w = w / w.sum()
    rng = np.random.default_rng(seed)
    picked = rng.choice(total, size=max_combinations, replace=False, p=w)
    return [all_idx[int(i)] for i in picked]


def run_planning(
    plan: dict,
    data: dict,
    axis: TimeAxis,
    options: dict | None = None,
) -> PlanningResult:
    """规划引擎(简版):新增设备容量离散网格枚举 + IRR 硬约束过滤(02 §5.6)。

    参数:
        plan: 方案模板(同 evaluate_plan);其中 is_new=True 的设备参与容量枚举,
            is_new=False 的存量设备容量固定。设备参数见 04 §3。
        data / axis: 同 evaluate_plan(逐时数据与时间轴)。
        options: dict:
            - "irr_floor": 最低税后项目 IRR(默认 0.08,02 §6.2 硬约束)。
            - "discount_rate": 贴现率 r(默认 0.08,02 §5.2)。
            - "tax_rate": 企业所得税率(默认 0.25,02 §5.4)。
            - "depreciation_years": 折旧年限(默认 10,02 §5.4)。
            - "project_years": 规划期(默认 20,02 附录 B)。
            - "grid_step": {type_id: 步长},缺省 = max_cap/5。
            - "max_combinations": 组合数上限(默认 200,超出按优先级抽样)。
            - "priority": {type_id: 权重},抽样权重(默认等权)。
            - "annual_om_rate": 年运维费率 = OM/CAPEX(默认 0,02 §5.4 OM_y)。
            - "timeout_per_eval": 每次 evaluate_plan 的时间上限(默认 60 s)。
            - "seed": 抽样种子(默认 42)。
    返回:
        PlanningResult。
    """
    opts = options or {}
    irr_floor = float(opts.get("irr_floor", DEFAULT_IRR_FLOOR))
    discount_rate = float(opts.get("discount_rate", DEFAULT_DISCOUNT_RATE))
    tax_rate = float(opts.get("tax_rate", 0.25))
    dep_years = int(opts.get("depreciation_years", 10))
    project_years = int(opts.get("project_years", 20))
    max_combinations = int(opts.get("max_combinations", DEFAULT_MAX_COMBINATIONS))
    annual_om_rate = float(opts.get("annual_om_rate", 0.0))
    timeout_per_eval = float(opts.get("timeout_per_eval", 60.0))
    seed = int(opts.get("seed", DEFAULT_SEED))
    grid_step = opts.get("grid_step") or {}
    priority = opts.get("priority") or {}

    devices = plan.get("devices", [])
    new_devices: list[tuple[str, dict]] = []
    for dev in devices:
        if dev.get("is_new"):
            type_id = dev.get("type", "")
            if type_id in CAPACITY_PARAM:
                new_devices.append((type_id, dev))

    # 基准方案:全部新增容量为 0(02 §5.3)
    baseline_plan = _with_capacities(plan, {})
    baseline_res = evaluate_plan(baseline_plan, data, axis, {"timeout": timeout_per_eval})
    diagnostics: list[dict] = []
    if baseline_res.status != "ok":
        diagnostics.append(
            _diag("ENG-PLAN-001", "error", "ies.diag.eng.plan_baseline_infeasible",
                  {"status": baseline_res.status, "reason": baseline_res.stop_reason})
        )
        return PlanningResult(status="no_feasible", candidates=[], best=None,
                              diagnostics=diagnostics, baseline_cost=None)
    baseline_cost = float(baseline_res.kpi["total_op_cost"])

    if not new_devices:
        # 无新增设备:唯一候选即基准(容量全 0),IRR 退化,直接 no_feasible
        diagnostics.append(
            _diag("ENG-PLAN-002", "warning", "ies.diag.eng.plan_no_new_devices")
        )
        return PlanningResult(status="no_feasible", candidates=[], best=None,
                              diagnostics=diagnostics, baseline_cost=baseline_cost)

    # 容量网格与组合抽样(02 §5.8 规模控制)
    grids = [_capacity_grid(tid, dev, grid_step.get(tid)) for tid, dev in new_devices]
    combos = _sample_combinations(grids, [priority.get(tid, 1.0) for tid, _ in new_devices],
                                  max_combinations, seed)
    if len(combos) < int(np.prod([len(g) for g in grids])):
        diagnostics.append(
            _diag("ENG-PLAN-003", "info", "ies.diag.eng.plan_sampled",
                  {"sampled": len(combos), "total": int(np.prod([len(g) for g in grids]))})
        )

    candidates: list[PlanCandidate] = []
    filtered_by_irr = 0
    infeasible_count = 0

    for idx in combos:
        # 组合 → 各新增设备容量(type_id → 容量;容量 0 表示不建设)
        caps: dict[str, float] = {}
        for j, ((tid, _dev), i) in enumerate(zip(new_devices, idx, strict=True)):
            cap = float(grids[j][i])
            if cap > 0:
                caps[tid] = cap

        if all(v <= 0 for v in caps.values()):
            continue  # 全零组合即基准,跳过
        combo_plan = _with_capacities(plan, caps)
        res = evaluate_plan(combo_plan, data, axis, {"timeout": timeout_per_eval})
        if res.status != "ok":
            infeasible_count += 1
            continue
        op_cost = float(res.kpi["total_op_cost"])
        capex = _compute_capex(new_devices, caps)
        annual_om = annual_om_rate * capex
        saving = baseline_cost - op_cost
        flows = build_project_cashflows(
            Decimal(str(capex)), Decimal(str(annual_om)), Decimal(str(saving)), Decimal("0"),
            Decimal(str(tax_rate)), dep_years, project_years=project_years,
            discount_rate=Decimal(str(discount_rate)),
        )
        irr, irr_status, _msg = cashflow_irr(flows)
        irr_degenerate = irr_status in (IRRStatus.none, IRRStatus.out_of_domain, IRRStatus.degenerate)
        if irr is None or irr_degenerate or irr < irr_floor:
            filtered_by_irr += 1
            continue
        npv = float(project_npv(
            Decimal(str(discount_rate)), investment=Decimal(str(capex)),
            annual_om=Decimal(str(annual_om)), annual_energy_saving=Decimal(str(saving)),
            tax_rate=Decimal(str(tax_rate)), depreciation_years=dep_years, project_years=project_years,
        ))
        candidates.append(PlanCandidate(
            capacities=caps, capex=capex, annual_op_cost=op_cost,
            annual_saving=saving, irr=irr, npv=npv,
        ))

    candidates.sort(key=lambda c: c.irr if c.irr is not None else -1.0, reverse=True)
    best = candidates[0] if candidates else None
    status = "ok" if best else "no_feasible"
    if not candidates:
        diagnostics.append(
            _diag("ENG-PLAN-004", "error", "ies.diag.eng.plan_no_candidate",
                  {"irr_floor": irr_floor, "filtered_by_irr": filtered_by_irr,
                   "infeasible": infeasible_count})
        )
    else:
        diagnostics.append(
            _diag("ENG-PLAN-005", "info", "ies.diag.eng.plan_summary",
                  {"candidates": len(candidates), "filtered_by_irr": filtered_by_irr,
                   "infeasible": infeasible_count, "baseline_cost": baseline_cost})
        )
    return PlanningResult(status=status, candidates=candidates, best=best,
                          diagnostics=diagnostics, baseline_cost=baseline_cost)


def _with_capacities(plan: dict, caps: dict[str, float]) -> dict:
    """复制方案模板,并将新增设备(is_new)的容量参数设为给定容量。

    caps 未列出的新增设备容量置 0(= 基准方案不建设,02 §5.3);存量设备
    (is_new=False)容量保持模板值(容量固定,02 §4.8)。
    """
    devices: list[dict] = []
    for dev in plan.get("devices", []):
        type_id = dev.get("type", "")
        if dev.get("is_new") and type_id in CAPACITY_PARAM:
            devices.append({
                **dev,
                "params": {**dev.get("params", {}), CAPACITY_PARAM[type_id]: caps.get(type_id, 0.0)},
            })
        else:
            devices.append(dev)
    return {**plan, "devices": devices}


def _compute_capex(new_devices: list[tuple[str, dict]], caps: dict[str, float]) -> float:
    """投资成本 CAPEX_0 = Σ c_i·C_i(固定费 F_i 默认 0,02 §4.8/§5.4)。"""
    capex = 0.0
    for tid, dev in new_devices:
        cap = caps.get(tid, 0.0)
        if cap > 0:
            capex += _param(dev, "unit_invest_cost", 0.0) * cap
    return capex
