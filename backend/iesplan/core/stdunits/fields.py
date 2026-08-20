"""数据字段单位契约与逐时输出单位表(审查意见第 0 条;裁决 7.5)。

方案 01 §5.2 的 DATA_FIELD_UNITS 契约表与 §5.6 的 hourly_meta 职责按裁决
落在 0 层(worker 与 assembly 共用),替代 worker/executors.py:267 的硬编码
单位串;换算函数唯一入口仍为 core.stdunits.convert。

- DATA_FIELD_UNITS:逐时数据集字段(数据集声明单位 / 引擎输入侧契约),
  与 services/dataset.py STANDARD_FIELDS 键对齐,单位取注册表规范形;
- FLOW_UNITS + hourly_meta:引擎逐时输出 flows 字段 → 逐字段单位契约
  (功率类 W、能量类 J、比例类 "-"),数值保持 SI,展示层按 meta 渲染。
"""

from __future__ import annotations

from typing import Final

#: 数据集/引擎输入字段 → 声明单位(01 §5.2;键与 dataset.STANDARD_FIELDS 一致,
#: 额外提供 01 原文键名的等价映射)
DATA_FIELD_UNITS: Final[dict[str, str]] = {
    "e_load": "kWh",
    "h_load": "kWh",
    "c_load": "kWh",
    "t_ambient": "C",
    "temperature": "C",  # 01 §5.2 原文键(引擎侧温度输入)
    "ghi": "W/m²",
    "electricity_price": "CNY/kWh",
    "tariff_buy": "CNY/kWh",  # 01 §5.2 原文键(购电/售电分时电价)
    "tariff_sell": "CNY/kWh",
    "grid_emission_factor": "kg/kWh",
    "emission_factor_grid": "kg/kWh",  # 01 §5.2 原文键
    "gas_price": "CNY/m³",
}

#: 逐时输出 flows 字段显式单位表(01 §5.6 示例 + eval_run flows 键)
#: 值为 (业务单位, SI 单位);未列入的字段走 _FLOW_UNIT_FALLBACK 规则
FLOW_UNITS: Final[dict[str, tuple[str, str]]] = {
    "e_import": ("W", "W"),
    "e_export": ("W", "W"),
    "e_grid_in": ("W", "W"),
    "e_grid_out": ("W", "W"),
    "e_battery": ("J", "J"),
    "e_bat": ("J", "J"),
    "pv_gen": ("W", "W"),
    "soc": ("-", "1"),
}

#: 逐时输出字段兜底规则(前缀 → (业务单位, SI 单位);0/1 控制量 u_* 为比例)
_FLOW_UNIT_FALLBACK: Final[tuple[tuple[tuple[str, ...], tuple[str, str]], ...]] = (
    (("p_", "h_", "c_"), ("W", "W")),
    (("e_",), ("J", "J")),
    (("u_", "soc"), ("-", "1")),
)


def flow_unit_of(field: str) -> tuple[str, str]:
    """逐时流字段 → (业务单位, SI 单位);显式表优先,兜底规则按前缀匹配。"""
    if field in FLOW_UNITS:
        return FLOW_UNITS[field]
    for prefixes, pair in _FLOW_UNIT_FALLBACK:
        if field.startswith(prefixes):
            return pair
    return ("-", "1")


def hourly_meta(fields: list[str]) -> dict:
    """逐时流字段 → 逐字段单位契约(01 §5.6;替代 executors 硬编码单位串)。

    返回 {"units": {field: {"unit": 业务单位, "si": SI 单位}}};调用方合并
    resolution/n 等外层 meta 字段:

        meta = {"resolution": "1h", "n": 8760, **hourly_meta(fields)}
    """
    return {"units": {f: {"unit": u, "si": s} for f in fields for u, s in (flow_unit_of(f),)}}
