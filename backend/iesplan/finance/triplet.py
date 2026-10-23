"""财务三件套领域结构: FinanceProfile / FinanceOverrides / EffectiveFinanceConfig (finance-yaml@3.0.0)。

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
SCHEMA_VERSION: Final[str] = "4.0.0"

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
TAX_RULE_KINDS: Final[tuple[str, ...]] = ("vat", "construction_input_vat", "income_tax")
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
class VatRule:
    """``kind: vat`` 规则(按月结算；期初/退款归池所有，本规则不携带)。"""

    label_ref: str
    settlement: str
    in_tax: tuple[InTaxEntry, ...]
    out_tax: tuple[OutTaxEntry, ...]
    om_input_tax: tuple[OmInputTaxEntry, ...]
    maintenance_input_tax: tuple[MaintInputTaxEntry, ...]
    demand_input_tax: tuple[DemandInputTaxEntry, ...]
    credit_pool: str
    confirmation: str

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
class DepreciationEntry:
    """直线折旧条目: base_from 仅为同类 ``<asset>.upfront_capex_net`` 派生引用。"""

    asset: str
    base_from: str
    method: str
    life_years: int
    residual: Money
    first_year: int

    def to_dict(self) -> dict:
        return {
            "asset": self.asset,
            "base_from": self.base_from,
            "method": self.method,
            "life_years": self.life_years,
            "residual": self.residual.to_dict(),
            "first_year": self.first_year,
        }


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
    demand_cost: tuple[str, ...]
    annual_om: tuple[str, ...]
    variable_om: tuple[str, ...]
    deductible_maintenance: DeductibleMaintenance
    depreciation: tuple[DepreciationEntry, ...]
    loss_carryforward_years: int
    opening_losses: tuple[OpeningLoss, ...]

    def to_dict(self) -> dict:
        return {
            "kind": "income_tax",
            "label_ref": self.label_ref,
            "rate": self.rate.to_dict(),
            "revenue": list(self.revenue),
            "energy_cost": list(self.energy_cost),
            **({"demand_cost": list(self.demand_cost)} if self.demand_cost else {}),
            "annual_om": list(self.annual_om),
            "variable_om": list(self.variable_om),
            "deductible_maintenance": self.deductible_maintenance.to_dict(),
            "depreciation": [item.to_dict() for item in self.depreciation],
            "loss_carryforward_years": self.loss_carryforward_years,
            "opening_losses": [item.to_dict() for item in self.opening_losses],
        }


#: 规则并集类型(文件内规则 ID → 规则对象)。
TaxRule = VatRule | ConstructionVatRule | IncomeTaxRule


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
        optional={"demand_input_tax"},
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
        optional={"demand_cost"},
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
            entry, {"asset", "base_from", "method", "life_years", "residual", "first_year"}, sub)
        if str(entry["method"]) != "straight_line":
            raise FinanceTripletError(f"{sub}.method 非法")
        depreciation.append(DepreciationEntry(
            asset=_validate_local_id(entry["asset"], f"{sub}.asset"),
            base_from=str(entry["base_from"]),
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


def _check_tax_refs(
    *,
    currency: str,
    energy_prices: Mapping[str, EnergyPrice],
    finance_types: Mapping[str, FinanceTypeEntry],
    demand_charges: Mapping[str, DemandCharge],
    taxes: Mapping[str, TaxEntry],
    credit_pools: Mapping[str, CreditPool],
    tax_rules: Mapping[str, TaxRule],
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
    seen_om: dict[str, str] = {}
    seen_for: dict[str, str] = {}
    seen_income_price: dict[str, str] = {}
    seen_income_om: dict[str, str] = {}
    seen_demand_input: dict[str, str] = {}
    seen_demand_cost: dict[str, str] = {}
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
                _claim_component(seen_om, entry.for_path, rule_id, ft_paths,
                                 ("annual_fixed_om", "period_variable_om"), field)
            seen_maint: set[tuple[int, int]] = set()
            for entry in rule.maintenance_input_tax:
                key = (entry.year, entry.position)
                if key in seen_maint:
                    raise FinanceTripletError(
                        f"{field}.maintenance_input_tax 维护引用重复: {key!r}")
                seen_maint.add(key)
        elif isinstance(rule, ConstructionVatRule):
            _claim_component(seen_for, rule.for_path, rule_id, ft_paths,
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
            seen_assets: set[str] = set()
            for item in rule.depreciation:
                if item.asset not in finance_types:
                    raise FinanceTripletError(f"{field}.depreciation.asset 不存在: {item.asset!r}")
                if item.asset in seen_assets:
                    raise FinanceTripletError(
                        f"{field}.depreciation 资产引用重复: {item.asset!r}")
                seen_assets.add(item.asset)
                if item.base_from != f"{item.asset}.upfront_capex_net":
                    raise FinanceTripletError(
                        f"{field}.depreciation.base_from 须为 {item.asset}.upfront_capex_net")
                if item.residual.unit != currency:
                    raise FinanceTripletError(f"{field}.depreciation.residual 单位须为 Profile 币种")


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
    credit_pools: Mapping[str, CreditPool] = field(default_factory=lambda: MappingProxyType({}))
    tax_rules: Mapping[str, TaxRule] = field(default_factory=lambda: MappingProxyType({}))
    demand_charges: Mapping[str, DemandCharge] = field(default_factory=lambda: MappingProxyType({}))

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
        # taxes.applies_to 引用存在性: 引用本文件 energy_prices 的 price_id,
        # 或 finance_types 的分量路径(<finance_type>.<分量>, 如 pv_system.upfront_capex)。
        for tax_id, tax in self.taxes.items():
            target = tax.applies_to
            if target in self.energy_prices or (target.startswith("demand_charges.") and target.removeprefix("demand_charges.") in self.demand_charges):
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
            "demand_charges": {k: v.to_dict() for k, v in sorted(self.demand_charges.items())},
            "taxes": {k: v.to_dict() for k, v in sorted(self.taxes.items())},
            "credit_pools": {k: v.to_dict() for k, v in sorted(self.credit_pools.items())},
            "tax_rules": {k: v.to_dict() for k, v in sorted(self.tax_rules.items())},
        }

    @classmethod
    def from_dict(cls, mapping: object) -> FinanceProfile:
        if not isinstance(mapping, Mapping):
            raise FinanceTripletError("FinanceProfile 必须是字典")
        known_profile = {"schema", "schema_version", "profile", "finance_types", "energy_prices", "demand_charges", "taxes",
                         "credit_pools", "tax_rules"}
        unknown = set(mapping) - known_profile
        if unknown:
            raise FinanceTripletError(f"FinanceProfile 未知字段: {sorted(unknown)}")
        missing = {"schema", "schema_version", "profile", "finance_types", "energy_prices", "demand_charges"} - set(mapping)
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
        _check_tax_refs(
            currency=currency,
            energy_prices=energy_prices,
            finance_types=finance_types,
            demand_charges=demand_charges,
            taxes=taxes,
            credit_pools=credit_pools,
            tax_rules=tax_rules,
        )

        return cls(
            schema=schema,
            schema_version=schema_version,
            profile=profile,
            finance_types=finance_types,
            energy_prices=energy_prices,
            taxes=taxes,
            credit_pools=credit_pools,
            tax_rules=tax_rules,
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
    #: 解析时绑定的 Profile id（合并器据此判定覆盖权限是否已核对；不序列化）。
    _bound_profile: str = ""
    #: 输入边界已形成的不可变类型化补丁（规则 id → 已解析值；未绑定为空）。
    _parsed_tax_rules: Mapping[str, Mapping[str, object]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    #: 输入边界已形成的不可变类型化补丁（池 id → 已解析值；未绑定为空）。
    _parsed_credit_pools: Mapping[str, Mapping[str, object]] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self) -> None:
        if self.schema != SCHEMA_OVERRIDES:
            raise FinanceTripletError(f"schema 必须为 {SCHEMA_OVERRIDES!r}")
        if self.schema_version != SCHEMA_VERSION:
            raise FinanceTripletError(f"schema_version 必须为 {SCHEMA_VERSION!r}")
        object.__setattr__(self, "profile_ref", MappingProxyType(dict(sorted(self.profile_ref.items()))))
        object.__setattr__(self, "finance_types", MappingProxyType(dict(sorted(self.finance_types.items()))))
        object.__setattr__(self, "energy_prices", MappingProxyType(dict(sorted(self.energy_prices.items()))))
        object.__setattr__(self, "demand_charges", MappingProxyType(dict(sorted(self.demand_charges.items()))))
        object.__setattr__(self, "tax_rules", MappingProxyType(dict(sorted(self.tax_rules.items()))))
        object.__setattr__(self, "credit_pools", MappingProxyType(dict(sorted(self.credit_pools.items()))))
        object.__setattr__(
            self, "_parsed_tax_rules", MappingProxyType(dict(sorted(self._parsed_tax_rules.items())))
        )
        object.__setattr__(
            self, "_parsed_credit_pools", MappingProxyType(dict(sorted(self._parsed_credit_pools.items())))
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
        known_overrides = {"schema", "schema_version", "profile_ref", "finance_types", "energy_prices", "demand_charges",
                           "tax_rules", "credit_pools"}
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

        return cls(
            schema=schema,
            schema_version=schema_version,
            profile_ref=profile_ref,
            finance_types=finance_types,
            energy_prices=energy_prices,
            demand_charges=demand_charges,
            tax_rules=tax_rules,
            credit_pools=credit_pools,
            _bound_profile=profile.profile_id if profile is not None else "",
            _parsed_tax_rules=_freeze(parsed_rules),
            _parsed_credit_pools=_freeze(parsed_pools),
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
        allowed = {"in_tax", "out_tax", "om_input_tax", "maintenance_input_tax", "demand_input_tax"}
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
            if not isinstance(raw, Mapping):
                raise FinanceTripletError(f"{field}.depreciation 必须是 asset 键控的字典")
            base_assets = {item.asset for item in prof_rule.depreciation}
            for asset, patch in raw.items():
                if asset not in base_assets:
                    raise FinanceTripletError(f"{field}.depreciation asset {asset!r} 在 Profile 中不存在")
                if not isinstance(patch, Mapping) or not patch:
                    raise FinanceTripletError(f"{field}.depreciation.{asset} 必须为非空字典")
                sub_unknown = set(patch) - {"life_years", "residual", "first_year"}
                if sub_unknown:
                    raise FinanceTripletError(
                        f"{field}.depreciation.{asset} 只允许覆盖已有参数: {sorted(sub_unknown)}")
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
            dep: dict = {}
            for asset, asset_patch in patch["depreciation"].items():
                sub = f"{field}.depreciation.{asset}"
                entry_values: dict = {}
                if "life_years" in asset_patch:
                    entry_values["life_years"] = _pos_int(
                        asset_patch["life_years"], f"{sub}.life_years")
                if "first_year" in asset_patch:
                    entry_values["first_year"] = _pos_int(
                        asset_patch["first_year"], f"{sub}.first_year")
                if "residual" in asset_patch:
                    entry_values["residual"] = Money.from_dict(
                        asset_patch["residual"], field=f"{sub}.residual")
                dep[asset] = entry_values
            values["depreciation"] = dep
        if "deductible_maintenance" in patch:
            values["deductible_maintenance"] = _parse_maint_items(
                {"mode": rule.deductible_maintenance.mode,
                 "items": patch["deductible_maintenance"]["items"]},
                f"{field}.deductible_maintenance",
            )
        if "opening_losses" in patch:
            values["opening_losses"] = _parse_opening_losses(
                patch["opening_losses"], f"{field}.opening_losses")
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
        }

    @classmethod
    def from_dict(cls, mapping: object) -> EffectiveFinanceConfig:
        if not isinstance(mapping, Mapping):
            raise FinanceTripletError("EffectiveFinanceConfig 必须是字典")
        unknown = set(mapping) - {
            "schema", "schema_version", "profile_id",
            "currency", "base_year", "price_basis", "cost_method",
            "finance_types", "energy_prices", "demand_charges", "taxes",
            "credit_pools", "tax_rules",
        }
        if unknown:
            raise FinanceTripletError(f"EffectiveFinanceConfig 未知字段: {sorted(unknown)}")
        missing = {
            "schema", "schema_version", "profile_id",
            "currency", "base_year", "price_basis", "cost_method",
            "finance_types", "energy_prices", "demand_charges",
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
        _check_tax_refs(
            currency=currency,
            energy_prices=energy_prices,
            finance_types=finance_types,
            demand_charges=demand_charges,
            taxes=taxes,
            credit_pools=credit_pools,
            tax_rules=tax_rules,
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

    # ---- credit_pools / tax_rules: 复用 Profile 已解析不可变事实，类型化应用
    # 覆盖变更。与 Profile 绑定过的 Overrides 直接消费对象保留的已解析
    # 类型化补丁，不再复核、不重解析；无绑定 Overrides 在此做唯一一次覆盖
    # 权限核对与值解析。合并后做一次组合领域引用核对；不重解析未覆盖项。
    bound = overrides._bound_profile == profile.profile_id and bool(overrides._bound_profile)
    for pool_id in sorted(overrides.credit_pools):
        if pool_id not in profile.credit_pools:
            raise FinanceTripletError(f"credit pool {pool_id!r} 在 Profile 中不存在, 禁止新增")
    for rule_id in sorted(overrides.tax_rules):
        if rule_id not in profile.tax_rules:
            raise FinanceTripletError(f"tax rule {rule_id!r} 在 Profile 中不存在, 禁止新增")
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
    _check_tax_refs(
        currency=profile.currency,
        energy_prices=profile.energy_prices,
        finance_types=profile.finance_types,
        demand_charges=merged_demand,
        taxes=profile.taxes,
        credit_pools=merged_pools,
        tax_rules=merged_rules,
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
        for asset, entry_values in values.get("depreciation", {}).items():
            for index, entry in enumerate(depreciation):
                if entry.asset == asset:
                    depreciation[index] = DepreciationEntry(
                        asset=entry.asset,
                        base_from=entry.base_from,
                        method=entry.method,
                        life_years=entry_values.get("life_years", entry.life_years),
                        residual=entry_values.get("residual", entry.residual),
                        first_year=entry_values.get("first_year", entry.first_year),
                    )
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
            annual_om=rule.annual_om,
            variable_om=rule.variable_om,
            deductible_maintenance=deductible_maintenance,
            depreciation=tuple(depreciation),
            loss_carryforward_years=values.get(
                "loss_carryforward_years", rule.loss_carryforward_years),
            opening_losses=values.get("opening_losses", rule.opening_losses),
        )
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
