"""财务三件套领域结构: FinanceProfile / FinanceOverrides / EffectiveFinanceConfig (finance-yaml@1.0.0)。

契约权威: manual/developer-guide/zh-CN/formats/finance-yaml.md。
代码边界: finance 模块(宪法 §4.6)。本模块只依赖标准库与 core.diagnostics / core.units，
不导入其他业务模块，不访问 HTTP / 数据库 / 前端。

本切片交付三件套**纯域契约** (含确定性合并器/空 overrides)，
0.6.5 条目 1 的持久化、API 与项目包接入属后续切片。

设计要点(与 finance-yaml.md 逐项对齐):
- 金额一律 ``{value, unit}`` 原子: ``value`` 为十进制定点字符串，禁止
  float/NaN/Infinity/int/null/bool；unit 经 core.units 规范化校验但保留原始拼写；
- 文本文件只校验字头（`schema`/`schema_version`）与领域约束，不做内容摘要；
- Overrides 稀疏原子覆盖: 只替换既有叶子，禁止新增/删除 finance_type/driver/price_id，
  禁止改写单位/carrier/direction/tax 与 profile 级字段；
- energy price 允许负数、0、正数(有限 Decimal)；成本金额非负；
- EffectiveFinanceConfig 只能由合并器 ``merge_effective`` 生成；空覆盖时使用空 Overrides 文档；
- 确定性合并：稀疏覆盖只替换被覆盖叶子。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
from types import MappingProxyType
from typing import Final

from iesplan.core.diagnostics import Diagnostic, make_diag
from iesplan.core.units import UnitError, normalize_unit

# ---------------------------------------------------------------------------
# 契约常量
# ---------------------------------------------------------------------------

SCHEMA_PROFILE: Final[str] = "ies.finance-profile"
SCHEMA_OVERRIDES: Final[str] = "ies.finance-overrides"
SCHEMA_EFFECTIVE: Final[str] = "ies.effective-finance-config"
SCHEMA_VERSION: Final[str] = "1.0.0"

CURRENCIES: Final[tuple[str, ...]] = ("CNY", "USD")
PRICE_BASIS_VALUES: Final[tuple[str, ...]] = ("tax_inclusive", "tax_exclusive")
COST_METHOD_VALUES: Final[tuple[str, ...]] = ("fixed_plus_linear",)
DIRECTION_VALUES: Final[tuple[str, ...]] = ("purchase", "sale")
KIND_VALUES: Final[tuple[str, ...]] = ("constant", "time_series")
RESOLUTION_VALUES: Final[tuple[str, ...]] = ("15min", "30min", "1h")

#: finance_type 分量(时间口径与量纲见 finance-yaml.md「时间口径与分量」)。
FINANCE_COMPONENTS: Final[tuple[str, ...]] = (
    "upfront_capex",
    "annual_fixed_om",
    "period_variable_om",
)
_DIMLESS_UNIT = "1"

#: 诊断码(登记于 core.diagnostics NEW_DIAG_CODES)。
FIN_TRIPLET_INVALID = "PROJ-FIN-001"

#: 金额/价格允许的最大十进制位(含整数与小数部分, 防病态指数输入)。
MAX_DIGITS: Final[int] = 30

_PROFILE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_LOCAL_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]*$")

class FinanceTripletError(ValueError):
    """财务三件套校验失败(非法结构/数值/摘要/越权覆盖)。"""

# ---------------------------------------------------------------------------
# 十进制与单位工具
# ---------------------------------------------------------------------------

def _to_decimal_str(value: object, field_name: str) -> Decimal:
    """字段 -> Decimal; 只接受十进制**字符串**(拒绝 float/int/bool/null)。

    YAML 中金额必须写为字符串(十进制定点), 禁止 float/int 参与金额语义;
    NaN/Infinity 一律拒绝; 超长精度拒绝。
    """
    if isinstance(value, (bool, int, float)) or value is None:
        raise FinanceTripletError(
            f"{field_name}: 金额 value 必须为十进制字符串, 禁止 "
            f"{'float/int/bool/null' if not isinstance(value, str) else '数值类型'}"
        )
    try:
        d = Decimal(str(value)) if not isinstance(value, Decimal) else value
        if not d.is_finite():
            raise FinanceTripletError(f"{field_name}: 禁止 NaN/Infinity")
    except (InvalidOperation, ValueError) as exc:
        raise FinanceTripletError(f"{field_name}: 十进制解析失败: {value!r}") from exc
    digits = d.as_tuple().digits
    exponent = d.as_tuple().exponent
    if len(digits) - (int(exponent) if isinstance(exponent, int) else 0) > MAX_DIGITS:
        raise FinanceTripletError(f"{field_name}: 超出 {MAX_DIGITS} 位十进制精度")
    return d

def _decimal_to_canonical(d: Decimal) -> str:
    """Decimal -> 定点十进制字符串(去指数形态, 摘要输入)。"""
    with localcontext() as ctx:
        ctx.prec = MAX_DIGITS
        return format(d, "f")

def _normalized_unit(unit: object, field: str) -> str:
    """单位规范化校验(经 core.units; 保留原始拼写, 仅校验可识别)。"""
    if not isinstance(unit, str) or not unit.strip():
        raise FinanceTripletError(f"{field}: 单位必须为非空字符串")
    try:
        normalize_unit(unit)
    except UnitError as exc:
        raise FinanceTripletError(f"{field}: 单位无法识别 {unit!r}: {exc}") from exc
    return unit

def _dims(unit: str) -> Mapping[str, int]:
    """单位量纲(供成本/价格单位一致性校验)。"""
    from iesplan.core.units import dims_of

    return dims_of(unit)

def _validate_local_id(value: object, field: str) -> str:
    """局部 ID(finance_type / price_id / driver / tax id): lower_snake_case 且不含 __。"""
    if not isinstance(value, str) or not _LOCAL_ID_RE.fullmatch(value) or "__" in value:
        raise FinanceTripletError(f"{field}: 必须为 lower_snake_case 且不含 '__': {value!r}")
    return value

# ---------------------------------------------------------------------------
# 规范化 YAML 字节(唯一规范形态; 映射键稳定排序/LF/非 ASCII 保留)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Money 原子
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Money:
    """``{value, unit}`` 金额原子: value 为十进制定点 Decimal, unit 来自公共单位词汇。

    禁止 null / 部分金额; cost 金额非负(allow_negative=False 时校验),
    energy price 允许负数(allow_negative=True)。
    """

    value: Decimal
    unit: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, Decimal):
            raise FinanceTripletError(f"金额 value 必须为 Decimal, 实际 {type(self.value).__name__}")
        if not self.value.is_finite():
            raise FinanceTripletError("金额禁止 NaN/Infinity")
        _normalized_unit(self.unit, "金额 unit")

    def to_dict(self) -> dict:
        return {"value": _decimal_to_canonical(self.value), "unit": self.unit}

    @classmethod
    def from_dict(cls, mapping: object, *, allow_negative: bool = False, field: str = "Money") -> Money:
        if not isinstance(mapping, Mapping):
            raise FinanceTripletError(f"{field}: 必须是 {{value, unit}} 字典")
        unknown = set(mapping) - {"value", "unit"}
        if unknown:
            raise FinanceTripletError(f"{field}: 未知字段 {sorted(unknown)}")
        missing = {"value", "unit"} - set(mapping)
        if missing:
            raise FinanceTripletError(f"{field}: 缺少字段 {sorted(missing)}")
        value = _to_decimal_str(mapping["value"], f"{field}.value")
        if not allow_negative and value < 0:
            raise FinanceTripletError(f"{field}.value: 成本金额禁止负数: {value}")
        unit = mapping["unit"]
        if unit is None:
            raise FinanceTripletError(f"{field}.unit: 禁止 null")
        return cls(value=value, unit=_normalized_unit(unit, f"{field}.unit"))

# ---------------------------------------------------------------------------
# 成本分量
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class LinearCost:
    """线性分量: driver -> {unit_cost: Money} 中的一项。"""

    unit_cost: Money

    def to_dict(self) -> dict:
        return {"unit_cost": self.unit_cost.to_dict()}

    @classmethod
    def from_dict(cls, mapping: object) -> LinearCost:
        if not isinstance(mapping, Mapping):
            raise FinanceTripletError("线性分量必须是字典")
        if set(mapping) != {"unit_cost"}:
            raise FinanceTripletError(f"线性分量字段非法: {sorted(mapping)}")
        return cls(unit_cost=Money.from_dict(mapping["unit_cost"], field="unit_cost"))

@dataclass(frozen=True, slots=True)
class CostComponent:
    """一个时间口径成本分量: ``fixed`` 与/或 ``linear``(至少一个; 金额非负)。"""

    fixed: Money | None = None
    linear: Mapping[str, LinearCost] | None = None

    def __post_init__(self) -> None:
        if self.fixed is None and (self.linear is None or not self.linear):
            raise FinanceTripletError("成本分量必须含 fixed 或 linear 之一")
        if self.linear is not None:
            object.__setattr__(self, "linear", MappingProxyType(dict(sorted(self.linear.items()))))

    def to_dict(self) -> dict:
        out: dict = {}
        if self.fixed is not None:
            out["fixed"] = self.fixed.to_dict()
        if self.linear is not None and self.linear:
            out["linear"] = {k: v.to_dict() for k, v in sorted(self.linear.items())}
        return out

    @classmethod
    def from_dict(cls, mapping: object) -> CostComponent:
        if not isinstance(mapping, Mapping):
            raise FinanceTripletError("成本分量必须是字典")
        unknown = set(mapping) - {"fixed", "linear"}
        if unknown:
            raise FinanceTripletError(f"成本分量未知字段: {sorted(unknown)}")
        fixed = None
        linear = None
        if "fixed" in mapping:
            if mapping["fixed"] is None:
                raise FinanceTripletError("fixed 禁止 null")
            fixed = Money.from_dict(mapping["fixed"], field="fixed")
        if "linear" in mapping:
            raw_linear = mapping["linear"]
            if not isinstance(raw_linear, Mapping) or not raw_linear:
                raise FinanceTripletError("linear 必须为非空字典")
            parsed: dict[str, LinearCost] = {}
            for driver, value in raw_linear.items():
                parsed[_validate_local_id(driver, "linear driver")] = LinearCost.from_dict(value)
            linear = parsed
        if fixed is None and not linear:
            raise FinanceTripletError("成本分量必须含 fixed 或 linear 之一")
        return cls(fixed=fixed, linear=linear)

@dataclass(frozen=True, slots=True)
class FinanceTypeEntry:
    """``finance_types.<name>`` 成本模型: 至多三个时间口径分量, 至少一个存在。"""

    upfront_capex: CostComponent | None = None
    annual_fixed_om: CostComponent | None = None
    period_variable_om: CostComponent | None = None

    def __post_init__(self) -> None:
        if self.upfront_capex is None and self.annual_fixed_om is None and self.period_variable_om is None:
            raise FinanceTripletError("finance_type 至少需要一个成本分量")

    def to_dict(self) -> dict:
        out: dict = {}
        if self.upfront_capex is not None:
            out["upfront_capex"] = self.upfront_capex.to_dict()
        if self.annual_fixed_om is not None:
            out["annual_fixed_om"] = self.annual_fixed_om.to_dict()
        if self.period_variable_om is not None:
            out["period_variable_om"] = self.period_variable_om.to_dict()
        return out

    @classmethod
    def from_dict(cls, mapping: object) -> FinanceTypeEntry:
        if not isinstance(mapping, Mapping):
            raise FinanceTripletError("finance_type 必须是字典")
        if not mapping:
            raise FinanceTripletError("finance_type 不能为空")
        unknown = set(mapping) - set(FINANCE_COMPONENTS)
        if unknown:
            raise FinanceTripletError(f"finance_type 未知分量: {sorted(unknown)}")
        # 分量均可选, 但 finance_type 至少声明一个(finance-yaml.md 字段表
        # "至少一个分量存在"; 空条目在 __post_init__ 拒绝)。
        # fixed(fixed Money)只允许在 upfront_capex(字段表); 其他分量只有 linear。
        kwargs: dict = {}
        for comp in FINANCE_COMPONENTS:
            if comp not in mapping:
                continue
            if mapping[comp] is None:
                raise FinanceTripletError(f"{comp} 禁止 null")
            if comp != "upfront_capex":
                if isinstance(mapping[comp], Mapping) and "fixed" in mapping[comp]:
                    raise FinanceTripletError(
                        f"{comp} 不允许 fixed(固定金额只属 upfront_capex; {comp} 只有 linear)"
                    )
            kwargs[comp] = CostComponent.from_dict(mapping[comp])
        return cls(**kwargs)

# ---------------------------------------------------------------------------
# 能源价格条目
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SeriesMeta:
    """``time_series`` 的不可变时间元数据。"""

    resolution: str
    leap_year: bool
    point_count: int
    unit: str

    def __post_init__(self) -> None:
        if self.resolution not in RESOLUTION_VALUES:
            raise FinanceTripletError(f"series_meta.resolution 非法: {self.resolution!r}")
        if not isinstance(self.leap_year, bool):
            raise FinanceTripletError("series_meta.leap_year 必须为 bool")
        if (
            not isinstance(self.point_count, int)
            or isinstance(self.point_count, bool)
            or self.point_count <= 0
        ):
            raise FinanceTripletError("series_meta.point_count 必须为正整数")
        _normalized_unit(self.unit, "series_meta.unit")

    def to_dict(self) -> dict:
        return {
            "resolution": self.resolution,
            "leap_year": self.leap_year,
            "point_count": self.point_count,
            "unit": self.unit,
        }

    @classmethod
    def from_dict(cls, mapping: object) -> SeriesMeta:
        if not isinstance(mapping, Mapping):
            raise FinanceTripletError("series_meta 必须是字典")
        unknown = set(mapping) - {"resolution", "leap_year", "point_count", "unit"}
        if unknown:
            raise FinanceTripletError(f"series_meta 未知字段: {sorted(unknown)}")
        missing = {"resolution", "leap_year", "point_count", "unit"} - set(mapping)
        if missing:
            raise FinanceTripletError(f"series_meta 缺少字段: {sorted(missing)}")
        leap = mapping["leap_year"]
        if not isinstance(leap, bool):
            raise FinanceTripletError("series_meta.leap_year 必须为 bool")
        pc = mapping["point_count"]
        if not isinstance(pc, int) or isinstance(pc, bool):
            raise FinanceTripletError("series_meta.point_count 必须为整数")
        return cls(
            resolution=str(mapping["resolution"]),
            leap_year=leap,
            point_count=pc,
            unit=_normalized_unit(mapping["unit"], "series_meta.unit"),
        )

@dataclass(frozen=True, slots=True)
class EnergyPrice:
    """能源价格条目(price_id 值): 判别联合 constant{value} / time_series{ref, series_meta}。"""

    carrier: str
    direction: str
    kind: str
    value: Money | None = None
    ref: str | None = None
    series_meta: SeriesMeta | None = None

    def __post_init__(self) -> None:
        _validate_local_id(self.carrier, "energy_price.carrier")
        if self.direction not in DIRECTION_VALUES:
            raise FinanceTripletError(f"direction 非法: {self.direction!r}")
        if self.kind not in KIND_VALUES:
            raise FinanceTripletError(f"kind 非法: {self.kind!r}")
        if self.kind == "constant":
            if self.value is None or self.ref is not None or self.series_meta is not None:
                raise FinanceTripletError("constant 必须且只能含 value")
        else:
            if self.value is not None or self.ref is None or self.series_meta is None:
                raise FinanceTripletError("time_series 必须且只能含 ref 与 series_meta")
            if not isinstance(self.ref, str) or not self.ref.strip():
                raise FinanceTripletError("energy_price.ref 必须为非空字符串")

    def to_dict(self) -> dict:
        out: dict = {"carrier": self.carrier, "direction": self.direction, "kind": self.kind}
        if self.kind == "constant":
            value = self.value
            if value is None:
                raise FinanceTripletError("constant 缺少 value(内部状态非法)")
            out["value"] = value.to_dict()
        else:
            meta = self.series_meta
            if meta is None or self.ref is None:
                raise FinanceTripletError("time_series 缺少 ref/series_meta(内部状态非法)")
            out["ref"] = self.ref
            out["series_meta"] = meta.to_dict()
        return out

    @classmethod
    def from_dict(cls, mapping: object) -> EnergyPrice:
        if not isinstance(mapping, Mapping):
            raise FinanceTripletError("energy_price 必须是字典")
        unknown = set(mapping) - {"carrier", "direction", "kind", "value", "ref", "series_meta"}
        if unknown:
            raise FinanceTripletError(f"energy_price 未知字段: {sorted(unknown)}")
        missing = {"carrier", "direction", "kind"} - set(mapping)
        if missing:
            raise FinanceTripletError(f"energy_price 缺少字段: {sorted(missing)}")
        kind = str(mapping["kind"])
        if kind not in KIND_VALUES:
            raise FinanceTripletError(f"kind 非法: {kind!r}")
        if kind == "constant":
            if "value" not in mapping:
                raise FinanceTripletError("constant 必须含 value")
            if "ref" in mapping or "series_meta" in mapping:
                raise FinanceTripletError("constant 不允许 ref/series_meta")
            return cls(
                carrier=_validate_local_id(mapping["carrier"], "carrier"),
                direction=str(mapping["direction"]),
                kind=kind,
                value=Money.from_dict(mapping["value"], allow_negative=True, field="value"),
            )
        if "ref" not in mapping or "series_meta" not in mapping:
            raise FinanceTripletError("time_series 必须含 ref 与 series_meta")
        if "value" in mapping:
            raise FinanceTripletError("time_series 不允许 value")
        return cls(
            carrier=_validate_local_id(mapping["carrier"], "carrier"),
            direction=str(mapping["direction"]),
            kind=kind,
            ref=str(mapping["ref"]).strip(),
            series_meta=SeriesMeta.from_dict(mapping["series_meta"]),
        )

# ---------------------------------------------------------------------------
# 税目登记
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TaxEntry:
    """``taxes.<id>`` 税目登记(声明性, 本版计算不消费税率)。"""

    display_name: str
    tax_type: str
    rate: Money
    applies_to: str

    def __post_init__(self) -> None:
        if not isinstance(self.display_name, str) or not self.display_name.strip():
            raise FinanceTripletError("tax.display_name 必须为非空字符串")
        if not isinstance(self.tax_type, str) or not self.tax_type.strip():
            raise FinanceTripletError("tax.tax_type 必须为非空字符串")
        if _dims(self.rate.unit):  # 无量纲单位(dims 为空 Counter)
            raise FinanceTripletError(f"tax rate unit 必须无量纲(如 {_DIMLESS_UNIT!r})")
        if not Decimal("0") <= self.rate.value <= Decimal("1"):
            raise FinanceTripletError("tax rate 必须在 0..1")
        if not isinstance(self.applies_to, str) or not self.applies_to.strip():
            raise FinanceTripletError("tax.applies_to 必须为非空字符串")

    def to_dict(self) -> dict:
        return {
            "display_name": self.display_name,
            "tax_type": self.tax_type,
            "rate": self.rate.to_dict(),
            "applies_to": self.applies_to,
        }

    @classmethod
    def from_dict(cls, mapping: object) -> TaxEntry:
        if not isinstance(mapping, Mapping):
            raise FinanceTripletError("tax 必须是字典")
        unknown = set(mapping) - {"display_name", "tax_type", "rate", "applies_to"}
        if unknown:
            raise FinanceTripletError(f"tax 未知字段: {sorted(unknown)}")
        missing = {"display_name", "tax_type", "rate", "applies_to"} - set(mapping)
        if missing:
            raise FinanceTripletError(f"tax 缺少字段: {sorted(missing)}")
        return cls(
            display_name=str(mapping["display_name"]),
            tax_type=str(mapping["tax_type"]),
            rate=Money.from_dict(mapping["rate"], field="rate"),
            applies_to=str(mapping["applies_to"]),
        )

# ---------------------------------------------------------------------------
# 类型级校验共享助手(量纲)
# ---------------------------------------------------------------------------

def _check_cost_units(currency: str, finance_type: str, component: str, cost: CostComponent) -> None:
    """成本分量单位量纲校验(宪法 7.4 + finance-yaml.md 字段表)。

    线性分量的 unit_cost 单位 x driver 单位必须得到目标量纲:
    - upfront_capex / period_variable_om: currency
    - annual_fixed_om: currency/time
    fixed(upfront_capex) 的单位必须就是本 Profile 币种(金额原子, 不接受其他币种)。
    driver 单位未在 Profile 内显式声明(driver 单位由装配绑定在技术接口上声明),
    本契约无法单文件校验线性分量的完整量纲; 因此这里只校验 unit_cost 的
    币种前缀(保证是 <currency>/... 形态), 完整量纲一致性由装配阶段的
    finance_binding 校验承担(finance-yaml.md「聚合与计价衔接」)。
    """
    if cost.fixed is not None and component == "upfront_capex" and cost.fixed.unit != currency:
        raise FinanceTripletError(
            f"{finance_type}.upfront_capex.fixed 单位必须为 Profile 币种 {currency!r}, "
            f"实际 {cost.fixed.unit!r}"
        )
    # annual/period 无 fixed(fixed_om_rate 等费率模型不在此契约)
    if cost.linear is not None:
        currency_prefix = f"{currency}/"
        for driver, lin in cost.linear.items():
            unit = lin.unit_cost.unit
            if not unit.startswith(currency_prefix):
                raise FinanceTripletError(
                    f"{finance_type}.{component}.linear.{driver} unit_cost 单位 {unit!r} "
                    f"必须以 {currency_prefix} 开头(币种前缀)"
                )

def _check_price_unit(currency: str, price_id: str, price: EnergyPrice) -> None:
    """能源价格单位量纲 = currency / (energy unit); 无法在 Profile 单文件
    推导 energy unit 时, 校验币种前缀 + value.unit == series_meta.unit。"""
    currency_prefix = f"{currency}/"
    if price.kind == "constant":
        price_value = price.value
        if price_value is None:
            raise FinanceTripletError(f"energy_price {price_id} 缺 value(内部状态非法)")
        unit = price_value.unit
        if not unit.startswith(currency_prefix):
            raise FinanceTripletError(
                f"energy_price {price_id} constant unit {unit!r} 必须以 {currency_prefix} 开头"
            )
    else:
        meta = price.series_meta
        if meta is None:
            raise FinanceTripletError(f"energy_price {price_id} 缺 series_meta(内部状态非法)")
        unit = meta.unit
        if not unit.startswith(currency_prefix):
            raise FinanceTripletError(
                f"energy_price {price_id} series_meta.unit {unit!r} 必须以 {currency_prefix} 开头"
            )

# ---------------------------------------------------------------------------
# FinanceProfile(地区财务基准, 人工 authoring)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FinanceProfile:
    """地区财务基准(已注册、可复用; 系统不得硬编码为全局默认)。

    ``profile`` 为只读字典 {id, region, currency, base_year, price_basis, cost_method}。
    文本文件只校验字头与领域约束，不做内容摘要。
    """

    schema: str
    schema_version: str
    profile: Mapping[str, object]
    finance_types: Mapping[str, FinanceTypeEntry]
    energy_prices: Mapping[str, EnergyPrice]
    taxes: Mapping[str, TaxEntry] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if self.schema != SCHEMA_PROFILE:
            raise FinanceTripletError(f"schema 必须为 {SCHEMA_PROFILE!r}")
        if self.schema_version != SCHEMA_VERSION:
            raise FinanceTripletError(f"schema_version 必须为 {SCHEMA_VERSION!r}")
        # 冻结
        profile_sorted = dict(sorted(self.profile.items(), key=lambda kv: kv[0]))
        object.__setattr__(self, "profile", MappingProxyType(profile_sorted))
        object.__setattr__(self, "finance_types", MappingProxyType(dict(sorted(self.finance_types.items()))))
        object.__setattr__(self, "energy_prices", MappingProxyType(dict(sorted(self.energy_prices.items()))))
        object.__setattr__(self, "taxes", MappingProxyType(dict(sorted(self.taxes.items()))))
        # taxes.applies_to 引用存在性: 引用本文件 energy_prices 的 price_id,
        # 或 finance_types 的分量路径(<finance_type>.<分量>, 如 pv_system.upfront_capex)。
        for tax_id, tax in self.taxes.items():
            target = tax.applies_to
            if target in self.energy_prices:
                continue
            parts = target.split(".")
            if (
                len(parts) == 2
                and parts[0] in self.finance_types
                and parts[1] in FINANCE_COMPONENTS
            ):
                continue
            raise FinanceTripletError(f"tax {tax_id!r} applies_to 引用不存在: {target!r}")

    # -- profile 便捷访问 --
    @property
    def profile_id(self) -> str:
        return str(self.profile["id"])

    @property
    def currency(self) -> str:
        return str(self.profile["currency"])

    @property
    def base_year(self) -> int:
        raw = self.profile["base_year"]
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise FinanceTripletError("profile.base_year 非法")
        return raw

    @property
    def price_basis(self) -> str:
        return str(self.profile["price_basis"])

    @property
    def cost_method(self) -> str:
        return str(self.profile["cost_method"])

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "profile": dict(sorted(self.profile.items())),
            "finance_types": {k: v.to_dict() for k, v in sorted(self.finance_types.items())},
            "energy_prices": {k: v.to_dict() for k, v in sorted(self.energy_prices.items())},
            "taxes": {k: v.to_dict() for k, v in sorted(self.taxes.items())},
        }

    @classmethod
    def from_dict(cls, mapping: object) -> FinanceProfile:
        if not isinstance(mapping, Mapping):
            raise FinanceTripletError("FinanceProfile 必须是字典")
        known_profile = {"schema", "schema_version", "profile", "finance_types", "energy_prices", "taxes"}
        unknown = set(mapping) - known_profile
        if unknown:
            raise FinanceTripletError(f"FinanceProfile 未知字段: {sorted(unknown)}")
        missing = {"schema", "schema_version", "profile", "finance_types", "energy_prices"} - set(mapping)
        if missing:
            raise FinanceTripletError(f"FinanceProfile 缺少字段: {sorted(missing)}")
        schema = str(mapping["schema"])
        schema_version = str(mapping["schema_version"])
        if schema != SCHEMA_PROFILE:
            raise FinanceTripletError(f"schema 非法: {schema!r}")
        if schema_version != SCHEMA_VERSION:
            raise FinanceTripletError(f"schema_version 非法: {schema_version!r}")

        profile_raw = mapping["profile"]
        if not isinstance(profile_raw, Mapping):
            raise FinanceTripletError("profile 必须是字典")
        unknown_p = set(profile_raw) - {"id", "region", "currency", "base_year", "price_basis", "cost_method"}
        if unknown_p:
            raise FinanceTripletError(f"profile 未知字段: {sorted(unknown_p)}")
        missing_p = {"id", "region", "currency", "base_year", "price_basis", "cost_method"} - set(profile_raw)
        if missing_p:
            raise FinanceTripletError(f"profile 缺少字段: {sorted(missing_p)}")
        pid = str(profile_raw["id"])
        if not _PROFILE_ID_RE.fullmatch(pid) or "__" in pid:
            raise FinanceTripletError(f"profile.id 非法: {pid!r}")
        region = str(profile_raw["region"])
        if not region.strip():
            raise FinanceTripletError("profile.region 不能为空")
        currency = str(profile_raw["currency"])
        if currency not in CURRENCIES:
            raise FinanceTripletError(f"currency 非法: {currency!r}")
        base_year = profile_raw["base_year"]
        if not isinstance(base_year, int) or isinstance(base_year, bool):
            raise FinanceTripletError("profile.base_year 必须为整数")
        if not 1900 <= base_year <= 2999:
            raise FinanceTripletError(f"base_year 超出范围: {base_year}")
        price_basis = str(profile_raw["price_basis"])
        if price_basis not in PRICE_BASIS_VALUES:
            raise FinanceTripletError(f"price_basis 非法: {price_basis!r}")
        cost_method = str(profile_raw["cost_method"])
        if cost_method not in COST_METHOD_VALUES:
            raise FinanceTripletError(f"cost_method 非法: {cost_method!r}")
        profile: dict = {
            "id": pid,
            "region": region,
            "currency": currency,
            "base_year": base_year,
            "price_basis": price_basis,
            "cost_method": cost_method,
        }

        ft_raw = mapping["finance_types"]
        if not isinstance(ft_raw, Mapping):
            raise FinanceTripletError("finance_types 必须是字典")
        if not ft_raw:
            raise FinanceTripletError("finance_types 不能为空")
        finance_types: dict[str, FinanceTypeEntry] = {}
        for ft_name, ft_val in ft_raw.items():
            ft_name = _validate_local_id(ft_name, "finance_type")
            if ft_val is None:
                raise FinanceTripletError(f"finance_types.{ft_name} 禁止 null")
            entry = FinanceTypeEntry.from_dict(ft_val)
            for comp in FINANCE_COMPONENTS:
                cost = getattr(entry, comp)
                if cost is not None:
                    _check_cost_units(currency, ft_name, comp, cost)
            finance_types[ft_name] = entry

        ep_raw = mapping["energy_prices"]
        if not isinstance(ep_raw, Mapping):
            raise FinanceTripletError("energy_prices 必须是字典")
        if not ep_raw:
            raise FinanceTripletError("energy_prices 不能为空")
        energy_prices: dict[str, EnergyPrice] = {}
        for price_id, price_val in ep_raw.items():
            price_id = _validate_local_id(price_id, "price_id")
            if price_val is None:
                raise FinanceTripletError(f"energy_prices.{price_id} 禁止 null")
            price = EnergyPrice.from_dict(price_val)
            _check_price_unit(currency, price_id, price)
            energy_prices[price_id] = price

        taxes_raw = mapping.get("taxes") or {}
        if not isinstance(taxes_raw, Mapping):
            raise FinanceTripletError("taxes 必须是字典")
        taxes: dict[str, TaxEntry] = {}
        for tax_id, tax_val in taxes_raw.items():
            tax_id = _validate_local_id(tax_id, "tax id")
            if tax_val is None:
                raise FinanceTripletError(f"taxes.{tax_id} 禁止 null")
            taxes[tax_id] = TaxEntry.from_dict(tax_val)

        return cls(
            schema=schema,
            schema_version=schema_version,
            profile=profile,
            finance_types=finance_types,
            energy_prices=energy_prices,
            taxes=taxes,
        )

    @classmethod
    def validate(cls, mapping: object) -> list[Diagnostic]:
        try:
            cls.from_dict(mapping)
            return []
        except FinanceTripletError as exc:
            return [make_diag(FIN_TRIPLET_INVALID, params={"detail": str(exc)})]

# ---------------------------------------------------------------------------
# FinanceOverrides(项目覆盖, 人工 authoring)
# ---------------------------------------------------------------------------

#: Overrides 允许的 finance_type 叶子键: <分量>.fixed 或 <分量>.linear.<driver>.unit_cost
_OVERRIDE_LEAF_FIELDS: Final[frozenset[str]] = frozenset({"fixed", "linear"})

@dataclass(frozen=True, slots=True)
class FinanceOverrides:
    """项目 FinanceOverrides: 稀疏原子覆盖(引用 Profile 稳定 ID)。

    覆盖子树的原始形状保留在 ``finance_types`` / ``energy_prices`` 中
    (仅按叶子做形状/单位/存在性校验), 由合并器应用。
    """

    schema: str
    schema_version: str
    profile_ref: Mapping[str, str]
    finance_types: Mapping[str, Mapping[str, object]] = field(default_factory=lambda: MappingProxyType({}))
    energy_prices: Mapping[str, Mapping[str, object]] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if self.schema != SCHEMA_OVERRIDES:
            raise FinanceTripletError(f"schema 必须为 {SCHEMA_OVERRIDES!r}")
        if self.schema_version != SCHEMA_VERSION:
            raise FinanceTripletError(f"schema_version 必须为 {SCHEMA_VERSION!r}")
        object.__setattr__(self, "profile_ref", MappingProxyType(dict(sorted(self.profile_ref.items()))))
        object.__setattr__(self, "finance_types", MappingProxyType(dict(sorted(self.finance_types.items()))))
        object.__setattr__(self, "energy_prices", MappingProxyType(dict(sorted(self.energy_prices.items()))))

    def to_dict(self) -> dict:
        d: dict = {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "profile_ref": dict(sorted(self.profile_ref.items())),
        }
        if self.finance_types:
            d["finance_types"] = _freeze_overrides(self.finance_types)
        if self.energy_prices:
            d["energy_prices"] = _freeze_overrides(self.energy_prices)
        return d

    @classmethod
    def empty_for_profile(cls, profile: FinanceProfile) -> FinanceOverrides:
        """生成空 Overrides 文档(不覆盖任何内容)。"""
        mapping = {
            "schema": SCHEMA_OVERRIDES,
            "schema_version": SCHEMA_VERSION,
            "profile_ref": {"id": profile.profile_id},
        }
        return cls.from_dict(mapping)

    @classmethod
    def from_dict(cls, mapping: object, *, profile: FinanceProfile | None = None) -> FinanceOverrides:
        if not isinstance(mapping, Mapping):
            raise FinanceTripletError("FinanceOverrides 必须是字典")
        # 禁改字段(顶层不得出现 Profile 级/税目)
        for forbidden in ("profile", "currency", "base_year", "price_basis", "cost_method", "taxes"):
            if forbidden in mapping:
                raise FinanceTripletError(f"FinanceOverrides 禁止出现字段: {forbidden!r}")
        known_overrides = {"schema", "schema_version", "profile_ref", "finance_types", "energy_prices"}
        unknown = set(mapping) - known_overrides
        if unknown:
            raise FinanceTripletError(f"FinanceOverrides 未知字段: {sorted(unknown)}")
        missing = {"schema", "schema_version", "profile_ref"} - set(mapping)
        if missing:
            raise FinanceTripletError(f"FinanceOverrides 缺少字段: {sorted(missing)}")
        schema = str(mapping["schema"])
        schema_version = str(mapping["schema_version"])
        if schema != SCHEMA_OVERRIDES:
            raise FinanceTripletError(f"schema 非法: {schema!r}")
        if schema_version != SCHEMA_VERSION:
            raise FinanceTripletError(f"schema_version 非法: {schema_version!r}")
        ref_raw = mapping["profile_ref"]
        if not isinstance(ref_raw, Mapping) or set(ref_raw) != {"id"}:
            raise FinanceTripletError("profile_ref 必须为 {id}")
        ref_id = str(ref_raw["id"])
        if not _PROFILE_ID_RE.fullmatch(ref_id) or "__" in ref_id:
            raise FinanceTripletError(f"profile_ref.id 非法: {ref_id!r}")
        profile_ref = {"id": ref_id}
        if profile is not None:
            if profile.profile_id != ref_id:
                raise FinanceTripletError(
                    f"profile_ref 与目标 Profile 不一致(id={ref_id})"
                )

        ft_raw = mapping.get("finance_types") or {}
        if not isinstance(ft_raw, Mapping):
            raise FinanceTripletError("finance_types 必须是字典")
        finance_types: dict[str, Mapping[str, object]] = {}
        for ft_name, ft_val in ft_raw.items():
            ft_name = _validate_local_id(ft_name, "finance_type")
            if profile is not None and ft_name not in profile.finance_types:
                raise FinanceTripletError(f"finance_type {ft_name!r} 在 Profile 中不存在, 禁止新增")
            if not isinstance(ft_val, Mapping) or not ft_val:
                raise FinanceTripletError(f"finance_types.{ft_name} 必须为非空字典")
            finance_types[ft_name] = _validate_ft_override(ft_name, ft_val, profile)

        ep_raw = mapping.get("energy_prices") or {}
        if not isinstance(ep_raw, Mapping):
            raise FinanceTripletError("energy_prices 必须是字典")
        energy_prices: dict[str, Mapping[str, object]] = {}
        for price_id, price_val in ep_raw.items():
            price_id = _validate_local_id(price_id, "price_id")
            if profile is not None and price_id not in profile.energy_prices:
                raise FinanceTripletError(f"price_id {price_id!r} 在 Profile 中不存在, 禁止新增")
            if not isinstance(price_val, Mapping) or not price_val:
                raise FinanceTripletError(f"energy_prices.{price_id} 必须为非空字典")
            energy_prices[price_id] = _validate_price_override(price_id, price_val, profile)

        return cls(
            schema=schema,
            schema_version=schema_version,
            profile_ref=profile_ref,
            finance_types=finance_types,
            energy_prices=energy_prices,
        )

    @classmethod
    def validate(cls, mapping: object, *, profile: FinanceProfile | None = None) -> list[Diagnostic]:
        try:
            cls.from_dict(mapping, profile=profile)
            return []
        except FinanceTripletError as exc:
            return [make_diag(FIN_TRIPLET_INVALID, params={"detail": str(exc)})]

def _freeze_overrides(mapping: Mapping[str, object]) -> dict:
    """覆盖子树原始形状递归转为稳定 dict(合并/摘要使用)。"""
    out: dict = {}
    for k, v in sorted(mapping.items()):
        if isinstance(v, Mapping):
            out[str(k)] = _freeze_overrides(v)
        elif isinstance(v, list):
            out[str(k)] = [_freeze_overrides(x) if isinstance(x, Mapping) else x for x in v]
        else:
            out[str(k)] = v
    return out

def _validate_ft_override(
    ft_name: str,
    ft_val: Mapping[str, object],
    profile: FinanceProfile | None,
) -> Mapping[str, object]:
    """校验单个 finance_type 覆盖: 只允许替换既有叶子(<分量>.fixed / linear.<driver>.unit_cost)。

    - 禁止出现未知分量、fixed/linear 之外的键;
    - 被替换叶子必须已在 Profile 声明(禁止新增 fixed 节点 / driver);
    - 金额完整原子、成本非负; 单位规范化后必须与 Profile 相同。
    """
    out: dict[str, object] = {}
    # Profile 在场时预取 <分量> -> CostComponent(存在性 + 叶子类型化访问)
    prof_comps: Mapping[str, CostComponent] | None = None
    if profile is not None:
        entry = profile.finance_types[ft_name]
        present: dict[str, CostComponent] = {}
        for comp in FINANCE_COMPONENTS:
            cost = getattr(entry, comp)
            if cost is not None:
                present[comp] = cost
        prof_comps = MappingProxyType(present)
    for comp_name, comp_val in ft_val.items():
        comp_name = str(comp_name)
        if comp_name not in FINANCE_COMPONENTS:
            raise FinanceTripletError(f"finance_types.{ft_name} 未知分量: {comp_name!r}")
        if not isinstance(comp_val, Mapping):
            raise FinanceTripletError(f"finance_types.{ft_name}.{comp_name} 必须是字典")
        unknown = set(comp_val) - _OVERRIDE_LEAF_FIELDS
        if unknown:
            raise FinanceTripletError(
                f"finance_types.{ft_name}.{comp_name} 未知字段: {sorted(unknown)}"
            )
        comp_out: dict[str, object] = {}
        prof_cost = prof_comps.get(comp_name) if prof_comps is not None else None
        if prof_cost is None and prof_comps is not None:
            raise FinanceTripletError(
                f"finance_type {ft_name} 分量 {comp_name} 在 Profile 中不存在, 禁止新增"
            )
        if "fixed" in comp_val:
            if comp_val["fixed"] is None:
                raise FinanceTripletError(f"finance_types.{ft_name}.{comp_name}.fixed 禁止 null")
            money = Money.from_dict(comp_val["fixed"], field=f"finance_types.{ft_name}.{comp_name}.fixed")
            if prof_cost is not None:
                prof_fixed = prof_cost.fixed
                if prof_fixed is None:
                    raise FinanceTripletError(
                        f"finance_types.{ft_name}.{comp_name}.fixed 在 Profile 中不存在, 禁止新增"
                    )
                _check_same_unit(money.unit, prof_fixed.unit, f"finance_types.{ft_name}.{comp_name}.fixed")
            comp_out["fixed"] = money.to_dict()
        if "linear" in comp_val:
            raw_linear = comp_val["linear"]
            if not isinstance(raw_linear, Mapping) or not raw_linear:
                raise FinanceTripletError(f"finance_types.{ft_name}.{comp_name}.linear 必须为非空字典")
            linear_out: dict[str, object] = {}
            prof_linear = prof_cost.linear if prof_cost is not None else None
            for driver, drv_val in raw_linear.items():
                driver = _validate_local_id(driver, f"finance_types.{ft_name}.{comp_name}.linear driver")
                if prof_linear is None:
                    if prof_cost is not None:
                        raise FinanceTripletError(
                            f"driver {driver!r} 在 Profile {ft_name}.{comp_name} 中不存在, 禁止新增"
                        )
                elif driver not in prof_linear:
                    raise FinanceTripletError(
                        f"driver {driver!r} 在 Profile {ft_name}.{comp_name} 中不存在, 禁止新增"
                    )
                if not isinstance(drv_val, Mapping) or set(drv_val) != {"unit_cost"}:
                    raise FinanceTripletError(f"linear.{driver} 必须为 {{unit_cost: Money}}")
                if drv_val["unit_cost"] is None:
                    raise FinanceTripletError("unit_cost 禁止 null")
                money = Money.from_dict(
                    drv_val["unit_cost"],
                    field=f"finance_types.{ft_name}.{comp_name}.linear.{driver}.unit_cost",
                )
                if prof_linear is not None:
                    # driver 存在性已在上方保证, 直接取 Profile 单价单位比较
                    _check_same_unit(
                        money.unit, prof_linear[driver].unit_cost.unit,
                        f"finance_types.{ft_name}.{comp_name}.linear.{driver}",
                    )
                linear_out[driver] = {"unit_cost": money.to_dict()}
            comp_out["linear"] = linear_out
        if not comp_out:
            raise FinanceTripletError(f"finance_types.{ft_name}.{comp_name} 必须覆盖至少一个叶子")
        out[comp_name] = comp_out
    return out

def _validate_price_override(
    price_id: str,
    price_val: Mapping[str, object],
    profile: FinanceProfile | None,
) -> Mapping[str, object]:
    """校验单个 energy_price 覆盖: 只写定价定义(kind + 全量字段); carrier/direction 由 Profile 继承。"""
    if "carrier" in price_val or "direction" in price_val:
        raise FinanceTripletError(
            f"energy_prices.{price_id} 禁止出现 carrier/direction, 由 Profile 继承"
        )
    unknown = set(price_val) - {"kind", "value", "ref", "series_meta"}
    if unknown:
        raise FinanceTripletError(f"energy_prices.{price_id} 未知字段: {sorted(unknown)}")
    if "kind" not in price_val:
        raise FinanceTripletError(f"energy_prices.{price_id} 缺少 kind")
    kind = str(price_val["kind"])
    if kind not in KIND_VALUES:
        raise FinanceTripletError(f"energy_prices.{price_id} kind 非法: {kind!r}")
    prof_unit: str | None = None
    if profile is not None:
        prof_price = profile.energy_prices[price_id]
        if prof_price.kind == "constant":
            price_value = prof_price.value
            if price_value is not None:
                prof_unit = price_value.unit
        else:
            price_meta = prof_price.series_meta
            if price_meta is not None:
                prof_unit = price_meta.unit
    if kind == "constant":
        if "value" not in price_val:
            raise FinanceTripletError(f"energy_prices.{price_id} constant 缺少 value")
        if "ref" in price_val or "series_meta" in price_val:
            raise FinanceTripletError(f"energy_prices.{price_id} constant 不允许 ref/series_meta")
        if price_val["value"] is None:
            raise FinanceTripletError(f"energy_prices.{price_id}.value 禁止 null")
        money = Money.from_dict(
            price_val["value"],
            allow_negative=True,
            field=f"energy_prices.{price_id}.value",
        )
        if prof_unit is not None:
            _check_same_unit(money.unit, prof_unit, f"energy_prices.{price_id}")
        return {"kind": "constant", "value": money.to_dict()}
    if "ref" not in price_val or "series_meta" not in price_val:
        raise FinanceTripletError(f"energy_prices.{price_id} time_series 必须含 ref 与 series_meta")
    if "value" in price_val:
        raise FinanceTripletError(f"energy_prices.{price_id} time_series 不允许 value")
    ref = str(price_val["ref"]).strip()
    if not ref:
        raise FinanceTripletError(f"energy_prices.{price_id}.ref 必须为非空字符串")
    meta = SeriesMeta.from_dict(price_val["series_meta"])
    if prof_unit is not None:
        _check_same_unit(meta.unit, prof_unit, f"energy_prices.{price_id}")
    return {"kind": "time_series", "ref": ref, "series_meta": meta.to_dict()}

def _check_same_unit(unit_a: str, unit_b: str, field: str) -> None:
    """覆盖单位的规范化等价校验(与原 Profile 相同; 不要求原拼写相同)。"""
    try:
        if normalize_unit(unit_a) != normalize_unit(unit_b):
            raise FinanceTripletError(
                f"{field} 单位 {unit_a!r} 与 Profile 的 {unit_b!r} 不一致(禁止改单位)"
            )
    except UnitError as exc:
        raise FinanceTripletError(f"{field} 单位无法识别 {unit_a!r}: {exc}") from exc

# ---------------------------------------------------------------------------
# EffectiveFinanceConfig(合并器产物, 不可人工 authoring)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class EffectiveFinanceConfig:
    """不可变 EffectiveFinanceConfig: 装配/规划/财务计算唯一消费的财务快照。

    只能由合并器 ``merge_effective`` 生成。本对象可导出/导入/进入快照。
    文本文件只校验字头与领域约束，不做内容摘要。
    """

    schema: str
    schema_version: str
    profile_id: str
    currency: str
    base_year: int
    price_basis: str
    cost_method: str
    finance_types: Mapping[str, FinanceTypeEntry]
    energy_prices: Mapping[str, EnergyPrice]
    taxes: Mapping[str, TaxEntry] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if self.schema != SCHEMA_EFFECTIVE:
            raise FinanceTripletError(f"schema 必须为 {SCHEMA_EFFECTIVE!r}")
        if self.schema_version != SCHEMA_VERSION:
            raise FinanceTripletError(f"schema_version 必须为 {SCHEMA_VERSION!r}")
        if not _PROFILE_ID_RE.fullmatch(self.profile_id) or "__" in self.profile_id:
            raise FinanceTripletError(f"profile_id 非法: {self.profile_id!r}")
        if self.currency not in CURRENCIES:
            raise FinanceTripletError(f"currency 非法: {self.currency!r}")
        if not 1900 <= self.base_year <= 2999:
            raise FinanceTripletError(f"base_year 超出范围: {self.base_year}")
        if self.price_basis not in PRICE_BASIS_VALUES:
            raise FinanceTripletError(f"price_basis 非法: {self.price_basis!r}")
        if self.cost_method not in COST_METHOD_VALUES:
            raise FinanceTripletError(f"cost_method 非法: {self.cost_method!r}")
        object.__setattr__(self, "finance_types", MappingProxyType(dict(sorted(self.finance_types.items()))))
        object.__setattr__(self, "energy_prices", MappingProxyType(dict(sorted(self.energy_prices.items()))))
        object.__setattr__(self, "taxes", MappingProxyType(dict(sorted(self.taxes.items()))))

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "currency": self.currency,
            "base_year": self.base_year,
            "price_basis": self.price_basis,
            "cost_method": self.cost_method,
            "finance_types": {k: v.to_dict() for k, v in sorted(self.finance_types.items())},
            "energy_prices": {k: v.to_dict() for k, v in sorted(self.energy_prices.items())},
            "taxes": {k: v.to_dict() for k, v in sorted(self.taxes.items())},
        }

    @classmethod
    def from_dict(cls, mapping: object) -> EffectiveFinanceConfig:
        if not isinstance(mapping, Mapping):
            raise FinanceTripletError("EffectiveFinanceConfig 必须是字典")
        unknown = set(mapping) - {
            "schema", "schema_version", "profile_id",
            "currency", "base_year", "price_basis", "cost_method",
            "finance_types", "energy_prices", "taxes",
        }
        if unknown:
            raise FinanceTripletError(f"EffectiveFinanceConfig 未知字段: {sorted(unknown)}")
        missing = {
            "schema", "schema_version", "profile_id",
            "currency", "base_year", "price_basis", "cost_method",
            "finance_types", "energy_prices",
        } - set(mapping)
        if missing:
            raise FinanceTripletError(f"EffectiveFinanceConfig 缺少字段: {sorted(missing)}")
        schema = str(mapping["schema"])
        schema_version = str(mapping["schema_version"])
        if schema != SCHEMA_EFFECTIVE:
            raise FinanceTripletError(f"schema 非法: {schema!r}")
        if schema_version != SCHEMA_VERSION:
            raise FinanceTripletError(f"schema_version 非法: {schema_version!r}")
        profile_id = str(mapping["profile_id"])
        if not _PROFILE_ID_RE.fullmatch(profile_id) or "__" in profile_id:
            raise FinanceTripletError(f"profile_id 非法: {profile_id!r}")
        currency = str(mapping["currency"])
        base_year = mapping["base_year"]
        if not isinstance(base_year, int) or isinstance(base_year, bool):
            raise FinanceTripletError("base_year 必须为整数")
        price_basis = str(mapping["price_basis"])
        cost_method = str(mapping["cost_method"])
        ft_raw = mapping["finance_types"]
        if not isinstance(ft_raw, Mapping):
            raise FinanceTripletError("finance_types 必须是字典")
        finance_types: dict[str, FinanceTypeEntry] = {}
        for ft_name, ft_val in ft_raw.items():
            ft_name = _validate_local_id(ft_name, "finance_type")
            if ft_val is None:
                raise FinanceTripletError(f"finance_types.{ft_name} 禁止 null")
            entry = FinanceTypeEntry.from_dict(ft_val)
            for comp in FINANCE_COMPONENTS:
                cost = getattr(entry, comp)
                if cost is not None:
                    _check_cost_units(currency, ft_name, comp, cost)
            finance_types[ft_name] = entry
        ep_raw = mapping["energy_prices"]
        if not isinstance(ep_raw, Mapping):
            raise FinanceTripletError("energy_prices 必须是字典")
        energy_prices: dict[str, EnergyPrice] = {}
        for price_id, price_val in ep_raw.items():
            price_id = _validate_local_id(price_id, "price_id")
            if price_val is None:
                raise FinanceTripletError(f"energy_prices.{price_id} 禁止 null")
            price = EnergyPrice.from_dict(price_val)
            _check_price_unit(currency, price_id, price)
            energy_prices[price_id] = price
        taxes_raw = mapping.get("taxes") or {}
        if not isinstance(taxes_raw, Mapping):
            raise FinanceTripletError("taxes 必须是字典")
        taxes: dict[str, TaxEntry] = {}
        for tax_id, tax_val in taxes_raw.items():
            tax_id = _validate_local_id(tax_id, "tax id")
            if tax_val is None:
                raise FinanceTripletError(f"taxes.{tax_id} 禁止 null")
            taxes[tax_id] = TaxEntry.from_dict(tax_val)

        return cls(
            schema=schema,
            schema_version=schema_version,
            profile_id=profile_id,
            currency=currency,
            base_year=base_year,
            price_basis=price_basis,
            cost_method=cost_method,
            finance_types=finance_types,
            energy_prices=energy_prices,
            taxes=taxes,
        )

    @classmethod
    def validate(cls, mapping: object) -> list[Diagnostic]:
        try:
            cls.from_dict(mapping)
            return []
        except FinanceTripletError as exc:
            return [make_diag(FIN_TRIPLET_INVALID, params={"detail": str(exc)})]

# ---------------------------------------------------------------------------
# 确定性合并器
# ---------------------------------------------------------------------------

def merge_effective(profile: FinanceProfile, overrides: FinanceOverrides | None) -> EffectiveFinanceConfig:
    """确定性合并 Profile 与 Overrides 生成不可变 EffectiveFinanceConfig。

    - overrides 为 None 时使用空 Overrides 文档(摘要 = 空覆盖摘要);
    - 校验 overrides.profile_ref 与 profile 精确一致(任一不一致拒绝);
    - 稀疏覆盖只替换被覆盖叶子, 未覆盖的 finance_types / energy_prices / taxes
      全部原样保留;
    - 合并结果重新跑完整校验, 任一失败不产生 Effective。
    """
    if overrides is None:
        overrides = FinanceOverrides.empty_for_profile(profile)
    else:
        if overrides.profile_ref["id"] != profile.profile_id:
            raise FinanceTripletError(
                f"Overrides profile_ref.id {overrides.profile_ref['id']!r} "
                f"与 Profile {profile.profile_id!r} 不一致"
            )

    # ---- 合并 finance_types: 逐项深拷贝 Profile 再应用覆盖叶子 ----
    merged_ft: dict[str, FinanceTypeEntry] = {
        name: FinanceTypeEntry.from_dict(entry.to_dict())
        for name, entry in sorted(profile.finance_types.items())
    }
    for ft_name, ft_override in sorted(overrides.finance_types.items()):
        # ft_override: {comp: {fixed: {...} | linear: {driver: {unit_cost: {...}}}}}
        ft_override_dict = _freeze_overrides(ft_override)
        if ft_name not in merged_ft:
            # from_dict 无 profile 校验创建的文档走到这里时拦截, 不产生 Effective
            raise FinanceTripletError(f"finance_type {ft_name!r} 在 Profile 中不存在, 禁止新增")
        base_entry = merged_ft[ft_name].to_dict()
        for comp_name, comp_override in sorted(ft_override_dict.items()):
            base_comp = dict(base_entry.get(comp_name, {}))
            if "fixed" in comp_override:
                base_comp["fixed"] = comp_override["fixed"]
            if "linear" in comp_override:
                linear_raw = comp_override["linear"]
                if not isinstance(linear_raw, Mapping):
                    raise FinanceTripletError(f"finance_types.{ft_name}.{comp_name}.linear 结构非法")
                base_linear = dict(base_comp.get("linear", {}))
                for driver, drv_val in sorted(linear_raw.items()):
                    base_linear[driver] = drv_val
                base_comp["linear"] = base_linear
            base_entry[comp_name] = base_comp
        merged_ft[ft_name] = FinanceTypeEntry.from_dict(base_entry)

    # ---- 合并 energy_prices: 定价定义整项替换, carrier/direction 继承 Profile ----
    merged_ep: dict[str, EnergyPrice] = {}
    for price_id, prof_price in sorted(profile.energy_prices.items()):
        merged_ep[price_id] = prof_price
    for price_id, price_override in sorted(overrides.energy_prices.items()):
        if price_id not in profile.energy_prices:
            # 同上: 无 profile 校验创建的文档拦截, 不产生 Effective
            raise FinanceTripletError(f"price_id {price_id!r} 在 Profile 中不存在, 禁止新增")
        price_override_dict = _freeze_overrides(price_override)
        kind = str(price_override_dict["kind"])
        prof_price = profile.energy_prices[price_id]
        if kind == "constant":
            merged_ep[price_id] = EnergyPrice(
                carrier=prof_price.carrier,
                direction=prof_price.direction,
                kind="constant",
                value=Money.from_dict(price_override_dict["value"], allow_negative=True),
            )
        else:
            merged_ep[price_id] = EnergyPrice(
                carrier=prof_price.carrier,
                direction=prof_price.direction,
                kind="time_series",
                ref=str(price_override_dict["ref"]),
                series_meta=SeriesMeta.from_dict(price_override_dict["series_meta"]),
            )

    # ---- taxes: 税目只属于 Profile, 原样继承 ----
    merged_taxes: dict[str, TaxEntry] = dict(profile.taxes)

    return EffectiveFinanceConfig(
        schema=SCHEMA_EFFECTIVE,
        schema_version=SCHEMA_VERSION,
        profile_id=profile.profile_id,
        currency=profile.currency,
        base_year=profile.base_year,
        price_basis=profile.price_basis,
        cost_method=profile.cost_method,
        finance_types=merged_ft,
        energy_prices=merged_ep,
        taxes=merged_taxes,
    )
