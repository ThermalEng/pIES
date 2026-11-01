"""Independent PR38 counterexamples; desired invariants, not production edits."""
import json
from dataclasses import replace

from iesplan.core.contracts.planning_config import PlanningVariable
from iesplan.computation.generators.compiler_ir import (
    BoundFact, CompilerAnalysis, CompilerInventory, IndexDomain, PlanningRule,
    SemanticRelation, SourceLocation,
)
from iesplan.computation.generators.linear import (
    LinearConstraint, LinearExpression, LinearVariable,
)
from iesplan.computation.generators.project import build_project_model
from iesplan.modeling import BinaryNode, NumberNode, RefNode
from test_p2_s2_derivation import _make_family
from test_project_model import GEN_YAML, _contribution, _gen_inputs, _finance_payload


def test_activation_reaches_public_planning_contract():
    from iesplan.core.contracts.planning_config import PlanningConfig, PlanningConfigError
    import pytest
    valid = {
        "objective": {"sense": "minimize", "expression": "gen_cap"},
        "variables": {"gen_cap": {
            "device_ref": "gen1", "parameter": "cap_kw", "lower_bound": "0",
            "upper_bound": "500", "unit": "kW",
        }},
        "construction_units": {"gen_build": {
            "device_ref": "gen1", "mode": "optional",
            "scales": {"gen_cap": {"active_lower_bound": "0", "active_upper_bound": None}},
        }},
    }
    assert PlanningConfig.from_dict(valid).to_dict()["construction_units"]["gen_build"]["mode"] == "optional"
    old = dict(valid)
    old["variables"] = {"gen_cap": dict(valid["variables"]["gen_cap"], activation=True)}
    with pytest.raises(PlanningConfigError):
        PlanningConfig.from_dict(old)


def test_valid_accumulator_survives_real_project_generation():
    args = list(_gen_inputs())
    text = GEN_YAML.replace(
        "  variables:\n",
        '  variables:\n    counter:\n      unit: "1"\n'
        '      valid_range: {minimum: 0, maximum: null}\n'
        '      initial: {expression: "0"}\n',
    ) + '    - id: counter_update\n      expression: "counter[t] = counter[t-1] + 1"\n'
    args[1]["gen1"] = _contribution(text)
    build_project_model(*args)


def test_small_nonzero_coefficient_preserves_feasible_witness():
    inv = CompilerInventory()
    inv.relations.append(_make_family(
        "tiny", RefNode("y"), BinaryNode("*", NumberNode(1e-13), RefNode("x")), inst="m",
    ))
    inv.register_bound_fact(BoundFact("m.x", lower=0.0, upper=1e15))
    inv.register_bound_fact(BoundFact("m.y", lower=0.0, upper=1000.0))
    inv.seal()
    ana = CompilerAnalysis(inv, {})
    # x=1e15,y=100 is a feasible point of y=1e-13*x.
    lo, hi = ana.variable_bounds("m.y")
    assert lo <= 100 <= hi, (lo, hi)


def test_derived_step_bound_reaches_actual_solver_column():
    inv = CompilerInventory()
    inv.property_bindings[("m", "capacity")] = "cap"
    # The P1 inventory publishes actual step columns; no consumer is allowed
    # to deduce symbol identity by splitting solver-variable name strings.
    inv.step_columns[("m", "flow")] = ("m__flow__t0", "m__flow__t1")
    inv.relations.append(replace(_make_family(
        "flow", RefNode("flow"), BinaryNode("*", NumberNode(2), RefNode("capacity")), inst="m",
    ), domain=IndexDomain("step", 2)))
    inv.register_bound_fact(BoundFact("cap", lower=0.0, upper=3.0))
    inv.seal()
    ana = CompilerAnalysis(inv, {
        "cap": LinearVariable("cap", 0, 3),
        "m__flow__t0": LinearVariable("m__flow__t0", 0, None),
        "m__flow__t1": LinearVariable("m__flow__t1", 0, None),
    })
    assert ana.variable_bounds("m__flow__t0")[1] == 6
    assert ana.variable_bounds("m__flow__t1")[1] == 6


def test_planning_inequality_supplies_requested_bound():
    inv = CompilerInventory()
    row = LinearConstraint("limit", LinearExpression(terms=((("q", 0), 1.0),)), "le", 7.0)
    inv.collect(SemanticRelation(
        "planning.limit", "planning_scalar", IndexDomain("scalar"),
        PlanningRule("limit", row, False, {}), SourceLocation("planning.constraints.limit"),
        input_symbols=("q",),
    ))
    inv.register_bound_fact(BoundFact("q", lower=0.0, upper=None))
    inv.seal()
    ana = CompilerAnalysis(inv, {"q": LinearVariable("q", 0, None)})
    assert ana.variable_bounds("q")[1] == 7


def test_optional_fixed_residual_uses_real_source_identity():
    from test_p2_s3_activation import _activation_inputs
    from iesplan.finance import FinanceProfile, merge_effective
    args = list(_activation_inputs(optional=True, fixed_fee=50000.0, lower_kw=0.0))
    p = _finance_payload()
    p["finance_types"]["tank_sys"]["upfront_capex"] = {
        "fixed": {"value": "50000", "unit": "CNY"},
        "linear": {"cap": {"unit_cost": {"value": "3500", "unit": "CNY/kW"}}},
    }
    p["taxes"] = {"income_tag": {
        "display_name": "Synthetic income", "tax_type": "income_tax",
        "applies_to": "tank_sys.upfront_capex",
    }}
    p["tax_rules"] = {"income": {
        "kind": "income_tax", "label_ref": "income_tag", "rate": {"value": "0.25", "unit": "1"},
        "revenue": [], "energy_cost": [], "annual_om": [], "variable_om": [],
        "deductible_maintenance": {"mode": "explicit_items", "items": []},
        "depreciation": [{
            "base_from": {"kind": "upfront_capex_net", "finance_type": "tank_sys", "scope": {"kind": "all_new_instances"}},
            "method": "straight_line", "life_years": 20, "first_year": 1,
            "residual": {"value": "100", "unit": "CNY"},
        }], "loss_carryforward_years": 5, "opening_losses": [],
    }}
    args[3] = merge_effective(FinanceProfile.from_dict(p), None)
    build_project_model(*args)


def test_strategy_evaluation_fixture_actually_contains_mutex(tmp_path):
    from p2_mutex_measurement import evaluate_mutex_strategies
    args = list(_gen_inputs())
    args[1]["gen1"] = _contribution(
        GEN_YAML
        + '    - id: dispatch_spare_mutex\n'
        + '      expression: "dispatch[t] * spare[t] = 0"\n'
    )
    result = evaluate_mutex_strategies(*args, tmp_path)
    print(result)
    assert result.full_integers > 0, "Cannot assess mutex strategy using a model with zero binaries"


def test_model_domain_upper_bound_reaches_actual_mutex():
    args = list(_gen_inputs())
    args[1]["gen1"] = _contribution(
        GEN_YAML + '    - id: dispatch_spare_mutex\n      expression: "dispatch[t] * spare[t] = 0"\n'
    )
    # spare_kw's planning upper is None, but its model domain upper is 1000 kW.
    # Both actual flows are thus bounded. A valid mutex must be lowerable.
    build_project_model(*args)
