"""标准单位注册表(审查意见第 0 条,方案 01 §2.2/§2.3)。

以既有 `iesplan.core.units.UNITS`(六类:能量/功率/温度/金额/时长/角度)为基底,
**不改写既有文件**,在本包内合并生成扩展注册表,新增四类:
mass(kg)/volume(m³)/voltage(V)/area(m²)与 dimensionless(无量纲,-/%),
并增补 duration 的 d/月、功率的 kWp/MWp 别名、温度的 °C 小写别名等。

- 规范形(UNITS 的键)即存储/展示单位串;`ALIAS_MAP` 大小写不敏感,冲突时
  "先注册者优先"(沿用既有 units.py 语义,如 度 → kWh 而非 deg);
- `kWp`/`MWp` 只作别名归一为 kW/MW(不注册为独立单位,裁决 7.4 以 01 为准);
- 中文前缀乘数(万元/万m³)不注册为别名,由解析器 `multiplier+symbol` 组合(01 §2.2);
- 本模块只依赖 0 层:iesplan.core.units 与 iesplan.core.errors,无业务依赖。
"""

from __future__ import annotations

from typing import Final

from iesplan.core.units import UNITS as BASE_UNITS
from iesplan.core.units import ALIAS_MAP as BASE_ALIAS_MAP
from iesplan.core.units import UnitError, UnitSpec, _u

# ---------------------------------------------------------------------------
# 类别常量(既有六类 + 新增四类)
# ---------------------------------------------------------------------------
CATEGORY_ENERGY = "energy"
CATEGORY_POWER = "power"
CATEGORY_TEMPERATURE = "temperature"
CATEGORY_CURRENCY = "currency"
CATEGORY_DURATION = "duration"
CATEGORY_ANGLE = "angle"
CATEGORY_MASS = "mass"
CATEGORY_VOLUME = "volume"
CATEGORY_VOLTAGE = "voltage"
CATEGORY_AREA = "area"
CATEGORY_DIMENSIONLESS = "dimensionless"

CATEGORIES: Final[tuple[str, ...]] = (
    CATEGORY_ENERGY,
    CATEGORY_POWER,
    CATEGORY_TEMPERATURE,
    CATEGORY_CURRENCY,
    CATEGORY_DURATION,
    CATEGORY_ANGLE,
    CATEGORY_MASS,
    CATEGORY_VOLUME,
    CATEGORY_VOLTAGE,
    CATEGORY_AREA,
    CATEGORY_DIMENSIONLESS,
)

#: 量纲键(01 §2.1;duration 的量纲键为 time,与 core.expression.DIM_TIME 一致;
#: 设计要求 DIM_MASS/DIM_VOLUME 增补进 core/expression.py,因不修改既有文件,
#: 在本包定义同名常量,取值与表达式的量纲 Counter 键约定一致。)
DIM_MASS: Final[str] = "mass"
DIM_VOLUME: Final[str] = "volume"
DIM_VOLTAGE: Final[str] = "voltage"
DIM_AREA: Final[str] = "area"

#: 量纲键在类别制中的命名(替换既有 units.py 的类别制精确查表,01 §4.2 dims_of)
_DIM_KEY_BY_CATEGORY: Final[dict[str, str]] = {
    CATEGORY_ENERGY: "energy",
    CATEGORY_POWER: "power",
    CATEGORY_TEMPERATURE: "temperature",
    CATEGORY_CURRENCY: "currency",
    CATEGORY_DURATION: "time",  # 01 §2.3 规范形表:{time:1}
    CATEGORY_ANGLE: "angle",
    CATEGORY_MASS: DIM_MASS,
    CATEGORY_VOLUME: DIM_VOLUME,
    CATEGORY_VOLTAGE: DIM_VOLTAGE,
    CATEGORY_AREA: DIM_AREA,
    CATEGORY_DIMENSIONLESS: None,  # 无量纲: 不产生量纲键
}

#: 中文乘数前缀(01 §3.1 MULT;解析器与数值后缀共用)
MULTIPLIERS: Final[dict[str, float]] = {"百": 1e2, "千": 1e3, "万": 1e4, "亿": 1e8}

#: 仿射单位(含加法偏移;只允许独立出现,禁止进入复合/分母,01 §3.1 约束 3)
AFFINE_UNITS: Final[frozenset[str]] = frozenset({"C", "F"})

#: 汇率为非固定换算、禁止自动折算的币种(01 §9.4;to_si/from_si/convert 抛 UnitError)
NON_CONVERTIBLE_CURRENCIES: Final[frozenset[str]] = frozenset({"USD"})

#: 各类别对应的 SI 基准单位符号(Quantity.si_unit 展示用)
SI_BASE_SYMBOL: Final[dict[str, str]] = {
    CATEGORY_ENERGY: "J",
    CATEGORY_POWER: "W",
    CATEGORY_TEMPERATURE: "K",
    CATEGORY_CURRENCY: "CNY",
    CATEGORY_DURATION: "s",
    CATEGORY_ANGLE: "rad",
    CATEGORY_MASS: "kg",
    CATEGORY_VOLUME: "m³",
    CATEGORY_VOLTAGE: "V",
    CATEGORY_AREA: "m²",
    CATEGORY_DIMENSIONLESS: "1",
}


def dim_key_of(category: str) -> str | None:
    """类别 → 量纲键(无量纲返回 None,量纲键不存在时自然无量纲,01 §2.1)。"""
    return _DIM_KEY_BY_CATEGORY.get(category)


# ---------------------------------------------------------------------------
# 扩展注册表(既有六类原样引用 + 01 §2.2 增补;新增词条用 setdefault 不抢占既有)
# ---------------------------------------------------------------------------

#: 新增单位(01 §2.2 逐条;id 遵循 ies.unit.* 命名)
_ADDITIONS: dict[str, UnitSpec] = {
    # 质量(基准 kg)
    "kg": _u("ies.unit.kg", CATEGORY_MASS, "千克", "kg", 1.0, aliases=("kg", "公斤", "千克")),
    "t": _u("ies.unit.t", CATEGORY_MASS, "吨", "t", 1e3, aliases=("t", "吨")),
    "tCO2": _u(
        "ies.unit.tco2",
        CATEGORY_MASS,
        "吨二氧化碳",
        "tCO2",
        1e3,
        aliases=("tco2", "吨二氧化碳", "tCO₂", "tCO2e"),
    ),
    # 体积(基准 m³)
    "m³": _u("ies.unit.m3", CATEGORY_VOLUME, "立方米", "m³", 1.0, aliases=("m3", "立方米", "方")),
    "千m³": _u("ies.unit.km3", CATEGORY_VOLUME, "千立方米", "千m³", 1e3, aliases=("千立方米",)),
    # 电压(基准 V)
    "V": _u("ies.unit.v", CATEGORY_VOLTAGE, "伏", "V", 1.0, aliases=("v", "伏")),
    "kV": _u("ies.unit.kv", CATEGORY_VOLTAGE, "千伏", "kV", 1e3, aliases=("kv", "千伏")),
    "MV": _u("ies.unit.mv", CATEGORY_VOLTAGE, "兆伏", "MV", 1e6, aliases=("mv",)),
    # 面积(基准 m²)
    "m²": _u("ies.unit.m2", CATEGORY_AREA, "平方米", "m²", 1.0, aliases=("m2", "平方米", "平米")),
    # 无量纲(基准 1)
    "-": _u(
        "ies.unit.dimless",
        CATEGORY_DIMENSIONLESS,
        "无量纲",
        "-",
        1.0,
        aliases=("-", "无量纲", "dimensionless", "1"),
    ),
    "%": _u("ies.unit.pct", CATEGORY_DIMENSIONLESS, "百分比", "%", 0.01, aliases=("%", "百分比", "percent", "pct")),
    # 时长增补
    "d": _u("ies.unit.d", CATEGORY_DURATION, "天", "d", 86400.0, aliases=("d", "天", "日", "day"), precision=0),
    "月": _u(
        "ies.unit.month",
        CATEGORY_DURATION,
        "月",
        "月",
        2_592_000.0,
        aliases=("月", "month", "mo"),
        precision=0,
    ),
}

#: 既有单位别名增补(01 §2.2:峰值容量语义 + 温度小写 + 中文全名解析友好)
_ALIAS_EXTRA: dict[str, tuple[str, ...]] = {
    "kW": ("kwp", "kWp", "千瓦"),
    "MW": ("mwp", "MWp", "兆瓦"),
    "C": ("°c",),
    "W": ("瓦",),
    "GW": ("吉瓦",),
    "J": ("焦耳",),
    "kJ": ("千焦",),
    "MJ": ("兆焦",),
    "GJ": ("吉焦",),
    "K": ("开尔文",),
    "F": ("华氏度",),
    "s": ("秒",),
    "min": ("分",),
    "h": ("小时",),
    "rad": ("弧度",),
}

#: 规范形 → 单位规格(既有词条 + 增补;增补的别名型单位以新规格并入)
UNITS: dict[str, UnitSpec] = dict(BASE_UNITS)
for _uid, _spec in _ADDITIONS.items():
    UNITS[_uid] = _spec
# 增补别名的单位重建新规格(既有对象不可变,不改写既有 UNITS)
for _uid, _extra in _ALIAS_EXTRA.items():
    _base = BASE_UNITS[_uid]
    UNITS[_uid] = _u(
        _base.id,
        _base.category,
        _base.symbol_zh,
        _base.symbol_en,
        _base.to_si,
        _base.offset,
        _base.format_zh,
        _base.format_en,
        aliases=_base.aliases + _extra,
        precision=_base.precision_digits,
    )

#: 别名 → 规范形(大小写不敏感;既有别名优先,新别名 setdefault 追加)
ALIAS_MAP: dict[str, str] = dict(BASE_ALIAS_MAP)
for _uid, _spec in UNITS.items():
    ALIAS_MAP.setdefault(_uid.lower(), _uid)
    for _alias in _spec.aliases:
        ALIAS_MAP.setdefault(_alias.lower(), _uid)

#: 单位 id → 注册 id(如 "kWh" → "ies.unit.kwh")
UNIT_REGISTRY_IDS: dict[str, str] = {_uid: _spec.id for _uid, _spec in UNITS.items()}


def lookup(unit: str) -> UnitSpec:
    """按单位名或别名查注册表(大小写不敏感,仅限单一单位)。

    异常:
        UnitError: 单位未注册(含建议信息)。
    """
    uid = ALIAS_MAP.get(unit.strip().lower())
    if uid is None:
        raise UnitError(f"未注册的单位: {unit!r}", params={"unit": unit, "expected": "已注册单位"})
    return UNITS[uid]


def canonical_of(unit: str) -> str:
    """单一单位 → 规范形(如 kWp → kW、元 → CNY、℃ → C)。

    异常:
        UnitError: 未注册。
    """
    uid = ALIAS_MAP.get(unit.strip().lower())
    if uid is None:
        raise UnitError(f"未注册的单位: {unit!r}", params={"unit": unit, "expected": "已注册单位"})
    return uid


def is_known_unit(unit: str) -> bool:
    """单位串是否可被识别(单一单位或复合单位,含非规范形态;裁决 7.4 辅助函数)。"""
    from iesplan.core.stdunits.convert import normalize_unit

    try:
        normalize_unit(unit)
        return True
    except UnitError:
        return False
