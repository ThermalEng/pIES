"""公共规划表达式编译入口与冻结 IR(0.8f-A)。

把规划目标/约束的用户表达式编译为生成器可消费的不可变 IR，
不新造第二套 parser：形状校验仍归 ``core.contracts.planning_config``
（``Objective``/``PlanningVariable``/``Constraint``），本模块只做
“符号表 + 源码 → 冻结编译结果”的一次性编译：

- 金额字面量币种核对（可选 ``currency``；不传则跳过，由调用方在
  用户输入闸门处先行负责）；
- 含 ``__`` 的财务标识符经确定性安全别名（``pfin<N>``，排序稳定）
  改写后送公共 ``core.expression`` 引擎；
- 返回冻结 ``PlanningExpression``（源码、别名表、编译 IR、符号量纲）；
- ``PlanningProgram`` 冻结一次规划的目标 sense、目标 IR、连续变量
  （上下界/单位/量纲）与已启用约束（type + IR），确定性序列化。

调用方约定：装配阶段的用户输入闸门（标识符存在性分类、设备引用、
币种首错等诊断）仍在 ``assembly``；生成器后续只消费已编译 IR，
不重新解析用户表达式。本模块不导入任何业务模块。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from iesplan.core.contracts.planning_config import (
    OBJECTIVE_SENSES,
    PlanningConfigError,
    PlanningVariable,
)
from iesplan.core.expression import (
    Dimensions,
    ExpressionDimensionError,
    ExpressionSyntaxError,
    parse_expr,
)

#: 金额字面量(数值 + 币种词)：只核已知币种词汇（与装配规划闸门同模式）。
_CURRENCY_LITERAL_RE = re.compile(
    r"(?<![A-Za-z0-9_.])\d[\d_]*(?:\.\d+)?\s*(CNY|USD)(?![A-Za-z0-9_])"
)

#: 别名改写前缀(引擎安全：不含被禁子串；与 qconst 内部名前缀不冲突)。
_ALIAS_PREFIX = "pfin"


def _freeze_dims(dims: Mapping[str, int] | Counter[str] | None) -> tuple[tuple[str, int], ...]:
    """量纲 → 排序冻结对（非法形态抛 TypeError）。"""
    if dims is None:
        return ()
    if not isinstance(dims, Mapping):
        raise TypeError(f"量纲须为 Mapping，得到 {type(dims).__name__}")
    frozen = []
    for key, power in dims.items():
        if not isinstance(key, str) or not isinstance(power, int) or isinstance(power, bool):
            raise TypeError(f"量纲条目非法: {key!r}={power!r}")
        if power != 0:
            frozen.append((key, power))
    return tuple(sorted(frozen))


def _thaw_ir(node: object) -> Any:
    """编译 IR → JSON 兼容结构（元组转列表，确定性）。"""
    if isinstance(node, tuple):
        return [_thaw_ir(item) for item in node]
    return node


@dataclass(frozen=True, slots=True)
class PlanningExpression:
    """已编译的规划表达式（不可变，可直接交生成器降级）。

    属性:
        source: 用户表达式源码。
        aliases: 财务标识符安全别名表（(原名, 别名) 排序元组）。
        ir: 公共表达式引擎编译 IR（嵌套元组；变量引用均为别名）。
        symbols: 原名 → 量纲冻结对（排序元组）。
        constants: 单位字面量常量（(qconst 名, SI 数值) 排序元组；
            求值/降级时按常量代入）。
    """

    source: str
    aliases: tuple[tuple[str, str], ...] = ()
    ir: tuple = ()
    symbols: tuple[tuple[str, tuple[tuple[str, int], ...]], ...] = ()
    constants: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.source, str):
            raise TypeError("source 须为字符串")
        for pair in self.aliases:
            if (
                not isinstance(pair, tuple)
                or len(pair) != 2
                or not all(isinstance(part, str) for part in pair)
            ):
                raise TypeError(f"别名条目非法: {pair!r}")
        if not isinstance(self.ir, tuple):
            raise TypeError("ir 须为元组")
        for entry in self.symbols:
            if (
                not isinstance(entry, tuple)
                or len(entry) != 2
                or not isinstance(entry[0], str)
                or not isinstance(entry[1], tuple)
            ):
                raise TypeError(f"符号条目非法: {entry!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "aliases": [[original, alias] for original, alias in self.aliases],
            "ir": _thaw_ir(self.ir),
            "symbols": {
                name: {dim: power for dim, power in dims}
                for name, dims in self.symbols
            },
            "constants": [
                [name, value] for name, value in self.constants
            ],
        }


def compile_planning_expression(
    source: object,
    symbols: Mapping[str, Dimensions | Mapping[str, int] | Counter[str] | None],
    *,
    currency: str | None = None,
) -> PlanningExpression:
    """编译规划表达式源码为冻结 IR（单次编译，无诊断包装）。

    参数:
        source: 用户表达式源码（须为非空字符串）。
        symbols: 允许引用的符号名 → 量纲（含 ``__`` 财务标识符的调用方
            已分类集合；别名改写内部完成）。
        currency: 金额字面量币种；None 表示跳过币种核对（调用方已在
            用户输入闸门处先行负责时使用）。

    返回:
        冻结 ``PlanningExpression``。

    异常:
        ``ExpressionError`` 子类（语法/量纲/未登记/其他引擎错误由调用方
        按边界映射为诊断）；``TypeError``（入参形态非法）。
    """
    if not isinstance(source, str) or not source.strip():
        raise ExpressionSyntaxError("空表达式", expr=str(source))
    if not isinstance(symbols, Mapping):
        raise TypeError(f"symbols 须为 Mapping，得到 {type(symbols).__name__}")
    frozen_symbols = tuple(
        sorted(
            (str(name), _freeze_dims(dims)) for name, dims in symbols.items()
        )
    )
    if currency is not None:
        for match in _CURRENCY_LITERAL_RE.finditer(source):
            if match.group(1) != currency:
                raise ExpressionDimensionError(
                    f"金额字面量币种 {match.group(0).strip()!r} 与 {currency!r} 不一致",
                    literal=match.group(0).strip(),
                    currency=currency,
                    expr=source,
                )
    # 安全别名改写（与装配规划闸门原编排一致：排序稳定、词边界、
    # 别名与已用名避让；调用方不得预改写，避免引擎二次改写丢失上下文）。
    aliases: dict[str, str] = {}
    used = {name for name, _ in frozen_symbols}
    counter = 0
    rewritten = source
    for name, _ in frozen_symbols:
        if not re.search(r"\b" + re.escape(name) + r"\b", rewritten):
            continue
        counter += 1
        alias = f"{_ALIAS_PREFIX}{counter}"
        while alias in used:
            counter += 1
            alias = f"{_ALIAS_PREFIX}{counter}"
        used.add(alias)
        aliases[name] = alias
        rewritten = re.sub(r"\b" + re.escape(name) + r"\b", alias, rewritten)
    var_dims = {
        aliases[name]: Counter(dict(symbols[name]) if symbols[name] else {})
        for name in aliases
    }
    compiled = parse_expr(rewritten, set(var_dims), var_dims)
    return PlanningExpression(
        source=source,
        aliases=tuple(sorted(aliases.items())),
        ir=compiled.ir,
        symbols=frozen_symbols,
        constants=tuple(sorted(compiled.qconst_values.items())),
    )


@dataclass(frozen=True, slots=True)
class PlanningProgramVariable:
    """规划连续变量（编译期冻结视图）。

    上下界沿用 ``PlanningVariable`` 的 Decimal 精度（None=无界）；
    ``dims`` 为声明单位的冻结量纲。
    """

    name: str
    device_ref: str
    parameter: str
    lower_bound: Decimal | None = None
    upper_bound: Decimal | None = None
    unit: str | None = None
    dims: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class PlanningProgramConstraint:
    """已启用规划约束（type + 已编译 IR）。"""

    name: str
    type: str
    expression: PlanningExpression


@dataclass(frozen=True, slots=True)
class PlanningProgram:
    """一次规划的冻结 IR（目标 + 连续变量 + 已启用约束）。

    变量按名排序冻结；约束按名排序冻结；确定性序列化。
    未启用约束不进入本 IR（保留但不参与规划，由调用方持有原文）。
    """

    sense: str
    objective: PlanningExpression
    variables: Mapping[str, PlanningProgramVariable]
    constraints: tuple[PlanningProgramConstraint, ...] = ()

    def __post_init__(self) -> None:
        if self.sense not in OBJECTIVE_SENSES:
            raise PlanningConfigError(f"非法目标方向: {self.sense!r}")
        if not isinstance(self.objective, PlanningExpression):
            raise TypeError("objective 须为 PlanningExpression")
        if not isinstance(self.variables, Mapping) or not self.variables:
            raise PlanningConfigError("规划程序必须至少声明一个规划变量")
        for name, variable in self.variables.items():
            if not isinstance(variable, PlanningProgramVariable) or variable.name != name:
                raise TypeError(f"规划变量条目非法: {name!r}")
        object.__setattr__(
            self,
            "variables",
            MappingProxyType(
                {key: self.variables[key] for key in sorted(self.variables)}
            ),
        )
        for constraint in self.constraints:
            if not isinstance(constraint, PlanningProgramConstraint):
                raise TypeError(f"规划约束条目非法: {constraint!r}")
        object.__setattr__(
            self,
            "constraints",
            tuple(sorted(self.constraints, key=lambda item: item.name)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sense": self.sense,
            "objective": self.objective.to_dict(),
            "variables": {
                name: {
                    "device_ref": variable.device_ref,
                    "parameter": variable.parameter,
                    "lower_bound": (
                        str(variable.lower_bound)
                        if variable.lower_bound is not None
                        else None
                    ),
                    "upper_bound": (
                        str(variable.upper_bound)
                        if variable.upper_bound is not None
                        else None
                    ),
                    "unit": variable.unit,
                    "dims": {dim: power for dim, power in variable.dims},
                }
                for name, variable in self.variables.items()
            },
            "constraints": [
                {
                    "name": constraint.name,
                    "type": constraint.type,
                    "expression": constraint.expression.to_dict(),
                }
                for constraint in self.constraints
            ],
        }


def build_planning_program(
    *,
    sense: str,
    objective: PlanningExpression,
    variables: Mapping[str, PlanningVariable],
    variable_dims: Mapping[str, Dimensions | Mapping[str, int] | Counter[str] | None],
    constraints: Iterable[PlanningProgramConstraint] = (),
) -> PlanningProgram:
    """冻结规划程序（类型/结构边界；调用方保证内容已过用户输入闸门）。

    参数:
        sense: 目标方向（minimize/maximize）。
        objective: 已编译目标 IR。
        variables: 规划变量名 → 已校验 ``PlanningVariable``。
        variable_dims: 变量名 → 声明单位量纲（调用方按设备参数单位解析）。
        constraints: 已启用约束的冻结条目（未启用由调用方过滤）。
    """
    if not isinstance(variables, Mapping):
        raise TypeError("variables 须为 Mapping")
    if not isinstance(variable_dims, Mapping):
        raise TypeError("variable_dims 须为 Mapping")
    frozen_variables: dict[str, PlanningProgramVariable] = {}
    for name, variable in variables.items():
        if not isinstance(variable, PlanningVariable):
            raise TypeError(f"规划变量 {name!r} 须为 PlanningVariable")
        if name not in variable_dims:
            raise PlanningConfigError(f"规划变量 {name!r} 缺少量纲")
        frozen_variables[str(name)] = PlanningProgramVariable(
            name=str(name),
            device_ref=variable.device_ref,
            parameter=variable.parameter,
            lower_bound=variable.lower_bound,
            upper_bound=variable.upper_bound,
            unit=variable.unit,
            dims=_freeze_dims(variable_dims[name]),
        )
    return PlanningProgram(
        sense=sense,
        objective=objective,
        variables=frozen_variables,
        constraints=tuple(constraints),
    )


__all__ = [
    "PlanningExpression",
    "PlanningProgram",
    "PlanningProgramConstraint",
    "PlanningProgramVariable",
    "build_planning_program",
    "compile_planning_expression",
]
