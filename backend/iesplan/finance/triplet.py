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
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation, localcontext
from types import MappingProxyType
from typing import Final

from iesplan.core.diagnostics import Diagnostic, make_diag
from iesplan.core.units import UnitError, normalize_unit
from iesplan.finance.monthly_daily import DAY_POINTS

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
KIND_VALUES: Final[tuple[str, ...]] = ("constant", "time_series", "monthly_daily")
RESOLUTION_VALUES: Final[tuple[str, ...]] = ("15min", "30min", "1h")

#: finance_type 分量(时间口径与量纲见 finance-yaml.md「时间口径与分量」)。
FINANCE_COMPONENTS: Final[tuple[str, ...]] = (
    "upfront_capex",
    "annual_fixed_om",
    "period_variable_om",
)
_DIMLESS_UNIT = "1"

CASHFLOW_DIRECTIONS: Final[tuple[str, ...]] = ("inflow", "outflow")
CASHFLOW_AT_KINDS: Final[tuple[str, ...]] = ("evaluation_end", "financial_year")
CASHFLOW_AMOUNT_KINDS: Final[tuple[str, ...]] = ("fixed", "source_share", "amount_ref")
BASE_FROM_KINDS: Final[tuple[str, ...]] = ("upfront_capex_net", "maintenance_expense_net", "asset")

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
class CostItem:
    """具有独立身份的成本明细；driver 是计量来源，不是费用身份。"""

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
    def from_dict(cls, mapping: object) -> CostItem:
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
class CostComponent:
    """原 fixed/linear 与命名 items 按加法组成同一时间口径分量。"""

    fixed: Money | None = None
    linear: Mapping[str, LinearCost] | None = None
    items: Mapping[str, CostItem] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if self.fixed is None and not self.linear and not self.items:
            raise FinanceTripletError("成本分量必须含 fixed、linear 或非空 items")
        if self.linear is not None:
            object.__setattr__(self, "linear", MappingProxyType(dict(sorted(self.linear.items()))))
        object.__setattr__(self, "items", MappingProxyType(dict(sorted(self.items.items()))))

    def to_dict(self) -> dict:
        out: dict = {}
        if self.fixed is not None:
            out["fixed"] = self.fixed.to_dict()
        if self.linear:
            out["linear"] = {key: value.to_dict() for key, value in self.linear.items()}
        if self.items:
            out["items"] = {key: value.to_dict() for key, value in self.items.items()}
        return out

    @classmethod
    def from_dict(cls, mapping: object) -> CostComponent:
        if not isinstance(mapping, Mapping):
            raise FinanceTripletError("成本分量必须是字典")
        unknown = set(mapping) - {"fixed", "linear", "items"}
        if unknown:
            raise FinanceTripletError(f"成本分量未知字段: {sorted(unknown)}")
        basic = {key: value for key, value in mapping.items() if key != "items"}
        base = CostItem.from_dict(basic) if basic else None
        raw_items = mapping.get("items", {})
        if not isinstance(raw_items, Mapping):
            raise FinanceTripletError("成本分量 items 必须为映射，禁止 null")
        items: dict[str, CostItem] = {}
        for item_id, item in raw_items.items():
            item_id = _validate_local_id(item_id, "items.item_id")
            try:
                items[item_id] = CostItem.from_dict(item)
            except FinanceTripletError as exc:
                raise FinanceTripletError(f"items.{item_id}: {exc}") from exc
        return cls(fixed=base.fixed if base else None,
                   linear=base.linear if base else None, items=items)


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
        # fixed(fixed Money)只允许在 upfront_capex 与 annual_fixed_om; period_variable_om 只有 linear。
        kwargs: dict = {}
        for comp in FINANCE_COMPONENTS:
            if comp not in mapping:
                continue
            if mapping[comp] is None:
                raise FinanceTripletError(f"{comp} 禁止 null")
            if comp == "period_variable_om":
                if isinstance(mapping[comp], Mapping) and "fixed" in mapping[comp]:
                    raise FinanceTripletError(
                        f"{comp} 不允许 fixed(固定金额只属 upfront_capex 或 "
                        f"annual_fixed_om; {comp} 只有 linear)"
                    )
            kwargs[comp] = CostComponent.from_dict(mapping[comp])
            if comp == "period_variable_om" and any(
                item.fixed is not None for item in kwargs[comp].items.values()
            ):
                raise FinanceTripletError(f"{comp}.items 不允许 fixed")
        return cls(**kwargs)

# ---------------------------------------------------------------------------
# 能源价格条目
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SeriesMeta:
    """``time_series`` 的不可变时间元数据(仅自身完整: 分辨率/全期点数/单位)。

    无年份字段: 全期覆盖(点数等于由基线起始年与已确认评价年限推导的
    全周期总点数)在任务提交边界核对, 装配只核对分辨率一致。
    """

    resolution: str
    point_count: int
    unit: str

    def __post_init__(self) -> None:
        if self.resolution not in RESOLUTION_VALUES:
            raise FinanceTripletError(f"series_meta.resolution 非法: {self.resolution!r}")
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
            "point_count": self.point_count,
            "unit": self.unit,
        }

    @classmethod
    def from_dict(cls, mapping: object) -> SeriesMeta:
        if not isinstance(mapping, Mapping):
            raise FinanceTripletError("series_meta 必须是字典")
        unknown = set(mapping) - {"resolution", "point_count", "unit"}
        if unknown:
            raise FinanceTripletError(f"series_meta 未知字段: {sorted(unknown)}")
        missing = {"resolution", "point_count", "unit"} - set(mapping)
        if missing:
            raise FinanceTripletError(f"series_meta 缺少字段: {sorted(missing)}")
        pc = mapping["point_count"]
        if not isinstance(pc, int) or isinstance(pc, bool):
            raise FinanceTripletError("series_meta.point_count 必须为整数")
        return cls(
            resolution=str(mapping["resolution"]),
            point_count=pc,
            unit=_normalized_unit(mapping["unit"], "series_meta.unit"),
        )

@dataclass(frozen=True, slots=True)
class DailyPriceSource:
    """One uploaded complete-day ies.price-series source, reusable across months."""

    ref: str
    series_meta: SeriesMeta

    def __post_init__(self) -> None:
        if not isinstance(self.ref, str) or not self.ref.strip():
            raise FinanceTripletError("monthly_daily.ref 必须为非空字符串")
        # Each source is one complete day, not one operation year or horizon.
        expected = DAY_POINTS[self.series_meta.resolution]
        if self.series_meta.point_count != expected:
            raise FinanceTripletError(
                f"monthly_daily.series_meta.point_count 必须是完整一天 ({expected})"
            )

    def to_dict(self) -> dict:
        return {"ref": self.ref, "series_meta": self.series_meta.to_dict()}

    @classmethod
    def from_dict(cls, mapping: object) -> DailyPriceSource:
        if not isinstance(mapping, Mapping) or set(mapping) != {"ref", "series_meta"}:
            raise FinanceTripletError("monthly_daily 月份条目必须且只能包含 ref/series_meta")
        return cls(
            ref=mapping["ref"],
            series_meta=SeriesMeta.from_dict(mapping["series_meta"]),
        )


def _parse_monthly_sources(value: object) -> Mapping[str, DailyPriceSource]:
    if not isinstance(value, Mapping):
        raise FinanceTripletError("monthly_daily.months 必须为映射")
    expected = {str(m) for m in range(1, 13)}
    missing, extra = expected - set(value), set(value) - expected
    if missing or extra:
        raise FinanceTripletError(
            f"monthly_daily.months 必须恰含字符串月份 1..12, "
            f"missing={sorted(missing)}, extra={sorted(extra)}"
        )
    sources = {
        str(month): DailyPriceSource.from_dict(value[str(month)])
        for month in range(1, 13)
    }
    unit = sources["1"].series_meta.unit
    if any(source.series_meta.unit != unit for source in sources.values()):
        raise FinanceTripletError("monthly_daily 各月份 series_meta.unit 必须一致")
    return MappingProxyType(sources)


@dataclass(frozen=True, slots=True)
class EnergyPrice:
    """Energy tariff: mutually exclusive constant / time_series / monthly_daily."""

    carrier: str
    direction: str
    kind: str
    value: Money | None = None
    ref: str | None = None
    series_meta: SeriesMeta | None = None
    months: Mapping[str, DailyPriceSource] | None = None

    def __post_init__(self) -> None:
        _validate_local_id(self.carrier, "energy_price.carrier")
        if self.direction not in DIRECTION_VALUES:
            raise FinanceTripletError(f"direction 非法: {self.direction!r}")
        if self.kind not in KIND_VALUES:
            raise FinanceTripletError(f"kind 非法: {self.kind!r}")
        if self.kind == "constant":
            if self.value is None or any((self.ref is not None, self.series_meta is not None, self.months is not None)):
                raise FinanceTripletError("constant 必须且只能含 value")
        elif self.kind == "time_series":
            if self.value is not None or self.ref is None or self.series_meta is None or self.months is not None:
                raise FinanceTripletError("time_series 必须且只能含 ref 与 series_meta")
            if not isinstance(self.ref, str) or not self.ref.strip():
                raise FinanceTripletError("energy_price.ref 必须为非空字符串")
        else:
            if self.value is not None or self.ref is not None or self.series_meta is not None or self.months is None:
                raise FinanceTripletError("monthly_daily 必须且只能含 months")
            # Deep-freeze even when constructed directly rather than via from_dict.
            frozen = _parse_monthly_sources({
                month: item.to_dict() if isinstance(item, DailyPriceSource) else item
                for month, item in self.months.items()
            })
            object.__setattr__(self, "months", frozen)

    def to_dict(self) -> dict:
        out: dict = {"carrier": self.carrier, "direction": self.direction, "kind": self.kind}
        if self.kind == "constant":
            out["value"] = self.value.to_dict()
        elif self.kind == "time_series":
            out["ref"] = self.ref
            out["series_meta"] = self.series_meta.to_dict()
        else:
            out["months"] = {month: self.months[month].to_dict() for month in (str(m) for m in range(1, 13))}
        return out

    @classmethod
    def from_dict(cls, mapping: object) -> EnergyPrice:
        if not isinstance(mapping, Mapping):
            raise FinanceTripletError("energy_price 必须是字典")
        unknown = set(mapping) - {"carrier", "direction", "kind", "value", "ref", "series_meta", "months"}
        if unknown:
            raise FinanceTripletError(f"energy_price 未知字段: {sorted(unknown)}")
        missing = {"carrier", "direction", "kind"} - set(mapping)
        if missing:
            raise FinanceTripletError(f"energy_price 缺少字段: {sorted(missing)}")
        kind = str(mapping["kind"])
        if kind not in KIND_VALUES:
            raise FinanceTripletError(f"kind 非法: {kind!r}")
        common = {
            "carrier": _validate_local_id(mapping["carrier"], "carrier"),
            "direction": str(mapping["direction"]),
            "kind": kind,
        }
        if kind == "constant":
            if set(mapping) != {"carrier", "direction", "kind", "value"}:
                raise FinanceTripletError("constant 必须且只能含 value")
            return cls(**common, value=Money.from_dict(mapping["value"], allow_negative=True, field="value"))
        if kind == "time_series":
            if set(mapping) != {"carrier", "direction", "kind", "ref", "series_meta"}:
                raise FinanceTripletError("time_series 必须且只能含 ref 与 series_meta")
            return cls(**common, ref=mapping["ref"], series_meta=SeriesMeta.from_dict(mapping["series_meta"]))
        if set(mapping) != {"carrier", "direction", "kind", "months"}:
            raise FinanceTripletError("monthly_daily 必须且只能含 months")
        return cls(**common, months=_parse_monthly_sources(mapping["months"]))


@dataclass(frozen=True, slots=True)
class DemandCharge:
    """自然月最大购电功率的单位峰值费率，月是计费周期而非时间换算单位。"""

    rate: Money

    def to_dict(self) -> dict:
        return {"rate": self.rate.to_dict()}

    @classmethod
    def from_dict(cls, mapping: object, *, currency: str, charge_id: str) -> DemandCharge:
        field = f"demand_charges.{charge_id}"
        if not isinstance(mapping, Mapping) or set(mapping) != {"rate"}:
            raise FinanceTripletError(f"{field} 必须且只能包含 rate")
        rate = Money.from_dict(mapping["rate"], field=f"{field}.rate")
        if _dims(rate.unit) != {"currency": 1, "power": -1}:
            raise FinanceTripletError(f"{field}.rate 单位必须为货币/功率")
        if rate.unit.split("/")[0] != currency:
            raise FinanceTripletError(f"{field}.rate 币种必须为 {currency}")
        return cls(rate=rate)


def _parse_demand_charges(mapping: object, currency: str) -> dict[str, DemandCharge]:
    if not isinstance(mapping, Mapping):
        raise FinanceTripletError("demand_charges 必须是映射（无需量费时显式为 {}）")
    return {
        _validate_local_id(charge_id, "charge_id"): DemandCharge.from_dict(
            value, currency=currency, charge_id=charge_id
        )
        for charge_id, value in mapping.items()
    }


# ---------------------------------------------------------------------------
# 税目登记
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TaxEntry:
    """``taxes.<id>`` 税目登记(纯展示标签，无税率；计算唯一消费 ``tax_rules``)。"""

    display_name: str
    tax_type: str
    applies_to: str

    def __post_init__(self) -> None:
        if not isinstance(self.display_name, str) or not self.display_name.strip():
            raise FinanceTripletError("tax.display_name 必须为非空字符串")
        if not isinstance(self.tax_type, str) or not self.tax_type.strip():
            raise FinanceTripletError("tax.tax_type 必须为非空字符串")
        if not isinstance(self.applies_to, str) or not self.applies_to.strip():
            raise FinanceTripletError("tax.applies_to 必须为非空字符串")

    def to_dict(self) -> dict:
        return {
            "display_name": self.display_name,
            "tax_type": self.tax_type,
            "applies_to": self.applies_to,
        }

    @classmethod
    def from_dict(cls, mapping: object) -> TaxEntry:
        if not isinstance(mapping, Mapping):
            raise FinanceTripletError("tax 必须是字典")
        unknown = set(mapping) - {"display_name", "tax_type", "applies_to"}
        if unknown:
            raise FinanceTripletError(f"tax 未知字段: {sorted(unknown)}")
        missing = {"display_name", "tax_type", "applies_to"} - set(mapping)
        if missing:
            raise FinanceTripletError(f"tax 缺少字段: {sorted(missing)}")
        return cls(
            display_name=str(mapping["display_name"]),
            tax_type=str(mapping["tax_type"]),
            applies_to=str(mapping["applies_to"]),
        )

# ---------------------------------------------------------------------------
# 抵扣池与税务计算规则(3.0.0; 字段权威 finance-yaml.md「税务计算输入」)
# ---------------------------------------------------------------------------

#: 规则 kind 判别(不接收可执行表达式/脚本/求解器选项)。
TAX_RULE_KINDS: Final[tuple[str, ...]] = ("vat", "construction_input_vat", "income_tax", "derived_tax")
#: 退款未到账策略(仅期末应收)。
RECEIVABLE_POLICIES: Final[tuple[str, ...]] = ("end_receivable",)
#: VAT 结算期(仅按月)。
VAT_SETTLEMENTS: Final[tuple[str, ...]] = ("monthly",)
#: 建设确认方法(无默认，各自只收必要字段)。
CONFIRM_METHODS: Final[tuple[str, ...]] = ("payment_period", "explicit_schedule")
#: 维护所得税处理模式(仅逐项)。
MAINT_MODES: Final[tuple[str, ...]] = ("explicit_items",)
#: 折旧方法(仅直线)。
DEP_METHODS: Final[tuple[str, ...]] = ("straight_line",)


def _ratio_money(mapping: object, field_name: str) -> Money:
    """税率/可抵比例原子: 完整 Money，单位无量纲，值域 0..1。"""
    money = Money.from_dict(mapping, field=field_name)
    if _dims(money.unit):
        raise FinanceTripletError(f"{field_name} unit 必须无量纲(如 {_DIMLESS_UNIT!r})")
    if not Decimal("0") <= money.value <= Decimal("1"):
        raise FinanceTripletError(f"{field_name} 必须在 0..1")
    return money


def _pos_int(value: object, field_name: str) -> int:
    """正整数(拒 bool)。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise FinanceTripletError(f"{field_name} 必须为正整数")
    return value


def _nonneg_int(value: object, field_name: str) -> int:
    """非负整数(拒 bool)。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FinanceTripletError(f"{field_name} 必须为非负整数")
    return value


def _strict_bool(value: object, field_name: str) -> bool:
    """严格布尔(拒 0/1)。"""
    if not isinstance(value, bool):
        raise FinanceTripletError(f"{field_name} 必须为布尔值")
    return value


@dataclass(frozen=True, slots=True)
class PoolRefund:
    """池退款声明: period 确认期扣减，arrival_period 到账(正整数，到账不早于确认)。"""

    period: int
    amount: Money
    arrival_period: int

    def to_dict(self) -> dict:
        return {
            "period": self.period,
            "amount": self.amount.to_dict(),
            "arrival_period": self.arrival_period,
        }

    @classmethod
    def from_dict(cls, mapping: object, *, field: str = "refunds") -> PoolRefund:
        if not isinstance(mapping, Mapping):
            raise FinanceTripletError(f"{field} 条目必须是字典")
        unknown = set(mapping) - {"period", "amount", "arrival_period"}
        if unknown:
            raise FinanceTripletError(f"{field} 未知字段: {sorted(unknown)}")
        missing = {"period", "amount", "arrival_period"} - set(mapping)
        if missing:
            raise FinanceTripletError(f"{field} 缺少字段: {sorted(missing)}")
        period = _pos_int(mapping["period"], f"{field}.period")
        arrival = _pos_int(mapping["arrival_period"], f"{field}.arrival_period")
        if arrival < period:
            raise FinanceTripletError(f"{field}.arrival_period 不得早于 period")
        return cls(
            period=period,
            amount=Money.from_dict(mapping["amount"], field=f"{field}.amount"),
            arrival_period=arrival,
        )


@dataclass(frozen=True, slots=True)
class CreditPool:
    """抵扣池: 期初余额与退款的唯一所有者(币种一致性由文件上下文核对)。"""

    opening_credit: Money
    receivable_policy: str
    refunds: tuple[PoolRefund, ...] = ()

    def to_dict(self) -> dict:
        return {
            "opening_credit": self.opening_credit.to_dict(),
            "receivable_policy": self.receivable_policy,
            "refunds": [item.to_dict() for item in self.refunds],
        }

    @classmethod
    def from_dict(cls, mapping: object, *, field: str = "credit_pools") -> CreditPool:
        if not isinstance(mapping, Mapping):
            raise FinanceTripletError(f"{field} 池必须是字典")
        unknown = set(mapping) - {"opening_credit", "receivable_policy", "refunds"}
        if unknown:
            raise FinanceTripletError(f"{field} 未知字段: {sorted(unknown)}")
        missing = {"opening_credit", "receivable_policy", "refunds"} - set(mapping)
        if missing:
            raise FinanceTripletError(f"{field} 缺少字段: {sorted(missing)}")
        policy = str(mapping["receivable_policy"])
        if policy not in RECEIVABLE_POLICIES:
            raise FinanceTripletError(f"{field}.receivable_policy 非法: {policy!r}")
        raw_refunds = mapping["refunds"]
        if not isinstance(raw_refunds, list):
            raise FinanceTripletError(f"{field}.refunds 必须是列表")
        return cls(
            opening_credit=Money.from_dict(
                mapping["opening_credit"], field=f"{field}.opening_credit"),
            receivable_policy=policy,
            refunds=tuple(
                PoolRefund.from_dict(item, field=f"{field}.refunds") for item in raw_refunds),
        )


@dataclass(frozen=True, slots=True)
class InTaxEntry:
    """购电进项条目: price 引用 purchase 价格，rate/deductible_ratio 为 0..1 原子。"""

    price: str
    rate: Money
    deductible_ratio: Money

    def to_dict(self) -> dict:
        return {
            "price": self.price,
            "rate": self.rate.to_dict(),
            "deductible_ratio": self.deductible_ratio.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class OutTaxEntry:
    """售电销项条目: price 引用 sale 价格。"""

    price: str
    rate: Money

    def to_dict(self) -> dict:
        return {"price": self.price, "rate": self.rate.to_dict()}


@dataclass(frozen=True, slots=True)
class OmInputTaxEntry:
    """非能源运维进项条目: for 为已有分量路径。"""

    for_path: str
    rate: Money
    deductible_ratio: Money

    def to_dict(self) -> dict:
        return {
            "for": self.for_path,
            "rate": self.rate.to_dict(),
            "deductible_ratio": self.deductible_ratio.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class MaintInputTaxEntry:
    """逐项维护进项条目: (year, position) 引用评价维护清单，不复制金额。"""

    year: int
    position: int
    rate: Money
    deductible_ratio: Money

    def to_dict(self) -> dict:
        return {
            "year": self.year,
            "position": self.position,
            "rate": self.rate.to_dict(),
            "deductible_ratio": self.deductible_ratio.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class DemandInputTaxEntry:
    charge: str
    rate: Money
    deductible_ratio: Money

    def to_dict(self) -> dict:
        return {
            "charge": self.charge,
            "rate": self.rate.to_dict(),
            "deductible_ratio": self.deductible_ratio.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class FinancialTimePoint:
    kind: str
    year: int | None = None
    month: int | None = None

    @property
    def axis(self) -> str:
        return self.kind

    @property
    def period(self) -> int:
        return self.year if self.kind == "financial_year" else self.month

    def to_dict(self) -> dict:
        return {"kind": self.kind,
                "year" if self.kind == "financial_year" else "month": self.period}


@dataclass(frozen=True, slots=True)
class MovementRecognition:
    kind: str
    at: FinancialTimePoint | None = None
    axis: str | None = None
    first: int | None = None
    last: int | None = None

    def to_dict(self) -> dict:
        if self.kind == "at":
            return {"kind": self.kind, "at": self.at.to_dict()}
        return {"kind": self.kind, "axis": self.axis, "first": self.first, "last": self.last}


@dataclass(frozen=True, slots=True)
class MovementPayment:
    offset: int
    ratio: Decimal

    def to_dict(self) -> dict:
        return {"offset": self.offset, "ratio": _decimal_to_canonical(self.ratio)}


@dataclass(frozen=True, slots=True)
class CashMovement:
    amount: AmountReference
    price_basis: str
    direction: str
    recognition: MovementRecognition
    payments: tuple[MovementPayment, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payments", tuple(self.payments))

    def to_dict(self) -> dict:
        return {"amount": self.amount.to_dict(), "price_basis": self.price_basis,
                "direction": self.direction, "recognition": self.recognition.to_dict(),
                "payments": [payment.to_dict() for payment in self.payments]}


@dataclass(frozen=True, slots=True)
class CapitalizedAsset:
    basis_from: AmountReference
    capitalized_at: FinancialTimePoint
    available_from: int

    def to_dict(self) -> dict:
        return {"basis_from": self.basis_from.to_dict(), "capitalized_at": self.capitalized_at.to_dict(),
                "available_from": self.available_from}


@dataclass(frozen=True, slots=True)
class MovementVatOccurrence:
    at: FinancialTimePoint
    schedule: tuple[ConstructionScheduleEntry, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "schedule", tuple(self.schedule))

    def to_dict(self) -> dict:
        return {"at": self.at.to_dict(), "schedule": [item.to_dict() for item in self.schedule]}


@dataclass(frozen=True, slots=True)
class MovementConfirmation:
    method: str
    occurrences: tuple[MovementVatOccurrence, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurrences", tuple(self.occurrences))

    def to_dict(self) -> dict:
        out = {"method": self.method}
        if self.method == "explicit_schedule":
            out["occurrences"] = [item.to_dict() for item in self.occurrences]
        return out


@dataclass(frozen=True, slots=True)
class MovementInputTaxEntry:
    movement: str
    rate: Money
    deductible_ratio: Money
    confirmation: MovementConfirmation

    def to_dict(self) -> dict:
        return {"movement": self.movement, "rate": self.rate.to_dict(),
                "deductible_ratio": self.deductible_ratio.to_dict(),
                "confirmation": self.confirmation.to_dict()}


@dataclass(frozen=True, slots=True)
class MovementOutputTaxEntry:
    movement: str
    rate: Money
    confirmation: MovementConfirmation

    def to_dict(self) -> dict:
        return {"movement": self.movement, "rate": self.rate.to_dict(),
                "confirmation": self.confirmation.to_dict()}


@dataclass(frozen=True, slots=True)
class DerivedTaxRule:
    label_ref: str
    axis: str
    base_from: AmountReference
    rate: Money
    payment_lag: int
    kind: str = "derived_tax"

    def to_dict(self) -> dict:
        return {"kind": self.kind, "label_ref": self.label_ref, "axis": self.axis,
                "base_from": self.base_from.to_dict(), "rate": self.rate.to_dict(),
                "payment_lag": self.payment_lag}


@dataclass(frozen=True, slots=True)
class VatRule:
    """``kind: vat`` 规则(按月结算；期初/退款归池所有，本规则不携带)。"""

    label_ref: str
    settlement: str
    in_tax: tuple[InTaxEntry, ...]
    out_tax: tuple[OutTaxEntry, ...]
    om_input_tax: tuple[OmInputTaxEntry, ...]
    maintenance_input_tax: tuple[MaintInputTaxEntry, ...]
    credit_pool: str
    confirmation: str
    demand_input_tax: tuple[DemandInputTaxEntry, ...] = ()
    movement_input_tax: tuple[MovementInputTaxEntry, ...] = ()
    movement_output_tax: tuple[MovementOutputTaxEntry, ...] = ()

    def to_dict(self) -> dict:
        return {
            "kind": "vat",
            "label_ref": self.label_ref,
            "settlement": self.settlement,
            "in_tax": [item.to_dict() for item in self.in_tax],
            "out_tax": [item.to_dict() for item in self.out_tax],
            "om_input_tax": [item.to_dict() for item in self.om_input_tax],
            "maintenance_input_tax": [item.to_dict() for item in self.maintenance_input_tax],
            **({"demand_input_tax": [item.to_dict() for item in self.demand_input_tax]} if self.demand_input_tax else {}),
            **({"movement_input_tax": [item.to_dict() for item in self.movement_input_tax]}
               if self.movement_input_tax else {}),
            **({"movement_output_tax": [item.to_dict() for item in self.movement_output_tax]}
               if self.movement_output_tax else {}),
            "credit_pool": self.credit_pool,
            "confirmation": self.confirmation,
        }


@dataclass(frozen=True, slots=True)
class ConstructionScheduleEntry:
    """显式确认时刻表条目: period 评价月，ratio 十进制字符串(非负，和精确为 1)。"""

    period: int
    ratio: Decimal

    def to_dict(self) -> dict:
        return {"period": self.period, "ratio": _decimal_to_canonical(self.ratio)}


@dataclass(frozen=True, slots=True)
class ConstructionConfirmation:
    """建设确认判别联合: payment_period 不带 schedule；explicit_schedule 带完整时刻表。"""

    method: str
    schedule: tuple[ConstructionScheduleEntry, ...] = ()

    def to_dict(self) -> dict:
        out: dict = {"method": self.method}
        if self.method == "explicit_schedule":
            out["schedule"] = [item.to_dict() for item in self.schedule]
        return out


@dataclass(frozen=True, slots=True)
class ConstructionVatRule:
    """``kind: construction_input_vat`` 规则(金额按选建及容量派生，不填固定税额)。"""

    label_ref: str
    for_path: str
    rate: Money
    deductible_ratio: Money
    credit_pool: str
    confirmation: ConstructionConfirmation

    def to_dict(self) -> dict:
        return {
            "kind": "construction_input_vat",
            "label_ref": self.label_ref,
            "for": self.for_path,
            "rate": self.rate.to_dict(),
            "deductible_ratio": self.deductible_ratio.to_dict(),
            "credit_pool": self.credit_pool,
            "confirmation": self.confirmation.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class MaintItem:
    """逐项维护所得税条目: 仅 (year, position, deductible)，不带 VAT 率。"""

    year: int
    position: int
    deductible: bool

    def to_dict(self) -> dict:
        return {"year": self.year, "position": self.position, "deductible": self.deductible}


@dataclass(frozen=True, slots=True)
class DeductibleMaintenance:
    """维护所得税处理(仅 explicit_items；与进项抵扣独立维度)。"""

    mode: str
    items: tuple[MaintItem, ...] = ()

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class UpfrontCapexNetBaseFrom:
    finance_type: str
    scope_kind: str
    instances: tuple[str, ...] = ()
    kind: str = "upfront_capex_net"

    def __post_init__(self) -> None:
        if self.scope_kind not in ("all_new_instances", "instances"):
            raise FinanceTripletError("upfront_capex_net.scope.kind 非法")
        if self.scope_kind == "all_new_instances" and self.instances:
            raise FinanceTripletError("all_new_instances 不接受实例列表")
        if self.scope_kind == "instances":
            if not self.instances or len(set(self.instances)) != len(self.instances):
                raise FinanceTripletError("scope.instances 必须为非空且无重复的实例集合")
            for instance in self.instances:
                _validate_local_id(instance, "scope.instances")
            object.__setattr__(self, "instances", tuple(sorted(self.instances)))

    def to_dict(self) -> dict:
        scope = {"kind": self.scope_kind}
        if self.scope_kind == "instances":
            scope["instances"] = list(self.instances)
        return {
            "kind": "upfront_capex_net", "finance_type": self.finance_type, "scope": scope
        }

    @property
    def identity(self) -> tuple:
        return ("upfront_capex_net", self.finance_type, self.scope_kind, self.instances)


@dataclass(frozen=True, slots=True)
class MaintenanceExpenseNetBaseFrom:
    year: int
    position: int
    kind: str = "maintenance_expense_net"

    def to_dict(self) -> dict:
        return {"kind": "maintenance_expense_net", "year": self.year, "position": self.position}

    @property
    def identity(self) -> tuple[str, int, int]:
        return ("maintenance_expense_net", self.year, self.position)


@dataclass(frozen=True, slots=True)
class AssetBaseFrom:
    id: str
    kind: str = "asset"

    def to_dict(self) -> dict:
        return {"kind": self.kind, "id": self.id}

    @property
    def identity(self) -> tuple[str, str]:
        return (self.kind, self.id)


AmountBaseFrom = UpfrontCapexNetBaseFrom | MaintenanceExpenseNetBaseFrom | AssetBaseFrom


def _parse_amount_base_from(mapping: object, field: str) -> AmountBaseFrom:
    if not isinstance(mapping, Mapping):
        raise FinanceTripletError(f"{field} 必须是字典")
    kind = mapping.get("kind")
    if kind == "upfront_capex_net":
        _require_keys(mapping, {"kind", "finance_type", "scope"}, field)
        ft = _validate_local_id(mapping["finance_type"], f"{field}.finance_type")
        scope = mapping["scope"]
        if not isinstance(scope, Mapping) or "kind" not in scope:
            raise FinanceTripletError(f"{field}.scope 必须显式声明 kind")
        scope_kind = scope["kind"]
        if scope_kind == "all_new_instances":
            _require_keys(scope, {"kind"}, f"{field}.scope")
            instances = ()
        elif scope_kind == "instances":
            _require_keys(scope, {"kind", "instances"}, f"{field}.scope")
            raw = scope["instances"]
            if not isinstance(raw, list) or not raw:
                raise FinanceTripletError(f"{field}.scope.instances 必须为非空列表")
            if any(not isinstance(x, str) for x in raw):
                raise FinanceTripletError(f"{field}.scope.instances 项必须为实例 ID")
            instances = tuple(raw)
        else:
            raise FinanceTripletError(f"{field}.scope.kind 未知: {scope_kind!r}")
        return UpfrontCapexNetBaseFrom(
            finance_type=ft, scope_kind=scope_kind, instances=instances
        )
    elif kind == "maintenance_expense_net":
        _require_keys(mapping, {"kind", "year", "position"}, field)
        year = _pos_int(mapping["year"], f"{field}.year")
        position = _pos_int(mapping["position"], f"{field}.position")
        return MaintenanceExpenseNetBaseFrom(year=year, position=position)
    elif kind == "asset":
        _require_keys(mapping, {"kind", "id"}, field)
        return AssetBaseFrom(id=_validate_local_id(mapping["id"], f"{field}.id"))
    else:
        raise FinanceTripletError(
            f"{field}.kind 非法: {kind!r} (须为 'upfront_capex_net' 或 'maintenance_expense_net')"
        )


@dataclass(frozen=True, slots=True)
class DepreciationEntry:
    """直线折旧条目: base_from 使用金额来源判别联合，不再另填 asset。"""

    base_from: AmountBaseFrom
    method: str
    life_years: int
    residual: Money
    first_year: int

    def to_dict(self) -> dict:
        return {
            "base_from": self.base_from.to_dict(),
            "method": self.method,
            "life_years": self.life_years,
            "residual": self.residual.to_dict(),
            "first_year": self.first_year,
        }

    @property
    def source_identity(self) -> tuple:
        return self.base_from.identity


@dataclass(frozen=True, slots=True)
class EvaluationEndAt:
    kind: str = "evaluation_end"

    def to_dict(self) -> dict:
        return {"kind": "evaluation_end"}


@dataclass(frozen=True, slots=True)
class FinancialYearAt:
    year: int
    kind: str = "financial_year"

    def to_dict(self) -> dict:
        return {"kind": "financial_year", "year": self.year}


CashflowAt = EvaluationEndAt | FinancialYearAt


@dataclass(frozen=True, slots=True)
class FixedCashflowAmount:
    value: Money
    kind: str = "fixed"

    def to_dict(self) -> dict:
        return {"kind": "fixed", "value": self.value.to_dict()}


@dataclass(frozen=True, slots=True)
class SourceShareCashflowAmount:
    base_from: AmountBaseFrom
    ratio: Money
    kind: str = "source_share"

    def to_dict(self) -> dict:
        return {
            "kind": "source_share",
            "base_from": self.base_from.to_dict(),
            "ratio": self.ratio.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class AmountRefCashflowAmount:
    id: str
    kind: str = "amount_ref"

    def to_dict(self) -> dict:
        return {"kind": self.kind, "id": self.id}


CashflowAmount = FixedCashflowAmount | SourceShareCashflowAmount | AmountRefCashflowAmount


@dataclass(frozen=True, slots=True)
class NetCashflowEntry:
    """显式净现金项目: at(时点), direction(inflow/outflow), amount(固定或来源比例)。"""

    at: CashflowAt
    direction: str
    amount: CashflowAmount

    def to_dict(self) -> dict:
        return {
            "at": self.at.to_dict(),
            "direction": self.direction,
            "amount": self.amount.to_dict(),
        }


def _parse_cashflow_at(mapping: object, field: str) -> CashflowAt:
    if not isinstance(mapping, Mapping):
        raise FinanceTripletError(f"{field} 必须是字典")
    kind = mapping.get("kind")
    if kind == "evaluation_end":
        _require_keys(mapping, {"kind"}, field)
        return EvaluationEndAt()
    elif kind == "financial_year":
        _require_keys(mapping, {"kind", "year"}, field)
        year = _nonneg_int(mapping["year"], f"{field}.year")
        return FinancialYearAt(year=year)
    else:
        raise FinanceTripletError(
            f"{field}.kind 非法: {kind!r} (须为 'evaluation_end' 或 'financial_year')"
        )


def _parse_cashflow_amount(mapping: object, currency: str, field: str) -> CashflowAmount:
    if not isinstance(mapping, Mapping):
        raise FinanceTripletError(f"{field} 必须是字典")
    kind = mapping.get("kind")
    if kind == "fixed":
        _require_keys(mapping, {"kind", "value"}, field)
        val = Money.from_dict(mapping["value"], field=f"{field}.value")
        if val.unit != currency:
            raise FinanceTripletError(f"{field}.value 币种必须为 {currency!r}: {val.unit!r}")
        if val.value < 0:
            raise FinanceTripletError(f"{field}.value 金额禁止负数: {val.value}")
        return FixedCashflowAmount(value=val)
    elif kind == "source_share":
        _require_keys(mapping, {"kind", "base_from", "ratio"}, field)
        ratio = Money.from_dict(mapping["ratio"], field=f"{field}.ratio")
        if ratio.unit != _DIMLESS_UNIT:
            raise FinanceTripletError(f"{field}.ratio 单位必须为 {_DIMLESS_UNIT!r}: {ratio.unit!r}")
        if ratio.value < 0:
            raise FinanceTripletError(f"{field}.ratio 比例禁止负数: {ratio.value}")
        base_from = _parse_amount_base_from(mapping["base_from"], f"{field}.base_from")
        if isinstance(base_from, AssetBaseFrom):
            raise FinanceTripletError(
                f"{field}.base_from source_share只接受初建/维护来源；资产使用amount_ref")
        return SourceShareCashflowAmount(base_from=base_from, ratio=ratio)
    elif kind == "amount_ref":
        _require_keys(mapping, {"kind", "id"}, field)
        return AmountRefCashflowAmount(id=_validate_local_id(mapping["id"], f"{field}.id"))
    else:
        raise FinanceTripletError(
            f"{field}.kind 非法: {kind!r} (须为 'fixed' 或 'source_share')"
        )


def _parse_net_cashflows(mapping: object, currency: str) -> dict[str, NetCashflowEntry]:
    if not isinstance(mapping, Mapping):
        raise FinanceTripletError("net_cashflows 必须是字典（无项目时显式为 {}）")
    out: dict[str, NetCashflowEntry] = {}
    for cf_id, cf_val in mapping.items():
        field = f"net_cashflows.{cf_id}"
        cf_id = _validate_local_id(cf_id, "cashflow_id")
        if not isinstance(cf_val, Mapping):
            raise FinanceTripletError(f"{field} 必须是字典")
        _require_keys(cf_val, {"at", "direction", "amount"}, field)
        direction = str(cf_val["direction"])
        if direction not in CASHFLOW_DIRECTIONS:
            raise FinanceTripletError(f"{field}.direction 非法: {direction!r} (须为 'inflow' 或 'outflow')")
        at = _parse_cashflow_at(cf_val["at"], f"{field}.at")
        amount = _parse_cashflow_amount(cf_val["amount"], currency, f"{field}.amount")
        out[cf_id] = NetCashflowEntry(at=at, direction=direction, amount=amount)
    return out


@dataclass(frozen=True, slots=True)
class OpeningLoss:
    """期初亏损: 金额非负，最后可用评价年正整数。"""

    amount: Money
    last_usable_year: int

    def to_dict(self) -> dict:
        return {
            "amount": self.amount.to_dict(),
            "last_usable_year": self.last_usable_year,
        }


@dataclass(frozen=True, slots=True)
class IncomeTaxRule:
    """``kind: income_tax`` 规则(按年核算；范围列表显式声明，不猜测)。"""

    label_ref: str
    rate: Money
    revenue: tuple[str, ...]
    energy_cost: tuple[str, ...]
    annual_om: tuple[str, ...]
    variable_om: tuple[str, ...]
    deductible_maintenance: DeductibleMaintenance
    depreciation: tuple[DepreciationEntry, ...]
    loss_carryforward_years: int
    opening_losses: tuple[OpeningLoss, ...]
    demand_cost: tuple[str, ...] = ()
    movement_revenue: tuple[str, ...] = ()
    movement_cost: tuple[str, ...] = ()
    derived_tax_cost: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "kind": "income_tax",
            "label_ref": self.label_ref,
            "rate": self.rate.to_dict(),
            "revenue": list(self.revenue),
            "energy_cost": list(self.energy_cost),
            **({"demand_cost": list(self.demand_cost)} if self.demand_cost else {}),
            **({"movement_revenue": list(self.movement_revenue)} if self.movement_revenue else {}),
            **({"movement_cost": list(self.movement_cost)} if self.movement_cost else {}),
            **({"derived_tax_cost": list(self.derived_tax_cost)} if self.derived_tax_cost else {}),
            "annual_om": list(self.annual_om),
            "variable_om": list(self.variable_om),
            "deductible_maintenance": self.deductible_maintenance.to_dict(),
            "depreciation": [item.to_dict() for item in self.depreciation],
            "loss_carryforward_years": self.loss_carryforward_years,
            "opening_losses": [item.to_dict() for item in self.opening_losses],
        }


#: 规则并集类型(文件内规则 ID → 规则对象)。
TaxRule = VatRule | ConstructionVatRule | IncomeTaxRule | DerivedTaxRule


def _parse_tax_rule(rule_id: str, mapping: object) -> TaxRule:
    """规则映射 → 不可变规则(引用存在性与同类单用性由文件后检统一保证，此处只做形状)。"""
    field = f"tax_rules.{rule_id}"
    if not isinstance(mapping, Mapping):
        raise FinanceTripletError(f"{field} 必须是字典")
    kind = mapping.get("kind")
    if kind == "vat":
        return _parse_vat_rule(rule_id, mapping)
    if kind == "construction_input_vat":
        return _parse_construction_rule(rule_id, mapping)
    if kind == "income_tax":
        return _parse_income_rule(rule_id, mapping)
    if kind == "derived_tax":
        return _parse_derived_rule(rule_id, mapping)
    raise FinanceTripletError(f"{field}.kind 非法: {kind!r}")


def _require_keys(mapping: Mapping, keys: set[str], field: str, *, optional: set[str] | None = None) -> None:
    unknown = set(mapping) - keys - (optional or set())
    if unknown:
        raise FinanceTripletError(f"{field} 未知字段: {sorted(unknown)}")
    missing = keys - set(mapping)
    if missing:
        raise FinanceTripletError(f"{field} 缺少字段: {sorted(missing)}")


def _parse_vat_rule(rule_id: str, mapping: Mapping) -> VatRule:
    field = f"tax_rules.{rule_id}"
    _require_keys(
        mapping,
        {"kind", "label_ref", "settlement", "in_tax", "out_tax", "om_input_tax",
         "maintenance_input_tax", "credit_pool", "confirmation"},
        field,
        optional={"demand_input_tax", "movement_input_tax", "movement_output_tax"},
    )
    settlement = str(mapping["settlement"])
    if settlement not in VAT_SETTLEMENTS:
        raise FinanceTripletError(f"{field}.settlement 非法: {settlement!r}")
    confirmation = str(mapping["confirmation"])
    if confirmation != "same_period":
        raise FinanceTripletError(f"{field}.confirmation 非法: {confirmation!r}")
    in_tax: list[InTaxEntry] = []
    for index, entry in enumerate(_as_list(mapping["in_tax"], f"{field}.in_tax")):
        sub = f"{field}.in_tax[{index}]"
        if not isinstance(entry, Mapping):
            raise FinanceTripletError(f"{sub} 必须是字典")
        _require_keys(entry, {"price", "rate", "deductible_ratio"}, sub)
        price = _validate_local_id(entry["price"], f"{sub}.price")
        in_tax.append(InTaxEntry(
            price=price,
            rate=_ratio_money(entry["rate"], f"{sub}.rate"),
            deductible_ratio=_ratio_money(entry["deductible_ratio"], f"{sub}.deductible_ratio"),
        ))
    out_tax: list[OutTaxEntry] = []
    for index, entry in enumerate(_as_list(mapping["out_tax"], f"{field}.out_tax")):
        sub = f"{field}.out_tax[{index}]"
        if not isinstance(entry, Mapping):
            raise FinanceTripletError(f"{sub} 必须是字典")
        _require_keys(entry, {"price", "rate"}, sub)
        price = _validate_local_id(entry["price"], f"{sub}.price")
        out_tax.append(OutTaxEntry(
            price=price, rate=_ratio_money(entry["rate"], f"{sub}.rate")))
    om_input_tax: list[OmInputTaxEntry] = []
    for index, entry in enumerate(_as_list(mapping["om_input_tax"], f"{field}.om_input_tax")):
        sub = f"{field}.om_input_tax[{index}]"
        if not isinstance(entry, Mapping):
            raise FinanceTripletError(f"{sub} 必须是字典")
        _require_keys(entry, {"for", "rate", "deductible_ratio"}, sub)
        for_path = str(entry["for"])
        om_input_tax.append(OmInputTaxEntry(
            for_path=for_path,
            rate=_ratio_money(entry["rate"], f"{sub}.rate"),
            deductible_ratio=_ratio_money(entry["deductible_ratio"], f"{sub}.deductible_ratio"),
        ))
    maintenance_input_tax: list[MaintInputTaxEntry] = []
    for index, entry in enumerate(
            _as_list(mapping["maintenance_input_tax"], f"{field}.maintenance_input_tax")):
        sub = f"{field}.maintenance_input_tax[{index}]"
        if not isinstance(entry, Mapping):
            raise FinanceTripletError(f"{sub} 必须是字典")
        _require_keys(entry, {"year", "position", "rate", "deductible_ratio"}, sub)
        maintenance_input_tax.append(MaintInputTaxEntry(
            year=_pos_int(entry["year"], f"{sub}.year"),
            position=_pos_int(entry["position"], f"{sub}.position"),
            rate=_ratio_money(entry["rate"], f"{sub}.rate"),
            deductible_ratio=_ratio_money(entry["deductible_ratio"], f"{sub}.deductible_ratio"),
        ))
    demand_input_tax: list[DemandInputTaxEntry] = []
    for index, entry in enumerate(_as_list(mapping.get("demand_input_tax", []), f"{field}.demand_input_tax")):
        sub = f"{field}.demand_input_tax[{index}]"
        if not isinstance(entry, Mapping):
            raise FinanceTripletError(f"{sub} 必须是映射")
        _require_keys(entry, {"charge", "rate", "deductible_ratio"}, sub)
        demand_input_tax.append(DemandInputTaxEntry(
            charge=_validate_local_id(entry["charge"], f"{sub}.charge"),
            rate=_ratio_money(entry["rate"], f"{sub}.rate"),
            deductible_ratio=_ratio_money(entry["deductible_ratio"], f"{sub}.deductible_ratio"),
        ))
    return VatRule(
        label_ref=str(mapping["label_ref"]),
        settlement=settlement,
        in_tax=tuple(in_tax),
        out_tax=tuple(out_tax),
        om_input_tax=tuple(om_input_tax),
        maintenance_input_tax=tuple(maintenance_input_tax),
        demand_input_tax=tuple(demand_input_tax),
        movement_input_tax=_parse_movement_tax_entries(
            mapping.get("movement_input_tax", []), field=f"{field}.movement_input_tax", input_tax=True),
        movement_output_tax=_parse_movement_tax_entries(
            mapping.get("movement_output_tax", []), field=f"{field}.movement_output_tax", input_tax=False),
        credit_pool=str(mapping["credit_pool"]),
        confirmation=confirmation,
    )


def _as_list(value: object, field: str) -> list:
    if not isinstance(value, list):
        raise FinanceTripletError(f"{field} 必须是列表")
    return value


def _optional_section(mapping: Mapping, key: str, field: str) -> Mapping:
    """可选节（仅 Overrides 稀疏节）：缺省合法；显式 null 非法；非字典非法。"""
    if key not in mapping:
        return {}
    value = mapping[key]
    if value is None:
        raise FinanceTripletError(f"{field} 显式 null 非法（缺省即合法空表）")
    if not isinstance(value, Mapping):
        raise FinanceTripletError(f"{field} 必须是字典")
    return value


def _require_section(mapping: Mapping, key: str, field: str) -> Mapping:
    """必填节（Profile/Effective 的空表必须显式 {}）：缺省/显式 null/非字典均非法。"""
    if key not in mapping:
        raise FinanceTripletError(f"{field} 缺失（必填，空表请显式写 {{}}）")
    value = mapping[key]
    if value is None:
        raise FinanceTripletError(f"{field} 显式 null 非法")
    if not isinstance(value, Mapping):
        raise FinanceTripletError(f"{field} 必须是字典")
    return value


def _parse_construction_rule(rule_id: str, mapping: Mapping) -> ConstructionVatRule:
    field = f"tax_rules.{rule_id}"
    _require_keys(
        mapping,
        {"kind", "label_ref", "for", "rate", "deductible_ratio", "credit_pool", "confirmation"},
        field,
    )
    raw_confirm = mapping["confirmation"]
    if not isinstance(raw_confirm, Mapping):
        raise FinanceTripletError(f"{field}.confirmation 必须是字典")
    method = raw_confirm.get("method")
    if method not in CONFIRM_METHODS:
        raise FinanceTripletError(f"{field}.confirmation.method 非法: {method!r}")
    schedule: tuple[ConstructionScheduleEntry, ...] = ()
    if method == "payment_period":
        if set(raw_confirm) != {"method"}:
            raise FinanceTripletError(f"{field}.confirmation 只接受所选方法字段")
    else:
        if set(raw_confirm) != {"method", "schedule"}:
            raise FinanceTripletError(f"{field}.confirmation 只接受所选方法字段")
        entries: list[ConstructionScheduleEntry] = []
        for index, entry in enumerate(
                _as_list(raw_confirm["schedule"], f"{field}.confirmation.schedule")):
            sub = f"{field}.confirmation.schedule[{index}]"
            if not isinstance(entry, Mapping):
                raise FinanceTripletError(f"{sub} 必须是字典")
            _require_keys(entry, {"period", "ratio"}, sub)
            ratio = _to_decimal_str(entry["ratio"], f"{sub}.ratio")
            if ratio < 0:
                raise FinanceTripletError(f"{sub}.ratio 必须非负")
            entries.append(ConstructionScheduleEntry(
                period=_pos_int(entry["period"], f"{sub}.period"), ratio=ratio))
        if not entries:
            raise FinanceTripletError(f"{field}.confirmation.schedule 不得为空")
        if sum((item.ratio for item in entries), Decimal("0")) != Decimal("1"):
            raise FinanceTripletError(f"{field}.confirmation.schedule 比例合计必须精确为 1")
        schedule = tuple(entries)
    return ConstructionVatRule(
        label_ref=str(mapping["label_ref"]),
        for_path=str(mapping["for"]),
        rate=_ratio_money(mapping["rate"], f"{field}.rate"),
        deductible_ratio=_ratio_money(mapping["deductible_ratio"], f"{field}.deductible_ratio"),
        credit_pool=str(mapping["credit_pool"]),
        confirmation=ConstructionConfirmation(method=str(method), schedule=schedule),
    )


def _parse_income_rule(rule_id: str, mapping: Mapping) -> IncomeTaxRule:
    field = f"tax_rules.{rule_id}"
    _require_keys(
        mapping,
        {"kind", "label_ref", "rate", "revenue", "energy_cost", "annual_om",
         "variable_om", "deductible_maintenance", "depreciation",
         "loss_carryforward_years", "opening_losses"},
        field,
        optional={"demand_cost", "movement_revenue", "movement_cost", "derived_tax_cost"},
    )
    raw_maint = mapping["deductible_maintenance"]
    if not isinstance(raw_maint, Mapping):
        raise FinanceTripletError(f"{field}.deductible_maintenance 必须是字典")
    _require_keys(raw_maint, {"mode", "items"}, f"{field}.deductible_maintenance")
    if str(raw_maint["mode"]) != "explicit_items":
        raise FinanceTripletError(f"{field}.deductible_maintenance.mode 非法")
    items: list[MaintItem] = []
    for index, entry in enumerate(_as_list(raw_maint["items"], f"{field}.deductible_maintenance.items")):
        sub = f"{field}.deductible_maintenance.items[{index}]"
        if not isinstance(entry, Mapping):
            raise FinanceTripletError(f"{sub} 必须是字典")
        _require_keys(entry, {"year", "position", "deductible"}, sub)
        items.append(MaintItem(
            year=_pos_int(entry["year"], f"{sub}.year"),
            position=_pos_int(entry["position"], f"{sub}.position"),
            deductible=_strict_bool(entry["deductible"], f"{sub}.deductible"),
        ))
    depreciation: list[DepreciationEntry] = []
    for index, entry in enumerate(_as_list(mapping["depreciation"], f"{field}.depreciation")):
        sub = f"{field}.depreciation[{index}]"
        if not isinstance(entry, Mapping):
            raise FinanceTripletError(f"{sub} 必须是字典")
        _require_keys(
            entry, {"base_from", "method", "life_years", "residual", "first_year"}, sub)
        if str(entry["method"]) != "straight_line":
            raise FinanceTripletError(f"{sub}.method 非法: 必须为 'straight_line'")
        base_from = _parse_amount_base_from(entry["base_from"], f"{sub}.base_from")
        depreciation.append(DepreciationEntry(
            base_from=base_from,
            method="straight_line",
            life_years=_pos_int(entry["life_years"], f"{sub}.life_years"),
            residual=Money.from_dict(entry["residual"], field=f"{sub}.residual"),
            first_year=_pos_int(entry["first_year"], f"{sub}.first_year"),
        ))
    opening_losses: list[OpeningLoss] = []
    for index, entry in enumerate(_as_list(mapping["opening_losses"], f"{field}.opening_losses")):
        sub = f"{field}.opening_losses[{index}]"
        if not isinstance(entry, Mapping):
            raise FinanceTripletError(f"{sub} 必须是字典")
        _require_keys(entry, {"amount", "last_usable_year"}, sub)
        opening_losses.append(OpeningLoss(
            amount=Money.from_dict(entry["amount"], field=f"{sub}.amount"),
            last_usable_year=_pos_int(entry["last_usable_year"], f"{sub}.last_usable_year"),
        ))
    return IncomeTaxRule(
        label_ref=str(mapping["label_ref"]),
        rate=_ratio_money(mapping["rate"], f"{field}.rate"),
        revenue=tuple(_as_price_list(mapping["revenue"], f"{field}.revenue")),
        energy_cost=tuple(_as_price_list(mapping["energy_cost"], f"{field}.energy_cost")),
        demand_cost=tuple(_as_price_list(mapping.get("demand_cost", []), f"{field}.demand_cost")),
        movement_revenue=_parse_id_list(mapping.get("movement_revenue", []), f"{field}.movement_revenue"),
        movement_cost=_parse_id_list(mapping.get("movement_cost", []), f"{field}.movement_cost"),
        derived_tax_cost=_parse_id_list(mapping.get("derived_tax_cost", []), f"{field}.derived_tax_cost"),
        annual_om=tuple(_as_path_list(mapping["annual_om"], f"{field}.annual_om")),
        variable_om=tuple(_as_path_list(mapping["variable_om"], f"{field}.variable_om")),
        deductible_maintenance=DeductibleMaintenance(mode="explicit_items", items=tuple(items)),
        depreciation=tuple(depreciation),
        loss_carryforward_years=_nonneg_int(
            mapping["loss_carryforward_years"], f"{field}.loss_carryforward_years"),
        opening_losses=tuple(opening_losses),
    )


def _as_price_list(value: object, field: str) -> list[str]:
    items = _as_list(value, field)
    return [_validate_local_id(item, field) for item in items]


def _as_path_list(value: object, field: str) -> list[str]:
    items = _as_list(value, field)
    out = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise FinanceTripletError(f"{field} 分量路径必须为非空字符串")
        out.append(item)
    return out


def _expand_target_leaves(
    path: str,
    finance_types: Mapping[str, FinanceTypeEntry],
    allowed_components: tuple[str, ...],
    field: str,
) -> tuple[str, set[str]]:
    """将 VAT 引用的成本分量或子项路径展开为其覆盖的全部实际叶子项。

    支持两级 (<ft>.<comp>)、三级 (<ft>.<comp>.fixed) 与四级 (<ft>.<comp>.linear.<driver>)。
    返回 (规范目标路径, 叶子项集合)。
    """
    return path, set(cost_target_leaves(path, finance_types, allowed_components, field=field))


def cost_target_leaves(
    path: str,
    finance_types: Mapping[str, FinanceTypeEntry],
    allowed_components: tuple[str, ...] = FINANCE_COMPONENTS,
    *,
    field: str = "成本引用",
) -> tuple[str, ...]:
    """公开成本范围解析；返回覆盖的真实叶子，保持基础项与命名项身份。"""
    if not isinstance(path, str):
        raise FinanceTripletError(f"{field} 分量路径必须为字符串")
    parts = path.split(".")
    if len(parts) >= 4 and parts[2] == "items":
        ft_name, comp_name, _, item_id, *suffix = parts
        if ft_name not in finance_types or comp_name not in allowed_components:
            raise FinanceTripletError(f"{field} 财务类别或分量不存在: {path!r}")
        comp = getattr(finance_types[ft_name], comp_name)
        if comp is None or item_id not in comp.items:
            raise FinanceTripletError(f"{field} 命名成本不存在: {path!r}")
        item = comp.items[item_id]
        prefix = ".".join(parts[:4])
        leaves = set()
        if item.fixed is not None:
            leaves.add(f"{prefix}.fixed")
        for driver in item.linear or {}:
            leaves.add(f"{prefix}.linear.{driver}")
        if suffix:
            if path not in leaves:
                raise FinanceTripletError(f"{field} 命名成本叶子不存在: {path!r}")
            return (path,)
        return tuple(sorted(leaves))
    if len(parts) == 2:
        ft_name, comp_name = parts
        if ft_name not in finance_types:
            raise FinanceTripletError(f"{field} 引用的 finance_type 不存在: {ft_name!r}")
        if comp_name not in allowed_components:
            raise FinanceTripletError(f"{field} 分量口径不符: {path!r}")
        comp = getattr(finance_types[ft_name], comp_name)
        if comp is None:
            raise FinanceTripletError(f"{field} 引用的成本分量不存在: {path!r}")
        leaves: set[str] = set()
        if comp.fixed is not None:
            leaves.add(f"{ft_name}.{comp_name}.fixed")
        if comp.linear:
            for driver in sorted(comp.linear):
                leaves.add(f"{ft_name}.{comp_name}.linear.{driver}")
        for item_id in comp.items:
            leaves.update(cost_target_leaves(
                f"{path}.items.{item_id}", finance_types, allowed_components, field=field
            ))
        if not leaves:
            raise FinanceTripletError(f"{field} 引用的成本分量无任何子项: {path!r}")
        return tuple(sorted(leaves))
    elif len(parts) == 3 and parts[2] == "fixed":
        ft_name, comp_name, _ = parts
        if ft_name not in finance_types:
            raise FinanceTripletError(f"{field} 引用的 finance_type 不存在: {ft_name!r}")
        if comp_name not in allowed_components:
            raise FinanceTripletError(f"{field} 分量口径不符: {path!r}")
        comp = getattr(finance_types[ft_name], comp_name)
        if comp is None or comp.fixed is None:
            raise FinanceTripletError(f"{field} 引用的 fixed 子项不存在: {path!r}")
        return (path,)
    elif len(parts) == 4 and parts[2] == "linear":
        ft_name, comp_name, _, driver = parts
        if ft_name not in finance_types:
            raise FinanceTripletError(f"{field} 引用的 finance_type 不存在: {ft_name!r}")
        if comp_name not in allowed_components:
            raise FinanceTripletError(f"{field} 分量口径不符: {path!r}")
        comp = getattr(finance_types[ft_name], comp_name)
        if comp is None or comp.linear is None or driver not in comp.linear:
            raise FinanceTripletError(f"{field} 引用的 linear driver 子项不存在: {path!r}")
        return (path,)
    else:
        raise FinanceTripletError(f"{field} 分量路径非法: {path!r}")


def _claim_component_leaves(
    seen_leaves: dict[str, str],
    path: str,
    rule_id: str,
    finance_types: Mapping[str, FinanceTypeEntry],
    allowed_components: tuple[str, ...],
    field: str,
) -> None:
    _, leaves = _expand_target_leaves(path, finance_types, allowed_components, field)
    for leaf in sorted(leaves):
        if leaf in seen_leaves:
            raise FinanceTripletError(
                f"{field} 引用 {path!r} (覆盖 {leaf!r}) 与规则 {seen_leaves[leaf]!r} 重复计算同一税基"
            )
        seen_leaves[leaf] = rule_id


def _check_tax_refs(
    *,
    currency: str,
    energy_prices: Mapping[str, EnergyPrice],
    finance_types: Mapping[str, FinanceTypeEntry],
    demand_charges: Mapping[str, DemandCharge],
    taxes: Mapping[str, TaxEntry],
    credit_pools: Mapping[str, CreditPool],
    tax_rules: Mapping[str, TaxRule],
    net_cashflows: Mapping[str, NetCashflowEntry],
) -> None:
    """文件内税务引用存在性与同类单用性后检(合并器与三件套 from_dict 共用)。"""
    ft_paths: set[str] = set()
    for ft_name, entry in finance_types.items():
        for comp in FINANCE_COMPONENTS:
            if getattr(entry, comp) is not None:
                ft_paths.add(f"{ft_name}.{comp}")
    for pool_id, pool in credit_pools.items():
        if pool.opening_credit.unit != currency:
            raise FinanceTripletError(
                f"credit_pools.{pool_id}.opening_credit 单位须为 Profile 币种 {currency!r}")
        for item in pool.refunds:
            if item.amount.unit != currency:
                raise FinanceTripletError(
                    f"credit_pools.{pool_id}.refunds 金额单位须为 Profile 币种 {currency!r}")
    seen_in: dict[str, str] = {}
    seen_out: dict[str, str] = {}
    seen_om_leaves: dict[str, str] = {}
    seen_con_leaves: dict[str, str] = {}
    seen_income_price: dict[str, str] = {}
    seen_income_om: dict[str, str] = {}
    seen_demand_input: dict[str, str] = {}
    seen_demand_cost: dict[str, str] = {}
    seen_depr_sources: set[tuple] = set()
    for rule_id, rule in tax_rules.items():
        field = f"tax_rules.{rule_id}"
        if rule.label_ref not in taxes:
            raise FinanceTripletError(f"{field}.label_ref 引用的税目不存在: {rule.label_ref!r}")
        pool_ref = getattr(rule, "credit_pool", None)
        if pool_ref is not None and pool_ref not in credit_pools:
            raise FinanceTripletError(f"{field}.credit_pool 引用的池不存在: {pool_ref!r}")
        if isinstance(rule, VatRule):
            for entry in rule.demand_input_tax:
                _claim_demand(seen_demand_input, entry.charge, rule_id, demand_charges, f"{field}.demand_input_tax")
            for entry in rule.in_tax:
                _claim_price(seen_in, entry.price, rule_id, energy_prices, "purchase", f"{field}.in_tax")
            for entry in rule.out_tax:
                _claim_price(seen_out, entry.price, rule_id, energy_prices, "sale", f"{field}.out_tax")
            for entry in rule.om_input_tax:
                _claim_component_leaves(seen_om_leaves, entry.for_path, rule_id, finance_types,
                                        ("annual_fixed_om", "period_variable_om"), f"{field}.om_input_tax")
            seen_maint: set[tuple[int, int]] = set()
            for entry in rule.maintenance_input_tax:
                key = (entry.year, entry.position)
                if key in seen_maint:
                    raise FinanceTripletError(
                        f"{field}.maintenance_input_tax 维护引用重复: {key!r}")
                seen_maint.add(key)
        elif isinstance(rule, ConstructionVatRule):
            _claim_component_leaves(seen_con_leaves, rule.for_path, rule_id, finance_types,
                                    ("upfront_capex",), field)
        elif isinstance(rule, IncomeTaxRule):
            for charge in rule.demand_cost:
                _claim_demand(seen_demand_cost, charge, rule_id, demand_charges, f"{field}.demand_cost")
            for price_id in rule.revenue:
                _claim_price(seen_income_price, price_id, rule_id, energy_prices, "sale",
                             f"{field}.revenue")
            for price_id in rule.energy_cost:
                _claim_price(seen_income_price, price_id, rule_id, energy_prices, "purchase",
                             f"{field}.energy_cost")
            for path in rule.annual_om:
                _claim_component(seen_income_om, path, rule_id, ft_paths, ("annual_fixed_om",), field)
            for path in rule.variable_om:
                _claim_component(seen_income_om, path, rule_id, ft_paths, ("period_variable_om",), field)
            seen_maint_items: set[tuple[int, int]] = set()
            for item in rule.deductible_maintenance.items:
                key = (item.year, item.position)
                if key in seen_maint_items:
                    raise FinanceTripletError(
                        f"{field}.deductible_maintenance 维护引用重复: {key!r}")
                seen_maint_items.add(key)
            for item in rule.depreciation:
                ident = item.source_identity
                if ident in seen_depr_sources:
                    raise FinanceTripletError(f"{field}.depreciation 来源身份重复: {ident!r}")
                seen_depr_sources.add(ident)
                if isinstance(item.base_from, UpfrontCapexNetBaseFrom):
                    ft = item.base_from.finance_type
                    if ft not in finance_types:
                        raise FinanceTripletError(
                            f"{field}.depreciation.base_from.finance_type 不存在: {ft!r}")
                    if finance_types[ft].upfront_capex is None:
                        raise FinanceTripletError(
                            f"{field}.depreciation.base_from 类别 {ft!r} 缺少 upfront_capex")
                if item.residual.unit == currency:
                    if item.residual.value < 0:
                        raise FinanceTripletError(f"{field}.depreciation.residual 金额禁止负数")
                elif item.residual.unit == _DIMLESS_UNIT:
                    if not Decimal("0") <= item.residual.value <= Decimal("1"):
                        raise FinanceTripletError(f"{field}.depreciation.residual 比例须在 0..1")
                else:
                    raise FinanceTripletError(
                        f"{field}.depreciation.residual 单位须为 Profile 币种 {currency!r} 或无量纲比例 '1'"
                    )
    for cf_id, cf in net_cashflows.items():
        if isinstance(cf.amount, SourceShareCashflowAmount):
            if isinstance(cf.amount.base_from, UpfrontCapexNetBaseFrom):
                ft = cf.amount.base_from.finance_type
                if ft not in finance_types:
                    raise FinanceTripletError(
                        f"net_cashflows.{cf_id}.amount.base_from.finance_type 不存在: {ft!r}")
                if finance_types[ft].upfront_capex is None:
                    raise FinanceTripletError(
                        f"net_cashflows.{cf_id}.amount.base_from 类别 {ft!r} 缺少 upfront_capex")


def _claim_demand(
    seen: dict[str, str], charge: str, rule_id: str,
    charges: Mapping[str, DemandCharge], field: str,
) -> None:
    if charge not in charges:
        raise FinanceTripletError(f"{field} 引用未知需量费项: {charge!r}")
    if charge in seen:
        raise FinanceTripletError(f"{field} 需量费项 {charge!r} 已被规则 {seen[charge]!r} 使用")
    seen[charge] = rule_id


def _claim_price(
    seen: dict[str, str],
    price_id: str,
    rule_id: str,
    energy_prices: Mapping[str, EnergyPrice],
    direction: str,
    field: str,
) -> None:
    price = energy_prices.get(price_id)
    if price is None:
        raise FinanceTripletError(f"{field} 引用的价格不存在: {price_id!r}")
    if price.direction != direction:
        raise FinanceTripletError(f"{field} 引用的价格方向须为 {direction}: {price_id!r}")
    if price_id in seen:
        raise FinanceTripletError(f"{field} 与规则 {seen[price_id]!r} 重复计算同一税基: {price_id!r}")
    seen[price_id] = rule_id


def _claim_component(
    seen: dict[str, str],
    path: str,
    rule_id: str,
    ft_paths: set[str],
    components: tuple[str, ...],
    field: str,
) -> None:
    if path not in ft_paths:
        raise FinanceTripletError(f"{field} 引用的分量路径不存在: {path!r}")
    if not any(path.endswith(f".{comp}") for comp in components):
        raise FinanceTripletError(f"{field} 分量口径不符: {path!r}")
    if path in seen:
        raise FinanceTripletError(f"{field} 与规则 {seen[path]!r} 重复计算同一税基: {path!r}")
    seen[path] = rule_id


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
    if cost.fixed is not None:
        if component == "upfront_capex":
            if cost.fixed.unit != currency:
                raise FinanceTripletError(
                    f"{finance_type}.upfront_capex.fixed 单位必须为 Profile 币种 {currency!r}, "
                    f"实际 {cost.fixed.unit!r}"
                )
        elif component == "annual_fixed_om":
            dims = _dims(cost.fixed.unit)
            if dims != {"currency": 1, "time": -1}:
                raise FinanceTripletError(
                    f"{finance_type}.annual_fixed_om.fixed 单位必须为本文件币种/年(如 {currency}/a), "
                    f"不接受纯币种金额冒充年率: {cost.fixed.unit!r}"
                )
            currency_prefix = f"{currency}/"
            if not cost.fixed.unit.startswith(currency_prefix):
                raise FinanceTripletError(
                    f"{finance_type}.annual_fixed_om.fixed 币种前缀必须为 {currency!r}: {cost.fixed.unit!r}"
                )
    if cost.linear is not None:
        currency_prefix = f"{currency}/"
        for driver, lin in cost.linear.items():
            unit = lin.unit_cost.unit
            if not unit.startswith(currency_prefix):
                raise FinanceTripletError(
                    f"{finance_type}.{component}.linear.{driver} unit_cost 单位 {unit!r} "
                    f"必须以 {currency_prefix} 开头(币种前缀)"
                )
    for item_id, item in cost.items.items():
        _check_cost_units(currency, f"{finance_type}.items.{item_id}", component,
                          CostComponent(fixed=item.fixed, linear=item.linear))

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
    elif price.kind == "time_series":
        meta = price.series_meta
        if meta is None:
            raise FinanceTripletError(f"energy_price {price_id} 缺 series_meta(内部状态非法)")
        unit = meta.unit
        if not unit.startswith(currency_prefix):
            raise FinanceTripletError(
                f"energy_price {price_id} series_meta.unit {unit!r} 必须以 {currency_prefix} 开头"
            )
    else:
        for month, source in price.months.items():
            unit = source.series_meta.unit
            if not unit.startswith(currency_prefix):
                raise FinanceTripletError(
                    f"energy_price {price_id} months.{month}.series_meta.unit "
                    f"{unit!r} 必须以 {currency_prefix} 开头"
                )

# ---------------------------------------------------------------------------
# 命名仿射金额与明确期间引用（finance-relations.md）
# ---------------------------------------------------------------------------

AMOUNT_AXES: Final[tuple[str, ...]] = ("scalar", "financial_year", "tax_month")
AMOUNT_BASES: Final[tuple[str, ...]] = (
    "raw", "gross", "deductible_input", "net_basis", "net_revenue",
)


@dataclass(frozen=True, slots=True)
class AmountPeriod:
    kind: str
    year: int | str | None = None
    month: int | None = None
    axis: str | None = None
    first: int | None = None
    last: int | None = None

    def to_dict(self) -> dict:
        return {key: value for key, value in (
            ("kind", self.kind), ("year", self.year), ("month", self.month),
            ("axis", self.axis), ("first", self.first), ("last", self.last),
        ) if value is not None}


def parse_amount_period(mapping: object, *, field: str = "period") -> AmountPeriod:
    """公开输入解析器；项目 E 范围在取得评价上下文的公开边界绑定。"""
    if not isinstance(mapping, Mapping):
        raise FinanceTripletError(f"{field} 必须为显式期间映射")
    kind = mapping.get("kind")
    if kind in ("scalar", "current"):
        _require_keys(mapping, {"kind"}, field)
        return AmountPeriod(kind=kind)
    if kind == "financial_year":
        _require_keys(mapping, {"kind", "year"}, field)
        return AmountPeriod(kind=kind, year=_nonneg_int(mapping["year"], f"{field}.year"))
    if kind == "tax_month":
        _require_keys(mapping, {"kind", "month"}, field)
        return AmountPeriod(kind=kind, month=_pos_int(mapping["month"], f"{field}.month"))
    if kind == "year_months":
        _require_keys(mapping, {"kind", "year"}, field)
        year = mapping["year"]
        if year != "current":
            year = _pos_int(year, f"{field}.year")
        return AmountPeriod(kind=kind, year=year)
    if kind == "range":
        _require_keys(mapping, {"kind", "axis", "first", "last"}, field)
        axis = mapping["axis"]
        if axis not in ("financial_year", "tax_month"):
            raise FinanceTripletError(f"{field}.axis 必须为 financial_year/tax_month")
        first = _pos_int(mapping["first"], f"{field}.first")
        last = _pos_int(mapping["last"], f"{field}.last")
        if first > last:
            raise FinanceTripletError(f"{field}.first 不得晚于 last")
        return AmountPeriod(kind=kind, axis=axis, first=first, last=last)
    raise FinanceTripletError(f"{field}.kind 非法: {kind!r}")


@dataclass(frozen=True, slots=True)
class ReferenceScope:
    kind: str
    instances: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        if self.kind == "instances":
            return {"kind": self.kind, "instances": list(self.instances)}
        return {"kind": self.kind}


def _reference_id(value: object, field: str) -> str:
    if not isinstance(value, str) or not _PROFILE_ID_RE.fullmatch(value) or "__" in value:
        raise FinanceTripletError(f"{field} 必须为明确稳定标识: {value!r}")
    return value


def _reference_ids(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise FinanceTripletError(f"{field} 必须为非空标识列表")
    out = tuple(_reference_id(item, field) for item in value)
    if len(set(out)) != len(out):
        raise FinanceTripletError(f"{field} 不允许重复标识")
    return tuple(sorted(out))


def _parse_reference_scope(mapping: object, field: str) -> ReferenceScope:
    if not isinstance(mapping, Mapping):
        raise FinanceTripletError(f"{field} 必须为明确实例范围")
    kind = mapping.get("kind")
    if kind == "all_new_instances":
        _require_keys(mapping, {"kind"}, field)
        return ReferenceScope(kind=kind)
    if kind == "instances":
        _require_keys(mapping, {"kind", "instances"}, field)
        return ReferenceScope(kind=kind, instances=_reference_ids(mapping["instances"], field))
    raise FinanceTripletError(f"{field}.kind 非法: {kind!r}")


@dataclass(frozen=True, slots=True)
class TaxReferenceSource:
    kind: str
    id: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "id": self.id}


@dataclass(frozen=True, slots=True)
class AmountReference:
    """结构明确的引用联合；每个 kind 的合法字段由公开解析器限定。"""

    kind: str
    period: AmountPeriod | None = None
    finance_type: str | None = None
    driver: str | None = None
    scope: ReferenceScope | None = None
    path: str | None = None
    basis: str | None = None
    price: str | None = None
    charge: str | None = None
    bindings: tuple[str, ...] | None = None
    year: int | None = None
    position: int | None = None
    id: str | None = None
    block: str | None = None
    variable: str | None = None
    aggregation: str | None = None
    unit_ref: str | None = None
    measure: str | None = None
    source: TaxReferenceSource | None = None

    def to_dict(self) -> dict:
        out: dict = {"kind": self.kind}
        for key in (
            "period", "finance_type", "driver", "scope", "path", "basis", "price", "charge",
            "bindings", "year", "position", "id", "block", "variable", "aggregation",
            "unit_ref", "measure", "source",
        ):
            value = getattr(self, key)
            if value is not None:
                out[key] = (value.to_dict() if hasattr(value, "to_dict")
                            else list(value) if isinstance(value, tuple) else value)
        return out


_AMOUNT_REFERENCE_FIELDS: Final[Mapping[str, frozenset[str]]] = MappingProxyType({
    "driver": frozenset({"finance_type", "driver", "scope", "period"}),
    "system_quantity": frozenset({"block", "variable", "aggregation", "period"}),
    "construction_unit": frozenset({"unit_ref", "period"}),
    "cost": frozenset({"path", "scope", "basis", "period"}),
    "tariff": frozenset({"price", "bindings", "basis", "period"}),
    "demand": frozenset({"charge", "bindings", "basis", "period"}),
    "maintenance": frozenset({"year", "position", "basis"}),
    "amount": frozenset({"id", "period"}),
    "movement": frozenset({"id", "basis", "period"}),
    "asset": frozenset({"id", "measure", "period"}),
    "tax": frozenset({"source", "measure", "period"}),
    "baseline": frozenset({"id", "period"}),
})


def parse_amount_reference(mapping: object, *, field: str = "source") -> AmountReference:
    """解析完整引用形状；文件内实体和轴关系在金额目录公开输入边界核对。"""
    if not isinstance(mapping, Mapping):
        raise FinanceTripletError(f"{field} 必须为来源引用映射")
    kind = mapping.get("kind")
    if not isinstance(kind, str) or kind not in _AMOUNT_REFERENCE_FIELDS:
        raise FinanceTripletError(f"{field}.kind 非法: {kind!r}")
    _require_keys(mapping, {"kind", *_AMOUNT_REFERENCE_FIELDS[kind]}, field)
    values: dict = {"kind": kind}
    for key in sorted(_AMOUNT_REFERENCE_FIELDS[kind]):
        raw = mapping[key]
        if key == "period":
            values[key] = parse_amount_period(raw, field=f"{field}.period")
        elif key == "scope":
            values[key] = _parse_reference_scope(raw, f"{field}.scope")
        elif key == "bindings":
            values[key] = _reference_ids(raw, f"{field}.bindings")
        elif key in ("year", "position"):
            values[key] = _pos_int(raw, f"{field}.{key}")
        elif key == "basis":
            if raw not in AMOUNT_BASES:
                raise FinanceTripletError(f"{field}.basis 非法: {raw!r}")
            if kind in ("cost", "demand", "maintenance") and raw == "net_revenue":
                raise FinanceTripletError(f"{field}.basis 支出来源不允许 net_revenue")
            values[key] = raw
        elif key == "aggregation":
            if raw not in ("integrate_positive", "sum_positive"):
                raise FinanceTripletError(f"{field}.aggregation 必须为明确非负聚合")
            values[key] = raw
        elif key == "source":
            if not isinstance(raw, Mapping):
                raise FinanceTripletError(f"{field}.source 必须为税额身份")
            _require_keys(raw, {"kind", "id"}, f"{field}.source")
            if raw["kind"] not in ("rule", "pool"):
                raise FinanceTripletError(f"{field}.source.kind 必须为 rule/pool")
            values[key] = TaxReferenceSource(
                kind=raw["kind"], id=_validate_local_id(raw["id"], f"{field}.source.id")
            )
        elif key == "path":
            if not isinstance(raw, str) or not raw:
                raise FinanceTripletError(f"{field}.path 必须为明确成本路径")
            values[key] = raw
        elif kind == "system_quantity" and key in ("block", "variable"):
            # Foreign identities belong to the system-equation contract, not
            # finance's local-ID namespace. The project boundary binds them
            # to the actual accepted block and variable (including legal __).
            if not isinstance(raw, str) or not raw:
                raise FinanceTripletError(f"{field}.{key} 必须为明确系统量标识")
            values[key] = raw
        else:
            values[key] = _reference_id(raw, f"{field}.{key}")
    ref = AmountReference(**values)
    if ref.kind == "construction_unit" and ref.period.kind != "scalar":
        raise FinanceTripletError(f"{field}.period 建设状态只接受 scalar")
    if ref.kind == "asset":
        if ref.measure not in ("basis", "depreciation", "book_value"):
            raise FinanceTripletError(f"{field}.measure 非法资产量")
        if ref.measure == "basis" and ref.period.kind != "scalar":
            raise FinanceTripletError(f"{field}.period 资产基础只接受 scalar")
    return ref


@dataclass(frozen=True, slots=True)
class AmountTerm:
    coefficient: Money
    source: AmountReference

    def to_dict(self) -> dict:
        return {"coefficient": self.coefficient.to_dict(), "source": self.source.to_dict()}


@dataclass(frozen=True, slots=True)
class AmountDefinition:
    unit: str
    axis: str
    constant: Money
    terms: Mapping[str, AmountTerm]

    def __post_init__(self) -> None:
        object.__setattr__(self, "terms", MappingProxyType(dict(sorted(self.terms.items()))))

    def to_dict(self) -> dict:
        return {"unit": self.unit, "axis": self.axis, "constant": self.constant.to_dict(),
                "terms": {key: value.to_dict() for key, value in self.terms.items()}}

    @classmethod
    def from_dict(cls, mapping: object, *, currency: str, field: str) -> AmountDefinition:
        if not isinstance(mapping, Mapping):
            raise FinanceTripletError(f"{field} 必须为金额定义映射")
        _require_keys(mapping, {"unit", "axis", "constant", "terms"}, field)
        unit = _normalized_unit(mapping["unit"], f"{field}.unit")
        if not (unit == currency or unit.startswith(f"{currency}/")):
            raise FinanceTripletError(f"{field}.unit 必须使用文件币种 {currency}")
        if _dims(unit).get("currency") != 1:
            raise FinanceTripletError(f"{field}.unit 必须为金额或明确金额费率")
        axis = mapping["axis"]
        if axis not in AMOUNT_AXES:
            raise FinanceTripletError(f"{field}.axis 非法: {axis!r}")
        constant = Money.from_dict(mapping["constant"], allow_negative=True, field=f"{field}.constant")
        _check_same_unit(constant.unit, unit, f"{field}.constant")
        if not isinstance(mapping["terms"], Mapping):
            raise FinanceTripletError(f"{field}.terms 必须为映射")
        terms: dict[str, AmountTerm] = {}
        for term_id, raw in mapping["terms"].items():
            term_id = _validate_local_id(term_id, f"{field}.terms.term_id")
            sub = f"{field}.terms.{term_id}"
            if not isinstance(raw, Mapping):
                raise FinanceTripletError(f"{sub} 必须为 coefficient/source 映射")
            _require_keys(raw, {"coefficient", "source"}, sub)
            source = parse_amount_reference(raw["source"], field=f"{sub}.source")
            period = source.period
            if period is not None:
                if period.kind == "current" and axis == "scalar":
                    raise FinanceTripletError(f"{sub}.source.period scalar 定义不允许 current")
                if period.kind == "year_months" and period.year == "current" and axis != "financial_year":
                    raise FinanceTripletError(f"{sub}.source.period.year current 只用于 financial_year 定义")
                if (period.kind == "financial_year" and period.year == 0
                        and source.kind not in ("movement", "baseline")):
                    raise FinanceTripletError(f"{sub}.source.period.year 该来源不接受时点0")
            terms[term_id] = AmountTerm(
                coefficient=Money.from_dict(
                    raw["coefficient"], allow_negative=True, field=f"{sub}.coefficient"
                ),
                source=source,
            )
        return cls(unit=unit, axis=axis, constant=constant, terms=terms)


def parse_financial_time_point(mapping: object, *, field: str = "at") -> FinancialTimePoint:
    if not isinstance(mapping, Mapping):
        raise FinanceTripletError(f"{field} 必须为明确年/月时点")
    kind = mapping.get("kind")
    if kind == "financial_year":
        _require_keys(mapping, {"kind", "year"}, field)
        return FinancialTimePoint(kind, year=_nonneg_int(mapping["year"], f"{field}.year"))
    if kind == "tax_month":
        _require_keys(mapping, {"kind", "month"}, field)
        return FinancialTimePoint(kind, month=_pos_int(mapping["month"], f"{field}.month"))
    raise FinanceTripletError(f"{field}.kind 必须为 financial_year/tax_month")


def _parse_recognition(mapping: object, field: str) -> MovementRecognition:
    if not isinstance(mapping, Mapping):
        raise FinanceTripletError(f"{field} 必须为明确发生安排")
    kind = mapping.get("kind")
    if kind == "at":
        _require_keys(mapping, {"kind", "at"}, field)
        return MovementRecognition(
            kind=kind, at=parse_financial_time_point(mapping["at"], field=f"{field}.at"))
    if kind == "each":
        _require_keys(mapping, {"kind", "axis", "first", "last"}, field)
        if mapping["axis"] not in ("financial_year", "tax_month"):
            raise FinanceTripletError(f"{field}.axis 必须为 financial_year/tax_month")
        first, last = _pos_int(mapping["first"], f"{field}.first"), _pos_int(mapping["last"], f"{field}.last")
        if first > last:
            raise FinanceTripletError(f"{field}.first 不得晚于 last")
        return MovementRecognition(kind=kind, axis=mapping["axis"], first=first, last=last)
    raise FinanceTripletError(f"{field}.kind 必须为 at/each")


def _parse_payments(value: object, field: str) -> tuple[MovementPayment, ...]:
    rows = _as_list(value, field)
    if not rows:
        raise FinanceTripletError(f"{field} 必须明确至少一笔付款")
    out = []
    for index, row in enumerate(rows):
        sub = f"{field}[{index}]"
        if not isinstance(row, Mapping):
            raise FinanceTripletError(f"{sub} 必须为 offset/ratio")
        _require_keys(row, {"offset", "ratio"}, sub)
        offset = row["offset"]
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise FinanceTripletError(f"{sub}.offset 必须为整数期间")
        ratio = _to_decimal_str(row["ratio"], f"{sub}.ratio")
        if ratio < 0:
            raise FinanceTripletError(f"{sub}.ratio 必须非负")
        out.append(MovementPayment(offset, ratio))
    if not _ratios_total_one(tuple(row.ratio for row in out)):
        raise FinanceTripletError(f"{field} 每笔发生项的付款比例必须精确合计1")
    return tuple(out)


def _ratios_total_one(ratios: tuple[Decimal, ...]) -> bool:
    with localcontext() as ctx:
        ctx.prec = MAX_DIGITS + len(str(len(ratios))) + 1
        return sum(ratios, Decimal("0")) == Decimal("1")


def cash_movement_occurrences(movement: CashMovement) -> tuple[FinancialTimePoint, ...]:
    """发生身份不以付款时点或票据时点替代；不读取项目或当前草稿。"""
    recognition = movement.recognition
    if recognition.kind == "at":
        return (recognition.at,)
    return tuple(FinancialTimePoint(recognition.axis, **{
        "year" if recognition.axis == "financial_year" else "month": value,
    }) for value in range(recognition.first, recognition.last + 1))


def expand_amount_period(
    period: AmountPeriod, current_axis: str, current_period: int,
) -> tuple[str, tuple[int, ...]]:
    """统一展开已绑定期间；调用边界已经核对轴和评价范围。"""
    if period.kind == "scalar":
        return "scalar", (0,)
    if period.kind == "current":
        return current_axis, (current_period,)
    if period.kind == "financial_year":
        return "financial_year", (period.year,)
    if period.kind == "tax_month":
        return "tax_month", (period.month,)
    if period.kind == "year_months":
        year = current_period if period.year == "current" else period.year
        return "tax_month", tuple(range(12 * (year - 1) + 1, 12 * year + 1))
    return period.axis, tuple(range(period.first, period.last + 1))


def _parse_movements(mapping: object) -> Mapping[str, CashMovement]:
    if not isinstance(mapping, Mapping):
        raise FinanceTripletError("cash_movements 必须为映射，禁止 null")
    out = {}
    for movement_id, raw in mapping.items():
        movement_id = _validate_local_id(movement_id, "cash_movements.id")
        field = f"cash_movements.{movement_id}"
        if not isinstance(raw, Mapping):
            raise FinanceTripletError(f"{field} 必须为发生项映射")
        _require_keys(raw, {"amount", "price_basis", "direction", "recognition", "payments"}, field)
        amount = parse_amount_reference(raw["amount"], field=f"{field}.amount")
        if amount.kind != "amount":
            raise FinanceTripletError(f"{field}.amount 只能引用 kind: amount")
        if raw["price_basis"] not in PRICE_BASIS_VALUES or raw["direction"] not in CASHFLOW_DIRECTIONS:
            raise FinanceTripletError(f"{field} price_basis/direction 非法")
        out[movement_id] = CashMovement(
            amount, raw["price_basis"], raw["direction"],
            _parse_recognition(raw["recognition"], f"{field}.recognition"),
            _parse_payments(raw["payments"], f"{field}.payments"),
        )
    return MappingProxyType(dict(sorted(out.items())))


def _parse_assets(mapping: object) -> Mapping[str, CapitalizedAsset]:
    if not isinstance(mapping, Mapping):
        raise FinanceTripletError("assets 必须为映射，禁止 null")
    out = {}
    for asset_id, raw in mapping.items():
        asset_id = _validate_local_id(asset_id, "assets.id")
        field = f"assets.{asset_id}"
        if not isinstance(raw, Mapping):
            raise FinanceTripletError(f"{field} 必须为资产关系映射")
        _require_keys(raw, {"basis_from", "capitalized_at", "available_from"}, field)
        out[asset_id] = CapitalizedAsset(
            parse_amount_reference(raw["basis_from"], field=f"{field}.basis_from"),
            parse_financial_time_point(raw["capitalized_at"], field=f"{field}.capitalized_at"),
            _pos_int(raw["available_from"], f"{field}.available_from"),
        )
    return MappingProxyType(dict(sorted(out.items())))


def _nonnegative_ratio_money(value: object, field: str) -> Money:
    rate = Money.from_dict(value, field=field)
    if rate.unit != "1":
        raise FinanceTripletError(f"{field} 必须为无量纲非负金额原子")
    return rate


def _parse_movement_confirmation(value: object, field: str) -> MovementConfirmation:
    if not isinstance(value, Mapping):
        raise FinanceTripletError(f"{field} 必须为明确确认方法")
    method = value.get("method")
    if method == "recognition_period":
        _require_keys(value, {"method"}, field)
        return MovementConfirmation(method)
    if method != "explicit_schedule":
        raise FinanceTripletError(f"{field}.method 必须为 recognition_period/explicit_schedule")
    _require_keys(value, {"method", "occurrences"}, field)
    occurrences = []
    seen = set()
    for index, raw in enumerate(_as_list(value["occurrences"], f"{field}.occurrences")):
        sub = f"{field}.occurrences[{index}]"
        if not isinstance(raw, Mapping):
            raise FinanceTripletError(f"{sub} 必须为 at/schedule")
        _require_keys(raw, {"at", "schedule"}, sub)
        at = parse_financial_time_point(raw["at"], field=f"{sub}.at")
        if (at.axis, at.period) in seen:
            raise FinanceTripletError(f"{sub}.at 重复发生身份")
        seen.add((at.axis, at.period))
        schedule = _parse_construction_schedule(raw["schedule"], f"{sub}.schedule")
        if not _ratios_total_one(tuple(row.ratio for row in schedule)):
            raise FinanceTripletError(f"{sub}.schedule 比例合计必须精确为1")
        if len({row.period for row in schedule}) != len(schedule):
            raise FinanceTripletError(f"{sub}.schedule 不允许重复确认月")
        occurrences.append(MovementVatOccurrence(at, tuple(sorted(schedule, key=lambda row: row.period))))
    if not occurrences:
        raise FinanceTripletError(f"{field}.occurrences 必须非空且逐次明确")
    return MovementConfirmation(
        method, tuple(sorted(occurrences, key=lambda row: (row.at.axis, row.at.period))))


def _parse_movement_tax_entries(value: object, *, field: str, input_tax: bool) -> tuple:
    out = []
    for index, raw in enumerate(_as_list(value, field)):
        sub = f"{field}[{index}]"
        if not isinstance(raw, Mapping):
            raise FinanceTripletError(f"{sub} 必须为收付税务条目")
        _require_keys(raw, {"movement", "rate", "confirmation"} |
                      ({"deductible_ratio"} if input_tax else set()), sub)
        values = {"movement": _validate_local_id(raw["movement"], f"{sub}.movement"),
                  "rate": _nonnegative_ratio_money(raw["rate"], f"{sub}.rate"),
                  "confirmation": _parse_movement_confirmation(raw["confirmation"], f"{sub}.confirmation")}
        if input_tax:
            values["deductible_ratio"] = _ratio_money(raw["deductible_ratio"], f"{sub}.deductible_ratio")
        out.append((MovementInputTaxEntry if input_tax else MovementOutputTaxEntry)(**values))
    return tuple(sorted(out, key=lambda entry: entry.movement))


def _parse_derived_rule(rule_id: str, mapping: Mapping) -> DerivedTaxRule:
    field = f"tax_rules.{rule_id}"
    _require_keys(mapping, {"kind", "label_ref", "axis", "base_from", "rate", "payment_lag"}, field)
    axis = mapping["axis"]
    if axis not in ("financial_year", "tax_month"):
        raise FinanceTripletError(f"{field}.axis 必须为 financial_year/tax_month")
    base = parse_amount_reference(mapping["base_from"], field=f"{field}.base_from")
    if base.kind != "tax" or not (base.period.kind == "current" or
                                  axis == "financial_year" and base.period.kind == "year_months"
                                  and base.period.year == "current"):
        raise FinanceTripletError(f"{field}.base_from 必须为同轴current或year_months current税额引用")
    return DerivedTaxRule(
        _validate_local_id(mapping["label_ref"], f"{field}.label_ref"), axis, base,
        _nonnegative_ratio_money(mapping["rate"], f"{field}.rate"),
        _nonneg_int(mapping["payment_lag"], f"{field}.payment_lag"),
    )


def _parse_id_list(value: object, field: str) -> tuple[str, ...]:
    rows = _as_list(value, field)
    ids = tuple(_validate_local_id(value, field) for value in rows)
    if len(set(ids)) != len(ids):
        raise FinanceTripletError(f"{field} 不允许重复身份")
    return tuple(sorted(ids))


@dataclass(frozen=True, slots=True)
class AssetBasisSource:
    source: AmountReference
    share: Decimal


def _asset_basis_sources(amounts, reference, *, current_axis="scalar", current_period=0,
                        share=Decimal("1"), stack=()) -> tuple[AssetBasisSource, ...]:
    if reference.kind == "amount":
        if reference.id in stack:
            raise FinanceTripletError("资产基础金额循环引用")
        definition = amounts.get(reference.id)
        if definition is None or definition.constant.value != 0:
            raise FinanceTripletError("资产基础组合须引用已声明的零constant金额")
        axis, periods = expand_amount_period(reference.period, current_axis, current_period)
        out = []
        for period in periods:
            for term in definition.terms.values():
                if term.coefficient.unit != "1" or term.coefficient.value < 0:
                    raise FinanceTripletError("资产基础组合系数须为非负无量纲已知份额")
                out.extend(_asset_basis_sources(
                    amounts, term.source, current_axis=axis, current_period=period,
                    share=share * term.coefficient.value, stack=(*stack, reference.id),
                ))
        return tuple(out)
    if reference.kind not in ("cost", "maintenance", "movement") or reference.basis != "net_basis":
        raise FinanceTripletError("资产基础根只允许初建cost、maintenance或movement的net_basis")
    if reference.kind == "cost":
        parts = reference.path.split(".")
        if len(parts) < 2 or parts[1] != "upfront_capex":
            raise FinanceTripletError("资产基础cost只允许upfront_capex及其子项")
    if reference.period is None:
        return (AssetBasisSource(reference, share),)
    axis, periods = expand_amount_period(reference.period, current_axis, current_period)
    return tuple(AssetBasisSource(replace(reference, period=AmountPeriod(
        "scalar" if axis == "scalar" else axis,
        year=period if axis == "financial_year" else None,
        month=period if axis == "tax_month" else None,
    )), share) for period in periods)


def asset_basis_sources(finance, reference: AmountReference) -> tuple[AssetBasisSource, ...]:
    """净基础组合的已知份额与原始发生来源；不消解项目实例/叶子归属。"""
    return _asset_basis_sources(finance.amounts, reference)


def amount_dependency_closure(
    amounts: Mapping[str, AmountDefinition], roots: tuple[str, ...] | list[str] | set[str],
) -> tuple[str, ...]:
    """确定性的来源先行依赖次序；不因闲置目录自动扩大执行范围。"""
    state: dict[str, int] = {}
    ordered: list[str] = []
    stack: list[str] = []

    def visit(amount_id: str) -> None:
        if amount_id not in amounts:
            raise FinanceTripletError(f"amounts 引用不存在: {amount_id!r}")
        if state.get(amount_id) == 2:
            return
        if state.get(amount_id) == 1:
            start = stack.index(amount_id)
            raise FinanceTripletError(f"amounts 循环引用: {' -> '.join((*stack[start:], amount_id))}")
        state[amount_id] = 1
        stack.append(amount_id)
        for term in amounts[amount_id].terms.values():
            if term.source.kind == "amount":
                visit(term.source.id)
        stack.pop()
        state[amount_id] = 2
        ordered.append(amount_id)

    for amount_id in sorted(roots):
        visit(amount_id)
    return tuple(ordered)


def _source_axis(period: AmountPeriod, owner_axis: str) -> str:
    if period.kind == "current":
        return owner_axis
    if period.kind == "year_months":
        return "tax_month"
    if period.kind == "range":
        return period.axis
    return period.kind


def _check_reference_axis(
    ref: AmountReference, owner_axis: str, allowed: tuple[str, ...], field: str,
) -> None:
    if ref.period is not None and _source_axis(ref.period, owner_axis) not in allowed:
        raise FinanceTripletError(f"{field}.period 与来源轴不匹配，允许 {allowed!r}")


def _check_amount_refs(
    amounts: Mapping[str, AmountDefinition], *, currency: str,
    finance_types: Mapping[str, FinanceTypeEntry], energy_prices: Mapping[str, EnergyPrice],
    demand_charges: Mapping[str, DemandCharge], credit_pools: Mapping[str, CreditPool],
    tax_rules: Mapping[str, TaxRule], net_cashflows: Mapping[str, NetCashflowEntry],
    cash_movements: Mapping[str, CashMovement], assets: Mapping[str, CapitalizedAsset],
) -> None:
    amount_dependency_closure(amounts, set(amounts))
    for amount_id, amount in amounts.items():
        for term_id, term in amount.terms.items():
            ref = term.source
            field = f"amounts.{amount_id}.terms.{term_id}.source"
            source_dims: Mapping[str, int] | None = None
            if ref.kind == "amount":
                other = amounts[ref.id]
                _check_reference_axis(ref, amount.axis, (other.axis,), field)
                source_dims = _dims(other.unit)
            elif ref.kind == "construction_unit":
                source_dims = {}
            elif ref.kind == "movement":
                movement = cash_movements.get(ref.id)
                if movement is None:
                    raise FinanceTripletError(f"{field}.id 收付发生项不存在: {ref.id!r}")
                _check_movement_basis(ref, movement, field)
                axis = (movement.recognition.at.axis if movement.recognition.kind == "at"
                        else movement.recognition.axis)
                _check_reference_axis(ref, amount.axis, (axis,), field)
                source_dims = {"currency": 1}
            elif ref.kind == "asset":
                if ref.id not in assets:
                    raise FinanceTripletError(f"{field}.id 资产不存在: {ref.id!r}")
                _check_reference_axis(ref, amount.axis,
                                      ("scalar",) if ref.measure == "basis" else ("financial_year",), field)
                source_dims = {"currency": 1}
            elif ref.kind == "cost":
                cost_target_leaves(ref.path, finance_types, field=field)
                comp = ref.path.split(".")[1]
                allowed = (("scalar",) if comp == "upfront_capex" else
                           ("scalar", "financial_year") if comp == "annual_fixed_om" else
                           ("financial_year", "tax_month"))
                _check_reference_axis(ref, amount.axis, allowed, field)
                annual_rate = comp == "annual_fixed_om" and ref.period.kind == "scalar"
                source_dims = {"currency": 1, **({"time": -1} if annual_rate else {})}
            elif ref.kind == "driver":
                entry = finance_types.get(ref.finance_type)
                if entry is None:
                    raise FinanceTripletError(f"{field}.finance_type 不存在: {ref.finance_type!r}")
                candidates: list[Mapping[str, int]] = []
                for comp_name in FINANCE_COMPONENTS:
                    comp = getattr(entry, comp_name)
                    if comp is None:
                        continue
                    for item in (comp, *comp.items.values()):
                        if ref.driver in (item.linear or {}):
                            dims = dict(_dims(item.linear[ref.driver].unit_cost.unit))
                            dims["currency"] = dims.get("currency", 0) - 1
                            if comp_name == "annual_fixed_om":
                                dims["time"] = dims.get("time", 0) + 1
                            candidates.append({key: -value for key, value in dims.items() if value})
                if not candidates:
                    raise FinanceTripletError(f"{field}.driver 不存在: {ref.driver!r}")
                if any(dims != candidates[0] for dims in candidates[1:]):
                    raise FinanceTripletError(f"{field}.driver 在成本单价中的量纲不一致")
                source_dims = candidates[0]
            elif ref.kind == "tariff":
                price = energy_prices.get(ref.price)
                if price is None:
                    raise FinanceTripletError(f"{field}.price 不存在: {ref.price!r}")
                if (price.direction == "sale" and ref.basis in ("deductible_input", "net_basis")
                        or price.direction == "purchase" and ref.basis == "net_revenue"):
                    raise FinanceTripletError(f"{field}.basis 与价格 direction 不匹配")
                _check_reference_axis(ref, amount.axis, ("financial_year", "tax_month"), field)
                source_dims = {"currency": 1}
            elif ref.kind == "demand":
                if ref.charge not in demand_charges:
                    raise FinanceTripletError(f"{field}.charge 不存在: {ref.charge!r}")
                _check_reference_axis(ref, amount.axis, ("financial_year", "tax_month"), field)
                source_dims = {"currency": 1}
            elif ref.kind == "maintenance":
                source_dims = {"currency": 1}
            elif ref.kind == "system_quantity":
                _check_reference_axis(ref, amount.axis, ("financial_year", "tax_month"), field)
            elif ref.kind == "tax":
                source = ref.source
                if source.kind == "pool":
                    if source.id not in credit_pools:
                        raise FinanceTripletError(f"{field}.source.id 抵扣池不存在: {source.id!r}")
                    measures = ("paid", "refund_confirmed", "refund_received", "credit_balance", "receivable")
                    axis = "tax_month"
                else:
                    rule = tax_rules.get(source.id)
                    if rule is None:
                        raise FinanceTripletError(f"{field}.source.id 税则不存在: {source.id!r}")
                    measures = (("taxable_base", "loss_used", "assessed", "paid")
                                if isinstance(rule, IncomeTaxRule) else
                                ("assessed", "paid") if isinstance(rule, DerivedTaxRule) else
                                ("output_tax", "deductible_input"))
                    axis = ("financial_year" if isinstance(rule, IncomeTaxRule) else
                            rule.axis if isinstance(rule, DerivedTaxRule) else "tax_month")
                if ref.measure not in measures:
                    raise FinanceTripletError(f"{field}.measure 不属于该税额来源: {ref.measure!r}")
                _check_reference_axis(ref, amount.axis, (axis,), field)
                source_dims = {"currency": 1}
            if source_dims is not None:
                dims = dict(_dims(term.coefficient.unit))
                for key, value in source_dims.items():
                    dims[key] = dims.get(key, 0) + value
                dims = {key: value for key, value in dims.items() if value}
                if dims != _dims(amount.unit):
                    raise FinanceTripletError(f"{field}: coefficient × source 单位不等于 {amount.unit!r}")
            for money in (term.coefficient,):
                if any(code in money.unit for code in CURRENCIES if code != currency):
                    raise FinanceTripletError(f"{field}: coefficient 不允许其他币种")
    for cf_id, cf in net_cashflows.items():
        if not isinstance(cf.amount, AmountRefCashflowAmount):
            continue
        amount = amounts.get(cf.amount.id)
        if amount is None:
            raise FinanceTripletError(f"net_cashflows.{cf_id}.amount.id 引用不存在: {cf.amount.id!r}")
        if amount.axis != "scalar" or amount.unit != currency:
            raise FinanceTripletError(f"net_cashflows.{cf_id}.amount 必须引用 scalar 本币金额")


def _parse_amounts(mapping: object, currency: str) -> Mapping[str, AmountDefinition]:
    if not isinstance(mapping, Mapping):
        raise FinanceTripletError("amounts 必须为映射，禁止 null")
    parsed = {
        _validate_local_id(amount_id, "amounts.id"): AmountDefinition.from_dict(
            raw, currency=currency, field=f"amounts.{amount_id}"
        ) for amount_id, raw in mapping.items()
    }
    return MappingProxyType(dict(sorted(parsed.items())))


def _check_movement_basis(reference: AmountReference, movement: CashMovement, field: str) -> None:
    if movement.direction == "inflow" and reference.basis in ("deductible_input", "net_basis"):
        raise FinanceTripletError(f"{field}.basis 收入不允许抵扣进项或支出净基础")
    if movement.direction == "outflow" and reference.basis == "net_revenue":
        raise FinanceTripletError(f"{field}.basis 支出不允许净收入")


def _check_f2_refs(*, amounts, cash_movements, assets, tax_rules, currency,
                   finance_types, credit_pools) -> None:
    """文件本地身份/方向/量纲；评价期间及实际归属留给公开项目边界。"""
    for movement_id, movement in cash_movements.items():
        field = f"cash_movements.{movement_id}.amount"
        definition = amounts.get(movement.amount.id)
        if definition is None or definition.unit != currency:
            raise FinanceTripletError(f"{field} 必须引用已声明的本币金额")
        axis = (movement.recognition.at.axis if movement.recognition.kind == "at"
                else movement.recognition.axis)
        expected_period = "scalar" if definition.axis == "scalar" else "current"
        if definition.axis not in ("scalar", axis) or movement.amount.period.kind != expected_period:
            raise FinanceTripletError(f"{field}.period 须为scalar金额或与recognition同轴的current金额")

    capitalized_movements = set()
    for asset_id, asset in assets.items():
        field = f"assets.{asset_id}.basis_from"
        source = asset.basis_from
        if source.period is not None and (source.period.kind == "current" or
                source.period.kind == "year_months" and source.period.year == "current"):
            raise FinanceTripletError(f"{field} 必须明确实际发生期，不能使用current")
        if source.kind == "amount":
            definition = amounts.get(source.id)
            if definition is None or definition.unit != currency:
                raise FinanceTripletError(f"{field} 必须引用本币金额")
            _check_reference_axis(source, "scalar", (definition.axis,), field)
        for root in _asset_basis_sources(amounts, source):
            leaf = root.source
            if leaf.kind == "cost":
                cost_target_leaves(leaf.path, finance_types, ("upfront_capex",), field=field)
                _check_reference_axis(leaf, "scalar", ("scalar",), field)
            elif leaf.kind == "movement":
                movement = cash_movements.get(leaf.id)
                if movement is None or movement.direction != "outflow":
                    raise FinanceTripletError(f"{field} 必须引用已有支出发生项net_basis")
                axis = (movement.recognition.at.axis if movement.recognition.kind == "at"
                        else movement.recognition.axis)
                _check_reference_axis(leaf, "scalar", (axis,), field)
                if root.share:
                    capitalized_movements.add(leaf.id)

    vat_claims, income_claims, derived_claims = {}, {}, {}
    for rule_id, rule in tax_rules.items():
        field = f"tax_rules.{rule_id}"
        if isinstance(rule, VatRule):
            for key, direction in (("movement_input_tax", "outflow"), ("movement_output_tax", "inflow")):
                for entry in getattr(rule, key):
                    sub = f"{field}.{key}.{entry.movement}"
                    movement = cash_movements.get(entry.movement)
                    if movement is None or movement.direction != direction:
                        raise FinanceTripletError(f"{sub} 引用未知发生项或方向不匹配")
                    if entry.movement in vat_claims:
                        raise FinanceTripletError(
                            f"{sub} 重复确认同一发生项（已有{vat_claims[entry.movement]}）")
                    vat_claims[entry.movement] = sub
        elif isinstance(rule, IncomeTaxRule):
            for key, direction in (("movement_revenue", "inflow"), ("movement_cost", "outflow")):
                for movement_id in getattr(rule, key):
                    sub = f"{field}.{key}"
                    movement = cash_movements.get(movement_id)
                    if movement is None or movement.direction != direction:
                        raise FinanceTripletError(f"{sub} 引用未知发生项或方向不匹配: {movement_id!r}")
                    if movement_id in income_claims:
                        raise FinanceTripletError(f"{sub} 重复计入所得税范围: {movement_id!r}")
                    income_claims[movement_id] = sub
                    if key == "movement_cost" and movement_id in capitalized_movements:
                        raise FinanceTripletError(
                            f"{sub} 已资本化发生项不得同时全额费用扣除: {movement_id!r}")
            for derived_id in rule.derived_tax_cost:
                if not isinstance(tax_rules.get(derived_id), DerivedTaxRule):
                    raise FinanceTripletError(f"{field}.derived_tax_cost 引用未知派生税: {derived_id!r}")
                if derived_id in derived_claims:
                    raise FinanceTripletError(f"{field}.derived_tax_cost 重复扣除派生税: {derived_id!r}")
                derived_claims[derived_id] = rule_id
            for entry in rule.depreciation:
                if isinstance(entry.base_from, AssetBaseFrom):
                    asset = assets.get(entry.base_from.id)
                    if asset is None:
                        raise FinanceTripletError(
                            f"{field}.depreciation 引用未知资产: {entry.base_from.id!r}")
                    if entry.first_year < asset.available_from:
                        raise FinanceTripletError(f"{field}.depreciation.first_year 早于资产available_from")
        elif isinstance(rule, DerivedTaxRule):
            ref = rule.base_from
            if ref.source.kind == "pool":
                if ref.source.id not in credit_pools:
                    raise FinanceTripletError(f"{field}.base_from 引用未知池")
                axis = "tax_month"
                measures = ("paid", "refund_confirmed", "refund_received", "credit_balance", "receivable")
            else:
                source = tax_rules.get(ref.source.id)
                if source is None:
                    raise FinanceTripletError(f"{field}.base_from 引用未知规则")
                axis = (source.axis if isinstance(source, DerivedTaxRule) else
                        "financial_year" if isinstance(source, IncomeTaxRule) else "tax_month")
                measures = (("assessed", "paid") if isinstance(source, DerivedTaxRule) else
                            ("taxable_base", "loss_used", "assessed", "paid")
                            if isinstance(source, IncomeTaxRule)
                            else ("output_tax", "deductible_input"))
            if ref.measure not in measures:
                raise FinanceTripletError(f"{field}.base_from.measure 不属于该税额来源")
            _check_reference_axis(ref, rule.axis, (axis,), f"{field}.base_from")


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
    credit_pools: Mapping[str, CreditPool] = field(default_factory=lambda: MappingProxyType({}))
    tax_rules: Mapping[str, TaxRule] = field(default_factory=lambda: MappingProxyType({}))
    demand_charges: Mapping[str, DemandCharge] = field(default_factory=lambda: MappingProxyType({}))
    net_cashflows: Mapping[str, NetCashflowEntry] = field(default_factory=lambda: MappingProxyType({}))
    amounts: Mapping[str, AmountDefinition] = field(default_factory=lambda: MappingProxyType({}))
    cash_movements: Mapping[str, CashMovement] = field(default_factory=lambda: MappingProxyType({}))
    assets: Mapping[str, CapitalizedAsset] = field(default_factory=lambda: MappingProxyType({}))

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
        object.__setattr__(self, "demand_charges", MappingProxyType(dict(sorted(self.demand_charges.items()))))
        object.__setattr__(self, "taxes", MappingProxyType(dict(sorted(self.taxes.items()))))
        object.__setattr__(self, "credit_pools", MappingProxyType(dict(sorted(self.credit_pools.items()))))
        object.__setattr__(self, "tax_rules", MappingProxyType(dict(sorted(self.tax_rules.items()))))
        object.__setattr__(self, "net_cashflows", MappingProxyType(dict(sorted(self.net_cashflows.items()))))
        object.__setattr__(self, "amounts", MappingProxyType(dict(sorted(self.amounts.items()))))
        object.__setattr__(
            self, "cash_movements", MappingProxyType(dict(sorted(self.cash_movements.items())))
        )
        object.__setattr__(self, "assets", MappingProxyType(dict(sorted(self.assets.items()))))
        # taxes.applies_to 引用存在性: 引用本文件 energy_prices 的 price_id,
        # 或 finance_types 的分量/子项路径, 或 demand_charges.<charge_id>。
        for tax_id, tax in self.taxes.items():
            target = tax.applies_to
            if (
                target in self.energy_prices
                or (
                    target.startswith("demand_charges.")
                    and target.removeprefix("demand_charges.") in self.demand_charges
                )
            ):
                continue
            try:
                _expand_target_leaves(
                    target, self.finance_types, FINANCE_COMPONENTS, f"tax {tax_id!r} applies_to"
                )
                continue
            except FinanceTripletError:
                pass
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
            "demand_charges": {k: v.to_dict() for k, v in sorted(self.demand_charges.items())},
            "taxes": {k: v.to_dict() for k, v in sorted(self.taxes.items())},
            "credit_pools": {k: v.to_dict() for k, v in sorted(self.credit_pools.items())},
            "tax_rules": {k: v.to_dict() for k, v in sorted(self.tax_rules.items())},
            "net_cashflows": {k: v.to_dict() for k, v in sorted(self.net_cashflows.items())},
            "amounts": {k: v.to_dict() for k, v in self.amounts.items()},
            "cash_movements": {k: v.to_dict() for k, v in self.cash_movements.items()},
            "assets": {k: v.to_dict() for k, v in self.assets.items()},
        }

    @classmethod
    def from_dict(cls, mapping: object) -> FinanceProfile:
        if not isinstance(mapping, Mapping):
            raise FinanceTripletError("FinanceProfile 必须是字典")
        known_profile = {
            "schema", "schema_version", "profile", "finance_types", "energy_prices",
            "demand_charges", "taxes", "credit_pools", "tax_rules", "net_cashflows", "amounts",
            "cash_movements", "assets",
        }
        unknown = set(mapping) - known_profile
        if unknown:
            raise FinanceTripletError(f"FinanceProfile 未知字段: {sorted(unknown)}")
        missing = {"schema", "schema_version", "profile", "finance_types", "energy_prices", "demand_charges",
                   "credit_pools", "tax_rules", "net_cashflows"} - set(mapping)
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

        demand_charges = _parse_demand_charges(mapping["demand_charges"], currency)

        taxes_raw = mapping.get("taxes") or {}
        if not isinstance(taxes_raw, Mapping):
            raise FinanceTripletError("taxes 必须是字典")
        taxes: dict[str, TaxEntry] = {}
        for tax_id, tax_val in taxes_raw.items():
            tax_id = _validate_local_id(tax_id, "tax id")
            if tax_val is None:
                raise FinanceTripletError(f"taxes.{tax_id} 禁止 null")
            taxes[tax_id] = TaxEntry.from_dict(tax_val)

        pools_raw = _require_section(mapping, "credit_pools", "credit_pools")
        credit_pools: dict[str, CreditPool] = {}
        for pool_id, pool_val in pools_raw.items():
            pool_id = _validate_local_id(pool_id, "credit pool")
            if pool_val is None:
                raise FinanceTripletError(f"credit_pools.{pool_id} 禁止 null")
            credit_pools[pool_id] = CreditPool.from_dict(pool_val, field=f"credit_pools.{pool_id}")

        rules_raw = _require_section(mapping, "tax_rules", "tax_rules")
        tax_rules: dict[str, TaxRule] = {}
        for rule_id, rule_val in rules_raw.items():
            rule_id = _validate_local_id(rule_id, "tax rule")
            if rule_val is None:
                raise FinanceTripletError(f"tax_rules.{rule_id} 禁止 null")
            tax_rules[rule_id] = _parse_tax_rule(rule_id, rule_val)
        net_cashflows_raw = _require_section(mapping, "net_cashflows", "net_cashflows")
        net_cashflows = _parse_net_cashflows(net_cashflows_raw, currency)
        amounts = _parse_amounts(mapping.get("amounts", {}), currency)
        cash_movements = _parse_movements(mapping.get("cash_movements", {}))
        assets = _parse_assets(mapping.get("assets", {}))
        _check_amount_refs(amounts, currency=currency, finance_types=finance_types,
                           energy_prices=energy_prices, demand_charges=demand_charges,
                           credit_pools=credit_pools, tax_rules=tax_rules, net_cashflows=net_cashflows,
                           cash_movements=cash_movements, assets=assets)
        _check_f2_refs(amounts=amounts, cash_movements=cash_movements, assets=assets,
                       tax_rules=tax_rules, currency=currency, finance_types=finance_types,
                       credit_pools=credit_pools)
        _check_tax_refs(
            currency=currency,
            energy_prices=energy_prices,
            finance_types=finance_types,
            demand_charges=demand_charges,
            taxes=taxes,
            credit_pools=credit_pools,
            tax_rules=tax_rules,
            net_cashflows=net_cashflows,
        )

        return cls(
            schema=schema,
            schema_version=schema_version,
            profile=profile,
            finance_types=finance_types,
            energy_prices=energy_prices,
            demand_charges=demand_charges,
            taxes=taxes,
            credit_pools=credit_pools,
            tax_rules=tax_rules,
            net_cashflows=net_cashflows,
            amounts=amounts,
            cash_movements=cash_movements,
            assets=assets,
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
_OVERRIDE_LEAF_FIELDS: Final[frozenset[str]] = frozenset({"fixed", "linear", "items"})

@dataclass(frozen=True, slots=True)
class FinanceOverrides:
    """项目 FinanceOverrides: 稀疏原子覆盖(引用 Profile 稳定 ID)。

    覆盖子树的原始形状保留在 ``finance_types`` / ``energy_prices`` 中
    (仅按叶子做形状/单位/存在性校验), 由合并器应用。
    税则/池补丁另以输入边界已解析的不可变类型化值保留在
    ``_parsed_tax_rules`` / ``_parsed_credit_pools``（已绑定时），
    供绑定合并直接消费，不重解析；不序列化。
    """

    schema: str
    schema_version: str
    profile_ref: Mapping[str, str]
    finance_types: Mapping[str, Mapping[str, object]] = field(default_factory=lambda: MappingProxyType({}))
    energy_prices: Mapping[str, Mapping[str, object]] = field(default_factory=lambda: MappingProxyType({}))
    demand_charges: Mapping[str, Mapping[str, object]] = field(default_factory=lambda: MappingProxyType({}))
    tax_rules: Mapping[str, Mapping[str, object]] = field(default_factory=lambda: MappingProxyType({}))
    credit_pools: Mapping[str, Mapping[str, object]] = field(default_factory=lambda: MappingProxyType({}))
    net_cashflows: Mapping[str, Mapping[str, object]] = field(default_factory=lambda: MappingProxyType({}))
    amounts: Mapping[str, Mapping[str, object]] = field(default_factory=lambda: MappingProxyType({}))
    cash_movements: Mapping[str, Mapping[str, object]] = field(default_factory=lambda: MappingProxyType({}))
    assets: Mapping[str, Mapping[str, object]] = field(default_factory=lambda: MappingProxyType({}))
    #: Cache ownership is the exact immutable input, not a reusable Profile ID.
    _bound_profile_object: FinanceProfile | None = field(default=None, repr=False, compare=False)
    #: 输入边界已形成的不可变类型化补丁（规则 id → 已解析值；未绑定为空）。
    _parsed_tax_rules: Mapping[str, Mapping[str, object]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    #: 输入边界已形成的不可变类型化补丁（池 id → 已解析值；未绑定为空）。
    _parsed_credit_pools: Mapping[str, Mapping[str, object]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    #: 已绑定 Profile 的净现金覆盖使用类型化补丁；未绑定时首次合并解析。
    _parsed_net_cashflows: Mapping[str, Mapping[str, object]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    _parsed_amounts: Mapping[str, AmountDefinition] = field(default_factory=lambda: MappingProxyType({}))
    _parsed_finance_types: Mapping[str, FinanceTypeEntry] = field(
        default_factory=lambda: MappingProxyType({})
    )
    _parsed_cash_movements: Mapping[str, CashMovement] = field(default_factory=lambda: MappingProxyType({}))
    _parsed_assets: Mapping[str, CapitalizedAsset] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if self.schema != SCHEMA_OVERRIDES:
            raise FinanceTripletError(f"schema 必须为 {SCHEMA_OVERRIDES!r}")
        if self.schema_version != SCHEMA_VERSION:
            raise FinanceTripletError(f"schema_version 必须为 {SCHEMA_VERSION!r}")
        object.__setattr__(self, "profile_ref", MappingProxyType(dict(sorted(self.profile_ref.items()))))
        object.__setattr__(self, "finance_types", _freeze(self.finance_types))
        object.__setattr__(self, "energy_prices", MappingProxyType(dict(sorted(self.energy_prices.items()))))
        object.__setattr__(self, "demand_charges", MappingProxyType(dict(sorted(self.demand_charges.items()))))
        object.__setattr__(self, "tax_rules", MappingProxyType(dict(sorted(self.tax_rules.items()))))
        object.__setattr__(self, "credit_pools", MappingProxyType(dict(sorted(self.credit_pools.items()))))
        object.__setattr__(self, "net_cashflows", MappingProxyType(dict(sorted(self.net_cashflows.items()))))
        object.__setattr__(self, "amounts", _freeze(self.amounts))
        object.__setattr__(self, "cash_movements", _freeze(self.cash_movements))
        object.__setattr__(self, "assets", _freeze(self.assets))
        object.__setattr__(
            self, "_parsed_cash_movements", MappingProxyType(dict(self._parsed_cash_movements))
        )
        object.__setattr__(self, "_parsed_assets", MappingProxyType(dict(self._parsed_assets)))
        object.__setattr__(self, "_parsed_amounts",
                           MappingProxyType(dict(sorted(self._parsed_amounts.items()))))
        object.__setattr__(self, "_parsed_finance_types",
                           MappingProxyType(dict(sorted(self._parsed_finance_types.items()))))
        object.__setattr__(
            self, "_parsed_tax_rules", MappingProxyType(dict(sorted(self._parsed_tax_rules.items())))
        )
        object.__setattr__(
            self, "_parsed_credit_pools", MappingProxyType(dict(sorted(self._parsed_credit_pools.items())))
        )
        object.__setattr__(
            self, "_parsed_net_cashflows", MappingProxyType(dict(sorted(self._parsed_net_cashflows.items())))
        )

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
        if self.demand_charges:
            d["demand_charges"] = _freeze_overrides(self.demand_charges)
        if self.tax_rules:
            d["tax_rules"] = _thaw(self.tax_rules)
        if self.credit_pools:
            d["credit_pools"] = _thaw(self.credit_pools)
        if self.net_cashflows:
            d["net_cashflows"] = _freeze_overrides(self.net_cashflows)
        if self.amounts:
            d["amounts"] = _thaw(self.amounts)
        if self.cash_movements:
            d["cash_movements"] = _thaw(self.cash_movements)
        if self.assets:
            d["assets"] = _thaw(self.assets)
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
        known_overrides = {
            "schema", "schema_version", "profile_ref", "finance_types", "energy_prices",
            "demand_charges", "tax_rules", "credit_pools", "net_cashflows", "amounts",
            "cash_movements", "assets",
        }
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
        parsed_finance_types: dict[str, FinanceTypeEntry] = {}
        for ft_name, ft_val in ft_raw.items():
            ft_name = _validate_local_id(ft_name, "finance_type")
            if profile is not None and ft_name not in profile.finance_types:
                raise FinanceTripletError(f"finance_type {ft_name!r} 在 Profile 中不存在, 禁止新增")
            if not isinstance(ft_val, Mapping) or not ft_val:
                raise FinanceTripletError(f"finance_types.{ft_name} 必须为非空字典")
            raw_patch, parsed_entry = _validate_ft_override(ft_name, ft_val, profile)
            finance_types[ft_name] = raw_patch
            if parsed_entry is not None:
                parsed_finance_types[ft_name] = parsed_entry

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

        demand_raw = _optional_section(mapping, "demand_charges", "demand_charges")
        demand_charges: dict[str, Mapping[str, object]] = {}
        for charge_id, patch in demand_raw.items():
            charge_id = _validate_local_id(charge_id, "charge_id")
            if not isinstance(patch, Mapping) or set(patch) != {"rate"}:
                raise FinanceTripletError(f"demand_charges.{charge_id} 只允许覆盖 rate 原子")
            if profile is not None and charge_id not in profile.demand_charges:
                raise FinanceTripletError(f"demand_charges.{charge_id} 不允许新增费项")
            rate = Money.from_dict(patch["rate"], field=f"demand_charges.{charge_id}.rate")
            if _dims(rate.unit) != {"currency": 1, "power": -1}:
                raise FinanceTripletError(f"demand_charges.{charge_id}.rate 必须为货币/功率")
            if profile is not None:
                base = profile.demand_charges[charge_id].rate
                _check_same_unit(rate.unit, base.unit, f"demand_charges.{charge_id}.rate")
            demand_charges[charge_id] = {"rate": rate.to_dict()}

        tax_raw = _optional_section(mapping, "tax_rules", "tax_rules")
        tax_rules: dict[str, Mapping[str, object]] = {}
        parsed_rules: dict[str, dict] = {}
        for rule_id, rule_val in tax_raw.items():
            rule_id = _validate_local_id(rule_id, "tax rule")
            if not isinstance(rule_val, Mapping) or not rule_val:
                raise FinanceTripletError(f"tax_rules.{rule_id} 必须为非空字典")
            raw_patch, parsed_values = _validate_tax_rule_override(rule_id, rule_val, profile)
            tax_rules[rule_id] = raw_patch
            if parsed_values:
                parsed_rules[rule_id] = parsed_values

        pools_raw = _optional_section(mapping, "credit_pools", "credit_pools")
        credit_pools: dict[str, Mapping[str, object]] = {}
        parsed_pools: dict[str, dict] = {}
        for pool_id, pool_val in pools_raw.items():
            pool_id = _validate_local_id(pool_id, "credit pool")
            if not isinstance(pool_val, Mapping) or not pool_val:
                raise FinanceTripletError(f"credit_pools.{pool_id} 必须为非空字典")
            raw_patch, parsed_values = _validate_pool_override(pool_id, pool_val, profile)
            credit_pools[pool_id] = raw_patch
            if parsed_values:
                parsed_pools[pool_id] = parsed_values

        cf_raw = _optional_section(mapping, "net_cashflows", "net_cashflows")
        net_cashflows: dict[str, Mapping[str, object]] = {}
        parsed_cashflows: dict[str, dict] = {}
        for cf_id, cf_patch in cf_raw.items():
            cf_id = _validate_local_id(cf_id, "cashflow_id")
            raw_patch, parsed_values = _validate_net_cashflow_override(cf_id, cf_patch, profile)
            net_cashflows[cf_id] = raw_patch
            if parsed_values:
                parsed_cashflows[cf_id] = parsed_values

        amount_patches: dict[str, Mapping] = {}
        parsed_amounts: dict[str, AmountDefinition] = {}
        for amount_id, patch in _optional_section(mapping, "amounts", "amounts").items():
            amount_id = _validate_local_id(amount_id, "amounts.id")
            raw, parsed = _validate_amount_override(amount_id, patch, profile)
            amount_patches[amount_id] = raw
            if parsed is not None:
                parsed_amounts[amount_id] = parsed

        movement_patches, parsed_movements, asset_patches, parsed_assets = {}, {}, {}, {}
        for movement_id, patch in _optional_section(mapping, "cash_movements", "cash_movements").items():
            movement_id = _validate_local_id(movement_id, "cash_movements.id")
            raw, parsed = _validate_movement_override(movement_id, patch, profile)
            movement_patches[movement_id] = raw
            if parsed is not None:
                parsed_movements[movement_id] = parsed
        for asset_id, patch in _optional_section(mapping, "assets", "assets").items():
            asset_id = _validate_local_id(asset_id, "assets.id")
            raw, parsed = _validate_asset_override(asset_id, patch, profile)
            asset_patches[asset_id] = raw
            if parsed is not None:
                parsed_assets[asset_id] = parsed

        return cls(
            schema=schema,
            schema_version=schema_version,
            profile_ref=profile_ref,
            finance_types=finance_types,
            energy_prices=energy_prices,
            demand_charges=demand_charges,
            tax_rules=tax_rules,
            credit_pools=credit_pools,
            net_cashflows=net_cashflows,
            amounts=amount_patches,
            _parsed_amounts=parsed_amounts,
            _parsed_finance_types=parsed_finance_types,
            cash_movements=movement_patches,
            assets=asset_patches,
            _parsed_cash_movements=parsed_movements,
            _parsed_assets=parsed_assets,
            _bound_profile_object=profile,
            _parsed_tax_rules=_freeze(parsed_rules),
            _parsed_credit_pools=_freeze(parsed_pools),
            _parsed_net_cashflows=_freeze(parsed_cashflows),
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


def _freeze(value: object) -> object:
    """深冻结：字典→排序不可变映射，列表→元组（切断调用方输入别名）。"""
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze(value[k]) for k in sorted(value, key=str)})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    """深解冻：冻结结构→全新可变副本（to_dict/合并消费，防回写）。"""
    if isinstance(value, Mapping):
        return {str(k): _thaw(value[k]) for k in value}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value

def _validate_ft_override(
    ft_name: str,
    ft_val: Mapping[str, object],
    profile: FinanceProfile | None,
) -> tuple[Mapping[str, object], FinanceTypeEntry | None]:
    """校验单个 finance_type 覆盖: 只允许替换既有叶子(<分量>.fixed / linear.<driver>.unit_cost)。

    - 禁止出现未知分量、fixed/linear 之外的键;
    - 被替换叶子必须已在 Profile 声明(禁止新增 fixed 节点 / driver);
    - 金额完整原子、成本非负; 单位规范化后必须与 Profile 相同。
    """
    out: dict[str, object] = {}
    merged_components: dict = {}
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
        merged_components = dict(present)
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
        merged_fixed = prof_cost.fixed if prof_cost is not None else None
        merged_linear = dict(prof_cost.linear or {}) if prof_cost is not None else {}
        merged_items = dict(prof_cost.items) if prof_cost is not None else {}
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
            merged_fixed = money
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
                merged_linear[driver] = LinearCost(unit_cost=money)
            comp_out["linear"] = linear_out
        if "items" in comp_val:
            raw_items = comp_val["items"]
            if not isinstance(raw_items, Mapping) or not raw_items:
                raise FinanceTripletError(f"finance_types.{ft_name}.{comp_name}.items 必须为非空映射")
            item_out: dict[str, object] = {}
            for item_id, raw_item in raw_items.items():
                item_id = _validate_local_id(item_id, "items.item_id")
                base_item = prof_cost.items.get(item_id) if prof_cost is not None else None
                sub = f"finance_types.{ft_name}.{comp_name}.items.{item_id}"
                if prof_cost is not None and base_item is None:
                    raise FinanceTripletError(f"{sub} 在 Profile 中不存在，禁止新增")
                if not isinstance(raw_item, Mapping) or not raw_item:
                    raise FinanceTripletError(f"{sub} 必须为非空参数覆盖")
                if set(raw_item) - {"fixed", "linear"}:
                    raise FinanceTripletError(f"{sub} 只允许 fixed/linear 原子参数覆盖")
                parsed_item = CostItem.from_dict(raw_item)
                if parsed_item.fixed is not None:
                    if comp_name == "period_variable_om":
                        raise FinanceTripletError(f"{sub}.fixed 不适用于 period_variable_om")
                    if base_item is not None:
                        if base_item.fixed is None:
                            raise FinanceTripletError(f"{sub}.fixed 禁止新增")
                        _check_same_unit(parsed_item.fixed.unit, base_item.fixed.unit, f"{sub}.fixed")
                for driver, lin in (parsed_item.linear or {}).items():
                    if base_item is not None:
                        if driver not in (base_item.linear or {}):
                            raise FinanceTripletError(f"{sub}.linear.{driver} 禁止新增 driver")
                        _check_same_unit(lin.unit_cost.unit, base_item.linear[driver].unit_cost.unit,
                                         f"{sub}.linear.{driver}")
                item_out[item_id] = parsed_item.to_dict()
                merged_items[item_id] = CostItem(
                    fixed=parsed_item.fixed if parsed_item.fixed is not None else
                    base_item.fixed if base_item is not None else None,
                    linear={**(base_item.linear or {}), **(parsed_item.linear or {})}
                    if base_item is not None else parsed_item.linear,
                )
            comp_out["items"] = item_out
        if not comp_out:
            raise FinanceTripletError(f"finance_types.{ft_name}.{comp_name} 必须覆盖至少一个叶子")
        out[comp_name] = comp_out
        if profile is not None:
            merged_components[comp_name] = CostComponent(
                fixed=merged_fixed, linear=merged_linear or None, items=merged_items
            )
    return out, FinanceTypeEntry(**merged_components) if profile is not None else None


def _validate_amount_override(
    amount_id: str, patch: object, profile: FinanceProfile | None,
) -> tuple[Mapping, AmountDefinition | None]:
    field = f"amounts.{amount_id}"
    if not isinstance(patch, Mapping) or not patch:
        raise FinanceTripletError(f"{field} 必须为非空参数覆盖")
    if set(patch) - {"constant", "terms"}:
        raise FinanceTripletError(f"{field} 只允许覆盖 constant/terms 参数，不能改身份、轴或单位")
    base = profile.amounts.get(amount_id) if profile is not None else None
    if profile is not None and base is None:
        raise FinanceTripletError(f"{field} 在 Profile 中不存在，禁止新增")
    constant = None
    out: dict = {}
    if "constant" in patch:
        constant = Money.from_dict(patch["constant"], allow_negative=True, field=f"{field}.constant")
        if base is not None:
            _check_same_unit(constant.unit, base.constant.unit, f"{field}.constant")
        out["constant"] = constant.to_dict()
    terms: dict[str, AmountTerm] = dict(base.terms) if base is not None else {}
    if "terms" in patch:
        raw_terms = patch["terms"]
        if not isinstance(raw_terms, Mapping) or not raw_terms:
            raise FinanceTripletError(f"{field}.terms 必须为非空参数覆盖")
        out_terms: dict = {}
        for term_id, raw in raw_terms.items():
            term_id = _validate_local_id(term_id, f"{field}.terms.id")
            sub = f"{field}.terms.{term_id}"
            if not isinstance(raw, Mapping) or not raw or set(raw) - {"coefficient", "source"}:
                raise FinanceTripletError(f"{sub} 只允许 coefficient/source 参数覆盖")
            term = base.terms.get(term_id) if base is not None else None
            if base is not None and term is None:
                raise FinanceTripletError(f"{sub} 禁止新增 term")
            coefficient = term.coefficient if term is not None else None
            source = term.source if term is not None else None
            raw_out: dict = {}
            if "coefficient" in raw:
                coefficient = Money.from_dict(
                    raw["coefficient"], allow_negative=True, field=f"{sub}.coefficient"
                )
                if term is not None:
                    _check_same_unit(coefficient.unit, term.coefficient.unit, f"{sub}.coefficient")
                raw_out["coefficient"] = coefficient.to_dict()
            if "source" in raw:
                source_patch = raw["source"]
                if not isinstance(source_patch, Mapping) or not source_patch:
                    raise FinanceTripletError(f"{sub}.source 必须为非空范围/期间覆盖")
                if set(source_patch) - {"scope", "bindings", "period"}:
                    raise FinanceTripletError(f"{sub}.source 禁止改变 kind/path/id 或来源身份")
                raw_source: dict = {}
                for key, value in source_patch.items():
                    if term is not None and getattr(term.source, key) is None:
                        raise FinanceTripletError(f"{sub}.source.{key} 原来源不存在该参数")
                    if key == "scope":
                        scope = _parse_reference_scope(value, f"{sub}.source.scope")
                        if (scope.kind != "instances"
                                or term is not None and scope.kind != term.source.scope.kind):
                            raise FinanceTripletError(f"{sub}.source.scope 仅可覆盖既有明确实例范围")
                        raw_source[key] = scope.to_dict()
                    elif key == "bindings":
                        raw_source[key] = list(_reference_ids(value, f"{sub}.source.bindings"))
                    else:
                        period = parse_amount_period(value, field=f"{sub}.source.period")
                        if term is not None and period.kind != term.source.period.kind:
                            raise FinanceTripletError(f"{sub}.source.period 不得改变期间方法")
                        if period.kind not in ("financial_year", "tax_month", "year_months", "range"):
                            raise FinanceTripletError(f"{sub}.source.period 只允许覆盖固定期间")
                        if period.kind == "year_months" and period.year == "current":
                            raise FinanceTripletError(f"{sub}.source.period 只允许覆盖固定期间")
                        if (term is not None and term.source.period.kind == "year_months"
                                and term.source.period.year == "current"):
                            raise FinanceTripletError(f"{sub}.source.period 不得把 current 改为固定期间")
                        if (term is not None and period.kind == "range"
                                and period.axis != term.source.period.axis):
                            raise FinanceTripletError(f"{sub}.source.period 不得改变来源轴")
                        raw_source[key] = period.to_dict()
                raw_out["source"] = raw_source
                if term is not None:
                    source = parse_amount_reference(
                        {**term.source.to_dict(), **raw_source}, field=f"{sub}.source"
                    )
            out_terms[term_id] = raw_out
            if term is not None:
                terms[term_id] = AmountTerm(coefficient=coefficient, source=source)
        out["terms"] = out_terms
    result = (AmountDefinition(unit=base.unit, axis=base.axis,
                               constant=constant or base.constant, terms=terms)
              if base is not None else None)
    return _freeze(out), result

def _validate_movement_override(movement_id, patch, profile):
    field = f"cash_movements.{movement_id}"
    if not isinstance(patch, Mapping) or not patch or set(patch) - {"recognition", "payments"}:
        raise FinanceTripletError(f"{field} 只允许整体替换recognition/payments，不得改方向、口径或金额来源")
    base = profile.cash_movements.get(movement_id) if profile is not None else None
    if profile is not None and base is None:
        raise FinanceTripletError(f"{field} 不允许新增发生项")
    values, raw = {}, {}
    if "recognition" in patch:
        values["recognition"] = _parse_recognition(patch["recognition"], f"{field}.recognition")
        raw["recognition"] = values["recognition"].to_dict()
    if "payments" in patch:
        values["payments"] = _parse_payments(patch["payments"], f"{field}.payments")
        raw["payments"] = [row.to_dict() for row in values["payments"]]
    return _freeze(raw), replace(base, **values) if base is not None else None


def _validate_asset_override(asset_id, patch, profile):
    field = f"assets.{asset_id}"
    if not isinstance(patch, Mapping) or not patch or set(patch) - {
        "basis_from", "capitalized_at", "available_from",
    }:
        raise FinanceTripletError(f"{field} 只允许项目实例范围、capitalized_at/available_from参数")
    base = profile.assets.get(asset_id) if profile is not None else None
    if profile is not None and base is None:
        raise FinanceTripletError(f"{field} 不允许新增资产")
    values, raw = {}, {}
    if "capitalized_at" in patch:
        values["capitalized_at"] = parse_financial_time_point(
            patch["capitalized_at"], field=f"{field}.capitalized_at")
        raw["capitalized_at"] = values["capitalized_at"].to_dict()
    if "available_from" in patch:
        values["available_from"] = _pos_int(patch["available_from"], f"{field}.available_from")
        raw["available_from"] = values["available_from"]
    if "basis_from" in patch:
        source_patch = patch["basis_from"]
        if not isinstance(source_patch, Mapping) or set(source_patch) != {"scope"}:
            raise FinanceTripletError(f"{field}.basis_from 只允许覆盖既有实例scope，不得换来源")
        scope = _parse_reference_scope(source_patch["scope"], f"{field}.basis_from.scope")
        if scope.kind != "instances" or base is not None and (
            base.basis_from.scope is None or base.basis_from.scope.kind != scope.kind
        ):
            raise FinanceTripletError(f"{field}.basis_from.scope 不得改变范围种类或新增来源scope")
        raw["basis_from"] = {"scope": scope.to_dict()}
        if base is not None:
            values["basis_from"] = replace(base.basis_from, scope=scope)
    return _freeze(raw), replace(base, **values) if base is not None else None


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
    unknown = set(price_val) - {"kind", "value", "ref", "series_meta", "months"}
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
        elif prof_price.kind == "time_series":
            price_meta = prof_price.series_meta
            if price_meta is not None:
                prof_unit = price_meta.unit
        else:
            prof_unit = prof_price.months["1"].series_meta.unit
    if kind == "constant":
        if "value" not in price_val:
            raise FinanceTripletError(f"energy_prices.{price_id} constant 缺少 value")
        if "ref" in price_val or "series_meta" in price_val or "months" in price_val:
            raise FinanceTripletError(f"energy_prices.{price_id} constant 不允许 ref/series_meta/months")
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
    if kind == "monthly_daily":
        if set(price_val) != {"kind", "months"}:
            raise FinanceTripletError(f"energy_prices.{price_id} monthly_daily 必须且只能含 months")
        sources = _parse_monthly_sources(price_val["months"])
        if prof_unit is not None:
            _check_same_unit(sources["1"].series_meta.unit, prof_unit, f"energy_prices.{price_id}")
        return {
            "kind": "monthly_daily",
            "months": {month: src.to_dict() for month, src in sources.items()},
        }
    if set(price_val) != {"kind", "ref", "series_meta"}:
        raise FinanceTripletError(f"energy_prices.{price_id} time_series 必须且只能含 ref 与 series_meta")
    ref = price_val["ref"]
    if not isinstance(ref, str) or not ref.strip():
        raise FinanceTripletError(f"energy_prices.{price_id}.ref 必须为非空字符串")
    meta = SeriesMeta.from_dict(price_val["series_meta"])
    if prof_unit is not None:
        _check_same_unit(meta.unit, prof_unit, f"energy_prices.{price_id}")
    return {"kind": "time_series", "ref": ref, "series_meta": meta.to_dict()}

def _validate_tax_rule_override(
    rule_id: str,
    rule_val: Mapping[str, object],
    profile: FinanceProfile | None,
) -> tuple[Mapping[str, object], dict]:
    """校验单个 tax_rule 覆盖: 只允许替换已有参数，不增删规则、不改方法/范围/池归属。

    - vat: in_tax/out_tax/om_input_tax 稀疏条目按 price/for 对应已有对象，
      只替换已有 rate/deductible_ratio；maintenance_input_tax 整表替换；
    - construction: rate/deductible_ratio 原子；confirmation.schedule 仅 Profile
      原方法 explicit_schedule 的规则可整表替换(method 保持不变，
      payment_period 出现 schedule 直接拒绝)；
    - income: rate 原子、折旧 life_years/residual/first_year(按已有 asset 对应)、
      loss_carryforward_years；deductible_maintenance.items 与 opening_losses
      整表替换。范围列表(revenue/energy_cost/annual_om/variable_om)不可覆盖。

    返回（冻结原始补丁，已解析不可变值）：有 Profile 时一次核对即形成
    类型化值，由对象保留供绑定合并直接消费；无 Profile 时值为空。
    """
    out: dict[str, object] = {}
    prof_rule: TaxRule | None = None
    if profile is not None:
        if rule_id not in profile.tax_rules:
            raise FinanceTripletError(f"tax rule {rule_id!r} 在 Profile 中不存在, 禁止新增")
        prof_rule = profile.tax_rules[rule_id]
    for key, value in rule_val.items():
        out[str(key)] = value
    if prof_rule is None:
        return _freeze(out), {}  # type: ignore[return-value]
    parsed = _check_rule_override_shape(rule_id, out, prof_rule)
    return _freeze(out), parsed  # type: ignore[return-value]


def _check_rule_override_shape(rule_id: str, out: Mapping[str, object], prof_rule: TaxRule) -> dict:
    """覆盖键集合合法性(有 Profile 时)：禁改 kind/method/范围/池归属，禁新增规则结构键。

    返回已解析的不可变值（应用时直接替换，不再解析）。
    """
    field = f"tax_rules.{rule_id}"
    if isinstance(prof_rule, VatRule):
        allowed = {"in_tax", "out_tax", "om_input_tax", "maintenance_input_tax", "demand_input_tax",
                   "movement_input_tax", "movement_output_tax"}
        unknown = set(out) - allowed
        if unknown:
            raise FinanceTripletError(f"{field} 只允许覆盖已有参数: {sorted(unknown)}")
        for key in ("in_tax", "out_tax", "om_input_tax", "demand_input_tax"):
            if key not in out:
                continue
            raw = out[key]
            if not isinstance(raw, Mapping):
                raise FinanceTripletError(f"{field}.{key} 必须是 price/for/charge 键控的字典")
            base_entries = {
                getattr(item, "price", getattr(item, "for_path", getattr(item, "charge", None))): item
                for item in getattr(prof_rule, key)
            }
            for target, patch in raw.items():
                if target not in base_entries:
                    raise FinanceTripletError(f"{field}.{key} {target!r} 在 Profile 中不存在, 禁止新增")
                if not isinstance(patch, Mapping) or not patch:
                    raise FinanceTripletError(f"{field}.{key}.{target} 必须为非空字典")
                if key == "out_tax":
                    sub_allowed = {"rate"}
                else:
                    sub_allowed = {"rate", "deductible_ratio"}
                sub_unknown = set(patch) - sub_allowed
                if sub_unknown:
                    raise FinanceTripletError(
                        f"{field}.{key}.{target} 只允许替换已有参数: {sorted(sub_unknown)}")
        if "maintenance_input_tax" in out:
            if not isinstance(out["maintenance_input_tax"], list):
                raise FinanceTripletError(
                    f"{field}.maintenance_input_tax 必须是列表")
        for key in ("movement_input_tax", "movement_output_tax"):
            if key not in out:
                continue
            raw = out[key]
            if not isinstance(raw, Mapping) or not raw:
                raise FinanceTripletError(f"{field}.{key} 须按已有movement_id稀疏覆盖")
            base_entries = {entry.movement: entry for entry in getattr(prof_rule, key)}
            for movement_id, patch in raw.items():
                sub = f"{field}.{key}.{movement_id}"
                entry = base_entries.get(movement_id)
                if entry is None:
                    raise FinanceTripletError(f"{sub} 禁止新增税务发生身份")
                allowed = {"rate", "confirmation"} | (
                    {"deductible_ratio"} if key == "movement_input_tax" else set())
                if not isinstance(patch, Mapping) or not patch or set(patch) - allowed:
                    raise FinanceTripletError(f"{sub} 只允许rate/deductible_ratio及完整occurrences参数")
                if "confirmation" in patch:
                    confirm = patch["confirmation"]
                    if (entry.confirmation.method != "explicit_schedule" or not isinstance(confirm, Mapping)
                            or set(confirm) != {"occurrences"}):
                        raise FinanceTripletError(
                            f"{sub}.confirmation 只允许替换既有explicit_schedule的occurrences")
    elif isinstance(prof_rule, ConstructionVatRule):
        allowed = {"rate", "deductible_ratio", "confirmation"}
        unknown = set(out) - allowed
        if unknown:
            raise FinanceTripletError(f"{field} 只允许覆盖已有参数: {sorted(unknown)}")
        if "confirmation" in out:
            if prof_rule.confirmation.method != "explicit_schedule":
                raise FinanceTripletError(
                    f"{field}.confirmation.schedule 只允许 Profile 原方法 "
                    f"explicit_schedule 的规则覆盖(method 保持不变)")
            raw = out["confirmation"]
            if not isinstance(raw, Mapping) or set(raw) != {"schedule"}:
                raise FinanceTripletError(
                    f"{field}.confirmation 只允许整表替换 schedule(method 保持不变)")
    elif isinstance(prof_rule, IncomeTaxRule):
        allowed = {"rate", "depreciation", "loss_carryforward_years",
                   "deductible_maintenance", "opening_losses"}
        unknown = set(out) - allowed
        if unknown:
            raise FinanceTripletError(f"{field} 只允许覆盖已有参数: {sorted(unknown)}")
        if "depreciation" in out:
            raw = out["depreciation"]
            if not isinstance(raw, list):
                raise FinanceTripletError(f"{field}.depreciation 必须是列表(整表替换)")
        if "deductible_maintenance" in out:
            raw_maint = out["deductible_maintenance"]
            if not isinstance(raw_maint, Mapping):
                raise FinanceTripletError(f"{field}.deductible_maintenance 必须是字典")
            sub_unknown = set(raw_maint) - {"mode", "items"}
            if sub_unknown:
                raise FinanceTripletError(
                    f"{field}.deductible_maintenance 只允许覆盖 items: {sorted(sub_unknown)}")
            if "items" not in raw_maint:
                raise FinanceTripletError(f"{field}.deductible_maintenance 缺少 items")
            if "mode" in raw_maint and str(raw_maint["mode"]) != prof_rule.deductible_maintenance.mode:
                raise FinanceTripletError(
                    f"{field}.deductible_maintenance.mode 跟随规则，不可更改")
            if not isinstance(raw_maint["items"], list):
                raise FinanceTripletError(f"{field}.deductible_maintenance.items 必须是列表")
        if "opening_losses" in out:
            if not isinstance(out["opening_losses"], list):
                raise FinanceTripletError(f"{field}.opening_losses 必须是列表")
    elif isinstance(prof_rule, DerivedTaxRule):
        if set(out) - {"rate", "payment_lag"}:
            raise FinanceTripletError(f"{field} 派生税只允许覆盖rate/payment_lag")
    else:
        raise FinanceTripletError(f"{field} 未知规则类型(内部状态非法)")
    return _parse_rule_patch_values(rule_id, out, prof_rule)


def _parse_rule_patch_values(rule_id: str, patch: Mapping, rule: TaxRule) -> dict:
    """规则补丁值解析(单次，返回已解析不可变值；不做权限核对)。

    调用方保证键集合与归属已核对（未绑定走 _check_rule_override_shape，
    已绑定在保存边界核对过）。应用时直接替换，不再解析。
    """
    field = f"tax_rules.{rule_id}"
    values: dict = {}
    if isinstance(rule, VatRule):
        for key in ("in_tax", "out_tax", "om_input_tax", "demand_input_tax"):
            if key not in patch:
                continue
            atoms: dict = {}
            for target, atom_patch in patch[key].items():
                sub = f"{field}.{key}.{target}"
                atoms[target] = {
                    atom_key: _ratio_money(atom_patch[atom_key], f"{sub}.{atom_key}")
                    for atom_key in atom_patch
                }
            values[key] = atoms
        if "maintenance_input_tax" in patch:
            values["maintenance_input_tax"] = _parse_maint_input_list(
                patch["maintenance_input_tax"], f"{field}.maintenance_input_tax")
        for key in ("movement_input_tax", "movement_output_tax"):
            if key not in patch:
                continue
            entries = {}
            for movement_id, parameters in patch[key].items():
                sub = f"{field}.{key}.{movement_id}"
                values_for_entry = {}
                if "rate" in parameters:
                    values_for_entry["rate"] = _nonnegative_ratio_money(parameters["rate"], f"{sub}.rate")
                if "deductible_ratio" in parameters:
                    values_for_entry["deductible_ratio"] = _ratio_money(
                        parameters["deductible_ratio"], f"{sub}.deductible_ratio")
                if "confirmation" in parameters:
                    values_for_entry["confirmation"] = _parse_movement_confirmation(
                        {"method": "explicit_schedule", **parameters["confirmation"]}, f"{sub}.confirmation")
                entries[movement_id] = values_for_entry
            values[key] = entries
    elif isinstance(rule, ConstructionVatRule):
        for atom_key in ("rate", "deductible_ratio"):
            if atom_key in patch:
                values[atom_key] = _ratio_money(patch[atom_key], f"{field}.{atom_key}")
        if "confirmation" in patch:
            values["schedule"] = _parse_construction_schedule(
                patch["confirmation"]["schedule"], f"{field}.confirmation.schedule")
    elif isinstance(rule, IncomeTaxRule):
        if "rate" in patch:
            values["rate"] = _ratio_money(patch["rate"], f"{field}.rate")
        if "loss_carryforward_years" in patch:
            values["loss_carryforward_years"] = _nonneg_int(
                patch["loss_carryforward_years"], f"{field}.loss_carryforward_years")
        if "depreciation" in patch:
            dep_list: list[DepreciationEntry] = []
            seen_dep_sources: set[tuple] = set()
            for idx, entry in enumerate(_as_list(patch["depreciation"], f"{field}.depreciation")):
                sub = f"{field}.depreciation[{idx}]"
                if not isinstance(entry, Mapping):
                    raise FinanceTripletError(f"{sub} 必须是字典")
                _require_keys(entry, {"base_from", "method", "life_years", "residual", "first_year"}, sub)
                if str(entry["method"]) != "straight_line":
                    raise FinanceTripletError(f"{sub}.method 非法: 必须为 'straight_line'")
                base_from = _parse_amount_base_from(entry["base_from"], f"{sub}.base_from")
                residual = Money.from_dict(entry["residual"], field=f"{sub}.residual")
                dep_entry = DepreciationEntry(
                    base_from=base_from,
                    method="straight_line",
                    life_years=_pos_int(entry["life_years"], f"{sub}.life_years"),
                    residual=residual,
                    first_year=_pos_int(entry["first_year"], f"{sub}.first_year"),
                )
                ident = dep_entry.source_identity
                if ident in seen_dep_sources:
                    raise FinanceTripletError(f"{field}.depreciation 来源身份重复: {ident!r}")
                seen_dep_sources.add(ident)
                dep_list.append(dep_entry)
            values["depreciation"] = tuple(dep_list)
        if "deductible_maintenance" in patch:
            values["deductible_maintenance"] = _parse_maint_items(
                {"mode": rule.deductible_maintenance.mode,
                 "items": patch["deductible_maintenance"]["items"]},
                f"{field}.deductible_maintenance",
            )
        if "opening_losses" in patch:
            values["opening_losses"] = _parse_opening_losses(
                patch["opening_losses"], f"{field}.opening_losses")
    elif isinstance(rule, DerivedTaxRule):
        if "rate" in patch:
            values["rate"] = _nonnegative_ratio_money(patch["rate"], f"{field}.rate")
        if "payment_lag" in patch:
            values["payment_lag"] = _nonneg_int(patch["payment_lag"], f"{field}.payment_lag")
    else:
        raise FinanceTripletError(f"{field} 未知规则类型(内部状态非法)")
    return values


def _parse_maint_input_list(value: object, field: str) -> tuple[MaintInputTaxEntry, ...]:
    """逐项维护进项整表解析(单次，返回不可变条目)。"""
    raw = _as_list(value, field)
    out = []
    for index, entry in enumerate(raw):
        sub = f"{field}[{index}]"
        if not isinstance(entry, Mapping):
            raise FinanceTripletError(f"{sub} 必须是字典")
        _require_keys(entry, {"year", "position", "rate", "deductible_ratio"}, sub)
        out.append(MaintInputTaxEntry(
            year=_pos_int(entry["year"], f"{sub}.year"),
            position=_pos_int(entry["position"], f"{sub}.position"),
            rate=_ratio_money(entry["rate"], f"{sub}.rate"),
            deductible_ratio=_ratio_money(entry["deductible_ratio"], f"{sub}.deductible_ratio"),
        ))
    return tuple(out)


def _parse_maint_items(value: object, field: str) -> tuple[MaintItem, ...]:
    """所得税逐项维护整表解析(单次，返回不可变条目)。"""
    if not isinstance(value, Mapping):
        raise FinanceTripletError(f"{field} 必须是字典")
    _require_keys(value, {"mode", "items"}, field)
    if str(value["mode"]) != "explicit_items":
        raise FinanceTripletError(f"{field}.mode 非法")
    raw = _as_list(value["items"], f"{field}.items")
    out = []
    for index, entry in enumerate(raw):
        sub = f"{field}.items[{index}]"
        if not isinstance(entry, Mapping):
            raise FinanceTripletError(f"{sub} 必须是字典")
        _require_keys(entry, {"year", "position", "deductible"}, sub)
        out.append(MaintItem(
            year=_pos_int(entry["year"], f"{sub}.year"),
            position=_pos_int(entry["position"], f"{sub}.position"),
            deductible=_strict_bool(entry["deductible"], f"{sub}.deductible"),
        ))
    return tuple(out)


def _parse_opening_losses(value: object, field: str) -> tuple[OpeningLoss, ...]:
    """期初亏损整表解析(单次，返回不可变条目)。"""
    raw = _as_list(value, field)
    out = []
    for index, entry in enumerate(raw):
        sub = f"{field}[{index}]"
        if not isinstance(entry, Mapping):
            raise FinanceTripletError(f"{sub} 必须是字典")
        _require_keys(entry, {"amount", "last_usable_year"}, sub)
        out.append(OpeningLoss(
            amount=Money.from_dict(entry["amount"], field=f"{sub}.amount"),
            last_usable_year=_pos_int(entry["last_usable_year"], f"{sub}.last_usable_year"),
        ))
    return tuple(out)


def _parse_construction_schedule(value: object, field: str) -> tuple[ConstructionScheduleEntry, ...]:
    """建设确认时刻表解析(单次；比例非负、合计精确为 1，返回不可变条目)。"""
    raw = _as_list(value, field)
    out = []
    for index, entry in enumerate(raw):
        sub = f"{field}[{index}]"
        if not isinstance(entry, Mapping):
            raise FinanceTripletError(f"{sub} 必须是字典")
        _require_keys(entry, {"period", "ratio"}, sub)
        ratio = _to_decimal_str(entry["ratio"], f"{sub}.ratio")
        if ratio < 0:
            raise FinanceTripletError(f"{sub}.ratio 必须非负")
        out.append(ConstructionScheduleEntry(
            period=_pos_int(entry["period"], f"{sub}.period"), ratio=ratio))
    if not out:
        raise FinanceTripletError(f"{field} 不得为空")
    if sum((item.ratio for item in out), Decimal("0")) != Decimal("1"):
        raise FinanceTripletError(f"{field} 比例合计必须精确为 1")
    return tuple(out)


def _validate_pool_override(
    pool_id: str,
    pool_val: Mapping[str, object],
    profile: FinanceProfile | None,
) -> tuple[Mapping[str, object], dict]:
    """校验单个池覆盖: 只允许 opening_credit 原子或 refunds 整表替换，不增删池，不改策略。

    返回（冻结原始补丁，已解析不可变值）：有 Profile 时键集合核对与值
    解析一次完成，由对象保留供绑定合并直接消费；无 Profile 时只做键集合
    核对，值留待首次绑定核对，对象保留值为空。
    """
    field = f"credit_pools.{pool_id}"
    if profile is not None and pool_id not in profile.credit_pools:
        raise FinanceTripletError(f"credit pool {pool_id!r} 在 Profile 中不存在, 禁止新增")
    unknown = set(pool_val) - {"opening_credit", "refunds"}
    if unknown:
        raise FinanceTripletError(f"{field} 只允许覆盖已有参数: {sorted(unknown)}")
    if not pool_val:
        raise FinanceTripletError(f"{field} 必须覆盖至少一个叶子")
    if profile is None:
        return _freeze(dict(pool_val)), {}  # type: ignore[return-value]
    parsed = _check_pool_patch_shape(pool_id, dict(pool_val))
    out: dict[str, object] = {}
    if "opening_credit" in parsed:
        out["opening_credit"] = parsed["opening_credit"].to_dict()
    if "refunds" in parsed:
        out["refunds"] = [refund.to_dict() for refund in parsed["refunds"]]
    return _freeze(out), parsed  # type: ignore[return-value]


def _validate_net_cashflow_override(
    cf_id: str,
    cf_val: Mapping[str, object],
    profile: FinanceProfile | None,
) -> tuple[dict[str, object], dict]:
    """校验单个 net_cashflows 覆盖: 只允许覆盖已有条目的 at 或 amount(固定 value 或比例 ratio)。"""
    field = f"net_cashflows.{cf_id}"
    prof_cf = profile.net_cashflows.get(cf_id) if profile is not None else None
    if prof_cf is None and profile is not None:
        raise FinanceTripletError(f"{field} 在 Profile 中不存在, 禁止新增")
    if not isinstance(cf_val, Mapping) or not cf_val:
        raise FinanceTripletError(f"{field} 必须为非空字典")
    allowed_keys = {"at", "amount"}
    unknown = set(cf_val) - allowed_keys
    if unknown:
        raise FinanceTripletError(f"{field} 只允许覆盖已有参数: {sorted(unknown)}")
    raw_patch: dict[str, object] = {}
    parsed_values: dict[str, object] = {}
    if "at" in cf_val:
        at = _parse_cashflow_at(cf_val["at"], f"{field}.at")
        raw_patch["at"] = at.to_dict()
        parsed_values["at"] = at
    if "amount" in cf_val:
        raw_amt = cf_val["amount"]
        if not isinstance(raw_amt, Mapping):
            raise FinanceTripletError(f"{field}.amount 必须是字典")
        if prof_cf is not None:
            if prof_cf.amount.kind == "fixed":
                if set(raw_amt) != {"value"}:
                    raise FinanceTripletError(f"{field}.amount 只允许覆盖 value (Money)")
                val = Money.from_dict(raw_amt["value"], field=f"{field}.amount.value")
                if val.unit != profile.currency:
                    raise FinanceTripletError(f"{field}.amount.value 币种必须为 {profile.currency}")
                if val.value < 0:
                    raise FinanceTripletError(f"{field}.amount.value 金额禁止负数")
                raw_patch["amount"] = {"value": val.to_dict()}
                parsed_values["amount"] = FixedCashflowAmount(value=val)
            elif prof_cf.amount.kind == "source_share":
                if set(raw_amt) != {"ratio"}:
                    raise FinanceTripletError(f"{field}.amount 只允许覆盖 ratio (Money)")
                ratio = Money.from_dict(raw_amt["ratio"], field=f"{field}.amount.ratio")
                if ratio.unit != _DIMLESS_UNIT:
                    raise FinanceTripletError(f"{field}.amount.ratio 单位必须为 '1'")
                if ratio.value < 0:
                    raise FinanceTripletError(f"{field}.amount.ratio 必须非负")
                raw_patch["amount"] = {"ratio": ratio.to_dict()}
                parsed_values["amount"] = SourceShareCashflowAmount(
                    base_from=prof_cf.amount.base_from, ratio=ratio
                )
            else:
                raise FinanceTripletError(f"{field}.amount amount_ref 的来源身份不允许覆盖")
        else:
            raw_patch["amount"] = dict(raw_amt)
    return raw_patch, parsed_values


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
    credit_pools: Mapping[str, CreditPool] = field(default_factory=lambda: MappingProxyType({}))
    tax_rules: Mapping[str, TaxRule] = field(default_factory=lambda: MappingProxyType({}))
    demand_charges: Mapping[str, DemandCharge] = field(default_factory=lambda: MappingProxyType({}))
    net_cashflows: Mapping[str, NetCashflowEntry] = field(default_factory=lambda: MappingProxyType({}))
    amounts: Mapping[str, AmountDefinition] = field(default_factory=lambda: MappingProxyType({}))
    cash_movements: Mapping[str, CashMovement] = field(default_factory=lambda: MappingProxyType({}))
    assets: Mapping[str, CapitalizedAsset] = field(default_factory=lambda: MappingProxyType({}))

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
        object.__setattr__(self, "demand_charges", MappingProxyType(dict(sorted(self.demand_charges.items()))))
        object.__setattr__(self, "taxes", MappingProxyType(dict(sorted(self.taxes.items()))))
        object.__setattr__(self, "credit_pools", MappingProxyType(dict(sorted(self.credit_pools.items()))))
        object.__setattr__(self, "tax_rules", MappingProxyType(dict(sorted(self.tax_rules.items()))))
        object.__setattr__(self, "net_cashflows", MappingProxyType(dict(sorted(self.net_cashflows.items()))))
        object.__setattr__(self, "amounts", MappingProxyType(dict(sorted(self.amounts.items()))))
        object.__setattr__(
            self, "cash_movements", MappingProxyType(dict(sorted(self.cash_movements.items())))
        )
        object.__setattr__(self, "assets", MappingProxyType(dict(sorted(self.assets.items()))))

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
            "demand_charges": {k: v.to_dict() for k, v in sorted(self.demand_charges.items())},
            "taxes": {k: v.to_dict() for k, v in sorted(self.taxes.items())},
            "credit_pools": {k: v.to_dict() for k, v in sorted(self.credit_pools.items())},
            "tax_rules": {k: v.to_dict() for k, v in sorted(self.tax_rules.items())},
            "net_cashflows": {k: v.to_dict() for k, v in sorted(self.net_cashflows.items())},
            "amounts": {k: v.to_dict() for k, v in self.amounts.items()},
            "cash_movements": {k: v.to_dict() for k, v in self.cash_movements.items()},
            "assets": {k: v.to_dict() for k, v in self.assets.items()},
        }

    @classmethod
    def from_dict(cls, mapping: object) -> EffectiveFinanceConfig:
        if not isinstance(mapping, Mapping):
            raise FinanceTripletError("EffectiveFinanceConfig 必须是字典")
        unknown = set(mapping) - {
            "schema", "schema_version", "profile_id",
            "currency", "base_year", "price_basis", "cost_method",
            "finance_types", "energy_prices", "demand_charges", "taxes",
            "credit_pools", "tax_rules", "net_cashflows", "amounts", "cash_movements", "assets",
        }
        if unknown:
            raise FinanceTripletError(f"EffectiveFinanceConfig 未知字段: {sorted(unknown)}")
        missing = {
            "schema", "schema_version", "profile_id",
            "currency", "base_year", "price_basis", "cost_method",
            "finance_types", "energy_prices", "demand_charges",
            "credit_pools", "tax_rules", "net_cashflows",
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
        demand_charges = _parse_demand_charges(mapping["demand_charges"], currency)
        taxes_raw = mapping.get("taxes") or {}
        if not isinstance(taxes_raw, Mapping):
            raise FinanceTripletError("taxes 必须是字典")
        taxes: dict[str, TaxEntry] = {}
        for tax_id, tax_val in taxes_raw.items():
            tax_id = _validate_local_id(tax_id, "tax id")
            if tax_val is None:
                raise FinanceTripletError(f"taxes.{tax_id} 禁止 null")
            taxes[tax_id] = TaxEntry.from_dict(tax_val)

        pools_raw = _require_section(mapping, "credit_pools", "credit_pools")
        credit_pools: dict[str, CreditPool] = {}
        for pool_id, pool_val in pools_raw.items():
            pool_id = _validate_local_id(pool_id, "credit pool")
            if pool_val is None:
                raise FinanceTripletError(f"credit_pools.{pool_id} 禁止 null")
            credit_pools[pool_id] = CreditPool.from_dict(pool_val, field=f"credit_pools.{pool_id}")

        rules_raw = _require_section(mapping, "tax_rules", "tax_rules")
        tax_rules: dict[str, TaxRule] = {}
        for rule_id, rule_val in rules_raw.items():
            rule_id = _validate_local_id(rule_id, "tax rule")
            if rule_val is None:
                raise FinanceTripletError(f"tax_rules.{rule_id} 禁止 null")
            tax_rules[rule_id] = _parse_tax_rule(rule_id, rule_val)
        net_cashflows_raw = _require_section(mapping, "net_cashflows", "net_cashflows")
        net_cashflows = _parse_net_cashflows(net_cashflows_raw, currency)
        amounts = _parse_amounts(mapping.get("amounts", {}), currency)
        cash_movements = _parse_movements(mapping.get("cash_movements", {}))
        assets = _parse_assets(mapping.get("assets", {}))
        _check_amount_refs(amounts, currency=currency, finance_types=finance_types,
                           energy_prices=energy_prices, demand_charges=demand_charges,
                           credit_pools=credit_pools, tax_rules=tax_rules, net_cashflows=net_cashflows,
                           cash_movements=cash_movements, assets=assets)
        _check_f2_refs(amounts=amounts, cash_movements=cash_movements, assets=assets,
                       tax_rules=tax_rules, currency=currency, finance_types=finance_types,
                       credit_pools=credit_pools)
        _check_tax_refs(
            currency=currency,
            energy_prices=energy_prices,
            finance_types=finance_types,
            demand_charges=demand_charges,
            taxes=taxes,
            credit_pools=credit_pools,
            tax_rules=tax_rules,
            net_cashflows=net_cashflows,
        )

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
            demand_charges=demand_charges,
            taxes=taxes,
            credit_pools=credit_pools,
            tax_rules=tax_rules,
            net_cashflows=net_cashflows,
            amounts=amounts,
            cash_movements=cash_movements,
            assets=assets,
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

    bound = overrides._bound_profile_object is profile
    # 输入边界已产生不可变替换值；未覆盖类别直接共享，不序列化后重校验。
    merged_ft = dict(profile.finance_types)
    for ft_name, ft_override in sorted(overrides.finance_types.items()):
        if ft_name not in merged_ft:
            raise FinanceTripletError(f"finance_type {ft_name!r} 在 Profile 中不存在, 禁止新增")
        merged_ft[ft_name] = (overrides._parsed_finance_types[ft_name] if bound else
                              _validate_ft_override(ft_name, ft_override, profile)[1])

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
            merged_ep[price_id] = EnergyPrice.from_dict({
                "carrier": prof_price.carrier,
                "direction": prof_price.direction,
                **price_override_dict,
            })

    # ---- demand_charges: only existing Profile rates may be overridden ----
    merged_demand = dict(profile.demand_charges)
    for charge_id, patch in sorted(overrides.demand_charges.items()):
        if charge_id not in profile.demand_charges:
            raise FinanceTripletError(f"demand_charges.{charge_id} 不允许新增费项")
        rate = Money.from_dict(patch["rate"], field=f"demand_charges.{charge_id}.rate")
        _check_same_unit(rate.unit, profile.demand_charges[charge_id].rate.unit, f"demand_charges.{charge_id}.rate")
        merged_demand[charge_id] = DemandCharge(rate=rate)

    # ---- taxes: 税目只属于 Profile, 原样继承 ----
    merged_taxes: dict[str, TaxEntry] = dict(profile.taxes)

    # ---- credit_pools / tax_rules / net_cashflows: 复用已解析不可变事实，类型化应用
    # 覆盖变更。与 Profile 绑定过的 Overrides 直接消费对象保留的已解析
    # 类型化补丁，不再复核、不重解析；无绑定 Overrides 在此做唯一一次覆盖
    # 权限核对与值解析。合并后做一次组合领域引用核对；不重解析未覆盖项。
    for pool_id in sorted(overrides.credit_pools):
        if pool_id not in profile.credit_pools:
            raise FinanceTripletError(f"credit pool {pool_id!r} 在 Profile 中不存在, 禁止新增")
    for rule_id in sorted(overrides.tax_rules):
        if rule_id not in profile.tax_rules:
            raise FinanceTripletError(f"tax rule {rule_id!r} 在 Profile 中不存在, 禁止新增")
    for cf_id in sorted(overrides.net_cashflows):
        if cf_id not in profile.net_cashflows:
            raise FinanceTripletError(f"net_cashflows.{cf_id} 在 Profile 中不存在, 禁止新增")
    merged_pools: dict[str, CreditPool] = {}
    for pool_id, pool in sorted(profile.credit_pools.items()):
        patch = _thaw(overrides.credit_pools.get(pool_id, {}))
        if not isinstance(patch, dict):
            raise FinanceTripletError(f"credit_pools.{pool_id} 覆盖形态非法")
        if not bound and patch:
            pool_values = _check_pool_patch_shape(pool_id, patch)
        elif patch:
            pool_values = overrides._parsed_credit_pools[pool_id]
        else:
            pool_values = {}
        merged_pools[pool_id] = _apply_pool_patch(pool, pool_values)
    merged_rules: dict[str, TaxRule] = {}
    for rule_id, rule in sorted(profile.tax_rules.items()):
        patch = _thaw(overrides.tax_rules.get(rule_id, {}))
        if not isinstance(patch, dict):
            raise FinanceTripletError(f"tax_rules.{rule_id} 覆盖形态非法")
        if not bound and patch:
            rule_values = _check_rule_override_shape(rule_id, patch, rule)
        elif patch:
            rule_values = overrides._parsed_tax_rules[rule_id]
        else:
            rule_values = {}
        merged_rules[rule_id] = _apply_rule_patch(rule_id, rule, rule_values)

    merged_cf: dict[str, NetCashflowEntry] = dict(profile.net_cashflows)
    for cf_id, patch in sorted(overrides.net_cashflows.items()):
        prof_cf = profile.net_cashflows[cf_id]
        # 与税则/池一致：已绑定不再解析；未绑定首次与 Profile 组合时
        # 调用同一解析器完成身份、覆盖权限、币种与形态检查。
        values = (overrides._parsed_net_cashflows[cf_id] if bound
                  else _validate_net_cashflow_override(cf_id, patch, profile)[1])
        merged_cf[cf_id] = NetCashflowEntry(
            at=values.get("at", prof_cf.at),
            direction=prof_cf.direction,
            amount=values.get("amount", prof_cf.amount),
        )

    merged_amounts = dict(profile.amounts)
    for amount_id, patch in overrides.amounts.items():
        if bound:
            merged_amounts[amount_id] = overrides._parsed_amounts[amount_id]
        else:
            merged_amounts[amount_id] = _validate_amount_override(amount_id, _thaw(patch), profile)[1]
    merged_movements, merged_assets = dict(profile.cash_movements), dict(profile.assets)
    for movement_id, patch in overrides.cash_movements.items():
        merged_movements[movement_id] = (overrides._parsed_cash_movements[movement_id] if bound else
                                         _validate_movement_override(movement_id, _thaw(patch), profile)[1])
    for asset_id, patch in overrides.assets.items():
        merged_assets[asset_id] = (overrides._parsed_assets[asset_id] if bound else
                                  _validate_asset_override(asset_id, _thaw(patch), profile)[1])
    _check_amount_refs(merged_amounts, currency=profile.currency, finance_types=merged_ft,
                       energy_prices=merged_ep, demand_charges=merged_demand,
                       credit_pools=merged_pools, tax_rules=merged_rules, net_cashflows=merged_cf,
                       cash_movements=merged_movements, assets=merged_assets)
    _check_f2_refs(amounts=merged_amounts, cash_movements=merged_movements, assets=merged_assets,
                   tax_rules=merged_rules, currency=profile.currency, finance_types=merged_ft,
                   credit_pools=merged_pools)

    _check_tax_refs(
        currency=profile.currency,
        energy_prices=profile.energy_prices,
        finance_types=profile.finance_types,
        demand_charges=merged_demand,
        taxes=profile.taxes,
        credit_pools=merged_pools,
        tax_rules=merged_rules,
        net_cashflows=merged_cf,
    )

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
        demand_charges=merged_demand,
        taxes=merged_taxes,
        credit_pools=merged_pools,
        tax_rules=merged_rules,
        net_cashflows=merged_cf,
        amounts=merged_amounts,
        cash_movements=merged_movements,
        assets=merged_assets,
    )


def _check_pool_patch_shape(pool_id: str, patch: dict) -> dict:
    """池覆盖键集合合法性：只允许 opening_credit 原子或 refunds 整表替换。

    返回已解析的不可变值（应用时直接替换，不再解析）。
    """
    field = f"credit_pools.{pool_id}"
    unknown = set(patch) - {"opening_credit", "refunds"}
    if unknown:
        raise FinanceTripletError(f"{field} 只允许覆盖已有参数: {sorted(unknown)}")
    if not patch:
        raise FinanceTripletError(f"{field} 必须覆盖至少一个叶子")
    return _parse_pool_patch_values(pool_id, patch)


def _parse_pool_patch_values(pool_id: str, patch: Mapping) -> dict:
    """池补丁值解析（单次；供绑定路径复用，不做权限核对）。"""
    field = f"credit_pools.{pool_id}"
    values: dict = {}
    if "opening_credit" in patch:
        values["opening_credit"] = Money.from_dict(
            patch["opening_credit"], field=f"{field}.opening_credit")
    if "refunds" in patch:
        raw = patch["refunds"]
        if not isinstance(raw, list):
            raise FinanceTripletError(f"{field}.refunds 必须是列表")
        values["refunds"] = tuple(
            PoolRefund.from_dict(entry, field=f"{field}.refunds[{index}]")
            for index, entry in enumerate(raw)
        )
    return values


def _apply_pool_patch(pool: CreditPool, values: Mapping) -> CreditPool:
    """池覆盖的类型化应用（只消费边界已解析值，不解析、不重验未覆盖项）。"""
    return CreditPool(
        opening_credit=values.get("opening_credit", pool.opening_credit),
        receivable_policy=pool.receivable_policy,
        refunds=values.get("refunds", pool.refunds),
    )


def _apply_rule_patch(rule_id: str, rule: TaxRule, values: Mapping) -> TaxRule:
    """规则覆盖的类型化应用（只消费边界已解析值，不解析、不重验未覆盖项）。"""
    field = f"tax_rules.{rule_id}"
    if isinstance(rule, VatRule):
        in_tax = list(rule.in_tax)
        out_tax = list(rule.out_tax)
        om_input_tax = list(rule.om_input_tax)
        demand_input_tax = list(rule.demand_input_tax)
        movement_entries = {}
        for key in ("movement_input_tax", "movement_output_tax"):
            movement_entries[key] = tuple(
                replace(entry, **values.get(key, {}).get(entry.movement, {}))
                for entry in getattr(rule, key)
            )
        for key, base in (("in_tax", in_tax), ("out_tax", out_tax), ("om_input_tax", om_input_tax), ("demand_input_tax", demand_input_tax)):
            for target, atom_values in values.get(key, {}).items():
                base[_rule_entry_index(base, target, field, key)] = _patched_entry(
                    base, target, atom_values, field, key)
        return VatRule(
            label_ref=rule.label_ref,
            settlement=rule.settlement,
            in_tax=tuple(in_tax),
            out_tax=tuple(out_tax),
            om_input_tax=tuple(om_input_tax),
            demand_input_tax=tuple(demand_input_tax),
            maintenance_input_tax=values.get(
                "maintenance_input_tax", rule.maintenance_input_tax),
            credit_pool=rule.credit_pool,
            confirmation=rule.confirmation,
            **movement_entries,
        )
    if isinstance(rule, ConstructionVatRule):
        confirmation = rule.confirmation
        if "schedule" in values:
            confirmation = ConstructionConfirmation(
                method=rule.confirmation.method, schedule=values["schedule"])
        return ConstructionVatRule(
            label_ref=rule.label_ref,
            for_path=rule.for_path,
            rate=values.get("rate", rule.rate),
            deductible_ratio=values.get("deductible_ratio", rule.deductible_ratio),
            credit_pool=rule.credit_pool,
            confirmation=confirmation,
        )
    if isinstance(rule, IncomeTaxRule):
        depreciation = list(rule.depreciation)
        if "depreciation" in values:
            depreciation = list(values["depreciation"])
        deductible_maintenance = rule.deductible_maintenance
        if "deductible_maintenance" in values:
            deductible_maintenance = DeductibleMaintenance(
                mode=rule.deductible_maintenance.mode,
                items=values["deductible_maintenance"],
            )
        return IncomeTaxRule(
            label_ref=rule.label_ref,
            rate=values.get("rate", rule.rate),
            revenue=rule.revenue,
            energy_cost=rule.energy_cost,
            demand_cost=rule.demand_cost,
            movement_revenue=rule.movement_revenue,
            movement_cost=rule.movement_cost,
            derived_tax_cost=rule.derived_tax_cost,
            annual_om=rule.annual_om,
            variable_om=rule.variable_om,
            deductible_maintenance=deductible_maintenance,
            depreciation=tuple(depreciation),
            loss_carryforward_years=values.get(
                "loss_carryforward_years", rule.loss_carryforward_years),
            opening_losses=values.get("opening_losses", rule.opening_losses),
        )
    if isinstance(rule, DerivedTaxRule):
        return replace(rule, **values)
    raise FinanceTripletError(f"{field} 未知规则类型(内部状态非法)")


def _rule_entry_index(base: list, target: str, field: str, key: str) -> int:
    """稀疏条目按 price/for 定位基线下标(纪律已核对，缺席为内部非法)。"""
    for index, entry in enumerate(base):
        candidate = getattr(entry, "price", getattr(entry, "for_path", getattr(entry, "charge", None)))
        if candidate == target:
            return index
    raise FinanceTripletError(f"{field}.{key} {target!r} 在 Profile 中不存在(内部状态非法)")


def _patched_entry(base: list, target: str, atom_values: Mapping, field: str, key: str):
    """稀疏条目原子替换（只消费边界已解析值，不解析）。"""
    index = _rule_entry_index(base, target, field, key)
    old = base[index]
    rate = atom_values.get("rate", old.rate)
    if isinstance(old, OutTaxEntry):
        return OutTaxEntry(price=old.price, rate=rate)
    ratio = atom_values.get("deductible_ratio", old.deductible_ratio)  # type: ignore[union-attr]
    if isinstance(old, InTaxEntry):
        return InTaxEntry(price=old.price, rate=rate, deductible_ratio=ratio)
    if isinstance(old, DemandInputTaxEntry):
        return DemandInputTaxEntry(charge=old.charge, rate=rate, deductible_ratio=ratio)
    return OmInputTaxEntry(for_path=old.for_path, rate=rate, deductible_ratio=ratio)  # type: ignore[union-attr]
