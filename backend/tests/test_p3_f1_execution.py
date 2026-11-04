"""F1 small genuine HiGHS solves and same-primal financial fact recovery.

Most public synthetic fixtures have only scalar decisions; physical metering
uses the public one-year network fixture. All cash/tax checks use the main
solution, not a second solver or a separate financial calculation.
"""
from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_finance_s2_execution import _income, _money, _vat_labels
from test_highs_result_adapter import (
    PARAMETERS_PATH,
    PROBLEM_PATH,
    SOLUTION_PATH,
    _bundle,
    _parameters,
    _receipt,
)
from test_issue06_s2 import _amount, _builder, _collect_only, _tiny_solve

from iesplan.application.worker.compute_cases import build_financial_evaluation
from iesplan.computation.generators.mps import write_mps
from iesplan.computation.generators.project import _ProjectBuilder
from iesplan.computation.result_adapters.highs import HIGHS_ADAPTER


def _rebuild(base, mutate=None):
    doc = deepcopy(base.doc)
    if mutate:
        mutate(doc)
    return _ProjectBuilder(
        doc=doc, devices=doc["devices"], models=base.models, series=(), finance=base.finance,
        evaluation=base.evaluation, point_count=0, step_s=3600.0,
    )


def _fix_capacity(doc, q="3", *, b_existing=True):
    doc["planning"]["variables"]["cap_a"].update({"lower_bound": q, "upper_bound": q})
    if b_existing:
        doc["devices"]["b"]["asset_origin"] = "existing"


def _items(profile):
    profile["finance_types"]["same"] = {
        "upfront_capex": {"linear": {"cap": {"unit_cost": _money("0", "CNY/kW")}}},
        "annual_fixed_om": {"items": {
            "materials": {"linear": {"cap": {"unit_cost": _money("2", "CNY/kW/a")}}},
            "service": {"fixed": _money("10", "CNY/a"),
                        "linear": {"cap": {"unit_cost": _money("5", "CNY/kW/a")}}},
        }},
    }
    _vat_labels(profile)
    profile["tax_rules"] = {
        "vat": {
            "kind": "vat", "label_ref": "vat_tag", "settlement": "monthly",
            "in_tax": [], "out_tax": [],
            "om_input_tax": [{
                "for": "same.annual_fixed_om.items.materials",
                "rate": _money("0.1", "1"), "deductible_ratio": _money("1", "1"),
            }],
            "maintenance_input_tax": [], "credit_pool": "pool", "confirmation": "same_period",
        },
        "income": _income([], annual=True),
    }


def _adapt(builder, model, facts, tmp_path):
    mps, offset = write_mps(model)
    parameters = json.loads(_parameters(offset=offset, objective_unit="CNY"))
    parameters["unit_map"] = dict(builder.unit_map)
    parameters["components"] = {name: dict(entry) for name, entry in builder.finance_components().items()}
    parameters["financial_facts"] = dict(facts)
    bundle = _bundle({
        PROBLEM_PATH: mps.encode(), PARAMETERS_PATH: json.dumps(parameters, sort_keys=True).encode(),
    })
    result = HIGHS_ADAPTER.adapt(
        bundle, _receipt(), {SOLUTION_PATH: (tmp_path / "output/solution.sol").read_bytes()},
    )
    assert result.math_status == "solved"
    return result


def _named_values(facts):
    return {
        (entry["id"], value["period"]): float(value["value"])
        for entry in facts["amounts"] for value in entry["values"]
    }


def test_two_named_items_same_driver_have_separate_tax_and_31_year_fee(tmp_path):
    builder = _rebuild(_builder([], finance_patch=_items), _fix_capacity)
    solved, facts, model = _tiny_solve(builder, tmp_path, with_model=True)
    assert solved["cap_a"] == pytest.approx(3000)
    assert _amount(facts["annual_fixed_om"], **solved) == pytest.approx(31)
    result = _adapt(builder, model, facts, tmp_path)
    recovered = result.outputs["financial_facts"]
    items = {(row["instance_id"], row["item_id"]): row for row in recovered["cost_items"]}
    assert set(items) == {("a", "materials"), ("a", "service")}
    material, service = items[("a", "materials")], items[("a", "service")]
    assert material["unit"] == service["unit"] == "CNY/a"
    assert float(material["raw"]) == pytest.approx(6)
    assert float(service["raw"]) == pytest.approx(25)
    assert float(material["deductible_input"]) == pytest.approx(6 / 11)
    assert float(service["deductible_input"]) == pytest.approx(0, abs=1e-8)
    assert float(material["net_basis"]) == pytest.approx(6 - 6 / 11)
    assert float(service["net_basis"]) == pytest.approx(25)
    income = recovered["tax_cash"]["income"]
    assert [float(row["taxable_base"]) for row in income] == pytest.approx([-31 + 6 / 11] * 2)
    assert [float(row["tax"]) for row in income] == pytest.approx([0, 0], abs=1e-8)
    assert [float(cf) for cf in recovered["annual_net_flows"]] == pytest.approx([0, -31, -31])
    sealed = build_financial_evaluation(
        SimpleNamespace(evaluation={"construction_period": 0, "operation_years": 2,
                                    "evaluation_years": 2, "discount_rate": 0.0}), builder.finance, result,
    )
    assert sealed["cost_items"] == [dict(row) for row in recovered["cost_items"]]
    assert float(sealed["evaluation"]["npv"]) == pytest.approx(-62)


def test_two_instances_same_category_keep_named_item_identity_and_vat_separate(tmp_path):
    def two(doc):
        _fix_capacity(doc, b_existing=False)
        doc["devices"]["b"]["properties"]["cap_kw"]["value"] = 4

    builder = _rebuild(_builder([], finance_patch=_items), two)
    solved, facts, model = _tiny_solve(builder, tmp_path, with_model=True)
    recovered = _adapt(builder, model, facts, tmp_path).outputs["financial_facts"]
    items = {(row["instance_id"], row["item_id"]): row for row in recovered["cost_items"]}
    expected = {("a", "materials"): 6, ("a", "service"): 25,
                ("b", "materials"): 8, ("b", "service"): 30}
    assert {key: float(row["raw"]) for key, row in items.items()} == pytest.approx(expected)
    assert float(recovered["annual_fixed_om"]) == pytest.approx(69)
    by_month = {row["month"]: row for row in recovered["tax_cash"]["vat"]}
    assert float(by_month[12]["carry"]) == pytest.approx(14 / 11)
    assert float(by_month[24]["carry"]) == pytest.approx(28 / 11)
    assert _amount(facts["annual_fixed_om"], **solved) == pytest.approx(69)


@pytest.mark.parametrize("built", [False, True])
def test_conditional_named_fixed_and_linear_use_same_state_not_always_cost(tmp_path, built):
    def patch(profile):
        profile["finance_types"]["same"] = {"upfront_capex": {"items": {
            "conditional": {"fixed": _money("10"),
                            "linear": {"cap": {"unit_cost": _money("2", "CNY/kW")}}},
            "always": {"fixed": _money("7")},
        }}}

    def activation(doc):
        doc["devices"]["b"]["asset_origin"] = "existing"
        doc["planning"]["variables"]["cap_a"]["lower_bound"] = "3" if built else "0"
        doc["planning"]["construction_units"] = {
            "build_a": {"device_ref": "a", "mode": "optional",
                        "scales": {"cap_a": {"active_lower_bound": "0", "active_upper_bound": None}}},
        }
        doc["finance_binding"]["instances"]["a"]["cost_occurrence"] = {
            "upfront_capex.items.conditional": {"kind": "construction_unit", "unit_ref": "build_a"},
        }

    builder = _rebuild(_builder([], finance_patch=patch), activation)
    solved, facts, model = _tiny_solve(builder, tmp_path, with_model=True)
    assert solved["built__build_a"] == pytest.approx(float(built))
    assert solved["cap_a"] == pytest.approx(3000 if built else 0)
    recovered = _adapt(builder, model, facts, tmp_path).outputs["financial_facts"]
    items = {row["item_id"]: row for row in recovered["cost_items"]}
    assert float(items["conditional"]["raw"]) == pytest.approx(16 if built else 0)
    assert float(items["always"]["raw"]) == pytest.approx(7)
    assert float(recovered["construction_total"]) == pytest.approx(23 if built else 7)
    assert [float(cf) for cf in recovered["annual_net_flows"]] == pytest.approx(
        [-23 if built else -7, 0, 0], abs=1e-8,
    )


def _definition(source, coefficient="1", *, unit="CNY", axis="scalar", constant="0"):
    return {"unit": unit, "axis": axis, "constant": _money(constant, unit),
            "terms": {} if source is None else {
                "term": {"coefficient": _money(coefficient, "1"), "source": source},
            }}


def _capacity_source():
    return {"kind": "driver", "finance_type": "same", "driver": "cap",
            "scope": {"kind": "instances", "instances": ["a"]}, "period": {"kind": "scalar"}}


def _amount_limit(doc, amount="spend", period=None, expression="spend <= 6 CNY"):
    doc["planning"]["constraints"]["limit"] = {
        "type": "amount_relation", "enabled": True,
        "references": {"spend": {"kind": "amount", "id": amount,
                                   "period": period or {"kind": "scalar"}}},
        "expression": expression,
    }


def _capacity_income(profile):
    profile["amounts"] = {
        "spend": _definition(_capacity_source()),
        "reward": _definition(_capacity_source()),
        "idle": _definition(None, constant="100000"),
    }
    profile["amounts"]["spend"]["terms"]["term"]["coefficient"] = _money("2", "CNY/kW")
    profile["amounts"]["reward"]["terms"]["term"]["coefficient"] = _money("400", "CNY/kW")
    profile["net_cashflows"] = {
        "reward": {"at": {"kind": "evaluation_end"}, "direction": "inflow",
                   "amount": {"kind": "amount_ref", "id": "reward"}},
    }


def test_amount_constraint_changes_capacity_and_is_visible_before_exact_conversion(tmp_path):
    def constrained():
        return _rebuild(_builder([], finance_patch=_capacity_income), _amount_limit)
    analyzed = _collect_only(constrained())
    assert analyzed.analysis.variable_bounds("cap_a")[1] == pytest.approx(3000)
    relation = analyzed.analysis.operation("planning.amount.limit")
    assert relation.payload.bound is not None
    assert relation.source.field_path == "planning.constraints.limit"
    unconstrained = _rebuild(_builder([], finance_patch=_capacity_income))
    full, _ = _tiny_solve(unconstrained, tmp_path / "unconstrained")
    limited_builder = constrained()
    limited, facts, model = _tiny_solve(limited_builder, tmp_path / "limited", with_model=True)
    assert full["cap_a"] == pytest.approx(10000)
    assert limited["cap_a"] == pytest.approx(3000)
    recovered = _adapt(limited_builder, model, facts, tmp_path / "limited").outputs["financial_facts"]
    assert _named_values(recovered) == pytest.approx({("reward", 0): 1200, ("spend", 0): 6})
    assert {entry["id"] for entry in recovered["amounts"]} == {"reward", "spend"}
    assert len(recovered["net_cashflows"]) == 1
    assert float(recovered["net_cashflows"][0]["amount"]) == pytest.approx(1200)
    # b contributes its 20,000 construction cost; idle 100,000 is not cash.
    assert [float(cf) for cf in recovered["annual_net_flows"]] == pytest.approx([-20600, 0, 1200])


def test_year_rate_current_and_explicit_range_keep_period_and_unit(tmp_path):
    def patch(profile):
        _items(profile)
        profile["taxes"], profile["tax_rules"], profile["credit_pools"] = {}, {}, {}
        cost = {"kind": "cost", "path": "same.annual_fixed_om", "basis": "raw",
                "scope": {"kind": "instances", "instances": ["a"]}}
        profile["amounts"] = {
            "annual_rate": _definition({**cost, "period": {"kind": "scalar"}}, unit="CNY/a"),
            "annual_cash": _definition({**cost, "period": {"kind": "current"}}, axis="financial_year"),
            "two_years": _definition({"kind": "amount", "id": "annual_cash",
                                       "period": {"kind": "range", "axis": "financial_year",
                                                  "first": 1, "last": 2}}),
        }
        for definition in profile["amounts"].values():
            definition["terms"]["term"]["coefficient"] = _money("1", "1")

    def document(doc):
        _fix_capacity(doc)
        _amount_limit(doc, "two_years", expression="spend <= 62 CNY")
        doc["planning"]["constraints"]["rate_limit"] = {
            "type": "amount_relation", "enabled": True,
            "references": {"fee": {"kind": "amount", "id": "annual_rate", "period": {"kind": "scalar"}}},
            "expression": "fee * 1 year <= 31 CNY",
        }

    builder = _rebuild(_builder([], finance_patch=patch), document)
    _, facts, model = _tiny_solve(builder, tmp_path, with_model=True)
    recovered = _adapt(builder, model, facts, tmp_path).outputs["financial_facts"]
    assert _named_values(recovered) == pytest.approx({
        ("annual_rate", 0): 31, ("annual_cash", 1): 31, ("annual_cash", 2): 31, ("two_years", 0): 62,
    })
    by_id = {entry["id"]: entry for entry in recovered["amounts"]}
    assert by_id["annual_rate"]["unit"] == "CNY/a"
    assert by_id["annual_cash"]["unit"] == "CNY"
    assert by_id["annual_cash"]["axis"] == "financial_year"
    assert [float(cf) for cf in recovered["annual_net_flows"]] == pytest.approx([0, -31, -31])


def test_named_amount_ordering_is_deterministic_and_idle_definitions_do_not_pay(tmp_path):
    first = _rebuild(_builder([], finance_patch=_capacity_income), _amount_limit)

    def reversed_profile(profile):
        _capacity_income(profile)
        profile["amounts"] = dict(reversed(tuple(profile["amounts"].items())))
        profile["finance_types"] = dict(reversed(tuple(profile["finance_types"].items())))

    second = _rebuild(_builder([], finance_patch=reversed_profile), _amount_limit)
    _, facts1, model1 = _tiny_solve(first, tmp_path / "first", with_model=True)
    _, facts2, model2 = _tiny_solve(second, tmp_path / "second", with_model=True)
    assert write_mps(model1) == write_mps(model2)
    assert json.dumps(dict(facts1), sort_keys=True) == json.dumps(dict(facts2), sort_keys=True)
    assert all(row["id"] != "idle" for row in facts1["amounts"])


def test_monthly_system_tariff_demand_and_conditional_cost_share_the_main_solution(tmp_path):
    """Real time-step sources, no exported-system-series prerequisite or second cash."""
    from test_p3_network_boundaries import _case, _solve
    from test_p3_port_network import SOURCE
    from test_p3_system_equations import _attach, _block
    from test_project_model import LOAD_YAML

    from iesplan.computation.generators.highs_milp import HIGHS_MILP_PROVIDER
    from iesplan.computation.generators.project import _new_builder
    from iesplan.finance import EffectiveFinanceConfig

    inputs, doc, documents = _case(
        {"source": SOURCE, "load": LOAD_YAML.replace("heat", "electricity")},
        {"delivery": {"from": "source.power_out", "to": "load.electricity_in"}},
        {"load": {"electricity_demand": {"mode": "constant", "value": 4}}},
    )
    finance = inputs[3].to_dict()
    finance["energy_prices"]["heat_import"].update({
        "carrier": "electricity", "value": _money("1", "CNY/kWh"),
    })
    finance["demand_charges"] = {"fee": {"rate": _money("33", "CNY/kW")}}
    finance["finance_types"]["dispatch"] = {"period_variable_om": {"items": {
        "service": {"linear": {"energy": {"unit_cost": _money("1", "CNY/kWh")}}},
    }}}
    sources = {
        "dispatch_month": ({"kind": "system_quantity", "block": "account", "variable": "dispatch",
                            "aggregation": "integrate_positive", "period": {"kind": "current"}}, "CNY/kWh"),
        "purchase_month": ({"kind": "tariff", "price": "heat_import", "bindings": ["purchase"],
                            "basis": "raw", "period": {"kind": "current"}}, "1"),
        "peak_month": ({"kind": "demand", "charge": "fee", "bindings": ["peak"],
                        "basis": "gross", "period": {"kind": "current"}}, "1"),
        "cost_month": ({"kind": "cost", "path": "dispatch.period_variable_om.items.service",
                        "scope": {"kind": "instances", "instances": ["source"]},
                        "basis": "net_basis", "period": {"kind": "current"}}, "1"),
    }
    finance["amounts"] = {}
    for name, (source, coefficient_unit) in sources.items():
        definition = _definition(source, axis="tax_month")
        definition["terms"]["term"]["coefficient"] = _money("1", coefficient_unit)
        finance["amounts"][name] = definition
    finance["amounts"]["all_months"] = {
        "axis": "scalar", "unit": "CNY", "constant": _money("0"),
        "terms": {name: {"coefficient": _money("1", "1"), "source": {
            "kind": "amount", "id": name,
            "period": {"kind": "range", "axis": "tax_month", "first": 1, "last": 12},
        }} for name in sources},
    }
    effective = EffectiveFinanceConfig.from_dict(finance)
    inputs = (*inputs[:3], effective, *inputs[4:])
    doc["devices"]["source"]["asset_origin"] = "new"
    doc["finance_binding"]["instances"]["source"] = {
        "type": "costed", "finance_type": "dispatch",
        "drivers": {"energy": {"interface": "power_out", "aggregation": "integrate_positive"}},
        "cost_occurrence": {"period_variable_om.items.service": {
            "kind": "construction_unit", "unit_ref": "build_source",
        }},
    }
    doc["planning"]["construction_units"] = {"build_source": {
        "device_ref": "source", "mode": "optional", "scales": {"fixture_scale": {
            "active_lower_bound": "1", "active_upper_bound": "1",
        }},
    }}
    doc["tariff_bindings"] = {"purchase": {
        "price": "heat_import", "instance": "load", "interface": "electricity_in",
        "aggregation": "integrate_positive",
    }}
    doc["demand_bindings"] = {"peak": {"charge": "fee", "instance": "source", "interface": "power_out"}}
    doc["planning"]["constraints"] = {"budget": {
        "type": "amount_relation", "references": {
            "spend": {"kind": "amount", "id": "all_months", "period": {"kind": "scalar"}},
        }, "expression": "spend <= 106704 CNY", "enabled": True,
    }}
    block = _block()
    block["references"] = {"supply": {"kind": "interface", "instance": "source", "interface": "power_out"}}
    block["variables"] = {"dispatch": {"unit": "kW", "valid_range": {"minimum": 0, "maximum": None}}}
    block["relations"] = [{"id": "actual", "expression": "dispatch[t] = supply[t]"}]
    checked, new_inputs = _attach(inputs, doc, documents, block, outputs=[])
    assert checked.ok, [(diag.location, diag.params) for diag in checked.diagnostics]
    builder = _new_builder(new_inputs[0], new_inputs[1], new_inputs[2], effective, new_inputs[5])
    model, _, _ = builder.build()
    before = (len(builder.variables), len(builder.rows))
    builder.financial_facts()
    builder.finance_components()
    builder.model_sources()
    assert (len(builder.variables), len(builder.rows)) == before
    assert len(model.variables) == before[0] and len(model.constraints) == before[1]
    bundle = HIGHS_MILP_PROVIDER.generate(*new_inputs)
    result = _solve(bundle, tmp_path)
    assert result.math_status == "solved"
    assert result.outputs["system_series"] == {}
    facts = result.outputs["financial_facts"]
    amounts = _named_values(facts)
    assert amounts[("dispatch_month", 1)] == pytest.approx(2976)
    assert amounts[("purchase_month", 1)] == pytest.approx(2976)
    assert amounts[("cost_month", 1)] == pytest.approx(2976)
    assert amounts[("peak_month", 1)] == pytest.approx(132)
    assert amounts[("all_months", 0)] == pytest.approx(106704)
    assert float(facts["cost_items"][0]["raw"]) == pytest.approx(35040)
    # Amount definitions explain existing costs; they must not book them again.
    assert [float(value) for value in facts["annual_net_flows"]] == pytest.approx([0, -71664])


@pytest.mark.parametrize("expression, accepted", [
    ("meter[t] = signal[t]", True),
    ("meter[t] = -signal[t]", False),
    (None, False),
])
def test_system_positive_measurement_uses_proved_bounds_and_precise_source(expression, accepted):
    from test_p3_network_boundaries import BOUNDARIES, _case
    from test_p3_system_equations import _attach, _block

    from iesplan.computation.generators.linear import LoweringUnsupportedError
    from iesplan.computation.generators.project import _new_builder
    from iesplan.finance import EffectiveFinanceConfig

    inputs, doc, documents = _case(
        {"unit": BOUNDARIES}, {}, {"unit": {"signal": {"mode": "constant", "value": 1}}},
    )
    finance = inputs[3].to_dict()
    finance["amounts"] = {"measurement": {
        "axis": "financial_year", "unit": "CNY", "constant": _money("0"),
        "terms": {"usage": {
            "coefficient": _money("1", "CNY"),
            "source": {"kind": "system_quantity", "block": "account", "variable": "meter",
                       "aggregation": "sum_positive", "period": {"kind": "current"}},
        }},
    }}
    effective = EffectiveFinanceConfig.from_dict(finance)
    inputs = (*inputs[:3], effective, *inputs[4:])
    doc["planning"]["constraints"] = {"budget": {
        "type": "amount_relation", "enabled": True,
        "references": {"spend": {"kind": "amount", "id": "measurement",
                                   "period": {"kind": "financial_year", "year": 1}}},
        "expression": "spend <= 10000 CNY",
    }}
    block = _block()
    block["references"] = {"signal": {"kind": "interface", "instance": "unit", "interface": "signal"}}
    block["variables"] = {"meter": {"unit": "1", "valid_range": {"minimum": -5, "maximum": 5}}}
    block["relations"] = [] if expression is None else [{"id": "actual", "expression": expression}]
    checked, new_inputs = _attach(inputs, doc, documents, block, outputs=[])
    assert checked.ok, [(d.location, d.params) for d in checked.diagnostics]
    builder = _new_builder(new_inputs[0], new_inputs[1], new_inputs[2], effective, new_inputs[5])
    if accepted:
        builder.build()
        assert builder.analysis.variable_bounds("system:account__meter__t0") == (1, 1)
    else:
        with pytest.raises(LoweringUnsupportedError, match="无法证明非负") as caught:
            builder.build()
        source = caught.value.model_source
        assert source["location"] == {
            "field_path": "finance.amounts.measurement.terms.usage.source",
            "block_id": "account", "variable_id": "meter",
        }
        assert source["axis"] == "step" and source["index"] == 0

