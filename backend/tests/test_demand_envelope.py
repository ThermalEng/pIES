"""Exact demand epigraphs: public synthetic inputs, actual HiGHS, counterexamples."""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

import pytest
from bundle_testkit import materialized_file
from test_issue04_d2 import POWER_KW, _inputs, _solved
from test_p3_f1_contract import _amount, _money

from iesplan.computation import HIGHS_MILP_PROVIDER
from iesplan.computation.generators import project
from iesplan.computation.generators.demand import monotone_demand_meters


def _npv(doc, _profile):
    doc["planning"]["objective"] = {"sense": "maximize", "expression": "system.after_tax_npv"}


def _surcharge(doc, profile, *, rate="0.1", yearly=False):
    _npv(doc, profile)
    profile["tax_rules"]["surcharge"] = {
        "kind": "derived_tax", "label_ref": "demand_label",
        "axis": "financial_year" if yearly else "tax_month",
        "base_from": {
            "kind": "tax", "source": {"kind": "pool", "id": "pool"}, "measure": "paid",
            "period": {"kind": "year_months", "year": "current"} if yearly else {"kind": "current"},
        },
        "rate": _money(rate, "1"), "payment_lag": 0,
    }
    if "income_demand" in profile["tax_rules"]:
        profile["tax_rules"]["income_demand"]["derived_tax_cost"] = ["surcharge"]


def _shape(bundle):
    raw = materialized_file(bundle, "input/problem.mps")
    return {
        kind: set(re.findall(rb"grid1__supply__demand_peak_m\d+__" + kind + rb"\d+", raw))
        for kind in (b"bit", b"select_", b"floor_")
    }


def _certificate(inputs):
    artifact, models, materialized, finance, _config, evaluation = inputs
    builder = project._new_builder(artifact, models, materialized, finance, evaluation)
    builder._declare_global_symbols()
    builder._collect_semantic_relations(builder._compute_buses(), builder._bind_technical_scopes())
    return monotone_demand_meters(builder.doc, finance, builder.analysis, builder._financial_graph)


def _assert_same_facts(actual, expected):
    if isinstance(actual, Mapping):
        assert actual.keys() == expected.keys()
        for key in actual:
            _assert_same_facts(actual[key], expected[key])
    elif isinstance(actual, Sequence) and not isinstance(actual, str):
        assert len(actual) == len(expected)
        for a, b in zip(actual, expected, strict=True):
            _assert_same_facts(a, b)
    elif isinstance(actual, str):
        try:
            value = float(actual)
        except ValueError:
            assert actual == expected
        else:
            assert value == pytest.approx(float(expected), rel=1e-9, abs=1e-5)
    else:
        assert actual == expected


@pytest.mark.parametrize("basis,deductible,yearly,resolution", [
    ("tax_inclusive", "1", False, "1h"),
    ("tax_exclusive", "0.5", True, "30min"),
])
def test_taxed_npv_epigraph_matches_exact_maximum(
    tmp_path, monkeypatch, basis, deductible, yearly, resolution,
):
    inputs = _inputs(
        start_year=2023, vat=True, income=True, sales=True, dispatch=True,
        basis=basis, deductible=deductible, resolution=resolution,
        configure=lambda doc, profile: _surcharge(doc, profile, yearly=yearly),
    )
    facts, sealed, bundle = _solved(inputs, tmp_path / "envelope")
    shape = _shape(bundle)
    assert not shape[b"bit"] and not shape[b"select_"]
    steps = 8760 if resolution == "1h" else 17520
    assert len(shape[b"floor_"]) == steps
    monkeypatch.setattr(project, "monotone_demand_meters", lambda *_: frozenset())
    exact, exact_sealed, exact_bundle = _solved(inputs, tmp_path / "exact")
    exact_shape = _shape(exact_bundle)
    assert len(exact_shape[b"bit"]) == (120 if resolution == "1h" else 132)
    assert len(exact_shape[b"select_"]) == steps
    # Comparison covers the same-solve peaks, charges, VAT/surcharges, income
    # and cash flows, not just the objective value or variable count.
    for result in (facts, exact):
        assert all(float(row["peak_power"]) == pytest.approx(POWER_KW, abs=1e-5)
                   for row in result["monthly_demand"])
    for field in ("monthly_demand", "annual_operation", "tax_cash", "annual_net_flows"):
        _assert_same_facts(facts[field], exact[field])
    _assert_same_facts(sealed["evaluation"], exact_sealed["evaluation"])


def test_discounted_vat_and_income_loss_carry_match_exact_maximum(tmp_path, monkeypatch):
    def configure(doc, profile):
        _surcharge(doc, profile)
        profile["credit_pools"]["pool"]["opening_credit"] = _money("10000")
        income = profile["tax_rules"]["income_demand"]
        income["loss_carryforward_years"] = 3
        income["opening_losses"] = [{"amount": _money("80000"), "last_usable_year": 3}]

    inputs = _inputs(start_year=2023, vat=True, income=True, sales=True, dispatch=True,
                     operation_years=2, discount_rate=0.07, configure=configure)
    facts, sealed, bundle = _solved(inputs, tmp_path / "envelope")
    assert not _shape(bundle)[b"bit"]
    monkeypatch.setattr(project, "monotone_demand_meters", lambda *_: frozenset())
    exact, exact_sealed, _ = _solved(inputs, tmp_path / "exact")
    for field in ("monthly_demand", "annual_operation", "tax_cash", "annual_net_flows"):
        _assert_same_facts(facts[field], exact[field])
    _assert_same_facts(sealed["evaluation"], exact_sealed["evaluation"])
    assert any(float(row["carry"]) > 0 for row in facts["tax_cash"]["vat"])
    assert any(float(row["loss_used"]) > 0 for row in facts["tax_cash"]["income"])


def test_epigraph_changes_dispatch_identically_to_exact_model(tmp_path, monkeypatch):
    inputs = _inputs(start_year=2023, rate="1000", dispatch=True)
    facts, sealed, bundle = _solved(inputs, tmp_path / "envelope")
    assert not _shape(bundle)[b"bit"]
    monkeypatch.setattr(project, "monotone_demand_meters", lambda *_: frozenset())
    exact, exact_sealed, _ = _solved(inputs, tmp_path / "exact")
    assert all(float(row["peak_power"]) == pytest.approx(0, abs=1e-5)
               for row in facts["monthly_demand"])
    _assert_same_facts(facts["annual_operation"], exact["annual_operation"])
    _assert_same_facts(sealed["evaluation"], exact_sealed["evaluation"])


@pytest.mark.parametrize("rate,configure", [("0", _npv), ("12", None)])
def test_zero_weight_and_non_economic_objectives_keep_exact_encoding(rate, configure):
    inputs = _inputs(rate=rate, configure=configure)
    assert not _certificate(inputs)
    assert _shape(HIGHS_MILP_PROVIDER.generate(*inputs))[b"bit"]


def test_shared_peak_considers_all_bindings_and_positive_total_cost():
    assert _certificate(_inputs(rate="0", second_rate="4", configure=_npv)) == {("grid1", "supply")}

    def constrained(doc, profile):
        _npv(doc, profile)
        doc["planning"]["constraints"] = {
            "fee_floor": {"type": "annual_purchase_cost_limit", "enabled": True,
                          "expression": "second__period_demand_charge >= 1000 CNY"},
        }

    assert not _certificate(_inputs(second_rate="4", configure=constrained))


def _refund(doc, profile):
    _npv(doc, profile)
    profile["credit_pools"]["pool"]["refunds"] = [
        {"period": 13, "arrival_period": 13, "amount": _money("10")},
    ]


def test_actual_refund_claim_prevents_epigraph_even_with_positive_cost():
    assert not _certificate(_inputs(vat=True, configure=_refund))


def _demand_source():
    return {"kind": "demand", "charge": "charge_a", "bindings": ["first"],
            "basis": "raw", "period": {"kind": "tax_month", "month": 13}}


@pytest.mark.parametrize("source", [
    _demand_source(),
    {"kind": "tax", "source": {"kind": "pool", "id": "pool"},
     "measure": "paid", "period": {"kind": "tax_month", "month": 13}},
    {"kind": "tax", "source": {"kind": "rule", "id": "income_demand"},
     "measure": "assessed", "period": {"kind": "financial_year", "year": 2}},
])
def test_consumed_amount_constraints_keep_exact_demand_and_tax_facts(source):
    def configure(doc, profile):
        _npv(doc, profile)
        profile["amounts"] = {"fee": _amount(source), "copy": _amount({
            "kind": "amount", "id": "fee", "period": {"kind": "scalar"},
        })}
        doc["planning"]["constraints"] = {"floor": {
            "type": "amount_relation", "enabled": True, "expression": "bill >= 1 CNY",
            "references": {"bill": {"kind": "amount", "id": "copy", "period": {"kind": "scalar"}}},
        }}

    assert not _certificate(_inputs(vat=True, income=True, sales=True, configure=configure))


def test_unused_amount_does_not_disable_equivalent_lowering():
    def configure(doc, profile):
        _npv(doc, profile)
        profile["amounts"] = {"unused": _amount(_demand_source())}

    assert _certificate(_inputs(configure=configure)) == {("grid1", "supply")}


def test_unrelated_consumed_amount_does_not_disable_equivalent_lowering():
    def configure(doc, profile):
        _npv(doc, profile)
        profile["amounts"] = {"independent": _amount()}
        doc["planning"]["constraints"] = {"floor": {
            "type": "amount_relation", "enabled": True, "expression": "bill >= 0 CNY",
            "references": {"bill": {"kind": "amount", "id": "independent", "period": {"kind": "scalar"}}},
        }}

    assert _certificate(_inputs(configure=configure)) == {("grid1", "supply")}


def test_extra_cash_movement_based_on_demand_is_not_a_cost_only_use():
    def configure(doc, profile):
        _npv(doc, profile)
        profile["amounts"] = {"fee": _amount(_demand_source())}
        profile["cash_movements"] = {"demand_revenue": {
            "amount": {"kind": "amount", "id": "fee", "period": {"kind": "scalar"}},
            "price_basis": "tax_inclusive", "direction": "inflow",
            "recognition": {"kind": "at", "at": {"kind": "tax_month", "month": 13}},
            "payments": [{"offset": 0, "ratio": "1"}],
        }}

    assert not _certificate(_inputs(configure=configure))


def test_full_income_tax_shield_has_no_strict_cost_and_keeps_exact_peak():
    def configure(doc, profile):
        _npv(doc, profile)
        profile["tax_rules"]["income_demand"]["rate"] = _money("1", "1")

    assert not _certificate(_inputs(vat=True, income=True, configure=configure))


def test_surcharge_on_income_tax_is_not_a_vat_savings_certificate():
    def configure(doc, profile):
        _surcharge(doc, profile, yearly=True)
        profile["tax_rules"]["income_demand"]["derived_tax_cost"] = []
        profile["tax_rules"]["surcharge"]["base_from"] = {
            "kind": "tax", "source": {"kind": "rule", "id": "income_demand"},
            "measure": "assessed", "period": {"kind": "current"},
        }

    assert not _certificate(_inputs(vat=True, income=True, configure=configure))
