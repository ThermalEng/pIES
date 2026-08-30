"""无状态纯数据类型: 规划配置 PlanningConfig(宪法 4.6 / 0.6.5 事项 3)。

规划配置只描述规划问题: 目标函数/目标权重、规划变量、容量上下界和规划/系统
约束。它不保存财务参数(设备单价、投资、O&M、能源价格、税率、资金时间成本
属于公共财务配置 FinanceConfig)或 generator/solver 选项(属于计算配置)。

- 规划与结果财务计算必须固定同一不可变 FinanceConfig revision:
  ``finance_revision`` 字段引用该 revision, 一致性由领域校验层
  (finance.contracts.check_revision_consistency)与装配边界共同强制;
- 目标函数可以引用模型技术量、财务配置公共参数、规划变量和约束;
- 约束为命名映射, 表达式使用受限声明式语法(语法本体属 modeling/装配域,
  本契约只做形状与白名单校验);
- 配置摘要为确定性 SHA-256(每次保存形成新的不可变 revision);
- 深度不可变: 嵌套容器构造时递归冻结, 同一对象摘要恒定。

本模块只依赖标准库与 ``core.diagnostics``, 不导入任何业务模块
(core/contracts 边界, 宪法 4.1)。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, Overflow, localcontext
from types import MappingProxyType
from typing import Final

from iesplan.core.diagnostics import Diagnostic, make_diag

#: 规划配置规范化算法 ID 与版本(写入摘要; 语义变化必须升版本)。
PLANNING_CANON_ALGORITHM_ID: Final[str] = "ies.planning_config.canonical"
PLANNING_CANON_ALGORITHM_VERSION: Final[str] = "1.0.0"

#: 目标方向。
OBJECTIVE_SENSES: Final[tuple[str, ...]] = ("minimize", "maximize")

#: 约束种类白名单(预定义约束 + 通用比例约束, 0.6.5 规划文档)。
CONSTRAINT_TYPES: Final[tuple[str, ...]] = (
    "load_balance",
    "capacity_limit",
    "co2_limit",
    "annual_purchase_cost_limit",
    "ratio",
)

#: 表达式最大长度(受限声明式语法, 防病态输入)。
EXPRESSION_MAX_LENGTH: Final[int] = 4096

#: 摘要必须为 64 位小写十六进制。
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")

#: 校验诊断码(登记于 core.diagnostics.NEW_DIAG_CODES)。
PLANNING_CONFIG_INVALID = "PROJ-PLAN-001"


class PlanningConfigError(ValueError):
    """PlanningConfig 校验失败(非法字段/类型/范围/未知键)。"""


def _to_decimal(value: object, field_name: str) -> Decimal:
    """字段 → Decimal(与 finance_config 同规: 拒 float/bool/NaN/Infinity)。"""
    if isinstance(value, bool) or value is None:
        raise PlanningConfigError(f"{field_name}: 必须是十进制数值")
    try:
        if isinstance(value, Decimal):
            d = value
        elif isinstance(value, int):
            d = Decimal(value)
        elif isinstance(value, str):
            d = Decimal(value)
        else:
            raise PlanningConfigError(f"{field_name}: 类型 {type(value).__name__} 不受支持")
        if not d.is_finite():
            raise PlanningConfigError(f"{field_name}: 禁止 NaN/Infinity")
    except (InvalidOperation, Overflow) as exc:
        raise PlanningConfigError(f"{field_name}: 十进制解析失败") from exc
    return d


def _decimal_to_canonical(d: Decimal) -> str:
    """Decimal → 定点十进制字符串(规范化摘要输入)。"""
    with localcontext() as ctx:
        ctx.prec = 30
        return format(d, "f")


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


# ---------------------------------------------------------------------------
# 子结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Objective:
    """目标函数(规划配置唯一目标; 表达式引用模型技术量与财务公共参数)。

    属性:
        sense: 'minimize' | 'maximize'。
        expression: 受限声明式表达式(语法本体属 modeling/装配域)。
    """

    sense: str
    expression: str

    def __post_init__(self) -> None:
        if self.sense not in OBJECTIVE_SENSES:
            raise PlanningConfigError(
                f"非法目标方向: {self.sense!r}, 允许值 {OBJECTIVE_SENSES}"
            )
        if not isinstance(self.expression, str) or not self.expression.strip():
            raise PlanningConfigError("目标表达式必须为非空字符串")
        if len(self.expression) > EXPRESSION_MAX_LENGTH:
            raise PlanningConfigError(
                f"目标表达式超出长度上限 {EXPRESSION_MAX_LENGTH}"
            )

    def to_dict(self) -> dict:
        return {"sense": self.sense, "expression": self.expression}

    @classmethod
    def from_dict(cls, mapping: object) -> "Objective":
        if not isinstance(mapping, Mapping):
            raise PlanningConfigError("objective 必须是字典")
        unknown = set(mapping) - {"sense", "expression"}
        if unknown:
            raise PlanningConfigError(f"objective 存在未知字段: {sorted(unknown)}")
        missing = {"sense", "expression"} - set(mapping)
        if missing:
            raise PlanningConfigError(f"objective 缺少字段: {sorted(missing)}")
        return cls(sense=str(mapping["sense"]), expression=str(mapping["expression"]))


@dataclass(frozen=True, slots=True)
class PlanningVariable:
    """规划变量(容量/建设决策候选; 上下界即容量边界)。

    属性:
        device_ref: 绑定的设备实例引用(如 'heat_pump_1'; 装配域校验存在性)。
        parameter: 变量对应的设备技术参数名。
        lower_bound / upper_bound: 容量上下界(Decimal; None=无界)。
        unit: 变量单位(可空; 声明时须与设备参数一致)。
    """

    device_ref: str
    parameter: str
    lower_bound: Decimal | None = None
    upper_bound: Decimal | None = None
    unit: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.device_ref, str) or not self.device_ref.strip():
            raise PlanningConfigError("规划变量 device_ref 必须为非空字符串")
        if not isinstance(self.parameter, str) or not self.parameter.strip():
            raise PlanningConfigError("规划变量 parameter 必须为非空字符串")
        if self.lower_bound is not None and self.upper_bound is not None:
            if self.lower_bound > self.upper_bound:
                raise PlanningConfigError(
                    f"规划变量上下界颠倒: lower={self.lower_bound} > upper={self.upper_bound}"
                )
        if self.unit is not None and (not isinstance(self.unit, str) or not self.unit):
            raise PlanningConfigError("规划变量 unit 必须为非空字符串")

    def to_dict(self) -> dict:
        return {
            "device_ref": self.device_ref,
            "parameter": self.parameter,
            "lower_bound": (
                _decimal_to_canonical(self.lower_bound)
                if self.lower_bound is not None
                else None
            ),
            "upper_bound": (
                _decimal_to_canonical(self.upper_bound)
                if self.upper_bound is not None
                else None
            ),
            "unit": self.unit,
        }

    @classmethod
    def from_dict(cls, mapping: object) -> "PlanningVariable":
        if not isinstance(mapping, Mapping):
            raise PlanningConfigError("规划变量必须是字典")
        unknown = set(mapping) - {
            "device_ref", "parameter", "lower_bound", "upper_bound", "unit",
        }
        if unknown:
            raise PlanningConfigError(f"规划变量存在未知字段: {sorted(unknown)}")
        missing = {"device_ref", "parameter"} - set(mapping)
        if missing:
            raise PlanningConfigError(f"规划变量缺少字段: {sorted(missing)}")
        lower = mapping.get("lower_bound")
        upper = mapping.get("upper_bound")
        return cls(
            device_ref=str(mapping["device_ref"]),
            parameter=str(mapping["parameter"]),
            lower_bound=(
                _to_decimal(lower, "lower_bound") if lower is not None else None
            ),
            upper_bound=(
                _to_decimal(upper, "upper_bound") if upper is not None else None
            ),
            unit=(
                str(mapping["unit"])
                if mapping.get("unit") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class Constraint:
    """规划/系统约束(命名映射中的一项)。

    属性:
        type: 约束种类白名单(load_balance/capacity_limit/co2_limit/
            annual_purchase_cost_limit/ratio)。
        expression: 受限声明式表达式。
        enabled: 是否启用(False=保留但不参与规划)。
    """

    type: str
    expression: str
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.type not in CONSTRAINT_TYPES:
            raise PlanningConfigError(
                f"非法约束种类: {self.type!r}, 允许值 {CONSTRAINT_TYPES}"
            )
        if not isinstance(self.expression, str) or not self.expression.strip():
            raise PlanningConfigError("约束表达式必须为非空字符串")
        if len(self.expression) > EXPRESSION_MAX_LENGTH:
            raise PlanningConfigError(f"约束表达式超出长度上限 {EXPRESSION_MAX_LENGTH}")
        if not isinstance(self.enabled, bool):
            raise PlanningConfigError(
                f"enabled 必须为布尔值, 实际 {type(self.enabled).__name__}"
            )

    def to_dict(self) -> dict:
        return {"type": self.type, "expression": self.expression, "enabled": self.enabled}

    @classmethod
    def from_dict(cls, mapping: object) -> "Constraint":
        if not isinstance(mapping, Mapping):
            raise PlanningConfigError("约束必须是字典")
        unknown = set(mapping) - {"type", "expression", "enabled"}
        if unknown:
            raise PlanningConfigError(f"约束存在未知字段: {sorted(unknown)}")
        missing = {"type", "expression"} - set(mapping)
        if missing:
            raise PlanningConfigError(f"约束缺少字段: {sorted(missing)}")
        enabled = mapping.get("enabled", True)
        if not isinstance(enabled, bool):
            raise PlanningConfigError(
                f"enabled 必须为布尔值, 实际 {type(enabled).__name__}"
            )
        return cls(
            type=str(mapping["type"]),
            expression=str(mapping["expression"]),
            enabled=enabled,
        )


# ---------------------------------------------------------------------------
# PlanningConfig
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanningConfig:
    """规划配置(不可变; 每次保存形成新的 revision)。

    属性:
        objective: 目标函数(sense + 受限声明式表达式)。
        variables: 规划变量命名映射(容量/建设决策候选 + 上下界)。
        constraints: 约束命名映射(规划/系统约束)。
        finance_revision: 规划与结果财务计算固定引用的 FinanceConfig
            revision(64 位小写十六进制; 一致性由领域校验强制)。
    """

    objective: Objective
    variables: Mapping[str, PlanningVariable]
    constraints: Mapping[str, Constraint]
    finance_revision: str

    def __post_init__(self) -> None:
        if not _SHA256_RE.fullmatch(self.finance_revision):
            raise PlanningConfigError(
                f"finance_revision 必须为 64 位小写十六进制: {self.finance_revision!r}"
            )
        if not self.variables:
            raise PlanningConfigError("规划配置必须至少声明一个规划变量")
        # 深度不可变 + 稳定键序
        object.__setattr__(
            self, "variables",
            MappingProxyType(
                {
                    k: v
                    for k, v in sorted(self.variables.items(), key=lambda kv: kv[0])
                }
            ),
        )
        object.__setattr__(
            self, "constraints",
            MappingProxyType(
                {
                    k: v
                    for k, v in sorted(self.constraints.items(), key=lambda kv: kv[0])
                }
            ),
        )

    @property
    def revision(self) -> str:
        """确定性 revision 摘要(ies.planning_config.canonical@1.0.0)。"""
        payload = {
            "objective": self.objective.to_dict(),
            "variables": {
                k: v.to_dict() for k, v in sorted(self.variables.items())
            },
            "constraints": {
                k: v.to_dict() for k, v in sorted(self.constraints.items())
            },
            "finance_revision": self.finance_revision,
        }
        return hashlib.sha256(
            (
                f"{PLANNING_CANON_ALGORITHM_ID}@{PLANNING_CANON_ALGORITHM_VERSION}\n"
                f"{_canonical_json(payload)}"
            ).encode("utf-8")
        ).hexdigest()

    def to_dict(self) -> dict:
        return {
            "objective": self.objective.to_dict(),
            "variables": {
                k: v.to_dict() for k, v in sorted(self.variables.items())
            },
            "constraints": {
                k: v.to_dict() for k, v in sorted(self.constraints.items())
            },
            "finance_revision": self.finance_revision,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, mapping: object) -> "PlanningConfig":
        """严格恢复: 未知字段拒绝; 必需字段缺失拒绝; 枚举之外拒绝。"""
        if not isinstance(mapping, Mapping):
            raise PlanningConfigError(
                f"规划配置必须是字典, 实际 {type(mapping).__name__}"
            )
        unknown = set(mapping) - {
            "objective", "variables", "constraints", "finance_revision", "revision",
        }
        if unknown:
            raise PlanningConfigError(f"规划配置存在未知字段: {sorted(unknown)}")
        missing = {"objective", "variables", "finance_revision"} - set(mapping)
        if missing:
            raise PlanningConfigError(f"规划配置缺少必需字段: {sorted(missing)}")
        variables_raw = mapping["variables"]
        if not isinstance(variables_raw, Mapping):
            raise PlanningConfigError("variables 必须是字典")
        variables = {
            str(k): PlanningVariable.from_dict(v) for k, v in variables_raw.items()
        }
        constraints_raw = mapping.get("constraints", {})
        if not isinstance(constraints_raw, Mapping):
            raise PlanningConfigError("constraints 必须是字典")
        constraints = {
            str(k): Constraint.from_dict(v) for k, v in constraints_raw.items()
        }
        config = cls(
            objective=Objective.from_dict(mapping["objective"]),
            variables=variables,
            constraints=constraints,
            finance_revision=str(mapping["finance_revision"]),
        )
        declared_revision = mapping.get("revision")
        if declared_revision is not None and str(declared_revision) != config.revision:
            raise PlanningConfigError(
                f"规划配置摘要与规范化算法不一致: 声明 {declared_revision!r}, "
                f"期望 {config.revision}"
            )
        return config

    @classmethod
    def validate(cls, mapping: object) -> list[Diagnostic]:
        """校验字典形态规划配置, 返回结构化诊断(非法时非空; 不抛异常)。"""
        try:
            cls.from_dict(mapping)
            return []
        except PlanningConfigError as exc:
            return [
                make_diag(
                    PLANNING_CONFIG_INVALID,
                    params={"detail": str(exc)},
                    location={"object_type": "planning_config", "field": ""},
                )
            ]
