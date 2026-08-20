"""单位标准化兼容垫片(审查意见第 0 条;01 方案定案 §4.1)。

实现已按定案合并入 `iesplan.core.units` 与 `iesplan.core.unitparse`:
- 注册表/换算/量纲/字段契约 → core/units.py;
- 非标准单位字符串解析 → core/unitparse.py。

本模块仅保留符号 re-export,保证既有调用点(建模 functions._to_si_param、
前端镜像导出、测试)无需改动;新代码一律从 core.units / core.unitparse 导入。
"""

from __future__ import annotations

import importlib

from iesplan.core.units import (  # noqa: F401
    AFFINE_UNITS,
    ALIAS_MAP,
    CATEGORIES,
    CATEGORY_ANGLE,
    CATEGORY_AREA,
    CATEGORY_CURRENCY,
    CATEGORY_DIMENSIONLESS,
    CATEGORY_DURATION,
    CATEGORY_ENERGY,
    CATEGORY_MASS,
    CATEGORY_POWER,
    CATEGORY_TEMPERATURE,
    CATEGORY_VOLTAGE,
    CATEGORY_VOLUME,
    DATA_FIELD_UNITS,
    DIM_AREA,
    DIM_MASS,
    DIM_VOLTAGE,
    DIM_VOLUME,
    FLOW_UNITS,
    MULTIPLIERS,
    NON_CONVERTIBLE_CURRENCIES,
    SI_BASE_SYMBOL,
    UNIT_REGISTRY_IDS,
    UNITS,
    UnitError,
    UnitSpec,
    assert_same_dims,
    canonical_of,
    convert,
    dim_key_of,
    dims_of,
    flow_unit_of,
    format_value,
    from_si,
    hourly_meta,
    is_known_unit,
    lookup,
    normalize_unit,
    to_si,
    unit_meta,
)
from iesplan.core.unitparse import (  # noqa: F401
    NUMBER_RE,
    UnitParseError,
    decompose,
    parse_number,
    parse_quantity,
    parse_unit_string,
    si_unit_of,
)
from iesplan.core.units import Quantity  # noqa: F401


def __getattr__(name: str):
    """动态转发 UNIT_META_TABLE(codex 二次审核 Low-1)。

    静态 ``from ... import UNIT_META_TABLE`` 会复制构建前的 None;
    源模块首次访问后缓存, shim 中的引用不会同步。经 ``__getattr__``
    动态转发保证始终取源模块当前值。
    """
    if name == "UNIT_META_TABLE":
        return getattr(importlib.import_module("iesplan.core.units"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    # 注册表与常量
    "UNITS",
    "ALIAS_MAP",
    "UNIT_REGISTRY_IDS",
    "CATEGORIES",
    "CATEGORY_ENERGY",
    "CATEGORY_POWER",
    "CATEGORY_TEMPERATURE",
    "CATEGORY_CURRENCY",
    "CATEGORY_DURATION",
    "CATEGORY_ANGLE",
    "CATEGORY_MASS",
    "CATEGORY_VOLUME",
    "CATEGORY_VOLTAGE",
    "CATEGORY_AREA",
    "CATEGORY_DIMENSIONLESS",
    "DIM_MASS",
    "DIM_VOLUME",
    "DIM_VOLTAGE",
    "DIM_AREA",
    "AFFINE_UNITS",
    "NON_CONVERTIBLE_CURRENCIES",
    "SI_BASE_SYMBOL",
    "MULTIPLIERS",
    "UnitSpec",
    # 解析
    "NUMBER_RE",
    "UnitParseError",
    "parse_number",
    "parse_unit_string",
    "si_unit_of",
    "parse_quantity",
    # 转换 API
    "Quantity",
    "normalize_unit",
    "to_si",
    "from_si",
    "dims_of",
    "unit_meta",
    "assert_same_dims",
    "convert",
    "is_known_unit",
    "canonical_of",
    "lookup",
    "dim_key_of",
    "UNIT_META_TABLE",
    # 字段契约
    "DATA_FIELD_UNITS",
    "FLOW_UNITS",
    "flow_unit_of",
    "hourly_meta",
    # 既有兼容
    "UnitError",
    "format_value",
]
