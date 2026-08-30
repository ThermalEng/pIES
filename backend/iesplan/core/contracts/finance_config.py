"""无状态纯数据类型: 公共财务配置 FinanceConfig(宪法 4.6 / 0.6.5 事项 3)。

公共财务配置是整体系统模型的公共财务参数输入, 只保存规划与结果财务计算
**共同使用**的参数: 设备单价与建设投资、固定/可变 O&M、能源购售价格、税率、
资金时间成本等。它不保存目标函数、规划变量/约束、计算选项或仅在某一阶段
生效的数据(0.6.5 规划文档「财务配置」边界):

- 规划生成与结果财务计算必须消费同一不可变 FinanceConfig revision;
- 存量设备的历史投资按沉没成本处理(不重复计入新增投资); 新增设备投资必须
  与建设或容量决策绑定;
- 金额使用十进制定点语义(Decimal, 宪法 7.3), 比例统一 0..1, 币种 CNY/USD;
- 配置摘要为确定性 SHA-256(revision 语义由持久化层承担: 每次保存形成
  新的不可变 revision);
- 深度不可变: 嵌套容器构造时递归冻结, 同一对象摘要恒定。

本模块只依赖标准库与 ``core.diagnostics``, 不导入任何业务模块
(core/contracts 边界, 宪法 4.1)。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, Overflow, localcontext
from types import MappingProxyType
from typing import Final

from iesplan.core.diagnostics import Diagnostic, make_diag

#: 支持币种。
CURRENCIES: Final[tuple[str, ...]] = ("CNY", "USD")

#: 财务配置规范化算法 ID 与版本(写入摘要; 语义变化必须升版本)。
FINANCE_CANON_ALGORITHM_ID: Final[str] = "ies.finance_config.canonical"
FINANCE_CANON_ALGORITHM_VERSION: Final[str] = "1.0.0"

#: 设备固定/可变 O&M 比例有效范围(0..1, 相对投资或容量的年费率)。
OM_RATE_MIN: Final[Decimal] = Decimal("0")
OM_RATE_MAX: Final[Decimal] = Decimal("1")

#: 税率/资金时间成本比例有效范围(0..1)。
RATE_MIN: Final[Decimal] = Decimal("0")
RATE_MAX: Final[Decimal] = Decimal("1")

#: 单价/投资金额上限(防溢出与病态输入)。
AMOUNT_MAX: Final[Decimal] = Decimal("1e18")

#: 能源购售价格上限(每单位能源价格)。
PRICE_MAX: Final[Decimal] = Decimal("1e12")

#: 金额/价格允许的最大十进制位数(含整数与小数部分, 防病态指数输入)。
MAX_DIGITS: Final[int] = 30

#: 能源购售价格键白名单(carrier + purchase/sale; 未列出的键不允许出现)。
ENERGY_PRICE_KEYS: Final[tuple[str, ...]] = (
    "electricity_purchase",
    "electricity_sale",
    "heat_purchase",
    "heat_sale",
    "cooling_purchase",
    "cooling_sale",
    "natural_gas",
)

#: 校验诊断码(登记于 core.diagnostics.NEW_DIAG_CODES)。
FINANCE_CONFIG_INVALID = "PROJ-FIN-001"


class FinanceConfigError(ValueError):
    """FinanceConfig 校验失败(非法字段/类型/范围/未知键)。"""


# ---------------------------------------------------------------------------
# 数值与序列化工具
# ---------------------------------------------------------------------------


def _to_decimal(value: object, field_name: str) -> Decimal:
    """字段 → Decimal; 仅接受 Decimal/规范十进制字符串/整数(宪法 7.3)。

    - 拒绝 float(浮点无法精确表达金额, 调用方负责显式转换);
    - 拒绝 bool(int 子类, 不参与数值语义);
    - 拒绝 NaN/Infinity(宪法 7.3 禁止进入持久化)。
    """
    if isinstance(value, bool) or value is None:
        raise FinanceConfigError(f"{field_name}: 必须是十进制数值")
    try:
        if isinstance(value, Decimal):
            d = value
        elif isinstance(value, int):
            d = Decimal(value)
        elif isinstance(value, str):
            d = Decimal(value)
        else:
            raise FinanceConfigError(f"{field_name}: 类型 {type(value).__name__} 不受支持")
        if not d.is_finite():
            raise FinanceConfigError(f"{field_name}: 禁止 NaN/Infinity")
        exponent = d.as_tuple().exponent
        if isinstance(exponent, int) and len(d.as_tuple().digits) - exponent > MAX_DIGITS:
            raise FinanceConfigError(f"{field_name}: 超出 {MAX_DIGITS} 位十进制精度")
    except (InvalidOperation, Overflow) as exc:
        raise FinanceConfigError(f"{field_name}: 十进制解析失败") from exc
    return d


def _decimal_to_canonical(d: Decimal) -> str:
    """Decimal → 定点十进制字符串(规范化摘要输入; 避免指数形态)。"""
    with localcontext() as ctx:
        ctx.prec = MAX_DIGITS
        return format(d, "f")


def _canonical_json(payload: Mapping[str, object]) -> str:
    """稳定键序紧凑 JSON(嵌套 dict 递归排序, 摘要计算输入)。"""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


# ---------------------------------------------------------------------------
# 金额与设备财务参数
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MoneyAmount:
    """带单位的十进制金额(设备单价/能源价格/O&M 单价)。

    属性:
        value: 金额(Decimal; 单价/投资/价格)。
        unit: 单位(如 CNY/kW、CNY/kWh; 与配置币种一致)。
    """

    value: Decimal
    unit: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, Decimal):
            raise FinanceConfigError(f"金额必须为 Decimal, 实际 {type(self.value).__name__}")
        if not self.value.is_finite():
            raise FinanceConfigError("金额禁止 NaN/Infinity")
        if not isinstance(self.unit, str) or not self.unit:
            raise FinanceConfigError("金额单位必须为非空字符串")

    def to_dict(self) -> dict:
        return {"value": _decimal_to_canonical(self.value), "unit": self.unit}

    @classmethod
    def from_dict(cls, mapping: object) -> "MoneyAmount":
        if not isinstance(mapping, Mapping):
            raise FinanceConfigError("金额必须是 {value, unit} 字典")
        unknown = set(mapping) - {"value", "unit"}
        if unknown:
            raise FinanceConfigError(f"金额存在未知字段: {sorted(unknown)}")
        if "value" not in mapping or "unit" not in mapping:
            raise FinanceConfigError("金额缺少 value 或 unit")
        return cls(
            value=_to_decimal(mapping["value"], "金额 value"),
            unit=str(mapping["unit"]),
        )


@dataclass(frozen=True, slots=True)
class DeviceFinanceParams:
    """单设备公共财务参数(规划与财务计算共同消费)。

    属性:
        unit_investment: 单位建设投资(金额; 新增设备与容量决策绑定)。
        fixed_om_rate: 固定 O&M 年费率(0..1, 相对投资)。
        variable_om: 可变 O&M 单价(金额/单位能量)。
    """

    unit_investment: MoneyAmount
    fixed_om_rate: Decimal
    variable_om: MoneyAmount

    def __post_init__(self) -> None:
        if not isinstance(self.fixed_om_rate, Decimal):
            raise FinanceConfigError(
                f"fixed_om_rate 必须为 Decimal, 实际 {type(self.fixed_om_rate).__name__}"
            )
        if not OM_RATE_MIN <= self.fixed_om_rate <= OM_RATE_MAX:
            raise FinanceConfigError(
                f"fixed_om_rate 必须在 0..1: {self.fixed_om_rate}"
            )

    def to_dict(self) -> dict:
        return {
            "unit_investment": self.unit_investment.to_dict(),
            "fixed_om_rate": _decimal_to_canonical(self.fixed_om_rate),
            "variable_om": self.variable_om.to_dict(),
        }

    @classmethod
    def from_dict(cls, mapping: object) -> "DeviceFinanceParams":
        if not isinstance(mapping, Mapping):
            raise FinanceConfigError("设备财务参数必须是字典")
        unknown = set(mapping) - {"unit_investment", "fixed_om_rate", "variable_om"}
        if unknown:
            raise FinanceConfigError(f"设备财务参数存在未知字段: {sorted(unknown)}")
        missing = {"unit_investment", "fixed_om_rate", "variable_om"} - set(mapping)
        if missing:
            raise FinanceConfigError(f"设备财务参数缺少字段: {sorted(missing)}")
        return cls(
            unit_investment=MoneyAmount.from_dict(mapping["unit_investment"]),
            fixed_om_rate=_to_decimal(mapping["fixed_om_rate"], "fixed_om_rate"),
            variable_om=MoneyAmount.from_dict(mapping["variable_om"]),
        )


# ---------------------------------------------------------------------------
# FinanceConfig
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FinanceConfig:
    """公共财务配置(不可变; 每次保存形成新的 revision)。

    属性:
        currency: 币种(CNY/USD; 设备报价单位与能源价格单位必须一致)。
        base_year: 财务基准年(整数)。
        devices: 设备实例 ID → 公共财务参数(设备单价/固定与可变 O&M)。
        energy_prices: 能源购售价格键 → 金额(白名单键)。
        tax_rate: 企业所得税率(0..1)。
        capital_time_cost: 资金时间成本(0..1)。
    """

    currency: str
    base_year: int
    devices: Mapping[str, DeviceFinanceParams]
    energy_prices: Mapping[str, MoneyAmount]
    tax_rate: Decimal
    capital_time_cost: Decimal

    def __post_init__(self) -> None:
        if self.currency not in CURRENCIES:
            raise FinanceConfigError(
                f"非法币种: {self.currency!r}, 允许值 {CURRENCIES}"
            )
        if not isinstance(self.base_year, int) or isinstance(self.base_year, bool):
            raise FinanceConfigError(
                f"base_year 必须为整数, 实际 {type(self.base_year).__name__}"
            )
        if not 1900 <= self.base_year <= 2999:
            raise FinanceConfigError(f"base_year 超出合理范围: {self.base_year}")
        if not isinstance(self.tax_rate, Decimal) or not RATE_MIN <= self.tax_rate <= RATE_MAX:
            raise FinanceConfigError(f"tax_rate 必须在 0..1: {self.tax_rate}")
        if not isinstance(self.capital_time_cost, Decimal) or not (
            RATE_MIN <= self.capital_time_cost <= RATE_MAX
        ):
            raise FinanceConfigError(
                f"capital_time_cost 必须在 0..1: {self.capital_time_cost}"
            )
        unknown_prices = set(self.energy_prices) - set(ENERGY_PRICE_KEYS)
        if unknown_prices:
            raise FinanceConfigError(
                f"能源价格存在未知键: {sorted(unknown_prices)}"
            )
        # 深度不可变: 嵌套容器递归冻结
        object.__setattr__(
            self, "devices",
            MappingProxyType(
                {
                    k: v
                    for k, v in sorted(self.devices.items(), key=lambda kv: kv[0])
                }
            ),
        )
        object.__setattr__(
            self, "energy_prices",
            MappingProxyType(
                {
                    k: v
                    for k, v in sorted(self.energy_prices.items(), key=lambda kv: kv[0])
                }
            ),
        )

    @property
    def revision(self) -> str:
        """确定性 revision 摘要(ies.finance_config.canonical@1.0.0)。"""
        payload = {
            "currency": self.currency,
            "base_year": self.base_year,
            "devices": {
                k: v.to_dict() for k, v in sorted(self.devices.items())
            },
            "energy_prices": {
                k: v.to_dict() for k, v in sorted(self.energy_prices.items())
            },
            "tax_rate": _decimal_to_canonical(self.tax_rate),
            "capital_time_cost": _decimal_to_canonical(self.capital_time_cost),
        }
        return hashlib.sha256(
            (
                f"{FINANCE_CANON_ALGORITHM_ID}@{FINANCE_CANON_ALGORITHM_VERSION}\n"
                f"{_canonical_json(payload)}"
            ).encode("utf-8")
        ).hexdigest()

    def to_dict(self) -> dict:
        """公开字典形态(含 revision; API/持久化/装配共用的唯一序列化)。"""
        return {
            "currency": self.currency,
            "base_year": self.base_year,
            "devices": {k: v.to_dict() for k, v in sorted(self.devices.items())},
            "energy_prices": {
                k: v.to_dict() for k, v in sorted(self.energy_prices.items())
            },
            "tax_rate": _decimal_to_canonical(self.tax_rate),
            "capital_time_cost": _decimal_to_canonical(self.capital_time_cost),
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, mapping: object) -> "FinanceConfig":
        """严格恢复: 未知字段拒绝; 必需字段缺失拒绝; 枚举之外拒绝。

        ``revision`` 为 ``to_dict`` 携带的派生摘要字段: 缺失时允许(重新计算),
        存在时必须与当前规范化算法摘要一致, 否则拒绝(防伪造/防摘要漂移)。
        """
        if not isinstance(mapping, Mapping):
            raise FinanceConfigError(
                f"财务配置必须是字典, 实际 {type(mapping).__name__}"
            )
        unknown = set(mapping) - {
            "currency", "base_year", "devices", "energy_prices",
            "tax_rate", "capital_time_cost", "revision",
        }
        if unknown:
            raise FinanceConfigError(f"财务配置存在未知字段: {sorted(unknown)}")
        missing = {
            "currency", "base_year", "devices", "energy_prices",
            "tax_rate", "capital_time_cost",
        } - set(mapping)
        if missing:
            raise FinanceConfigError(f"财务配置缺少必需字段: {sorted(missing)}")
        devices_raw = mapping["devices"]
        if not isinstance(devices_raw, Mapping):
            raise FinanceConfigError("devices 必须是字典")
        devices = {
            str(k): DeviceFinanceParams.from_dict(v)
            for k, v in devices_raw.items()
        }
        prices_raw = mapping["energy_prices"]
        if not isinstance(prices_raw, Mapping):
            raise FinanceConfigError("energy_prices 必须是字典")
        energy_prices = {
            str(k): MoneyAmount.from_dict(v) for k, v in prices_raw.items()
        }
        base_year_raw = mapping["base_year"]
        if not isinstance(base_year_raw, int) or isinstance(base_year_raw, bool):
            raise FinanceConfigError(
                f"base_year 必须为整数, 实际 {type(base_year_raw).__name__}"
            )
        config = cls(
            currency=str(mapping["currency"]),
            base_year=base_year_raw,
            devices=devices,
            energy_prices=energy_prices,
            tax_rate=_to_decimal(mapping["tax_rate"], "tax_rate"),
            capital_time_cost=_to_decimal(
                mapping["capital_time_cost"], "capital_time_cost"
            ),
        )
        declared_revision = mapping.get("revision")
        if declared_revision is not None and str(declared_revision) != config.revision:
            raise FinanceConfigError(
                f"财务配置摘要与规范化算法不一致: 声明 {declared_revision!r}, "
                f"期望 {config.revision}"
            )
        return config

    @classmethod
    def validate(cls, mapping: object) -> list[Diagnostic]:
        """校验字典形态财务配置, 返回结构化诊断(非法时非空; 不抛异常)。"""
        try:
            cls.from_dict(mapping)
            return []
        except FinanceConfigError as exc:
            return [
                make_diag(
                    FINANCE_CONFIG_INVALID,
                    params={"detail": str(exc)},
                    location={"object_type": "finance_config", "field": ""},
                )
            ]
