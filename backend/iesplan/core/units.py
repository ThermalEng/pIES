"""单位注册表与换算(依据 02 §2、04 §8)。

内部基准单位(SI + 货币):能量 J、功率 W、温度 K、金额 CNY、时长 s、角度 rad。
- convert:同类单位间换算(仅限同类别,跨类拒绝)。
- energy_to_joules / power_to_watts / temperature_kelvin:接口→内部基准。
- format_value:中英展示格式(不用于计算)。
- 温度:摄氏度是仿射单位(含 273.15 偏置),只允许与温度/温差做加减(04 §8.3 规则 4)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from iesplan.core.errors import AppError

# ---------------------------------------------------------------------------
# 单位类别
# ---------------------------------------------------------------------------
CATEGORY_ENERGY = "energy"
CATEGORY_POWER = "power"
CATEGORY_TEMPERATURE = "temperature"
CATEGORY_CURRENCY = "currency"
CATEGORY_DURATION = "duration"
CATEGORY_ANGLE = "angle"

CATEGORIES: Final[tuple[str, ...]] = (
    CATEGORY_ENERGY,
    CATEGORY_POWER,
    CATEGORY_TEMPERATURE,
    CATEGORY_CURRENCY,
    CATEGORY_DURATION,
    CATEGORY_ANGLE,
)

# 温度转换常量(02 §2.2)
KELVIN_OFFSET_C = 273.15  # T[K] = θ[°C] + 273.15


@dataclass(frozen=True, slots=True)
class UnitSpec:
    """单位规格(04 §8.2 定义的子集)。

    属性:
        id: 注册单位 id(如 ies.unit.kwh)。
        category: 单位类别(energy/power/temperature/currency/duration/angle)。
        symbol_zh / symbol_en: 中英文符号(如 "千瓦时"/"kWh")。
        to_si: 换算到 SI 基准的系数(factor);仿射单位另有 offset。
        offset: 加法偏移(仅温度类用,单位 °C → K:offset=273.15)。
        format_zh / format_en: 展示格式模板(str.format 风格,{0:g} 表示去尾零)。
        aliases: 别名(解析输入时归一化用,04 §8.2)。
        precision_digits: 默认显示小数位数。
    """

    id: str
    category: str
    symbol_zh: str
    symbol_en: str
    to_si: float = 1.0
    offset: float = 0.0
    format_zh: str = "{0:g} "
    format_en: str = "{0:g} "
    aliases: tuple[str, ...] = ()
    precision_digits: int = 2


def _u(
    uid: str,
    category: str,
    sym_zh: str,
    sym_en: str,
    to_si: float = 1.0,
    offset: float = 0.0,
    fmt_zh: str | None = None,
    fmt_en: str | None = None,
    aliases: tuple[str, ...] = (),
    precision: int = 2,
) -> UnitSpec:
    """构造单位规格,格式模板缺省为 "{0:g} <符号>"。"""
    return UnitSpec(
        id=uid,
        category=category,
        symbol_zh=sym_zh,
        symbol_en=sym_en,
        to_si=to_si,
        offset=offset,
        format_zh=fmt_zh or f"{{0:g}} {sym_zh}",
        format_en=fmt_en or f"{{0:g}} {sym_en}",
        aliases=aliases,
        precision_digits=precision,
    )


# ---------------------------------------------------------------------------
# 单位注册表(内置六类,04 §8.1 全部示例单位 + 复合单位)
# ---------------------------------------------------------------------------
UNITS: dict[str, UnitSpec] = {
    # 能量(基准 J):04 §8.1 示例
    "J": _u("ies.unit.j", CATEGORY_ENERGY, "焦耳", "J", 1.0, aliases=("j", "J")),
    "kJ": _u("ies.unit.kj", CATEGORY_ENERGY, "千焦", "kJ", 1e3, aliases=("kj",)),
    "MJ": _u("ies.unit.mj", CATEGORY_ENERGY, "兆焦", "MJ", 1e6, aliases=("mj",)),
    "GJ": _u("ies.unit.gj", CATEGORY_ENERGY, "吉焦", "GJ", 1e9, aliases=("gj",)),
    "kWh": _u("ies.unit.kwh", CATEGORY_ENERGY, "千瓦时", "kWh", 3.6e6, aliases=("kwh", "度", "千瓦时")),
    "MWh": _u("ies.unit.mwh", CATEGORY_ENERGY, "兆瓦时", "MWh", 3.6e9, aliases=("mwh",)),
    "GWh": _u("ies.unit.gwh", CATEGORY_ENERGY, "吉瓦时", "GWh", 3.6e12, aliases=("gwh",)),
    "kcal": _u("ies.unit.kcal", CATEGORY_ENERGY, "千卡", "kcal", 4186.8, aliases=("kcal",)),
    # 功率(基准 W):04 §8.1 示例
    "W": _u("ies.unit.w", CATEGORY_POWER, "瓦", "W", 1.0, aliases=("w",)),
    "kW": _u("ies.unit.kw", CATEGORY_POWER, "千瓦", "kW", 1e3, aliases=("kw",), precision=1),
    "MW": _u("ies.unit.mw", CATEGORY_POWER, "兆瓦", "MW", 1e6, aliases=("mw",)),
    "GW": _u("ies.unit.gw", CATEGORY_POWER, "吉瓦", "GW", 1e9, aliases=("gw",)),
    # 温度(基准 K):04 §8.1 示例(含仿射偏移)
    "K": _u("ies.unit.k", CATEGORY_TEMPERATURE, "开尔文", "K", 1.0, aliases=("k",)),
    "C": _u(
        "ies.unit.c", CATEGORY_TEMPERATURE, "摄氏度", "°C", 1.0, KELVIN_OFFSET_C, aliases=("c", "℃", "摄氏度")
    ),
    "F": _u(
        "ies.unit.f", CATEGORY_TEMPERATURE, "华氏度", "°F", 5.0 / 9.0, 459.67 * 5.0 / 9.0, aliases=("f",)
    ),
    # 金额(基准 CNY):04 §8.1 示例(汇率非固定换算,USD 不自动换算)
    "CNY": _u("ies.unit.cny", CATEGORY_CURRENCY, "元", "CNY", 1.0, aliases=("元", "人民币"), precision=2),
    "USD": _u("ies.unit.usd", CATEGORY_CURRENCY, "美元", "USD", 1.0, aliases=("美元",)),
    # 时长(基准 s):04 §8.1 示例
    "s": _u("ies.unit.s", CATEGORY_DURATION, "秒", "s", 1.0, aliases=("s", "sec")),
    "min": _u("ies.unit.min", CATEGORY_DURATION, "分", "min", 60.0, aliases=("分钟", "minute")),
    "h": _u("ies.unit.h", CATEGORY_DURATION, "小时", "h", 3600.0, aliases=("时", "hour", "hr")),
    "a": _u(
        "ies.unit.a",
        CATEGORY_DURATION,
        "年",
        "a",
        8760.0 * 3600.0,
        aliases=("年", "yr", "year"),
        precision=0,
    ),
    # 角度(基准 rad):04 §8.1 示例
    "rad": _u("ies.unit.rad", CATEGORY_ANGLE, "弧度", "rad", 1.0, aliases=("rad",)),
    "deg": _u(
        "ies.unit.deg",
        CATEGORY_ANGLE,
        "度",
        "deg",
        3.141592653589793 / 180.0,
        aliases=("°", "度", "degree"),
        precision=1,
    ),
}

# 别名 → 标准单位 id(解析用户输入时归一化,04 §8.2;大小写不敏感)
# 冲突时"先注册者优先"(如 "度" 同时是 kWh 千瓦时 与 deg 角度 的别名,能量语义优先)。
ALIAS_MAP: dict[str, str] = {}
for _uid, _spec in UNITS.items():
    ALIAS_MAP.setdefault(_uid.lower(), _uid)
    for _alias in _spec.aliases:
        ALIAS_MAP.setdefault(_alias.lower(), _uid)

# 单位 id → 注册 id(如 "kWh" → "ies.unit.kwh")
UNIT_REGISTRY_IDS: dict[str, str] = {_uid: _spec.id for _uid, _spec in UNITS.items()}


class UnitError(AppError):
    """单位换算/解析错误。"""

    code = "PARAM-UNIT-002"
    message_key = "ies.diag.param.unit_mismatch"


def _lookup(unit: str) -> UnitSpec:
    """按单位名或别名查注册表(大小写不敏感)。

    异常:
        UnitError: 单位未注册(含建议信息)。
    """
    key = unit.strip().lower()
    uid = ALIAS_MAP.get(key)
    if uid is None:
        raise UnitError(f"未注册的单位: {unit!r}", params={"unit": unit, "expected": "已注册单位"})
    return UNITS[uid]


def convert(value: float, from_unit: str, to_unit: str) -> float:
    """在同类单位间换算(04 §8.3 规则 6:跨类换算一律拒绝)。

    参数:
        value: 数值(内部一律 float;展示层再用 Decimal 重算金额)。
        from_unit: 源单位(名或别名,如 "kWh"/"度")。
        to_unit: 目标单位。
    返回:
        换算后的数值。
    异常:
        UnitError: 单位未注册或类别不同。
    """
    frm = _lookup(from_unit)
    to = _lookup(to_unit)
    if frm.category != to.category:
        raise UnitError(
            f"跨类别单位换算被拒绝: {from_unit}({frm.category}) → {to_unit}({to.category})",
            params={
                "param": from_unit,
                "expected": frm.category,
                "actual": to.category,
            },
        )
    # 统一到 SI 基准再转目标单位;仿射单位(温度)仅允许恒等类别间的加减换算
    base = value * frm.to_si + frm.offset
    return (base - to.offset) / to.to_si


def energy_to_joules(value: float, unit: str) -> float:
    """能量换算到内部基准 J(02 §2.2:kWh/MWh/GJ/J)。"""
    return convert(value, unit, "J")


def power_to_watts(value: float, unit: str) -> float:
    """功率换算到内部基准 W(02 §2.2:kW/MW/W)。"""
    return convert(value, unit, "W")


def temperature_kelvin(value: float, unit: str) -> float:
    """温度换算到内部基准 K(02 §2.2:°C → K 加 273.15;K 直通)。"""
    return convert(value, unit, "K")


def format_value(value, unit: str, lang: str = "zh") -> str:
    """按单位展示格式格式化数值(仅用于展示,不用于计算)。

    参数:
        value: 数值(int/float/Decimal/str 均可)。
        unit: 单位名或别名(如 "kWh"/"MW"/"C")。
        lang: 语言('zh' 或 'en')。
    返回:
        格式化字符串,如 format_value(3.6, "kWh") → "3.6 千瓦时"(zh)。
    """
    spec = _lookup(unit)
    fmt = spec.format_zh if lang == "zh" else spec.format_en
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise UnitError(f"无法格式化的数值: {value!r}", params={"param": unit, "expected": "数值"}) from None
    return fmt.format(num)
