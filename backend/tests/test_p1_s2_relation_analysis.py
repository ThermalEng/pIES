"""P1-S2: hand-computable bound requests and a sealed source dependency view.

These tests use only synthetic in-memory source identities; no customer data,
runtime state or second public contract. Real provider/HiGHS and campus
resource tests live in the existing Docker integration lanes.
"""
from __future__ import annotations

import pytest

from iesplan.computation.generators.compiler_ir import (
    BoundRequest,
    CompilerAnalysis,
    CompilerInventory,
    IndexDomain,
    SemanticRelation,
    SourceLocation,
    VatPoolOperation,
    IncomeTaxOperation,
)
from iesplan.computation.generators.linear import (
    LinearVariable, LoweringUnsupportedError,
)
from iesplan.computation.generators.project import _AffineAcc


def test_shared_interval_hand_arithmetic_and_exact_mutex() -> None:
    origin = SourceLocation("finance.credit_pools.demo", binding_id="demo")
    vat = SemanticRelation(
        "vat.demo", "vat", IndexDomain("month", 2),
        VatPoolOperation("demo", {"vat": [], "construction": []}),
        origin, input_symbols=("x", "y"), requires_bounds=True,
    )
    inventory = CompilerInventory()
    inventory.collect(vat)
    with pytest.raises(RuntimeError, match="complete"):
        CompilerAnalysis(inventory, {})
    inventory.seal()
    analysis = CompilerAnalysis(inventory, {
        "x": LinearVariable("x", 0.0, 8.0),
        "y": LinearVariable("y", 0.0, 6.0),
    })
    amount = _AffineAcc()
    amount.add_const(2)
    amount.add_term("x", -1)
    amount.add_term("y", 2)
    # 2 - 8 + 0 = -6 and 2 + 0 + 12 = 14.
    assert analysis.affine_side(
        amount, side="min", label="synthetic VAT month 1", relation=vat, period=1,
    ) == -6
    assert analysis.affine_side(
        amount, side="max", label="synthetic VAT month 1", relation=vat, period=1,
    ) == 14
    assert {request.side for request in analysis.bound_requests} == {"min", "max"}
    assert all(
        isinstance(request, BoundRequest)
        and request.relation_id == vat.identity
        and request.period == 1
        and request.source == origin
        for request in analysis.bound_requests
    )
    result = analysis.mutex("vat__demo__m1", "x", "y", relation=vat, period=1)
    assert len(result.variables) == 1
    assert result.variables[0].kind == "binary"
    assert len(result.constraints) == 2
    assert vat.identity in analysis.uses["x"]
    assert vat.identity in analysis.uses["y"]


def test_unbounded_condition_is_not_guessed_from_another_source() -> None:
    relation = SemanticRelation(
        "vat.unbounded", "vat", IndexDomain("month"),
        VatPoolOperation("unbounded", {"vat": [], "construction": []}),
        SourceLocation("finance.credit_pools.unbounded"),
        input_symbols=("z",),
        requires_bounds=True,
    )
    inventory = CompilerInventory()
    inventory.collect(relation)
    inventory.seal()
    analysis = CompilerAnalysis(inventory, {
        "z": LinearVariable("z", lower=0, upper=None),
    })
    amount = _AffineAcc()
    amount.add_term("z", 1)
    with pytest.raises(LoweringUnsupportedError, match="缺有限界"):
        analysis.affine_side(
            amount, side="max", label="synthetic month", relation=relation,
        )
    with pytest.raises(LoweringUnsupportedError, match="缺有限上界"):
        analysis.mutex("vat_unbounded", "z", "z", relation=relation)


def test_income_conversion_dependency_is_predeclared_and_stable() -> None:
    inventory = CompilerInventory()
    vat = SemanticRelation(
        "vat.p", "vat", IndexDomain("month", 12),
        VatPoolOperation("p", {"vat": [], "construction": []}),
        SourceLocation("finance.credit_pools.p"),
    )
    income = SemanticRelation(
        "income.t", "income", IndexDomain("year"),
        IncomeTaxOperation("t", object()),
        SourceLocation("finance.tax_rules.t"),
        depends_on=("vat.p",),
        requires_bounds=True,
    )
    inventory.collect(vat)
    inventory.collect(income)
    inventory.seal()
    analysis = CompilerAnalysis(inventory, {})
    phases = analysis.conversion_phases()
    assert phases.index("demand") < phases.index("vat") < phases.index("income")
    assert phases.index("income") < phases.index("net_cash") < phases.index("objective")
    assert analysis.dependencies["income.t"] == ("vat.p",)
    with pytest.raises(RuntimeError, match="no source relations"):
        inventory.collect(vat)
