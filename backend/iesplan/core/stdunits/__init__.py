"""单位标准化内核包(审查意见第 0 条;方案 01,裁决 7.4/7.5 以 01 为准)。

职责:
- 标准单位体系:既有 `core/units.py` 六类 + 新增 mass/volume/voltage/area/
  dimensionless 四类注册表(registry.py,不改写既有文件,词条兼容不冲突);
- 非标准单位字符串解析:parse.py("1000 kW" / "1.5MWh" / "3 元/kWh" / "25℃");
- 转换 API:Quantity / normalize_unit / to_si / from_si / dims_of / unit_meta /
  assert_same_dims / convert(convert.py,支持复合单位与仿射温度);
- 数据字段单位契约:DATA_FIELD_UNITS / hourly_meta(fields.py,裁决 7.5 归 0 层)。

换算唯一入口语义:计算边界输入装配(业务 → SI)与引擎 KPI 输出(SI → 业务)
一律经 to_si / from_si;禁止引擎/执行器内自行 ×1000 或维护换算表。

与既有 core/units.py 兼容:UnitError / UnitSpec / format_value 原样复用,
convert 为超集实现(量纲制,跨类拒绝语义保持)。
"""

from __future__ import annotations

from iesplan.core.units import UnitError, UnitSpec, format_value

from iesplan.core.stdunits.convert import (
    UNIT_META_TABLE,
    Quantity,
    assert_same_dims,
    convert,
    dims_of,
    from_si,
    normalize_unit,
    to_si,
    unit_meta,
)
from iesplan.core.stdunits.fields import (
    DATA_FIELD_UNITS,
    FLOW_UNITS,
    flow_unit_of,
    hourly_meta,
)
from iesplan.core.stdunits.parse import (
    MULTIPLIERS,
    NUMBER_RE,
    UnitParseError,
    parse_number,
    parse_quantity,
    parse_unit_string,
    si_unit_of,
)
from iesplan.core.stdunits.registry import (
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
    DIM_AREA,
    DIM_MASS,
    DIM_VOLTAGE,
    DIM_VOLUME,
    NON_CONVERTIBLE_CURRENCIES,
    SI_BASE_SYMBOL,
    UNIT_REGISTRY_IDS,
    UNITS,
    canonical_of,
    dim_key_of,
    is_known_unit,
    lookup,
)

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
