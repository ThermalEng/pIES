"""公共规划表达式 IR 契约测试(0.8f-A)。

覆盖 compile_planning_expression 的确定性别名/编译、币种门禁、
引擎错误透传，以及 build_planning_program 的冻结与确定性序列化。
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

import pytest  # type: ignore[import-untyped]

from iesplan.core.contracts import (
    PlanningConfigError,
    PlanningProgramConstraint,
    PlanningVariable,
    build_planning_program,
    compile_planning_expression,
)
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
    assert first.to_dict() == second.to_dict()
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
    expr = compile_planning_expression("100 USD", {}, currency=None)
    assert expr.constants == (("qconst1", 100.0),)


def test_compile_empty_rejected() -> None:
    with pytest.raises(ExpressionSyntaxError):
        compile_planning_expression("  ", SYMBOLS)


def _program() -> object:
    objective = compile_planning_expression(
        "pv1__upfront_capex + om__annual_fixed", SYMBOLS
    )
    constraint_expr = compile_planning_expression("pv_cap <= 500 kW", SYMBOLS)
    return build_planning_program(
        sense="minimize",
        objective=objective,
        variables={
            "pv_cap": PlanningVariable(
                device_ref="pv1",
                parameter="rated_capacity_kwp",
                lower_bound=Decimal("0"),
                upper_bound=Decimal("500"),
                unit="kWp",
            )
        },
        variable_dims={"pv_cap": Counter({"power": 1})},
        constraints=[
            PlanningProgramConstraint(
                name="cap", type="capacity_limit", expression=constraint_expr
            )
        ],
    )


def test_program_freezes_and_serializes_deterministically() -> None:
    first = _program().to_dict()
    second = _program().to_dict()
    assert first == second
    assert first["sense"] == "minimize"
    assert first["variables"]["pv_cap"]["lower_bound"] == "0"
    assert first["variables"]["pv_cap"]["upper_bound"] == "500"
    assert first["constraints"][0]["type"] == "capacity_limit"


def test_program_bad_sense_rejected() -> None:
    objective = compile_planning_expression("pv_cap", SYMBOLS)
    with pytest.raises(PlanningConfigError):
        build_planning_program(
            sense="sideways",
            objective=objective,
            variables={
                "pv_cap": PlanningVariable(device_ref="pv1", parameter="p")
            },
            variable_dims={"pv_cap": Counter()},
        )
