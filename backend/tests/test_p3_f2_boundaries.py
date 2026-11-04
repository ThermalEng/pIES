"""F2 actual-period, spending-identity and capability boundary evidence."""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_config_revisions import PROFILE_PAYLOAD

from iesplan.application.tasks.finance_relations import check_financial_relation_inputs
from iesplan.application.tasks.submissions import InvalidRequestError, _check_amount_capabilities
from iesplan.assembly import consumed_amounts, financial_dependency_graph
from iesplan.assembly.amounts import check_amount_sources
from iesplan.finance import FinanceProfile, FinanceTripletError, merge_effective


def _amount(value="100", *, axis="scalar", source=None):
    return {
        "unit": "CNY",
        "axis": axis,
        "constant": {"value": value, "unit": "CNY"},
        "terms": {}
        if source is None
        else {
            "source": {"coefficient": {"value": "1", "unit": "1"}, "source": source},
        },
    }


def _movement(*, year=0, offset=0):
    return {
        "amount": {"kind": "amount", "id": "quote", "period": {"kind": "scalar"}},
        "price_basis": "tax_exclusive",
        "direction": "outflow",
        "recognition": {"kind": "at", "at": {"kind": "financial_year", "year": year}},
        "payments": [{"offset": offset, "ratio": "1"}],
    }


def _asset(source=None, *, capitalized=1, available=1):
    return {
        "basis_from": source
        or {
            "kind": "movement",
            "id": "purchase",
            "basis": "net_basis",
            "period": {"kind": "financial_year", "year": 0},
        },
        "capitalized_at": {"kind": "financial_year", "year": capitalized},
        "available_from": available,
    }


def _income(*, assets=(), movement_cost=()):
    return {
        "kind": "income_tax",
        "label_ref": "income_tag",
        "rate": {"value": "0.25", "unit": "1"},
        "revenue": [],
        "energy_cost": [],
        "annual_om": [],
        "variable_om": [],
        "deductible_maintenance": {"mode": "explicit_items", "items": []},
        "depreciation": [
            {
                "base_from": {"kind": "asset", "id": identity},
                "method": "straight_line",
                "life_years": 5,
                "residual": {"value": "0", "unit": "1"},
                "first_year": 1,
            }
            for identity in assets
        ],
        "loss_carryforward_years": 5,
        "opening_losses": [],
        "movement_cost": list(movement_cost),
    }


def _profile():
    value = deepcopy(PROFILE_PAYLOAD)
    value["profile"]["id"] = "f2-periods"
    value["amounts"] = {"quote": _amount()}
    value["cash_movements"] = {"purchase": _movement()}
    value["assets"] = {}
    value["taxes"] = {
        name: {"display_name": name, "tax_type": name, "applies_to": "pv_system.upfront_capex"}
        for name in ("vat_tag", "income_tag", "derived_tag")
    }
    return value


def _context(payload=None):
    finance = merge_effective(FinanceProfile.from_dict(payload or _profile()), None)
    doc = {
        "devices": {"pv1": {"asset_origin": "new"}, "pv2": {"asset_origin": "new"}},
        "planning": {"constraints": {}},
        "finance_binding": {
            "instances": {
                identity: {"type": "costed", "finance_type": "pv_system"} for identity in ("pv1", "pv2")
            }
        },
        "tariff_bindings": {},
        "demand_bindings": {},
    }
    artifact = SimpleNamespace(canonical_text=json.dumps(doc))
    evaluation = SimpleNamespace(
        evaluation_years=4, construction_period=1, operation_years=3, maintenance_expenses=[]
    )
    return doc, artifact, finance, evaluation


def test_future_contractual_payment_is_preserved_and_only_occurrence_is_bounded():
    payload = _profile()
    payload["cash_movements"]["purchase"] = _movement(year=2, offset=10)
    doc, artifact, finance, evaluation = _context(payload)
    check_financial_relation_inputs(evaluation, artifact, finance)
    assert finance.cash_movements["purchase"].payments[0].offset == 10
    assert consumed_amounts(doc, finance) == ("quote",)
    payload["cash_movements"]["purchase"]["recognition"]["at"]["year"] = 5
    _, artifact, finance, evaluation = _context(payload)
    with pytest.raises(InvalidRequestError) as error:
        check_financial_relation_inputs(evaluation, artifact, finance)
    assert error.value.params["reason"] == "financial_occurrence_outside_evaluation"
    assert error.value.location["field"].endswith("purchase.recognition")


def test_payment_before_axis_start_rejected_not_clamped():
    payload = _profile()
    payload["cash_movements"]["purchase"] = _movement(year=0, offset=-1)
    _, artifact, finance, evaluation = _context(payload)
    with pytest.raises(InvalidRequestError) as error:
        check_financial_relation_inputs(evaluation, artifact, finance)
    assert error.value.params["payment_period"] == -1
    assert error.value.location["field"].endswith("payments[0].offset")


def _vat(confirmation):
    return {
        "kind": "vat",
        "label_ref": "vat_tag",
        "settlement": "monthly",
        "in_tax": [],
        "out_tax": [],
        "om_input_tax": [],
        "maintenance_input_tax": [],
        "credit_pool": "pool",
        "confirmation": "same_period",
        "movement_input_tax": [
            {
                "movement": "purchase",
                "rate": {"value": "0.1", "unit": "1"},
                "deductible_ratio": {"value": "1", "unit": "1"},
                "confirmation": confirmation,
            }
        ],
    }


def _with_pool(payload):
    payload["credit_pools"] = {
        "pool": {
            "opening_credit": {"value": "0", "unit": "CNY"},
            "receivable_policy": "end_receivable",
            "refunds": [],
        }
    }


def test_each_occurrence_has_its_own_invoice_schedule_and_reordering_is_irrelevant():
    payload = _profile()
    payload["cash_movements"]["purchase"]["recognition"] = {
        "kind": "each",
        "axis": "financial_year",
        "first": 1,
        "last": 2,
    }
    _with_pool(payload)
    schedules = [
        {"at": {"kind": "financial_year", "year": year}, "schedule": [{"period": month, "ratio": "1"}]}
        for year, month in ((2, 25), (1, 13))
    ]
    payload["tax_rules"] = {"vat": _vat({"method": "explicit_schedule", "occurrences": schedules})}
    doc, artifact, finance, evaluation = _context(payload)
    check_financial_relation_inputs(evaluation, artifact, finance)
    graph = financial_dependency_graph(doc, finance, evaluation)
    nodes = {node.key: node for node in graph.nodes}
    assert ("movement", "purchase", "financial_year", 1, "raw") in nodes[
        ("tax_rule", "vat", "tax_month", 13, "deductible_input")
    ].dependencies
    assert ("movement", "purchase", "financial_year", 2, "raw") in nodes[
        ("tax_rule", "vat", "tax_month", 25, "deductible_input")
    ].dependencies
    assert not nodes[("tax_rule", "vat", "tax_month", 12, "deductible_input")].dependencies
    schedules.pop()
    _, artifact, finance, evaluation = _context(payload)
    with pytest.raises(InvalidRequestError) as error:
        check_financial_relation_inputs(evaluation, artifact, finance)
    assert error.value.params["reason"] == "movement_vat_occurrence_coverage_mismatch"
    assert error.value.params["missing"] == [("financial_year", 1)]


def test_asset_capitalization_availability_and_depreciation_are_separate():
    payload = _profile()
    payload["assets"] = {"unit": _asset()}
    payload["tax_rules"] = {"income": _income(assets=("unit",))}
    _, artifact, finance, evaluation = _context(payload)
    check_financial_relation_inputs(evaluation, artifact, finance)
    payload["assets"]["unit"]["capitalized_at"]["year"] = 2
    _, artifact, finance, evaluation = _context(payload)
    with pytest.raises(InvalidRequestError) as error:
        check_financial_relation_inputs(evaluation, artifact, finance)
    assert error.value.params["reason"] == "asset_available_before_capitalization"
    payload["assets"]["unit"] = _asset(capitalized=8, available=9)
    payload["tax_rules"]["income"]["depreciation"][0]["first_year"] = 9
    _, artifact, finance, evaluation = _context(payload)
    check_financial_relation_inputs(evaluation, artifact, finance)  # future formation is not forced into E


def test_shared_source_fractions_are_exact_and_full_deduction_conflicts():
    payload = _profile()
    source = _asset()["basis_from"]
    for identity, share in (("basis_a", "0.4"), ("basis_b", "0.6")):
        payload["amounts"][identity] = _amount("0", source=deepcopy(source))
        payload["amounts"][identity]["terms"]["source"]["coefficient"]["value"] = share
    payload["assets"] = {
        identity: _asset({"kind": "amount", "id": basis, "period": {"kind": "scalar"}})
        for identity, basis in (("a", "basis_a"), ("b", "basis_b"))
    }
    _, artifact, finance, evaluation = _context(payload)
    check_financial_relation_inputs(evaluation, artifact, finance)
    payload["amounts"]["basis_a"]["terms"]["source"]["coefficient"]["value"] = "0.5"
    _, artifact, finance, evaluation = _context(payload)
    with pytest.raises(InvalidRequestError) as error:
        check_financial_relation_inputs(evaluation, artifact, finance)
    assert error.value.params["reason"] == "capitalized_source_share_exceeds_one"
    assert error.value.params["share"] == "1.1"
    payload["amounts"]["basis_a"]["terms"]["source"]["coefficient"]["value"] = "0.4"
    payload["tax_rules"] = {"income": _income(movement_cost=("purchase",))}
    # The file already contains both complete identities; reject once there,
    # not again at submission or frozen-input reads.
    with pytest.raises(FinanceTripletError, match="已资本化发生项"):
        _context(payload)


def test_identical_amount_recipe_is_not_the_identity_of_an_occurrence():
    payload = _profile()
    payload["cash_movements"]["another"] = deepcopy(payload["cash_movements"]["purchase"])
    payload["assets"] = {"unit": _asset()}
    payload["tax_rules"] = {"income": _income(movement_cost=("another",))}
    _, artifact, finance, evaluation = _context(payload)
    check_financial_relation_inputs(evaluation, artifact, finance)


def test_same_period_pool_amount_movement_cycle_reports_precise_periods():
    payload = _profile()
    _with_pool(payload)
    payload["amounts"]["quote"] = _amount(
        "0",
        axis="tax_month",
        source={
            "kind": "tax",
            "source": {"kind": "pool", "id": "pool"},
            "measure": "paid",
            "period": {"kind": "current"},
        },
    )
    movement = payload["cash_movements"]["purchase"]
    movement["amount"]["period"] = {"kind": "current"}
    movement["direction"] = "inflow"
    movement["recognition"] = {"kind": "each", "axis": "tax_month", "first": 1, "last": 48}
    vat = _vat({"method": "recognition_period"})
    entry = vat.pop("movement_input_tax")[0]
    entry.pop("deductible_ratio")
    vat["movement_output_tax"] = [entry]
    payload["tax_rules"] = {"vat": vat}
    _, artifact, finance, evaluation = _context(payload)
    with pytest.raises(InvalidRequestError) as error:
        check_financial_relation_inputs(evaluation, artifact, finance)
    assert error.value.params["reason"] == "financial_period_dependency_cycle"
    assert {key[3] for key in error.value.params["cycle"]} == {1}
    assert any("amounts.quote" in field for field in error.value.params["fields"])


def test_cross_period_self_paid_reference_is_not_a_static_entity_cycle():
    payload = _profile()
    payload["tax_rules"] = {
        "lagged": {
            "kind": "derived_tax",
            "label_ref": "derived_tag",
            "axis": "financial_year",
            "base_from": {
                "kind": "tax",
                "source": {"kind": "rule", "id": "lagged"},
                "measure": "paid",
                "period": {"kind": "current"},
            },
            "rate": {"value": "0.1", "unit": "1"},
            "payment_lag": 1,
        }
    }
    doc, artifact, finance, evaluation = _context(payload)
    check_financial_relation_inputs(evaluation, artifact, finance)
    graph = financial_dependency_graph(doc, finance, evaluation)
    assert not graph.unresolved
    earlier = ("tax_rule", "lagged", "financial_year", 1, "assessed")
    later = ("tax_rule", "lagged", "financial_year", 2, "assessed")
    assert graph.order.index(earlier) < graph.order.index(later)
    payload["tax_rules"]["lagged"]["payment_lag"] = 0
    _, artifact, finance, evaluation = _context(payload)
    with pytest.raises(InvalidRequestError) as error:
        check_financial_relation_inputs(evaluation, artifact, finance)
    assert error.value.params["reason"] == "financial_period_dependency_cycle"


def test_amount_graph_expands_only_real_consumption_periods_and_is_deterministic():
    payload = _profile()
    payload["cash_movements"]["purchase"]["recognition"] = {
        "kind": "each",
        "axis": "financial_year",
        "first": 1,
        "last": 2,
    }
    payload["amounts"]["observed"] = _amount(
        "0",
        axis="financial_year",
        source={
            "kind": "movement",
            "id": "purchase",
            "basis": "gross",
            "period": {"kind": "current"},
        },
    )
    # This definition is intentionally not a root. Its missing occurrence is
    # legal until it is actually consumed.
    payload["amounts"]["idle"] = _amount(
        "0",
        source={
            "kind": "movement",
            "id": "purchase",
            "basis": "gross",
            "period": {"kind": "financial_year", "year": 4},
        },
    )
    payload["cash_movements"]["resale"] = deepcopy(payload["cash_movements"]["purchase"])
    payload["cash_movements"]["resale"]["direction"] = "inflow"
    payload["cash_movements"]["resale"]["amount"] = {
        "kind": "amount",
        "id": "observed",
        "period": {"kind": "current"},
    }
    doc, artifact, finance, evaluation = _context(payload)
    check_financial_relation_inputs(evaluation, artifact, finance)
    graph = financial_dependency_graph(doc, finance, evaluation)
    assert {key[3] for key in graph.order if key[:2] == ("amount", "observed")} == {1, 2}
    assert not any(key[:2] == ("amount", "idle") for key in graph.order)
    payload["amounts"] = dict(reversed(list(payload["amounts"].items())))
    payload["cash_movements"] = dict(reversed(list(payload["cash_movements"].items())))
    doc, _, finance, evaluation = _context(payload)
    assert financial_dependency_graph(doc, finance, evaluation) == graph


def _cost_source(instance, path="pv_system.upfront_capex.linear.capacity_kw"):
    return {
        "kind": "cost",
        "path": path,
        "basis": "net_basis",
        "scope": {"kind": "instances", "instances": [instance]},
        "period": {"kind": "scalar"},
    }


def test_asset_ownership_resolves_project_instances_and_actual_cost_leaves():
    payload = _profile()
    payload["assets"] = {
        "a": _asset(_cost_source("pv1")),
        "b": _asset(_cost_source("pv2")),
        "fixed": _asset(_cost_source("pv1", "pv_system.upfront_capex.fixed")),
    }
    _, artifact, finance, evaluation = _context(payload)
    check_financial_relation_inputs(evaluation, artifact, finance)
    payload["assets"]["duplicate"] = _asset(_cost_source("pv1"))
    _, artifact, finance, evaluation = _context(payload)
    with pytest.raises(InvalidRequestError) as error:
        check_financial_relation_inputs(evaluation, artifact, finance)
    assert error.value.params["source"] == ("cost", "pv1", "pv_system.upfront_capex.linear.capacity_kw")
    assert error.value.params["share"] == "2"


def test_direct_cost_depreciation_and_named_asset_cannot_reclaim_same_leaf():
    payload = _profile()
    payload["assets"] = {"a": _asset(_cost_source("pv1"))}
    income = _income(assets=("a",))
    entry = deepcopy(income["depreciation"][0])
    entry["base_from"] = {
        "kind": "upfront_capex_net",
        "finance_type": "pv_system",
        "scope": {"kind": "all_new_instances"},
    }
    income["depreciation"].append(entry)
    payload["tax_rules"] = {"income": income}
    _, artifact, finance, evaluation = _context(payload)
    with pytest.raises(InvalidRequestError) as error:
        check_financial_relation_inputs(evaluation, artifact, finance)
    assert error.value.params["reason"] == "capitalized_source_share_exceeds_one"
    assert error.value.params["source"] == ("cost", "pv1", "pv_system.upfront_capex.linear.capacity_kw")


def test_direct_asset_scope_is_checked_at_assembly_not_repeated_by_worker():
    payload = _profile()
    payload["assets"] = {"a": _asset(_cost_source("unbound"))}
    doc, _, finance, _ = _context(payload)
    diagnostics = []
    check_amount_sources(doc, finance, {}, diagnostics)
    assert diagnostics[0].params["reason"] == "asset_scope_wrong_new_instance"
    assert diagnostics[0].location["field"] == "effective_finance.assets.a.basis_from.scope"


def test_named_maintenance_asset_respects_original_expense_identity():
    payload = _profile()
    payload["assets"] = {
        "a": _asset(
            {
                "kind": "maintenance",
                "year": 2,
                "position": 1,
                "basis": "net_basis",
            },
            capitalized=2,
            available=2,
        )
    }
    income = _income()
    income["deductible_maintenance"]["items"] = [{"year": 2, "position": 1, "deductible": True}]
    payload["tax_rules"] = {"income": income}
    _, artifact, finance, evaluation = _context(payload)
    evaluation.maintenance_expenses = [SimpleNamespace(year=2)]
    with pytest.raises(InvalidRequestError) as error:
        check_financial_relation_inputs(evaluation, artifact, finance)
    assert error.value.params["reason"] == "capitalized_maintenance_also_expensed"
    assert error.value.params["source"] == ("maintenance", 2, 1)
    evaluation.maintenance_expenses = []
    with pytest.raises(InvalidRequestError) as error:
        check_financial_relation_inputs(evaluation, artifact, finance)
    assert error.value.params["reason"] == "capitalized_maintenance_source_missing"


def test_reference_must_select_an_actual_occurrence_not_a_payment_date():
    payload = _profile()
    payload["cash_movements"]["purchase"] = _movement(year=1, offset=1)
    payload["assets"] = {
        "a": _asset(
            {
                "kind": "movement",
                "id": "purchase",
                "basis": "net_basis",
                "period": {"kind": "financial_year", "year": 2},
            },
            capitalized=2,
            available=2,
        )
    }
    _, artifact, finance, evaluation = _context(payload)
    with pytest.raises(InvalidRequestError) as error:
        check_financial_relation_inputs(evaluation, artifact, finance)
    assert error.value.params["reason"] == "movement_reference_occurrence_missing"
    assert error.value.params["periods"] == [("financial_year", 2)]


def test_repeated_movement_asset_shares_are_per_occurrence_not_definition_id():
    payload = _profile()
    payload["cash_movements"]["purchase"]["recognition"] = {
        "kind": "each",
        "axis": "financial_year",
        "first": 1,
        "last": 2,
    }
    payload["assets"] = {
        f"a{year}": _asset(
            {
                "kind": "movement",
                "id": "purchase",
                "basis": "net_basis",
                "period": {"kind": "financial_year", "year": year},
            },
            capitalized=year,
            available=year,
        )
        for year in (1, 2)
    }
    _, artifact, finance, evaluation = _context(payload)
    check_financial_relation_inputs(evaluation, artifact, finance)


def test_asset_depreciation_is_zero_without_a_declared_tax_depreciation_relation():
    payload = _profile()
    payload["assets"] = {"a": _asset()}
    payload["amounts"]["quote"] = _amount(
        source={
            "kind": "asset",
            "id": "a",
            "measure": "depreciation",
            "period": {"kind": "financial_year", "year": 2},
        }
    )
    doc, artifact, finance, evaluation = _context(payload)
    check_financial_relation_inputs(evaluation, artifact, finance)
    graph = financial_dependency_graph(doc, finance, evaluation)
    nodes = {node.key: node for node in graph.nodes}
    assert not nodes[("asset", "a", "financial_year", 2, "depreciation")].dependencies
    assert nodes[("asset", "a", "financial_year", 2, "book_value")].dependencies == (
        ("asset", "a", "scalar", 0, "basis"),
    )


def test_year_zero_vat_requires_explicit_schedule_and_invoice_stays_in_evaluation():
    payload = _profile()
    _with_pool(payload)
    payload["tax_rules"] = {"vat": _vat({"method": "recognition_period"})}
    _, artifact, finance, evaluation = _context(payload)
    with pytest.raises(InvalidRequestError) as error:
        check_financial_relation_inputs(evaluation, artifact, finance)
    assert error.value.params["reason"] == "year_zero_movement_requires_explicit_vat_schedule"
    payload["tax_rules"]["vat"]["movement_input_tax"][0]["confirmation"] = {
        "method": "explicit_schedule",
        "occurrences": [
            {
                "at": {"kind": "financial_year", "year": 0},
                "schedule": [{"period": 49, "ratio": "1"}],
            }
        ],
    }
    _, artifact, finance, evaluation = _context(payload)
    with pytest.raises(InvalidRequestError) as error:
        check_financial_relation_inputs(evaluation, artifact, finance)
    assert error.value.params["reason"] == "movement_vat_confirmation_outside_evaluation"
    assert error.value.location["field"].endswith("occurrences[0].schedule[0].period")


def test_direct_asset_amount_selector_cannot_hide_an_out_of_range_period():
    payload = _profile()
    payload["amounts"]["basis"] = _amount("0", axis="financial_year", source=_cost_source("pv1"))
    payload["assets"] = {
        "a": _asset(
            {
                "kind": "amount",
                "id": "basis",
                "period": {"kind": "financial_year", "year": 5},
            }
        )
    }
    _, artifact, finance, evaluation = _context(payload)
    with pytest.raises(InvalidRequestError) as error:
        check_financial_relation_inputs(evaluation, artifact, finance)
    assert error.value.params["reason"] == "financial_reference_outside_evaluation"
    assert error.value.params["limit"] == 4
    assert error.value.location["field"] == "effective_finance.assets.a.basis_from.period"


def test_f2_execution_requires_declared_source_and_same_solution_fact_capabilities():
    _, artifact, finance, _ = _context()
    with pytest.raises(InvalidRequestError) as error:
        _check_amount_capabilities(artifact, finance, "custom.generator", ())
    assert error.value.code == "TASK-REQ-007"
    assert set(error.value.params["missing_capabilities"]) == {
        "finance.amount_references",
        "finance.cash_movements",
        "finance.financial_facts",
    }
    _check_amount_capabilities(
        artifact,
        finance,
        "custom.generator",
        (
            "finance.amount_references",
            "finance.cash_movements",
            "finance.financial_facts",
        ),
    )
