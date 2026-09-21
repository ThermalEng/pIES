"""线性/MILP 降级基础契约测试(0.8f-A)。

表达式/单关系级：仿射成功、常量部分求值、t-1 初值、有限 Big-M；
双线性、无界/非负不可证互斥、t-2、严格比较明确失败。
"""

from __future__ import annotations

from collections import Counter

import pytest  # type: ignore[import-untyped]

from iesplan.computation.generators.linear import (
    LoweringUnsupportedError,
    lower_expression,
    lower_planning_constraint,
    lower_planning_expression,
    lower_relation,
)
from iesplan.core.contracts import (
    PlanningProgramConstraint,
    compile_planning_expression,
)
from iesplan.core.yamlmini import load as yaml_load
from iesplan.devices.parser2 import parse_device_model_v2
from iesplan.modeling import (
    BinaryNode,
    MathRelation,
    NumberNode,
    RefNode,
    RelationAst,
    build_math_contribution,
)

HEATER_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.heater, names: {zh-CN: 加热器, en-US: Heater}}
properties:
  efficiency: {value: 0.9, unit: "1", valid_range: {minimum: 0, maximum: 1}}
interfaces:
  electricity_in: {type: in, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
  heat_out: {type: out, carrier: heat, unit: kW, valid_range: {minimum: 0, maximum: null}}
equations:
  variables: {}
  relations:
    - id: conversion
      expression: "heat_out[t] = electricity_in[t] * efficiency"
"""

SOC_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.tank, names: {zh-CN: 罐, en-US: Tank}}
properties:
  soc0: {value: 0.5, unit: "1", valid_range: {minimum: 0, maximum: 1}}
  step_c: {value: 1, unit: "1", valid_range: {minimum: 0, maximum: 10}}
interfaces:
  flow: {type: in, carrier: electricity, unit: "1", valid_range: {minimum: 0, maximum: null}}
equations:
  variables:
    soc: {unit: "1", valid_range: {minimum: 0, maximum: 1}, initial: {property_ref: soc0}}
  relations:
    - id: soc_step
      expression: "soc[t] = soc[t-1] + flow[t] * step_c"
"""

MUTEX_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.hub, names: {zh-CN: 枢纽, en-US: Hub}}
properties: {}
interfaces:
  charge: {type: in, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: 100}}
  discharge: {type: in, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: 100}}
equations:
  variables: {}
  relations:
    - id: exclusivity
      expression: "charge[t] * discharge[t] = 0"
"""

BILINEAR_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.bl, names: {zh-CN: 双线性, en-US: Bilinear}}
properties: {}
interfaces:
  x: {type: in, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
  z: {type: in, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
  y: {type: out, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
equations:
  variables: {}
  relations:
    - id: bilin
      expression: "y[t] = x[t] * z[t]"
"""


def _relation(yaml_text: str, rid: str):
    result = parse_device_model_v2(yaml_load(yaml_text), file="test.yaml")
    assert result.ok, result.diagnostics
    built = build_math_contribution(result.document)
    assert built.ok, built.diagnostics
    for relation in built.contribution.relations:
        if relation.id == rid:
            return relation
    raise AssertionError(f"关系缺失: {rid}")


def _coeffs(expr):
    return {key: coeff for key, coeff in expr.terms}


def test_affine_relation_with_const_folding() -> None:
    relation = _relation(HEATER_YAML, "conversion")
    symbols = {
        "efficiency": ("const", 0.9),
        "electricity_in": ("var", 0.0, None),
        "heat_out": ("var", 0.0, None),
    }
    lowered = lower_relation(relation, symbols, step=0)
    assert lowered.variables == ()
    (row,) = lowered.constraints
    assert (row.name, row.sense, row.rhs) == ("conversion", "eq", 0.0)
    assert _coeffs(row.expression) == {
        ("heat_out", 0): 1.0, ("electricity_in", 0): -0.9,
    }
    assert row.expression.constant == 0.0


def test_state_initial_at_step_zero_and_symbolic_later() -> None:
    relation = _relation(SOC_YAML, "soc_step")
    symbols = {
        "soc": ("var", 0.0, 1.0),
        "flow": ("var", 0.0, None),
        "step_c": ("const", 1.0),
    }
    first = lower_relation(relation, symbols, step=0, initial={"soc": 0.5})
    (row,) = first.constraints
    assert _coeffs(row.expression) == {("soc", 0): 1.0, ("flow", 0): -1.0}
    assert row.expression.constant == -0.5
    later = lower_relation(relation, symbols, step=2, initial={"soc": 0.5})
    (later_row,) = later.constraints
    assert _coeffs(later_row.expression) == {
        ("soc", 0): 1.0, ("soc", -1): -1.0, ("flow", 0): -1.0,
    }


def test_state_missing_initial_unsupported() -> None:
    relation = _relation(SOC_YAML, "soc_step")
    symbols = {
        "soc": ("var", 0.0, 1.0),
        "flow": ("var", 0.0, None),
        "step_c": ("const", 1.0),
    }
    with pytest.raises(LoweringUnsupportedError):
        lower_relation(relation, symbols, step=0, initial={})


def test_mutex_big_m_with_finite_bounds() -> None:
    relation = _relation(MUTEX_YAML, "exclusivity")
    symbols = {
        "charge": ("var", 0.0, 100.0),
        "discharge": ("var", 0.0, 100.0),
    }
    lowered = lower_relation(relation, symbols, step=3)
    (introduced,) = lowered.variables
    assert (introduced.name, introduced.lower, introduced.upper, introduced.kind) == (
        "exclusivity__mutex", 0.0, 1.0, "binary",
    )
    rows = {row.name: row for row in lowered.constraints}
    assert _coeffs(rows["exclusivity__mutex_x"].expression) == {
        ("charge", 0): 1.0, ("exclusivity__mutex", 0): -100.0,
    }
    assert rows["exclusivity__mutex_x"].rhs == 0.0
    assert _coeffs(rows["exclusivity__mutex_y"].expression) == {
        ("discharge", 0): 1.0, ("exclusivity__mutex", 0): 100.0,
    }
    assert rows["exclusivity__mutex_y"].rhs == 100.0


def test_mutex_without_upper_bound_unsupported() -> None:
    relation = _relation(MUTEX_YAML, "exclusivity")
    symbols = {
        "charge": ("var", 0.0, None),
        "discharge": ("var", 0.0, 100.0),
    }
    with pytest.raises(LoweringUnsupportedError):
        lower_relation(relation, symbols, step=0)


def test_mutex_without_nonnegativity_unsupported() -> None:
    relation = _relation(MUTEX_YAML, "exclusivity")
    symbols = {
        "charge": ("var", -5.0, 100.0),
        "discharge": ("var", 0.0, 100.0),
    }
    with pytest.raises(LoweringUnsupportedError):
        lower_relation(relation, symbols, step=0)


def test_bilinear_unsupported() -> None:
    relation = _relation(BILINEAR_YAML, "bilin")
    symbols = {
        "x": ("var", 0.0, 10.0),
        "z": ("var", 0.0, 10.0),
        "y": ("var", 0.0, None),
    }
    with pytest.raises(LoweringUnsupportedError):
        lower_relation(relation, symbols, step=0)


def test_deep_past_reference_unsupported_at_lowering() -> None:
    ast = RelationAst(
        id="r1",
        output="a",
        lhs_root=RefNode(name="a", offset=0),
        rhs_root=RefNode(name="a", offset=-2),
        lhs_refs=(),
        rhs_refs=(),
    )
    relation = MathRelation(id="r1", output="a", ast=ast)
    with pytest.raises(LoweringUnsupportedError):
        lower_relation(relation, {"a": ("var", 0.0, 1.0)}, step=5)


def test_expression_division_and_terms_sorted() -> None:
    node = BinaryNode(
        op="+",
        left=RefNode(name="b", offset=0),
        right=BinaryNode(
            op="/", left=RefNode(name="a", offset=0), right=NumberNode(value=2.0)
        ),
    )
    expr = lower_expression(
        node, {"a": ("var", 0.0, None), "b": ("var", 0.0, None)}, step=0
    )
    assert expr.terms == ((("a", 0), 0.5), (("b", 0), 1.0))


def test_expression_var_divisor_unsupported() -> None:
    node = BinaryNode(
        op="/", left=RefNode(name="a", offset=0), right=RefNode(name="b", offset=0)
    )
    with pytest.raises(LoweringUnsupportedError):
        lower_expression(
            node, {"a": ("var", 0.0, None), "b": ("var", 1.0, 2.0)}, step=0
        )


def test_planning_constraint_lowering() -> None:
    symbols = {"import_meter__period_energy_purchase": Counter({"currency": 1})}
    compiled = compile_planning_expression(
        "import_meter__period_energy_purchase <= 900000 CNY",
        symbols,
        currency="CNY",
    )
    constraint = PlanningProgramConstraint(
        name="cap", type="annual_purchase_cost_limit", expression=compiled
    )
    row = lower_planning_constraint(
        constraint, {"import_meter__period_energy_purchase": ("var", 0.0, None)}
    )
    assert (row.name, row.sense, row.rhs) == ("cap", "le", 0.0)
    assert _coeffs(row.expression) == {("import_meter__period_energy_purchase", 0): 1.0}
    assert row.expression.constant == -900000.0


def test_planning_strict_comparison_unsupported() -> None:
    compiled = compile_planning_expression("score < 5", {"score": Counter()})
    constraint = PlanningProgramConstraint(
        name="strict", type="ratio", expression=compiled
    )
    with pytest.raises(LoweringUnsupportedError):
        lower_planning_constraint(constraint, {"score": ("var", 0.0, 10.0)})


def test_planning_expression_lowering() -> None:
    compiled = compile_planning_expression(
        "pv1__upfront_capex + pv_cap * 2",
        {
            "pv1__upfront_capex": Counter({"currency": 1}),
            "pv_cap": Counter({"currency": 1}),
        },
    )
    expr = lower_planning_expression(
        compiled,
        {
            "pv1__upfront_capex": ("const", 3500.0),
            "pv_cap": ("var", 0.0, 500.0),
        },
    )
    assert _coeffs(expr) == {("pv_cap", 0): 2.0}
    assert expr.constant == 3500.0
