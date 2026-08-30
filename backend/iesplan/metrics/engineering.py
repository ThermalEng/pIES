"""工程指标模块:能量平衡、峰值需量、容量利用率、负荷满足率。

依据 ARCHITECTURE_CONSTITUTION.md §4.5 computation 与 modules/analysis.md（电/热/冷平衡与输配损耗、逐时结果与 KPI）。

所有指标输出均携带 definition_version、unit、refs(REQ-RESULT-002),
保证结果可追溯其定义口径。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

import numpy as np

_DEFINITION_VERSION = "1.0.0"
_REF_BASE = ["ies.metric.energy_balance@1.0.0", "ARCHITECTURE_CONSTITUTION.md#4.5"]

# 年步数表:分辨率 -> 年步数（由 CalculationConfig 分辨率决定）
_RESOLUTION_STEPS = {"15min": 35040, "30min": 17520, "1h": 8760}
# 输配损耗率默认值(02 附录 B:热 0.05、冷 0.08)
_DEFAULT_LOSS = {"heat": 0.05, "cool": 0.08}


def _hours_per_step(resolution: str) -> float:
    """分辨率 -> 每步小时数。"""
    try:
        n = _RESOLUTION_STEPS[resolution]
    except KeyError:
        raise ValueError(f"不支持的分辨率:{resolution!r}(可选 15min/30min/1h)") from None
    return 8760.0 / n


def _series_energy(
    value: object, resolution: str | None = None, default_unit: str = "kWh"
) -> tuple[float, str]:
    """把标量(已聚合能量)或逐时功率序列转成年度能量(单位保持约定)。

    标量直接返回;序列按 (数组和 × 每步小时数) 积分,即假定序列单位为 kW,
    返回单位为 kWh(02 §2.2 功率×时间=能量)。
    """
    if isinstance(value, (int, float, Decimal)):
        return float(value), default_unit
    if isinstance(value, (np.ndarray, list, tuple, Sequence)):
        if resolution is None:
            n = len(value)
            if n in (8760, 17520, 35040):
                resolution = {8760: "1h", 17520: "30min", 35040: "15min"}[n]
            else:
                raise ValueError(
                    f"无法推断序列分辨率(期望长度 8760/17520/35040,实际 {n});请显式传入 resolution"
                )
        arr = np.asarray(value, dtype=np.float64)
        if not np.all(np.isfinite(arr)):
            raise ValueError("逐时序列含非有限值")
        return float(np.sum(arr) * _hours_per_step(resolution)), default_unit
    raise TypeError(f"不支持的能量取值类型:{type(value)!r}")


# ---------------------------------------------------------------------------
# 能量平衡汇总
# ---------------------------------------------------------------------------

# 各能种的供给/需求/损耗键映射(字段名对齐 02 §8.1 逐时输出表)
_BALANCE_SCHEMA: dict[str, dict] = {
    "electric": {
        "production": ["p_pv", "p_grid_buy", "p_bat_dis"],
        "consumption": ["e_load", "p_grid_sell", "p_bat_ch", "p_hp", "p_chl", "p_pump"],
        "loss": [],
    },
    "heat": {
        "production": ["q_b", "q_hp_h"],
        "consumption": ["q_del_h"],
        "loss": ["q_loss_h"],
    },
    "cool": {
        "production": ["q_chl"],
        "consumption": ["q_del_c"],
        "loss": ["q_loss_c"],
    },
}


def energy_balance_summary(
    flows: Mapping[str, object],
    resolution: str | None = None,
    refs: Sequence[str] | None = None,
) -> dict:
    """电/热/冷年度能量平衡表:生产 - 消费 - 损耗 = 残差。

    残差定义(02 §3.8 后验审计):
        电:  残差 = 生产 - 消费(母线无损耗项)
        热:  残差 = Σ供给 - (1 + λ_h) × 需求;损耗 = λ_h × 需求
        冷:  残差 = Σ供给 - (1 + λ_c) × 需求;损耗 = λ_c × 需求
    λ 默认 0.05(热)/0.08(冷),可用 flows['lambda_h'] / flows['lambda_c'] 覆盖。
    守恒数据残差应约等于 0。
    """
    result: dict[str, dict] = {}
    for carrier, schema in _BALANCE_SCHEMA.items():
        production = {k: _series_energy(flows[k], resolution)[0] for k in schema["production"] if k in flows}
        consumption = {
            k: _series_energy(flows[k], resolution)[0] for k in schema["consumption"] if k in flows
        }
        prod_total = sum(production.values())
        cons_total = sum(consumption.values())
        if carrier == "electric":
            loss_total = 0.0
            residual = prod_total - cons_total
        else:
            lam = float(flows.get(f"lambda_{'h' if carrier == 'heat' else 'c'}", _DEFAULT_LOSS[carrier]))
            loss_total = lam * cons_total
            residual = prod_total - (1.0 + lam) * cons_total
        result[carrier] = {
            "production_kwh": {k: round(v, 4) for k, v in production.items()},
            "production_total_kwh": round(prod_total, 4),
            "consumption_kwh": {k: round(v, 4) for k, v in consumption.items()},
            "consumption_total_kwh": round(cons_total, 4),
            "loss_kwh": round(loss_total, 4),
            "residual_kwh": round(residual, 4),
            "unit": "kWh",
            "definition_version": _DEFINITION_VERSION,
            "refs": list(refs) if refs else _REF_BASE,
        }
    return result


# ---------------------------------------------------------------------------
# 峰值需量 / 容量利用率 / 负荷满足率
# ---------------------------------------------------------------------------


def peak_demand(
    series: object,
    resolution: str = "1h",
    refs: Sequence[str] | None = None,
) -> dict:
    """峰值需量统计:返回峰值、出现步索引与均值(02 §8.2 KPI:逐月最大需量等)。"""
    if isinstance(series, (int, float, Decimal)):
        arr = np.asarray([float(series)], dtype=np.float64)
    else:
        arr = np.asarray(series, dtype=np.float64)
    if arr.ndim != 1 or not np.all(np.isfinite(arr)):
        raise ValueError("series 必须是一维有限数值序列")
    if arr.size == 0:
        raise ValueError("series 为空")
    peak_idx = int(np.argmax(arr))
    return {
        "peak_kw": round(float(arr[peak_idx]), 4),
        "peak_index": peak_idx,
        "mean_kw": round(float(np.mean(arr)), 4),
        "n_steps": int(arr.size),
        "resolution": resolution,
        "unit": "kW",
        "definition_version": _DEFINITION_VERSION,
        "refs": list(refs) if refs else _REF_BASE,
    }


def capacity_utilization(
    capacity: float,
    annual_energy: float,
    resolution: str = "1h",
    refs: Sequence[str] | None = None,
) -> dict:
    """容量利用率 = 年能量 / (容量 × 年满负荷小时数)(02 §8.2 并网利用率口径)。

    capacity 单位 kW,annual_energy 单位 kWh,二者量纲须一致(如 kW 与 kWh)。
    """
    if capacity is None or float(capacity) <= 0:
        return {
            "ratio": None,
            "capacity_kw": float(capacity) if capacity is not None else None,
            "note": "容量无效(<=0),利用率未定义",
            "unit": "-",
            "definition_version": _DEFINITION_VERSION,
            "refs": list(refs) if refs else _REF_BASE,
        }
    # 年满负荷小时数 = 年步数 × 每步小时数 = 8760(与分辨率无关)
    annual_hours = 8760.0
    cap_kw = float(capacity)
    energy = float(annual_energy)
    ratio = energy / (cap_kw * annual_hours) if cap_kw * annual_hours > 0 else None
    return {
        "ratio": round(ratio, 6),
        "capacity_kw": cap_kw,
        "annual_energy_kwh": round(energy, 4),
        "annual_hours": annual_hours,
        "unit": "-",
        "definition_version": _DEFINITION_VERSION,
        "refs": list(refs) if refs else _REF_BASE,
    }


def load_met_ratio(
    delivered: object,
    required: object,
    resolution: str | None = None,
    refs: Sequence[str] | None = None,
) -> dict:
    """负荷满足率 = 已供给 / 需求;返回 ratio 与未满足量(02 §3.7 削减语义)。

    delivered/required 可为年度能量标量或逐时序列(序列按 resolution 积分,
    单位约定 kW→kWh);未满足量 = max(required - delivered, 0)。
    """
    deliv, _ = _series_energy(delivered, resolution)
    req, _ = _series_energy(required, resolution)
    if req <= 0:
        return {
            "ratio": None,
            "delivered_kwh": round(deliv, 4),
            "required_kwh": round(req, 4),
            "unmet_kwh": 0.0,
            "note": "需求 <= 0,满足率未定义",
            "unit": "kWh",
            "definition_version": _DEFINITION_VERSION,
            "refs": list(refs) if refs else _REF_BASE,
        }
    unmet = max(req - deliv, 0.0)
    return {
        "ratio": round(deliv / req, 6),
        "delivered_kwh": round(deliv, 4),
        "required_kwh": round(req, 4),
        "unmet_kwh": round(unmet, 4),
        "unit": "kWh",
        "definition_version": _DEFINITION_VERSION,
        "refs": list(refs) if refs else _REF_BASE,
    }
