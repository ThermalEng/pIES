"""公共规划表达式编译契约测试(0.8f-A)。

覆盖 compile_planning_expression 的确定性别名/编译、币种门禁与
引擎错误透传。规划的目标 sense、变量与约束归装配产物，不另设
包装对象，本文件不覆盖任何程序级冻结。
"""

from __future__ import annotations

from collections import Counter

import pytest  # type: ignore[import-untyped]

from iesplan.core.contracts import compile_planning_expression
from iesplan.core.expression import (
    ExpressionCodeError,
    ExpressionDimensionError,
    ExpressionSyntaxError,
)

SYMBOLS = {
    "pv1__upfront_capex": Counter({"currency": 1}),
    "om__annual_fixed": Counter({"currency": 1}),
    "pv_cap": Counter({"power": 1}),
}


def test_compile_aliases_deterministic() -> None:
    first = compile_planning_expression(
        "pv1__upfront_capex + om__annual_fixed * 2", SYMBOLS
    )
    second = compile_planning_expression(
        "pv1__upfront_capex + om__annual_fixed * 2", SYMBOLS
    )
    assert first.aliases == (
        ("om__annual_fixed", "pfin1"),
        ("pv1__upfront_capex", "pfin2"),
    )
    assert first == second
    assert first.ir[0] == "bin"


def test_compile_unknown_symbol_rejected() -> None:
    with pytest.raises(ExpressionCodeError):
        compile_planning_expression("pv_cap + nope", SYMBOLS)


def test_compile_dimension_mismatch_rejected() -> None:
    with pytest.raises(ExpressionDimensionError):
        compile_planning_expression("pv1__upfront_capex + pv_cap", SYMBOLS)


def test_compile_currency_mismatch_rejected() -> None:
    with pytest.raises(ExpressionDimensionError):
        compile_planning_expression("pv_cap * 2 + 100 USD", SYMBOLS, currency="CNY")


def test_compile_currency_skipped_when_none() -> None:
    # 单位字面量直接编译为 SI 数值 num 节点，不保留 qconst 变量
    expr = compile_planning_expression("100 USD", {}, currency=None)
    assert expr.ir == ("num", 100.0)


def test_compile_reports_root_dimension() -> None:
    """编译期根量纲随附（生成器目标单位的唯一真值源，不重解析）。"""
    assert compile_planning_expression("pv_cap", SYMBOLS).dimension == (("power", 1),)
    assert compile_planning_expression("pv_cap / 1 kW", SYMBOLS).dimension == ()
    assert compile_planning_expression("1 kW", {}).dimension == (("power", 1),)
    assert compile_planning_expression("100 USD", {}, currency=None).dimension == (
        ("currency", 1),
    )


def test_compile_keeps_signed_exponents() -> None:
    """乘除保留负指数（Counter 算术丢弃非正指数，此处不许丢）。"""
    assert compile_planning_expression("1 / 1 kW", {}).dimension == (("power", -1),)
    assert compile_planning_expression("pv_cap / 1 year", SYMBOLS).dimension == (
        ("power", 1),
        ("time", -1),
    )
    assert compile_planning_expression("(1 / 1 kW) ** 2", {}).dimension == (("power", -2),)


@pytest.mark.parametrize(("source", "dimension"), [
    ("pv_cap * 1 h", (("energy", 1),)),
    ("1 kWh / 1 h", (("power", 1),)),
    ("1 kWh / pv_cap", (("time", 1),)),
    ("pv_cap * 1 h / 1 kWh", ()),
    ("1 / (pv_cap * 1 h)", (("energy", -1),)),
])
def test_compile_energy_equivalence_preserves_business_dimensions(source, dimension) -> None:
    assert compile_planning_expression(source, SYMBOLS).dimension == dimension


def test_compile_empty_rejected() -> None:
    with pytest.raises(ExpressionSyntaxError):
        compile_planning_expression("  ", SYMBOLS)
