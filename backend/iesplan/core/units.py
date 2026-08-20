"""单位注册表与换算(依据 02 §2、04 §8;审查意见第 0 条扩展,01 方案定案 §4.1)。

内部基准单位(SI + 货币):能量 J、功率 W、温度 K、金额 CNY、时长 s、角度 rad,
扩展类:质量 kg、体积 m³、电压 V、面积 m²、无量纲。
- convert:同类单位间换算(仅限同量纲,跨量纲拒绝;支持复合单位)。
- to_si / from_si:计算边界唯一换算入口(业务单位 ↔ SI)。
- normalize_unit / dims_of / unit_meta / assert_same_dims:单位串规范与量纲。
- parse_quantity(见 core/unitparse.py):非标准单位字符串解析("1000 kW"/"3 元/kWh")。
- energy_to_joules / power_to_watts / temperature_kelvin:接口→内部基准(薄封装)。
- format_value:中英展示格式(不用于计算)。
- 温度:摄氏度是仿射单位(含 273.15 偏置),只允许与温度/温差做加减(04 §8.3 规则 4)。
- 币种:CNY 为基准,USD 等非固定汇率币种禁止自动折算(01 §9.4,汇率走经济参数)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from iesplan.core.errors import AppError

# ---------------------------------------------------------------------------
# 单位类别(既有六类 + 新增四类 + 无量纲,01 §2.2)
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

# 温度转换常量(02 §2.2)
KELVIN_OFFSET_C = 273.15  # T[K] = θ[°C] + 273.15

#: 量纲键(01 §2.1;duration 的量纲键为 time,与 core.expression.DIM_TIME 一致)
DIM_MASS: Final[str] = "mass"
DIM_VOLUME: Final[str] = "volume"
DIM_VOLTAGE: Final[str] = "voltage"
DIM_AREA: Final[str] = "area"

#: 量纲键在类别制中的命名(01 §4.2 dims_of)
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
    """类别 → 量纲键(无量纲返回 None,01 §2.1)。"""
    return _DIM_KEY_BY_CATEGORY.get(category)


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
    # 质量(基准 kg,01 §2.2)
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
    # 体积(基准 m³,01 §2.2)
    "m³": _u("ies.unit.m3", CATEGORY_VOLUME, "立方米", "m³", 1.0, aliases=("m3", "立方米", "方")),
    "千m³": _u("ies.unit.km3", CATEGORY_VOLUME, "千立方米", "千m³", 1e3, aliases=("千立方米",)),
    # 电压(基准 V,01 §2.2)
    "V": _u("ies.unit.v", CATEGORY_VOLTAGE, "伏", "V", 1.0, aliases=("v", "伏")),
    "kV": _u("ies.unit.kv", CATEGORY_VOLTAGE, "千伏", "kV", 1e3, aliases=("kv", "千伏")),
    "MV": _u("ies.unit.mv", CATEGORY_VOLTAGE, "兆伏", "MV", 1e6, aliases=("mv",)),
    # 面积(基准 m²,01 §2.2)
    "m²": _u("ies.unit.m2", CATEGORY_AREA, "平方米", "m²", 1.0, aliases=("m2", "平方米", "平米")),
    # 无量纲(基准 1,01 §2.2)
    "-": _u(
        "ies.unit.dimless",
        CATEGORY_DIMENSIONLESS,
        "无量纲",
        "-",
        1.0,
        aliases=("-", "无量纲", "dimensionless", "1"),
    ),
    "%": _u(
        "ies.unit.pct", CATEGORY_DIMENSIONLESS, "百分比", "%", 0.01, aliases=("%", "百分比", "percent", "pct")
    ),
    # 时长增补(01 §2.2)
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

# 既有单位别名增补(01 §2.2:峰值容量语义 + 温度小写 + 中文全名解析友好)
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
for _uid, _extra in _ALIAS_EXTRA.items():
    _base = UNITS[_uid]
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


def lookup(unit: str) -> UnitSpec:
    """按单位名或别名查注册表(大小写不敏感,仅限单一单位)。

    异常:
        UnitError: 单位未注册(含建议信息)。
    """
    return _lookup(unit)


def canonical_of(unit: str) -> str:
    """单一单位 → 规范形(如 kWp → kW、元 → CNY、℃ → C)。

    异常:
        UnitError: 未注册。
    """
    key = unit.strip().lower()
    uid = ALIAS_MAP.get(key)
    if uid is None:
        raise UnitError(f"未注册的单位: {unit!r}", params={"unit": unit, "expected": "已注册单位"})
    return uid


# ---------------------------------------------------------------------------
# 单位串解析(01 §3:复合单位文法;实现于 core/unitparse.py,经模块级
# __getattr__ 惰性转发 —— unitparse 反向依赖本模块,避免循环导入)
# ---------------------------------------------------------------------------

_UNITPARSE_EXPORTS: Final[tuple[str, ...]] = (
    "NUMBER_RE",
    "UnitParseError",
    "decompose",
    "parse_number",
    "parse_quantity",
    "parse_unit_string",
    "si_unit_of",
)


def __getattr__(name: str):
    """PEP 562: 惰性转发 unitparse 导出(规避 unitparse ↔ units 循环导入)。"""
    if name in _UNITPARSE_EXPORTS:
        from iesplan.core import unitparse

        return getattr(unitparse, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ---------------------------------------------------------------------------
# 扩展换算 API(01 §4.2;计算边界唯一换算入口)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Quantity:
    """"数值+单位"解析产物(标准单位形态,非 SI;si_* 供计算边界)。

    属性:
        value: 数值(所在单位为 unit,如 1000 kW 的 value=1000)。
        unit: 规范化单位串(如 "kW" / "CNY/kW·月";可被 normalize_unit 接受)。
        si_value: 换算到 SI 基准后的数值(复合单位=value×组合系数;温度=仿射)。
        si_unit: SI 基准单位描述(如 "W"、"CNY/J"、"kg/m³")。
    """

    value: float
    unit: str
    si_value: float
    si_unit: str

    def to(self, target: str) -> float:
        """换算到目标注册单位(同量纲断言,跨量纲抛 UnitError)。

        示例: Quantity(1000, "kW", 1e6, "W").to("MW") == 1.0。
        """
        assert_same_dims(self.unit, target)
        return from_si(self.si_value, target)

    def __float__(self) -> float:
        """返回 si_value(SI 优先,防误用原值,01 §4.2)。"""
        return self.si_value


def _wrap_unit_error(unit: str, exc: object) -> UnitError:
    """解析失败 → 未注册单位 UnitError(设计契约:normalize_unit 等抛 PARAM-UNIT-002)。"""
    params = getattr(exc, "params", {})
    return UnitError(
        str(exc.args[0]) if exc.args else f"单位无法解析: {unit!r}",
        params={"unit": unit, "expected": params.get("expected", "已注册单位或复合形态")},
    )


def _unitparse():
    """惰性取 core/unitparse 模块(规避 unitparse ↔ units 循环导入)。"""
    from iesplan.core import unitparse

    return unitparse


def normalize_unit(unit: str) -> str:
    """单位串 → 规范形:别名/大小写/kWp→kW/元→CNY/℃→C/复合统一(01 §4.2)。

    示例:
        normalize_unit("kWp") == "kW"; normalize_unit("元/kWh") == "CNY/kWh";
        normalize_unit("℃") == "C"; normalize_unit("0.35元/kWh") == "CNY/kWh"(忽略数值);
        normalize_unit("tCO2/万m³") == "tCO2/万m³"。

    异常:
        UnitError(PARAM-UNIT-002): 未注册/无法解析。
    """
    up = _unitparse()
    s = unit.strip()
    if not s:
        raise UnitError(f"单位串为空", params={"unit": unit, "expected": "已注册单位"})
    try:
        canon, _ = up.parse_unit_string(s)
        return canon
    except up.UnitParseError:
        pass
    # 忽略数值前缀("0.35元/kWh"、"25 ℃" 等,01 §4.2)
    match = up.NUMBER_RE.match(s)
    if match is not None and match.end() < len(s):
        rest = s[match.end():].strip()
        if rest:
            try:
                canon, _ = up.parse_unit_string(rest)
                return canon
            except up.UnitParseError:
                pass
    raise UnitError(f"无法识别的单位: {unit!r}", params={"unit": unit, "expected": "已注册单位或其别名"})


def _check_convertible(canonical: str) -> None:
    """非固定汇率币种禁止自动折算(01 §9.4)。

    检查整个复合单位(分子/分母任一含 USD 即拒绝, codex 二次审核 Medium-7:
    USD/kWh、USD/kW 等复合形态也必须拒绝, 不能只匹配纯 "USD")。
    """
    for token in canonical.replace("·", "/").split("/"):
        if token in NON_CONVERTIBLE_CURRENCIES:
            raise UnitError(
                f"{canonical} 含非固定汇率币种 {token}, 禁止自动折算(汇率走经济参数配置)",
                params={"unit": canonical, "expected": "经汇率配置折算"},
            )


def to_si(value: float, unit: str) -> float:
    """任意注册单位(含复合)→ SI 数值(线性×系数,温度仿射),计算边界唯一入口(01 §4.2)。

    示例:
        to_si(1000, "kW") == 1e6; to_si(40, "CNY/kW·月") == 40 / (1e3 * 2.592e6);
        to_si(25, "C") == 298.15; to_si(10, "%") == 0.1。

    异常:
        UnitError: 单位无法解析;USD 等非固定汇率币种拒绝折算。
    """
    up = _unitparse()
    try:
        canonical, factor = up.parse_unit_string(unit)
    except up.UnitParseError as exc:
        raise _wrap_unit_error(unit, exc) from None
    _check_convertible(canonical)
    if canonical in AFFINE_UNITS:
        spec = UNITS[canonical]
        return value * spec.to_si + spec.offset
    return value * factor


def from_si(si_value: float, unit: str) -> float:
    """SI → 注册单位数值(逆变换;温度仿射取反),结果装配/展示层唯一出口(01 §4.2)。

    示例:
        from_si(1e6, "kW") == 1000.0; from_si(298.15, "C") == 25.0。

    异常:
        UnitError: 单位无法解析;USD 等非固定汇率币种拒绝折算。
    """
    up = _unitparse()
    try:
        canonical, factor = up.parse_unit_string(unit)
    except up.UnitParseError as exc:
        raise _wrap_unit_error(unit, exc) from None
    _check_convertible(canonical)
    if canonical in AFFINE_UNITS:
        spec = UNITS[canonical]
        return (si_value - spec.offset) / spec.to_si
    return si_value / factor


def dims_of(unit: str) -> "Counter":
    """注册单位(含复合)→ 量纲多重集(01 §4.2;无量纲返回空 Counter)。

    量纲键:energy/power/time/temperature/currency/angle/mass/volume/voltage/area
    (duration 类别映射为 time,与 core.expression.DIM_TIME 一致)。

    示例:
        dims_of("kW") == {power:1}; dims_of("CNY/kWh") == {currency:1, energy:-1};
        dims_of("tCO2/万m³") == {mass:1, volume:-1}; dims_of("%") == {}。
    """
    from collections import Counter

    up = _unitparse()
    canonical = normalize_unit(unit)
    try:
        numerator, denominator = up.decompose(canonical)
    except up.UnitParseError as exc:
        raise _wrap_unit_error(unit, exc) from None
    dims: Counter = Counter()
    for _, _, spec in numerator:
        key = dim_key_of(spec.category)
        if key is not None:
            dims[key] += 1
    for _, _, spec in denominator:
        key = dim_key_of(spec.category)
        if key is not None:
            dims[key] -= 1
    return dims


def assert_same_dims(unit_a: str, unit_b: str) -> None:
    """量纲一致性断言(跨类换算防护的字符串层,01 §4.2)。

    异常:
        UnitError(PARAM-UNIT-002): 量纲不一致(含未注册单位)。
    """
    dims_a, dims_b = dims_of(unit_a), dims_of(unit_b)
    if dims_a != dims_b:
        raise UnitError(
            f"量纲不一致,拒绝换算: {unit_a}({dict(dims_a)}) → {unit_b}({dict(dims_b)})",
            params={"param": unit_a, "expected": dict(dims_a), "actual": dict(dims_b)},
        )


def convert(value: float, from_unit: str, to_unit: str) -> float:
    """同类量纲单位间换算(经 dims_of 一致性检查后 to_si/from_si 组合,01 §4.2)。

    支持复合单位与仿射温度;跨量纲(如 kW → kWh)抛 UnitError;
    CNY ↔ USD 等非固定汇率币种抛 UnitError(01 §9.4)。

    示例:
        convert(1000, "kW", "MW") == 1.0; convert(3.6e6, "J", "kWh") == 1.0;
        convert(25, "C", "F") == 77.0; convert(0.5, "tCO2/万m³", "kg/m³") == 0.05。
    """
    assert_same_dims(from_unit, to_unit)
    return from_si(to_si(value, from_unit), to_unit)


def unit_meta(unit: str) -> dict:
    """单位元数据(前端渲染/校验用,01 §4.2):
    {"unit","category","si_unit","to_si","dims","precision_digits","symbol_zh","symbol_en"}。

    复合单位为组合系数与 "composite" 类别;无量纲 dims 为空字典。
    """
    canonical = normalize_unit(unit)
    spec = UNITS.get(canonical)
    dims = dict(dims_of(canonical))
    up = _unitparse()
    if spec is not None:
        return {
            "unit": canonical,
            "category": spec.category,
            "si_unit": up.si_unit_of(canonical),
            "to_si": spec.to_si,
            "dims": dims,
            "precision_digits": spec.precision_digits,
            "symbol_zh": spec.symbol_zh,
            "symbol_en": spec.symbol_en,
        }
    _, factor = up.parse_unit_string(canonical)
    return {
        "unit": canonical,
        "category": "composite",
        "si_unit": up.si_unit_of(canonical),
        "to_si": factor,
        "dims": dims,
        "precision_digits": 2,
        "symbol_zh": canonical,
        "symbol_en": canonical,
    }


#: 单位注册表对外的换算系数表快照(unit_meta 批量导出 / 前端镜像数据源,只读)。
#: 惰性构建:unit_meta 依赖 core/unitparse,而 unitparse 反向依赖本模块
#: (循环导入,见 _unitparse);首次访问时才构建并缓存。
UNIT_META_TABLE: dict[str, dict] | None = None


def unit_meta_table() -> dict[str, dict]:
    """单位注册表换算系数表快照(unit_meta 批量导出 / 前端镜像数据源)。"""
    global UNIT_META_TABLE
    if UNIT_META_TABLE is None:
        UNIT_META_TABLE = {uid: unit_meta(uid) for uid in UNITS}
    return UNIT_META_TABLE


def is_known_unit(unit: str) -> bool:
    """单位串是否可被识别(单一单位或复合单位,含非规范形态)。"""
    try:
        normalize_unit(unit)
        return True
    except UnitError:
        return False


# ---------------------------------------------------------------------------
# 数据字段单位契约(01 §5.2/§5.6;worker 与 assembly 共用)
# ---------------------------------------------------------------------------

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
