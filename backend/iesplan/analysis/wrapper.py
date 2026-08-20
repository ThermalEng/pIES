"""计算分析 wrapper:调用计算模块与财务计算模块(03 §8.2,审查意见第 7 条)。

职责:
  - `run_sweep`: 单因子扫描 — 对 `SweepSpec.values` 每个值,`apply_param` 改写
    content(深拷贝)→ 装配为 plan → 引擎(`evaluate_plan`)→ `compute_financials`
    → `SweepResult`。纯函数,无 DB,便于单测;
  - `run_batch` / `summarize_batch`: 批量分析 — 多场景 × 多参数组合笛卡尔积
    (任务范围:批量分析(多场景/多参数组合跑));
  - `apply_param`: 点路径改写(校验参数存在、单位已知、数值有限);
  - `summarize_sweep`: 汇总表(基准值/变化率/单调性/极值点,前端图表数据)。

依赖(单向无环,03 §11): analysis → engines(`evaluate_plan`)/finance
(`compute_financials`;finance 包为里程碑 M5 交付,当前用本包内置最小实现
`_minfinance`,接口与 03 §7.2 一致,落地后切换导入点)/assembly(M4 落地后
plan 装配改经 `assembly.plan.plan_from_content`,当前用镜像 executors._build_plan
的本地适配)。逐时大结果不落盘,只产出 SweepResult + financial 块(03 §8.3)。
"""

from __future__ import annotations

import copy
import itertools
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

import numpy as np

from iesplan.finance import (
    FinanceParams,
    FinancialResult,
    compute_financials,
    finance_params_from_config,
)
from iesplan.core.diagnostics import SEVERITY_ERROR
from iesplan.core.errors import AppError
from iesplan.core.timeaxis import TimeAxis
from iesplan.engines.eval_run import evaluate_plan

if TYPE_CHECKING:
    from collections.abc import Any

__all__ = [
    "AnalysisError",
    "BatchResult",
    "SweepResult",
    "SweepSpec",
    "apply_param",
    "change_rate",
    "run_batch",
    "run_sweep",
    "summarize_batch",
    "summarize_sweep",
]

#: 装配层(M4)落地后启用:plan 装配改经 assembly.plan.plan_from_content
try:  # pragma: no cover - 依赖包未落地时走本地适配
    from iesplan.assembly.plan import plan_from_content as _assembly_plan
except ImportError:
    _assembly_plan = None

#: 设备容量参数候选键(投资估算:capex = Σ unit_invest_cost × 容量,02 §5.3)
_CAPACITY_KEYS: tuple[str, ...] = (
    "rated_capacity_kwp",
    "rated_capacity_kw",
    "capacity_kwh",
    "rated_heat_kw",
    "rated_cooling_kw",
    "rated_power_kw",
    "max_import_power_kw",
)

#: 财务指标展示单位(其余 kpi 键单位 "-")
_FINANCIAL_INDICATOR_UNITS: dict[str, str] = {
    "irr": "-",
    "npv": "CNY",
    "lcoe": "CNY/kWh",
    "payback_years": "a",
}

#: 单调性判定容差(相对最大 |值|)
_TOL = 1e-12


class AnalysisError(AppError):
    """分析模块错误(参数路径/单位/扫描值/任务编排)。"""

    code = "ANA-PARAM-001"
    severity = SEVERITY_ERROR
    message_key = "ies.diag.analysis.param"
    http_status = 400


# ---------------------------------------------------------------------------
# 单位辅助(units.py 扩展 is_known_unit 落地前的本地实现,03 §3.2)
# ---------------------------------------------------------------------------


def _is_known_unit(unit: str) -> bool:
    """unit 是否已注册(别名归一化,大小写不敏感;0 层 core.units,无环)。"""
    from iesplan.core.units import ALIAS_MAP

    return bool(unit) and unit.strip().lower() in ALIAS_MAP


# ---------------------------------------------------------------------------
# 数据结构(03 §8.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SweepSpec:
    """单因子扫描规格(03 §8.2)。

    属性:
        param_path: 点路径,如 'calc_config.params.discount_rate' /
            'device.pv1.params.rated_capacity_kwp' / 'calc_config.irr_floor';
        values: 扫描取值序列(统一归一为 tuple,数值须有限);
        unit: 展示单位(覆盖注册表单位);提供时须为已注册单位。
    """

    param_path: str
    values: tuple[float, ...]
    unit: str | None = None

    def __post_init__(self) -> None:
        vals = tuple(self.values)
        for v in vals:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
                raise AnalysisError(
                    f"扫描值须为有限数值: {v!r}", params={"param_path": self.param_path, "value": repr(v)}
                )
        object.__setattr__(self, "values", vals)


@dataclass(frozen=True, slots=True)
class SweepResult:
    """单点扫描结果(03 §8.2)。

    属性:
        param_path / param_value / unit: 本次扫描点;
        status: 'ok' | 'infeasible' | 'error'(引擎异常/求解失败);
        kpi: 引擎 KPI dict(Decimal 金额键保留,落库前经 _jsonable_kpi);
        financial: FinancialResult | None(仅 status='ok' 时计算);
        solver_status: 引擎原始停止原因(如 'optimal'/'infeasible')。
    """

    param_path: str
    param_value: float
    unit: str
    status: str
    kpi: dict | None = None
    financial: FinancialResult | None = None
    solver_status: str = ""


@dataclass(frozen=True, slots=True)
class BatchResult:
    """批量组合结果(任务范围:多场景/多参数组合跑,一次引擎运行)。

    scenario_index: 场景索引;param_values: 参数路径 → 取值(本次组合);
    status / kpi / financial / solver_status: 同 SweepResult。
    """

    scenario_index: int
    param_values: dict[str, float]
    status: str
    kpi: dict | None = None
    financial: FinancialResult | None = None
    solver_status: str = ""


# ---------------------------------------------------------------------------
# 点路径改写(03 §8.2 apply_param)
# ---------------------------------------------------------------------------


def _split_path(param_path: str) -> list[str]:
    """点路径切分;空/非字符串抛 AnalysisError。"""
    if not isinstance(param_path, str) or not param_path.strip():
        raise AnalysisError(f"param_path 非法: {param_path!r}", params={"param_path": param_path})
    return param_path.split(".")


def _get_at(node: object, parts: Sequence[str]) -> tuple[dict, str]:
    """按点路径取 (父容器, 末键);中间节点缺失抛 AnalysisError。"""
    for part in parts[:-1]:
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            raise AnalysisError(
                f"参数路径不存在: {'.'.join(parts)}", params={"param_path": ".".join(parts)}
            )
    key = parts[-1]
    if not isinstance(node, dict) or key not in node:
        raise AnalysisError(
            f"参数路径不存在: {'.'.join(parts)}", params={"param_path": ".".join(parts)}
        )
    return node, key


def _resolve_named_device_path(content: dict, parts: Sequence[str]) -> list[str]:
    """'device.<实例名>.params.<key>' → 'model.devices.<索引>.params.<key>'。

    实例名匹配 model.devices[].name / id / instance_id(03 §8.2 命名示例
    'device.pv1.params.rated_capacity_kwp')。
    """
    if len(parts) != 4 or parts[2] != "params":
        raise AnalysisError(
            f"设备路径须为 device.<实例名>.params.<key>: {'.'.join(parts)}",
            params={"param_path": ".".join(parts)},
        )
    name = parts[1]
    devices = (content.get("model") or {}).get("devices") or []
    for i, dev in enumerate(devices):
        if isinstance(dev, dict) and (
            dev.get("name") == name or dev.get("id") == name or dev.get("instance_id") == name
        ):
            return ["model", "devices", str(i), "params", parts[3]]
    raise AnalysisError(
        f"未找到设备实例: {name!r}", params={"param_path": ".".join(parts), "instance": name}
    )


def apply_param(content: dict, param_path: str, value: float, unit: str | None = None) -> dict:
    """按点路径改写 content(深拷贝),校验参数存在且单位合法(03 §8.2)。

    路径支持:
      - 'calc_config.params.<key>' / 'calc_config.<key>'(如 irr_floor);
      - 'model.devices.<索引>.<key>'(如 'model.devices.0.params.rated_capacity_kwp');
      - 'device.<实例名>.params.<key>'(实例名匹配 model.devices[].name,03 §8.2 命名);
      - 'data.<key>'(标量数据,如 gas_price)。
    单位: unit 提供时须为已注册单位(core.units,别名归一);数值须有限。
    返回: 改写后的深拷贝(原 content 不变)。
    """
    if not isinstance(content, dict):
        raise AnalysisError("content 须为 dict", params={"param_path": param_path})
    if unit is not None and not _is_known_unit(unit):
        raise AnalysisError(
            f"未注册单位: {unit!r}", params={"param_path": param_path, "unit": unit}
        )
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise AnalysisError(
            f"扫描值须为有限数值: {value!r}", params={"param_path": param_path, "value": repr(value)}
        )
    parts = _split_path(param_path)
    if parts[0] == "device":
        parts = _resolve_named_device_path(content, parts)
    if parts[0] not in ("calc_config", "model", "data"):
        raise AnalysisError(
            f"不支持的点路径前缀: {parts[0]!r}(允许 calc_config/model/data/device)",
            params={"param_path": param_path},
        )
    new = copy.deepcopy(content)
    parent, key = _get_at(new, parts)
    parent[key] = float(value)
    return new


# ---------------------------------------------------------------------------
# content → plan(装配边界;M4 落地后经 assembly.plan.plan_from_content)
# ---------------------------------------------------------------------------


def _local_plan(content: dict) -> dict:
    """content → 方案 dict(镜像 worker.executors._build_plan,02 §7.4 输入结构)。"""
    model = content.get("model") or {}
    devices: list[dict] = []
    for dev in model.get("devices") or []:
        if not isinstance(dev, dict) or not dev.get("device_type"):
            continue
        kind = dev.get("kind") or ("new" if dev.get("is_new") else "existing")
        devices.append(
            {"type": dev["device_type"], "params": dict(dev.get("params") or {}), "is_new": kind == "new"}
        )
    cfg = content.get("calc_config") or {}
    params = cfg.get("params") or {}
    return {
        "devices": devices,
        "reverse_feed_allowed": bool(params.get("reverse_feed_allowed", False)),
        "lambda_h": float(params.get("lambda_h", 0.05)),
        "lambda_c": float(params.get("lambda_c", 0.08)),
        "c_ph": float(params.get("c_ph", 0.02)),
        "c_pc": float(params.get("c_pc", 0.02)),
    }


def _plan_for(content: dict, data: dict, axis: TimeAxis) -> dict:
    """content → evaluate_plan 方案 dict。

    优先经 `assembly.plan.plan_from_content`(M4 装配层,03 §6.2,含业务单位 → SI
    换算);未落地时用本地适配(_local_plan,镜像 executors._build_plan)。
    """
    if _assembly_plan is not None:
        return _assembly_plan(content, data, axis)
    return _local_plan(content)


# ---------------------------------------------------------------------------
# 财务输入提取(capex / baseline_cost)
# ---------------------------------------------------------------------------


def _estimate_capex(plan: dict) -> Decimal:
    """新增设备投资估算:capex = Σ(unit_invest_cost × 容量),02 §5.3 口径。

    存量设备(is_new=False)不计;缺 unit_invest_cost 或容量参数键的设备计 0。
    """
    total = Decimal("0")
    for dev in plan.get("devices") or []:
        if not isinstance(dev, dict) or dev.get("is_new") is not True:
            continue
        params = dev.get("params") or {}
        unit_cost = params.get("unit_invest_cost")
        if unit_cost is None:
            continue
        cap = next((params.get(k) for k in _CAPACITY_KEYS if params.get(k) is not None), None)
        if cap is None:
            continue
        total += Decimal(str(float(unit_cost))) * Decimal(str(float(cap)))
    return total


def _project_financial_inputs(content: dict, plan: dict) -> tuple[Decimal, Decimal | None]:
    """提取财务输入 (capex, baseline_cost)。

    baseline_cost 来源: calc_config.params.baseline_cost 或 content['baseline_cost']
    (元/年);缺失 → None(compute_financials 记 detail 说明)。
    """
    cfg = (content.get("calc_config") or {}).get("params") or {}
    raw = cfg.get("baseline_cost", content.get("baseline_cost"))
    baseline: Decimal | None = None
    if raw is not None:
        baseline = raw if isinstance(raw, Decimal) else Decimal(str(raw))
    return _estimate_capex(plan), baseline


# ---------------------------------------------------------------------------
# 引擎调用(状态映射;异常不中断扫描)
# ---------------------------------------------------------------------------

_INFEASIBLE_STATUSES: frozenset[str] = frozenset({"infeasible", "unbounded"})


def _map_status(raw: str | None) -> str:
    """引擎 status → 扫描结果 status('ok' | 'infeasible' | 'error',03 §8.2)。"""
    if raw == "ok":
        return "ok"
    if raw in _INFEASIBLE_STATUSES:
        return "infeasible"
    return "error"


def _run_engine(
    engine: Callable, plan: dict, data: dict, axis: TimeAxis, options: dict | None
) -> tuple[str, str, dict | None, dict | None]:
    """调用引擎并归一化结果;异常 → ('error', 异常信息, None, None)。"""
    try:
        result = engine(plan, data, axis, options)
    except Exception as exc:  # noqa: BLE001 - 引擎异常记入单点结果,不中断整条扫描
        return "error", f"{type(exc).__name__}: {exc}", None, None
    status = _map_status(getattr(result, "status", None))
    return (
        status,
        getattr(result, "stop_reason", "") or "",
        getattr(result, "kpi", None),
        getattr(result, "flows", None),
    )


# ---------------------------------------------------------------------------
# 单因子扫描(03 §8.2 run_sweep)
# ---------------------------------------------------------------------------


def run_sweep(
    content: dict,
    data: dict,
    axis: TimeAxis,
    spec: SweepSpec,
    base_options: dict | None = None,
    *,
    finance_params: FinanceParams | None = None,
    engine: Callable = evaluate_plan,
) -> list[SweepResult]:
    """单因子扫描(03 §8.2):对 spec.values 每个值,apply_param → 引擎 → 财务。

    参数:
        content: 项目版本内容(calc_config/model.devices,仅读取+深拷贝改写);
        data: 逐时数据(引擎输入,evaluate_plan 语义);
        axis: 时间轴(TimeAxis);
        spec: 扫描规格(参数路径 + 取值序列 + 单位);
        base_options: 计算选项(透传引擎 options,如 {'shedding': True});
        finance_params: 财务参数(缺省取 content.calc_config 推导);
        engine: 计算引擎(默认 evaluate_plan;接口 engine(plan, data, axis, options)
            → 结果对象含 status/kpi/flows)。
    返回: SweepResult 列表(与 spec.values 同序);仅 'ok' 点计算 financial,
    逐时大结果不落盘(03 §8.3)。
    """
    results: list[SweepResult] = []
    for value in spec.values:
        modified = apply_param(content, spec.param_path, value, spec.unit)
        plan = _plan_for(modified, data, axis)
        status, stop_reason, kpi, flows = _run_engine(engine, plan, data, axis, base_options)
        financial: FinancialResult | None = None
        if status == "ok" and isinstance(kpi, dict):
            fp = (
                finance_params
                if finance_params is not None
                else finance_params_from_config(modified.get("calc_config") or {})
            )
            capex, baseline = _project_financial_inputs(modified, plan)
            try:
                financial = compute_financials(kpi, flows or {}, capex, baseline, fp)
            except (ValueError, TypeError):
                # 财务输入不完整(如 kpi 缺费用键且 flows 空): 降级为 None, 不阻断扫描
                financial = None
        results.append(
            SweepResult(
                param_path=spec.param_path,
                param_value=float(value),
                unit=spec.unit or "",
                status=status,
                kpi=kpi if isinstance(kpi, dict) else None,
                financial=financial,
                solver_status=stop_reason,
            )
        )
    return results


# ---------------------------------------------------------------------------
# 批量分析(任务范围:多场景/多参数组合跑)
# ---------------------------------------------------------------------------


def run_batch(
    content: dict,
    data: dict,
    axis: TimeAxis,
    sweeps: Sequence[SweepSpec],
    *,
    scenarios: Sequence[dict] | None = None,
    base_options: dict | None = None,
    finance_params: FinanceParams | None = None,
    engine: Callable = evaluate_plan,
) -> list[BatchResult]:
    """批量分析:场景 × 参数组合笛卡尔积,逐组合跑引擎(结构化输出)。

    每个组合 = 各 sweep 各取一个值同时写入场景 content(apply_param),一次引擎
    运行;输出 BatchResult 列表(param_values 记录组合取值)。场景缺省为单个
    [content];组合数为 Σ场景 × Π各 sweep 取值数。
    """
    if not sweeps:
        raise AnalysisError("sweeps 不能为空", params={"detail": "批量分析至少需要一个扫描参数"})
    scene_list: list[dict] = list(scenarios) if scenarios is not None else [content]
    out: list[BatchResult] = []
    for scene_idx, scene in enumerate(scene_list):
        for combo in itertools.product(*[tuple(s.values) for s in sweeps]):
            modified = scene
            param_values: dict[str, float] = {}
            for spec, value in zip(sweeps, combo, strict=True):
                modified = apply_param(modified, spec.param_path, value, spec.unit)
                param_values[spec.param_path] = float(value)
            plan = _plan_for(modified, data, axis)
            status, stop_reason, kpi, flows = _run_engine(engine, plan, data, axis, base_options)
            financial: FinancialResult | None = None
            if status == "ok" and isinstance(kpi, dict):
                fp = (
                    finance_params
                    if finance_params is not None
                    else finance_params_from_config(modified.get("calc_config") or {})
                )
                capex, baseline = _project_financial_inputs(modified, plan)
                try:
                    financial = compute_financials(kpi, flows or {}, capex, baseline, fp)
                except (ValueError, TypeError):
                    financial = None
            out.append(
                BatchResult(
                    scenario_index=scene_idx,
                    param_values=param_values,
                    status=status,
                    kpi=kpi if isinstance(kpi, dict) else None,
                    financial=financial,
                    solver_status=stop_reason,
                )
            )
    return out


# ---------------------------------------------------------------------------
# 指标提取与汇总(03 §8.2 summarize_sweep;前端图表数据)
# ---------------------------------------------------------------------------


def _indicators_from(kpi: dict | None, financial: FinancialResult | None) -> dict[str, float]:
    """从 (kpi, financial) 提取数值指标: kpi 数值键(含 Decimal)+ financial 关键字段。"""
    from decimal import Decimal as _Decimal

    out: dict[str, float] = {}
    for key, val in (kpi or {}).items():
        if isinstance(val, bool):
            continue
        if isinstance(val, (int, float, _Decimal)):
            out[key] = float(val)
    if financial is not None:
        if financial.irr is not None:
            out["irr"] = float(financial.irr)
        out["npv"] = float(financial.npv)
        if financial.lcoe is not None:
            out["lcoe"] = float(financial.lcoe)
        if financial.payback_years is not None:
            out["payback_years"] = float(financial.payback_years)
    return out


def _result_indicators(result: SweepResult) -> dict[str, float]:
    """SweepResult → 数值指标 dict。"""
    return _indicators_from(result.kpi, result.financial)


def change_rate(base: float, current: float) -> float | None:
    """相对变化率 (current − base) / |base|;base 为 0 返回 None(未定义)。"""
    if base == 0.0:
        return None
    return (current - base) / abs(base)


def _monotonicity(points: Sequence[Mapping[str, Any]]) -> str:
    """逐点差分判定单调性: increasing/decreasing/flat/non_monotonic/insufficient。"""
    if len(points) < 2:
        return "insufficient"
    diffs = [points[i + 1]["value"] - points[i]["value"] for i in range(len(points) - 1)]
    scale = max(1.0, max(abs(p["value"]) for p in points))
    tol = _TOL * scale
    if all(d > tol for d in diffs):
        return "increasing"
    if all(d < -tol for d in diffs):
        return "decreasing"
    if all(abs(d) <= tol for d in diffs):
        return "flat"
    return "non_monotonic"


def _extremum(points: Sequence[Mapping[str, Any]], *, label_key: str = "param_value") -> dict:
    """极值点(按指标值取最大/最小扫描点);label_key 为点标识键。

    sweep 场景点含 param_value(取值);batch 场景点含 scenario_index+param_values。
    """
    if not points:
        return {"max": None, "min": None}
    mx = max(points, key=lambda p: p["value"])
    mn = min(points, key=lambda p: p["value"])
    return {
        "max": {label_key: mx[label_key], "value": mx["value"]},
        "min": {label_key: mn[label_key], "value": mn["value"]},
    }


def _jsonable_kpi(kpi: dict | None) -> dict | None:
    """KPI → 可 JSON 落库(Decimal 金额 → float;shed_events 等列表原样)。"""
    if not isinstance(kpi, dict):
        return kpi
    out: dict[str, Any] = {}
    for key, val in kpi.items():
        if isinstance(val, Decimal):
            out[key] = float(val)
        elif isinstance(val, np.ndarray):
            out[key] = val.tolist()
        else:
            out[key] = val
    return out


def _financial_to_dict(fin: FinancialResult | None) -> dict | None:
    """FinancialResult → 可 JSON 落库 dict(evidence financial 块,03 §7.4)。"""
    if fin is None:
        return None
    return {
        "irr": fin.irr,
        "irr_status": str(fin.irr_status.value) if fin.irr_status is not None else None,
        "npv": float(fin.npv),
        "investment": float(fin.capex),  # 与 §7.4 块键一致(供四维评估消费)
        "baseline_cost": float(fin.baseline_cost),
        "cashflows": [float(c) for c in fin.cashflows],
        "lcoe": float(fin.lcoe) if fin.lcoe is not None else None,
        "payback_years": fin.payback_years,
        "annual_op_cost": float(fin.annual_op_cost),
        "annual_revenue": float(fin.annual_revenue),
        "detail": dict(fin.detail),
    }


def summarize_sweep(results: Sequence[SweepResult]) -> dict:
    """汇总表(03 §8.2):基准值/变化率/单调性/极值点(前端图表数据)。

    基准 = 首个 'ok' 结果(无 ok 结果时取首个结果,指标表为空);指标 = kpi
    数值键 + financial 字段(irr/npv/lcoe/payback_years);change_rate 相对基准;
    单调性按按参数值排序后的逐点差分判定。输出含原始结果行(results)供结构化消费。
    """
    result_list = list(results)
    ok = [r for r in result_list if r.status == "ok"]
    base = ok[0] if ok else (result_list[0] if result_list else None)
    if base is None:
        return {"param_path": "", "unit": "", "base_value": None, "results": [], "indicators": {}}
    base_vals = _result_indicators(base)
    indicators: dict[str, dict] = {}
    for key in base_vals:
        points: list[dict[str, Any]] = []
        for r in ok:
            vals = _result_indicators(r)
            if key not in vals:
                continue
            points.append(
                {
                    "param_value": r.param_value,
                    "value": vals[key],
                    "change_rate": change_rate(base_vals[key], vals[key]),
                }
            )
        points.sort(key=lambda p: p["param_value"])
        indicators[key] = {
            "unit": _FINANCIAL_INDICATOR_UNITS.get(key, "-"),
            "base_value": base_vals[key],
            "points": points,
            "monotonicity": _monotonicity(points),
            "extremum": _extremum(points),
        }
    return {
        "param_path": base.param_path,
        "unit": base.unit,
        "base_value": base.param_value,
        "results": [
            {
                "param_value": r.param_value,
                "status": r.status,
                "kpi": _jsonable_kpi(r.kpi),
                "financial": _financial_to_dict(r.financial),
                "solver_status": r.solver_status,
            }
            for r in result_list
        ],
        "indicators": indicators,
    }


def summarize_batch(results: Sequence[BatchResult]) -> dict:
    """批量结果汇总:行表(scenario_index + 参数取值 + 指标)+ 各指标极值点。

    输出:
        rows: 每行 {scenario_index, param_values, status, indicators, solver_status};
        indicators: 指标 → {unit, points, max, min}(极值点含场景与组合取值);
        scenarios / runs: 场景数与总运行数。
    """
    rows: list[dict[str, Any]] = []
    indicator_units: dict[str, str] = {}
    indicator_points: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        vals = _indicators_from(r.kpi, r.financial)
        rows.append(
            {
                "scenario_index": r.scenario_index,
                "param_values": dict(r.param_values),
                "status": r.status,
                "indicators": vals,
                "solver_status": r.solver_status,
            }
        )
        for key, val in vals.items():
            indicator_units.setdefault(key, _FINANCIAL_INDICATOR_UNITS.get(key, "-"))
            indicator_points.setdefault(key, []).append(
                {
                    "scenario_index": r.scenario_index,
                    "param_values": dict(r.param_values),
                    "value": val,
                }
            )
    indicators: dict[str, dict] = {}
    for key, points in indicator_points.items():
        indicators[key] = {
            "unit": indicator_units[key],
            "points": points,
            **_extremum(points, label_key="scenario_index"),
        }
    return {
        "rows": rows,
        "indicators": indicators,
        "scenarios": len({r.scenario_index for r in results}),
        "runs": len(results),
    }
