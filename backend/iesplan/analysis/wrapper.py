"""扫描点结果消费与纯分析聚合(03 §8.2,审查意见第 7 条)。

职责(Wave 4-B: 只消费统一、不可变的计算结果/扫描点结果并做纯分析):
  - 消费调用方(0.8 调度, application/Worker 侧)产出的扫描点结果
    (`SweepResult` / `BatchResult`: param_value + status/kpi/financial/
    solver_status),只做纯分析聚合,不执行计算;
  - `apply_param`: 点路径改写(分析命令的参数应用,纯函数,无 DB);
  - `summarize_sweep` / `summarize_batch`: 汇总表(基准值/变化率/单调性/
    极值点,前端图表数据)。

不负责(预留 application/0.8,不在 analysis 内执行): 构造 solver plan、
调用计算引擎、按扫描值扇出多个计算请求并调度执行、逐点计算财务。
`financial` 块由调用方随扫描点结果提供(analysis 只读其 irr/npv/lcoe/
payback_years 等公开属性,不执行财务计算)。逐时大结果不落盘,只产出
汇总与 financial 块(03 §8.3)。
"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

import numpy as np

from iesplan.core.diagnostics import SEVERITY_ERROR
from iesplan.core.errors import AppError

if TYPE_CHECKING:
    from collections.abc import Any

__all__ = [
    "AnalysisError",
    "BatchResult",
    "SweepResult",
    "SweepSpec",
    "apply_param",
    "change_rate",
    "financial_to_dict",
    "jsonable_kpi",
    "summarize_batch",
    "summarize_sweep",
]

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
    """单点扫描结果(03 §8.2): 调度侧产出的不可变扫描点记录, analysis 只消费。

    属性:
        param_path / param_value / unit: 本次扫描点;
        status: 'ok' | 'infeasible' | 'error'(求解失败/执行异常, 由调度侧判定);
        kpi: 计算 KPI dict(Decimal 金额键保留,落库前经 jsonable_kpi);
        financial: 调用方随点提供的不可变财务块(仅 status='ok' 时存在;
            analysis 只读 irr/npv/lcoe/payback_years 等公开属性,不执行计算);
        solver_status: 原始停止原因(如 'optimal'/'infeasible')。
    """

    param_path: str
    param_value: float
    unit: str
    status: str
    kpi: dict | None = None
    financial: Any | None = None
    solver_status: str = ""


@dataclass(frozen=True, slots=True)
class BatchResult:
    """批量组合结果: 调度侧产出的不可变扫描点记录, analysis 只消费。

    scenario_index: 场景索引;param_values: 参数路径 → 取值(本次组合);
    status / kpi / financial / solver_status: 同 SweepResult。
    """

    scenario_index: int
    param_values: dict[str, float]
    status: str
    kpi: dict | None = None
    financial: Any | None = None
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
# 指标提取与汇总(03 §8.2 summarize_sweep;前端图表数据)
# ---------------------------------------------------------------------------


def _indicators_from(kpi: dict | None, financial: Any | None) -> dict[str, float]:
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


def jsonable_kpi(kpi: dict | None) -> dict | None:
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


def financial_to_dict(fin: Any | None) -> dict | None:
    """财务块 → 可 JSON 落库 dict(evidence financial 块,03 §7.4)。

    输入为调用方随扫描点提供的不可变财务块(公开属性 irr/irr_status/npv/
    capex/baseline_cost/cashflows/lcoe/payback_years/annual_op_cost/
    annual_revenue/detail);analysis 只做结构转换,不执行计算。
    """
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
                "kpi": jsonable_kpi(r.kpi),
                "financial": financial_to_dict(r.financial),
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
