"""环境指标模块:运行期温室气体排放核算。

依据 02-calc-model.md §8(逐时结果含 co2_grid/co2_gas/co2_total)与附录 B
(默认排放因子:电网 0.581 kgCO2/kWh、燃气 2.0 kgCO2/m3)。

关键不变量(REQ-ENV-001):排放边界(boundary)与因子版本(factor_version)
必须随输出绑定,保证任何结果都能追溯其口径。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

import numpy as np

# 指标定义版本:与注册表指标条目 ies.metric.operational_emissions 对应
_DEFINITION_VERSION = "1.0.0"
# 金额与排放均需保留的默认小数位数(展示层另行处理)
_TOTAL_DECIMALS = 3


def _energy_value(value: object) -> tuple[float, bool]:
    """把能量流取值转成 (总能量, 是否逐时序列)。

    支持标量(已聚合的年/期能量)或可迭代逐时功率序列(数组求和,单位 kWh 等
    由调用方约定,本函数不换算单位)。
    """
    if isinstance(value, (int, float, Decimal)):
        return float(value), False
    if isinstance(value, np.ndarray) or isinstance(value, (list, tuple, Sequence)):
        arr = np.asarray(value, dtype=np.float64)
        if not np.all(np.isfinite(arr)):
            raise ValueError("能量流序列含非有限值")
        return float(np.sum(arr)), True
    raise TypeError(f"不支持的能量流取值类型:{type(value)!r}")


def operational_emissions(
    energy_flows: Mapping[str, object],
    factors: Mapping[str, object],
    boundary: str,
    factor_version: str,
    data_refs: Sequence[str] | None = None,
) -> dict:
    """计算运行期排放总量与分燃料排放。

    参数:
        energy_flows: 燃料/载体 -> 能量或逐时序列。约定键名与单位:
            grid_purchase (kWh)、gas (m3)、heat (kWh)、cool (kWh) 等;
            逐时序列按步长求和(单位语义由调用方保证一致)。
        factors: 燃料/载体 -> 排放因子(kgCO2 / 对应单位),键须与
            energy_flows 对齐;无因子或未知因子会被排除并记录。
        boundary: 排放边界标识(如 'scope1+scope2'、'full_lifecycle'),
            原样绑定到输出。
        factor_version: 排放因子版本标识(如 '2024-v1.0'),
            原样绑定到输出。
        data_refs: 数据来源引用清单(数据集版本 id、因子源 id 等)。

    返回 dict:
        {
          "total_kg": float,                       # 总排放 kgCO2e
          "by_fuel": {fuel: {"energy": float, "unit": str, "factor_kg_per_unit": float,
                             "emissions_kg": float}},
          "boundary": boundary,
          "factor_version": factor_version,
          "data_refs": [...],
          "missing_factors": [燃料清单],           # 有能量无因子的燃料
          "definition_version": "1.0.0",           # 指标定义版本
        }
    """
    if boundary is None or boundary == "":
        raise ValueError("boundary 不能为空(排放边界必须显式绑定)")
    if factor_version is None or factor_version == "":
        raise ValueError("factor_version 不能为空(因子版本必须显式绑定)")

    by_fuel: dict[str, dict] = {}
    missing: list[str] = []
    total = 0.0
    for fuel, amount in energy_flows.items():
        energy, _is_series = _energy_value(amount)
        if fuel not in factors or factors[fuel] is None:
            if energy != 0.0:
                missing.append(fuel)
            continue
        factor = float(factors[fuel])
        if not np.isfinite(factor):
            raise ValueError(f"燃料 {fuel} 的排放因子非有限值")
        emissions = energy * factor
        total += emissions
        by_fuel[fuel] = {
            "energy": round(energy, _TOTAL_DECIMALS),
            "unit": _default_unit(fuel),
            "factor_kg_per_unit": factor,
            "emissions_kg": round(emissions, _TOTAL_DECIMALS),
        }

    return {
        "total_kg": round(total, _TOTAL_DECIMALS),
        "by_fuel": by_fuel,
        "boundary": boundary,
        "factor_version": factor_version,
        "data_refs": list(data_refs) if data_refs else [],
        "missing_factors": missing,
        "definition_version": _DEFINITION_VERSION,
    }


def _default_unit(fuel: str) -> str:
    """燃料键 -> 约定能量单位(02 §2.2 单位换算表)。"""
    if fuel in {"gas", "natural_gas"}:
        return "m3"
    return "kWh"
