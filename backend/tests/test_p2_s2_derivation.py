"""P2-S2: relation-based bound derivation and correlated expression queries.

Requirements verified:
1. Derive conservative bounds from affine equations: C = 3h * P, 0 <= P <= 4 kW => C <= 12 kWh.
2. Order independence: relation declaration order does not affect derived bounds.
3. Avoid false back-propagation: flow <= capacity does not cap capacity when flow is capped.
4. Correlated expressions: x - y = 0 has difference bound 0 even if x, y are unbounded.
   Single-sided difference constraint (x - y <= 5) reports only the proven upper side.
5. State transition, initial value, and coefficient propagation.
6. Deterministic stopping rule and budget, no sub-solver, no Big-M guessing.
"""
from __future__ import annotations

import pytest

from iesplan.computation.generators.compiler_ir import (
    BoundFact,
    CompilerAnalysis,
    CompilerInventory,
    IndexDomain,
    KnownCoefficient,
    RelationFamily,
    SemanticRelation,
    SourceLocation,
    StateInitial,
    CapacityLimit,
)
from iesplan.computation.generators.linear import (
    LinearVariable,
    LoweringUnsupportedError,
)
from iesplan.computation.generators.project import _AffineAcc
from iesplan.modeling import (
    BinaryNode,
    MathRelation,
    NumberNode,
    RefNode,
    RelationAst,
)


def _make_family(
    rel_id: str,
    lhs_node: object,
    rhs_node: object,
    inst: str = "dev1",
) -> RelationFamily:
    ast = RelationAst(
        id=rel_id,
        output=None,
        lhs_root=lhs_node,
        rhs_root=rhs_node,
        lhs_refs=(),
        rhs_refs=(),
    )
    math_rel = MathRelation(id=rel_id, output=None, ast=ast)
    return RelationFamily(
        identity=f"{inst}.{rel_id}",
        relation=math_rel,
        domain=IndexDomain("scalar"),
        referenced_symbols=(),
        output_symbol=None,
        known_coefficients=(),
        source=SourceLocation(f"equations.{rel_id}", instance_id=inst),
    )


def test_relation_derivation_capacity_from_power() -> None:
    """C = 3h * P, 0 <= P <= 4 kW 推出 C <= 12 kWh (43.2 MJ)。"""
    inventory = CompilerInventory()
    origin = SourceLocation("properties.duration")
    # Known coefficient: 3h = 10800 s
    inventory.coefficients["duration"] = KnownCoefficient("duration", "fixed_property", origin, 10800.0)

    # Relation: C = duration * P => dev1.C = 10800 * dev1.P
    # LHS: C, RHS: duration * P
    lhs = RefNode("C")
    rhs = BinaryNode("*", RefNode("duration"), RefNode("P"))
    family = _make_family("cap_rel", lhs, rhs, inst="dev1")
    inventory.relations.append(family)

    # Bounds on P: [0, 4000] W (4 kW)
    fact_p = BoundFact("dev1.P", lower=0.0, upper=4000.0)
    inventory.register_bound_fact(fact_p)
    inventory.seal()

    analysis = CompilerAnalysis(inventory, {})
    lo, hi = analysis.variable_bounds("dev1.C")
    assert lo == pytest.approx(0.0)
    assert hi == pytest.approx(43_200_000.0)  # 12 kWh in Joules


def test_relation_order_independence() -> None:
    """调换无语义的关系声明顺序不改变有效推导结果。"""
    def build_with_order(swap: bool) -> tuple[float | None, float | None]:
        inv = CompilerInventory()
        # Equalities: Y = 2 * X, and Z = Y + 10
        # X in [1, 5]
        rel1 = _make_family("r1", RefNode("Y"), BinaryNode("*", NumberNode(2.0), RefNode("X")), inst="dev")
        rel2 = _make_family("r2", RefNode("Z"), BinaryNode("+", RefNode("Y"), NumberNode(10.0)), inst="dev")

        if swap:
            inv.relations.extend([rel2, rel1])
        else:
            inv.relations.extend([rel1, rel2])

        inv.register_bound_fact(BoundFact("dev.X", lower=1.0, upper=5.0))
        inv.seal()
        ana = CompilerAnalysis(inv, {})
        return ana.variable_bounds("dev.Z")

    b1 = build_with_order(swap=False)
    b2 = build_with_order(swap=True)
    assert b1 == b2
    # 2*1 + 10 = 12, 2*5 + 10 = 20
    assert b1 == (pytest.approx(12.0), pytest.approx(20.0))


def test_avoid_false_backpropagation_to_capacity() -> None:
    """仅有实际输出受限时，不给额定容量凭空加界。"""
    inventory = CompilerInventory()
    # Operation: flow <= capacity (CapacityLimit)
    cap_limit = SemanticRelation(
        "cap_limit", "capacity", IndexDomain("scalar"),
        CapacityLimit(instance="cell", local="flow", planning_symbol="cell_cap", sense="le"),
        SourceLocation("interfaces.flow"),
    )
    inventory.collect(cap_limit)
    # flow is bounded to [0, 4000] W, cell_cap is unbounded [0, None]
    inventory.register_bound_fact(BoundFact("cell.flow", lower=0.0, upper=4000.0))
    inventory.register_bound_fact(BoundFact("cell_cap", lower=0.0, upper=None))
    inventory.seal()

    analysis = CompilerAnalysis(inventory, {})
    cap_lo, cap_hi = analysis.variable_bounds("cell_cap")
    assert cap_lo == pytest.approx(0.0)
    # cell_cap MUST NOT be capped at 4000!
    assert cap_hi is None


def test_correlated_expression_bounds_exact_zero_and_single_sided() -> None:
    """关联表达式查询：x - y = 0 时差值为零；单侧差值约束只报告已证明的一侧。"""
    # Case 1: x - y = 0 with unbounded x, y
    inv1 = CompilerInventory()
    # rel: x = y => x - y = 0
    rel_eq = _make_family("eq", RefNode("x"), RefNode("y"), inst="m")
    inv1.relations.append(rel_eq)
    # x and y are unbounded
    inv1.register_bound_fact(BoundFact("m.x", lower=None, upper=None))
    inv1.register_bound_fact(BoundFact("m.y", lower=None, upper=None))
    inv1.seal()

    ana1 = CompilerAnalysis(inv1, {})
    expr = _AffineAcc()
    expr.add_term("m.x", 1.0)
    expr.add_term("m.y", -1.0)
    # Exact zero on both sides
    assert ana1.affine_side(expr, side="min", label="test") == pytest.approx(0.0)
    assert ana1.affine_side(expr, side="max", label="test") == pytest.approx(0.0)

    # Case 2: Single-sided difference constraint flow <= cap (flow - cap <= 0)
    # cap and flow are unbounded individually
    inv2 = CompilerInventory()
    cap_limit = SemanticRelation(
        "cap_limit", "capacity", IndexDomain("scalar"),
        CapacityLimit(instance="m", local="flow", planning_symbol="cap", sense="le"),
        SourceLocation("interfaces.flow"),
    )
    inv2.collect(cap_limit)
    inv2.register_bound_fact(BoundFact("m.flow", lower=None, upper=None))
    inv2.register_bound_fact(BoundFact("cap", lower=None, upper=None))
    inv2.seal()
    ana2 = CompilerAnalysis(inv2, {})

    diff = _AffineAcc()
    diff.add_term("m.flow", 1.0)
    diff.add_term("cap", -1.0)
    # Upper bound is proven (<= 0.0)
    assert ana2.affine_side(diff, side="max", label="test") == pytest.approx(0.0)
    # Lower bound is not proven => raises LoweringUnsupportedError
    with pytest.raises(LoweringUnsupportedError, match="缺有限界"):
        ana2.affine_side(diff, side="min", label="test")


def test_state_initial_propagation_from_planning_symbol() -> None:
    """状态初值依赖规划变量，有效界正确跨期推导。"""
    inventory = CompilerInventory()
    # cap in [0, 1000] kWh = [0, 3.6e9] J
    inventory.register_bound_fact(BoundFact("cap", lower=0.0, upper=3.6e9))
    # initial state = 0.5 * cap
    from iesplan.computation.generators.linear import LinearExpression
    expr = LinearExpression(terms=((("cap", 0), 0.5),), constant=0.0)
    state_init = SemanticRelation(
        "init", "initial", IndexDomain("scalar"),
        StateInitial(instance="batt", state="soc_init", value=expr),
        SourceLocation("equations.variables.soc_init"),
    )
    inventory.collect(state_init)
    inventory.seal()

    analysis = CompilerAnalysis(inventory, {})
    lo, hi = analysis.variable_bounds("batt.soc_init.initial")
    assert lo == pytest.approx(0.0)
    assert hi == pytest.approx(1.8e9)  # 0.5 * 3.6e9


def test_deterministic_stopping_on_cyclic_relations() -> None:
    """物理环状联立关系在工作预算内确定性收敛终止，不挂起。"""
    inventory = CompilerInventory()
    # Cycle: A = B, B = A
    rel1 = _make_family("r1", RefNode("A"), RefNode("B"), inst="c")
    rel2 = _make_family("r2", RefNode("B"), RefNode("A"), inst="c")
    inventory.relations.extend([rel1, rel2])
    inventory.register_bound_fact(BoundFact("c.A", lower=10.0, upper=20.0))
    inventory.seal()

    # Terminates deterministically and derives B in [10, 20]
    analysis = CompilerAnalysis(inventory, {})
    b_lo, b_hi = analysis.variable_bounds("c.B")
    assert b_lo == pytest.approx(10.0)
    assert b_hi == pytest.approx(20.0)


def test_state_lag_keeps_actual_step_and_initial_boundary() -> None:
    """s[0]=1, s[1]=2, s[2]=3; no indexed bound poisons another step."""
    from dataclasses import replace
    from iesplan.computation.generators.templates import compile_technical
    inv = CompilerInventory()
    inst = "m"
    inv.step_columns[(inst, "s")] = tuple(f"state_step_{i}" for i in range(3))
    inv.register_bound_fact(BoundFact("m.s", lower=0.0, upper=3.0))
    rhs = BinaryNode("+", RefNode("s", offset=-1), NumberNode(1.0))
    family = replace(
        _make_family("recurrence", RefNode("s"), rhs, inst),
        domain=IndexDomain("step", 3),
    )
    inv.relations.append(family)
    inv.technical_contexts[family.identity] = (
        compile_technical(family.relation, {"s": "var"}),
        {}, {"s": 0.0}, {}, {"s": ("var", 0.0, 3.0)},
    )
    inv.seal()
    analysis = CompilerAnalysis(inv, {})
    for i, expected in enumerate((1.0, 2.0, 3.0)):
        lo, hi = analysis.variable_bounds(f"state_step_{i}")
        assert lo == pytest.approx(expected, abs=1e-10)
        assert hi == pytest.approx(expected, abs=1e-10)


def test_near_proportional_inequality_retains_large_witness() -> None:
    """x=y=1e15 remains feasible despite nonidentical query coefficients."""
    inv = CompilerInventory()
    inv.collect(SemanticRelation(
        "x_less_y", "capacity", IndexDomain("scalar"),
        CapacityLimit("m", "x", "y", "le"), SourceLocation("comparison"),
    ))
    inv.register_bound_fact(BoundFact("m.x", 0.0, 1e15))
    inv.register_bound_fact(BoundFact("y", 0.0, 1e15))
    inv.seal()
    analysis = CompilerAnalysis(inv, {})
    expr = _AffineAcc()
    expr.add_term("m.x", 1.0)
    expr.add_term("y", -(1.0 - 1e-10))
    upper = analysis.affine_side(expr, side="max", label="near_ratio")
    witness = 1e15 - (1.0 - 1e-10) * 1e15
    assert upper >= witness - 1e-5
    assert upper < 100_001.0


def test_existing_bus_balance_propagates_to_actual_time_slots() -> None:
    """The old m.out=n.in bus and n.in<=7 bound every actual m.out column."""
    from iesplan.computation.generators.compiler_ir import BusBalance
    inv = CompilerInventory()
    inv.step_columns[("m", "out")] = ("m_out_0", "m_out_1")
    inv.step_columns[("n", "in")] = ("n_in_0", "n_in_1")
    inv.register_bound_fact(BoundFact("n.in", 0.0, 7.0))
    inv.collect(SemanticRelation(
        "bus.carrier", "bus", IndexDomain("step", 2),
        BusBalance("heat", 0, (("m", "out", 1.0), ("n", "in", -1.0))),
        SourceLocation("connections"),
    ))
    inv.seal()
    analysis = CompilerAnalysis(inv, {})
    for step in range(2):
        lo, hi = analysis.variable_bounds(f"m_out_{step}")
        assert lo == pytest.approx(0.0, abs=1e-10)
        assert hi == pytest.approx(7.0, abs=1e-10)


def test_state_lag_across_blocks_and_year_with_changing_known_coefficient() -> None:
    """P1 coefficient binding honors the 256-step block and annual boundary."""
    from dataclasses import replace
    from iesplan.computation.generators.templates import compile_technical
    n = 8761  # hourly year plus the first step of the next year
    inv = CompilerInventory()
    inv.step_columns[("m", "s")] = tuple(f"s_{i}" for i in range(n))
    inv.register_bound_fact(BoundFact("m.s", lower=0.0, upper=10000.0))
    rhs = BinaryNode("+", RefNode("s", offset=-1), RefNode("increment"))
    family = replace(
        _make_family("step", RefNode("s"), rhs, "m"),
        domain=IndexDomain("step", n),
    )
    inv.relations.append(family)
    inv.technical_contexts[family.identity] = (
        compile_technical(family.relation, {"s": "var", "increment": "known"}),
        {}, {"s": 0.0},
        {"increment": tuple(2.0 if i == 8760 else 1.0 for i in range(n))},
        {"s": ("var", 0.0, 10000.0)},
    )
    inv.seal()
    analysis = CompilerAnalysis(inv, {})
    for step, expected in (
        (0, 1.0), (255, 256.0), (256, 257.0),
        (8759, 8760.0), (8760, 8762.0),
    ):
        lo, hi = analysis.variable_bounds(f"s_{step}")
        assert lo == pytest.approx(expected, abs=1e-6)
        assert hi == pytest.approx(expected, abs=1e-6)


def test_exact_zero_propagates_without_artificial_subnormal() -> None:
    """A mathematically zero bound must stay 0, never nextafter(0) epsilon."""
    inventory = CompilerInventory()
    inventory.relations.append(_make_family(
        "zero", RefNode("x"), NumberNode(0.0), inst="m",
    ))
    inventory.register_bound_fact(BoundFact("m.x", 0.0, 10.0))
    inventory.seal()
    analysis = CompilerAnalysis(inventory, {})
    assert analysis.variable_bounds("m.x") == (0.0, 0.0)


def test_infeasible_source_rows_do_not_get_rewritten_as_invalid_bounds() -> None:
    """Retain original rows for the exact MILP to diagnose infeasibility.

    Contradictory *derived* intervals are not a license to remove a planning
    row, create a fake Big-M, or prevent the public generator emitting rows.
    """
    from iesplan.computation.generators.compiler_ir import PlanningRule
    from iesplan.computation.generators.linear import LinearConstraint, LinearExpression

    inventory = CompilerInventory()
    for name, sense, rhs in (("upper", "le", 0.0), ("lower", "ge", 1.0)):
        row = LinearConstraint(
            name,
            LinearExpression(terms=((("x", 0), 1.0),), constant=0.0),
            sense, rhs,
        )
        inventory.collect(SemanticRelation(
            f"planning.{name}", "planning_scalar", IndexDomain("scalar"),
            PlanningRule(name, row, False, {}),
            SourceLocation(f"planning.constraints.{name}"),
            input_symbols=("x",),
        ))
    inventory.register_bound_fact(BoundFact("x", 0.0, 2.0))
    inventory.seal()
    analysis = CompilerAnalysis(inventory, {})
    assert analysis.inference.conflicts
    assert analysis.variable_bounds("x") == (0.0, 2.0)
    assert len(analysis.relations("planning_scalar")) == 2
