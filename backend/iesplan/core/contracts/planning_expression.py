"""公共规划表达式编译入口(0.8f-A)。

把规划目标/约束的用户表达式编译为生成器可消费的不可变
``PlanningExpression``，不新造第二套 parser：形状校验仍归
``core.contracts.planning_config``
（``Objective``/``PlanningVariable``/``Constraint``），本模块只做
“符号表 + 源码 → 冻结编译结果”的一次性编译：

- 金额字面量币种核对（可选 ``currency``；不传则跳过，由调用方在
  用户输入闸门处先行负责）；
- 含 ``__`` 的财务标识符经确定性安全别名（``pfin<N>``，排序稳定）
  改写后送公共 ``core.expression`` 引擎；
- 返回冻结 ``PlanningExpression``（别名表、编译 IR）。

调用方约定：装配在用户输入闸门调用本入口做合法性校验；生成器在
可信 artifact 上调用同一入口完成问题构造，不建立第二 parser，也
不把这次编译包装成第六项业务输入。本模块不导入任何业务模块。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

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


@dataclass(frozen=True, slots=True)
class PlanningExpression:
    """已编译的规划表达式（不可变，可直接交生成器降级）。

    属性:
        aliases: 财务标识符安全别名表（(原名, 别名) 排序元组）。
        ir: 公共表达式引擎编译 IR（嵌套元组；变量引用均为别名）。
    """

    aliases: tuple[tuple[str, str], ...] = ()
    ir: tuple = ()

    def __post_init__(self) -> None:
        for pair in self.aliases:
            if (
                not isinstance(pair, tuple)
                or len(pair) != 2
                or not all(isinstance(part, str) for part in pair)
            ):
                raise TypeError(f"别名条目非法: {pair!r}")
        if not isinstance(self.ir, tuple):
            raise TypeError("ir 须为元组")


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
    names = sorted(str(name) for name in symbols)
    used = set(names)
    counter = 0
    rewritten = source
    for name in names:
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
        aliases=tuple(sorted(aliases.items())),
        ir=compiled.ir,
    )


__all__ = [
    "PlanningExpression",
    "compile_planning_expression",
]
