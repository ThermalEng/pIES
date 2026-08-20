"""单位换算与量纲(审查意见第 0 条;方案 01 §4.2 的 core/units.py 扩展等价实现)。

既有 `iesplan.core.units.py` 不做修改,本包按 01 §4.2 签名提供扩展换算层:
- Quantity / normalize_unit / to_si / from_si / dims_of / unit_meta /
  assert_same_dims / convert(支持复合单位);
- 换算唯一入口语义:计算边界(worker 输入装配、引擎 KPI 输出)一律经
  to_si / from_si,禁止各调用点自行维护换算表(01 §4.1);
- 复合单位只允许一层除法,分子分母内用 · 连乘;仿射单位(C/F)只允许独立出现;
- 币种折算:CNY 为基准,USD 等非固定汇率币种 to_si/from_si/convert 抛 UnitError
  (01 §9.4,汇率走经济参数配置)。

依赖:core.stdunits.registry / core.stdunits.parse(仅 0 层)。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Final

from iesplan.core.expression import Dimensions
from iesplan.core.units import UnitError
from iesplan.core.stdunits.parse import (
    NUMBER_RE,
    UnitParseError,
    decompose,
    parse_number,
    parse_unit_string,
    si_unit_of,
)
from iesplan.core.stdunits.registry import (
    AFFINE_UNITS,
    NON_CONVERTIBLE_CURRENCIES,
    UNITS,
    dim_key_of,
)


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


def _wrap_unit_error(unit: str, exc: UnitParseError) -> UnitError:
    """解析失败 → 未注册单位 UnitError(设计契约:normalize_unit 等抛 PARAM-UNIT-002)。"""
    return UnitError(
        str(exc.args[0]) if exc.args else f"单位无法解析: {unit!r}",
        params={"unit": unit, "expected": exc.params.get("expected", "已注册单位或复合形态")},
    )


def normalize_unit(unit: str) -> str:
    """单位串 → 规范形:别名/大小写/kWp→kW/元→CNY/℃→C/复合统一(01 §4.2)。

    示例:
        normalize_unit("kWp") == "kW"; normalize_unit("元/kWh") == "CNY/kWh";
        normalize_unit("℃") == "C"; normalize_unit("0.35元/kWh") == "CNY/kWh"(忽略数值);
        normalize_unit("tCO2/万m³") == "tCO2/万m³"。

    异常:
        UnitError(PARAM-UNIT-002): 未注册/无法解析。
    """
    s = unit.strip()
    if not s:
        raise UnitError(f"单位串为空", params={"unit": unit, "expected": "已注册单位"})
    try:
        canon, _ = parse_unit_string(s)
        return canon
    except UnitParseError:
        pass
    # 忽略数值前缀("0.35元/kWh"、"25 ℃" 等,01 §4.2)
    match = NUMBER_RE.match(s)
    if match is not None and match.end() < len(s):
        rest = s[match.end():].strip()
        if rest:
            try:
                canon, _ = parse_unit_string(rest)
                return canon
            except UnitParseError:
                pass
    raise UnitError(f"无法识别的单位: {unit!r}", params={"unit": unit, "expected": "已注册单位或其别名"})


def _check_convertible(canonical: str) -> None:
    """非固定汇率币种禁止自动折算(01 §9.4)。"""
    if canonical in NON_CONVERTIBLE_CURRENCIES:
        raise UnitError(
            f"{canonical} 汇率非固定换算,禁止自动折算(汇率走经济参数配置)",
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
    try:
        canonical, factor = parse_unit_string(unit)
    except UnitParseError as exc:
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
    try:
        canonical, factor = parse_unit_string(unit)
    except UnitParseError as exc:
        raise _wrap_unit_error(unit, exc) from None
    _check_convertible(canonical)
    if canonical in AFFINE_UNITS:
        spec = UNITS[canonical]
        return (si_value - spec.offset) / spec.to_si
    return si_value / factor


def dims_of(unit: str) -> Dimensions:
    """注册单位(含复合)→ 量纲多重集(01 §4.2;无量纲返回空 Counter)。

    量纲键:energy/power/time/temperature/currency/angle/mass/volume/voltage/area
    (duration 类别映射为 time,与 core.expression.DIM_TIME 一致)。

    示例:
        dims_of("kW") == {power:1}; dims_of("CNY/kWh") == {currency:1, energy:-1};
        dims_of("tCO2/万m³") == {mass:1, volume:-1}; dims_of("%") == {}。
    """
    canonical = normalize_unit(unit)
    try:
        numerator, denominator = decompose(canonical)
    except UnitParseError as exc:
        raise _wrap_unit_error(unit, exc) from None
    dims: Dimensions = Counter()
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
    if spec is not None:
        return {
            "unit": canonical,
            "category": spec.category,
            "si_unit": si_unit_of(canonical),
            "to_si": spec.to_si,
            "dims": dims,
            "precision_digits": spec.precision_digits,
            "symbol_zh": spec.symbol_zh,
            "symbol_en": spec.symbol_en,
        }
    _, factor = parse_unit_string(canonical)
    return {
        "unit": canonical,
        "category": "composite",
        "si_unit": si_unit_of(canonical),
        "to_si": factor,
        "dims": dims,
        "precision_digits": 2,
        "symbol_zh": canonical,
        "symbol_en": canonical,
    }


#: 单位注册表对外的换算系数表快照(unit_meta 批量导出 / 前端镜像数据源,只读)
UNIT_META_TABLE: Final[dict[str, dict]] = {uid: unit_meta(uid) for uid in UNITS}
