"""Exact demand epigraphs: public synthetic inputs, actual HiGHS, counterexamples."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from types import SimpleNamespace

import pytest
from bundle_testkit import materialized_file
from test_highs_result_adapter import SOLUTION_PATH, _receipt
from test_issue04_d2 import POWER_KW, _inputs
from test_p3_f1_contract import _amount, _money
from test_vat_monthly import _solve_internal

from iesplan.application.worker.compute_cases import build_financial_evaluation
from iesplan.computation import HIGHS_MILP_PROVIDER
from iesplan.computation.generators import project
from iesplan.computation.generators.demand import monotone_demand_meters
from iesplan.computation.generators.parameters import RECORDS_PATH
from iesplan.computation.result_adapters.highs import ADAPTER_REF, HIGHS_ADAPTER
from iesplan.core.content import text_lines


def _solved(inputs, tmp_path):
    artifact, models, mat, finance, config, evaluation = inputs
    bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, mat, finance, config, evaluation)
    _, _, raw = _solve_internal(bundle, tmp_path)
    adapted = HIGHS_ADAPTER.adapt(
        bundle, _receipt(result_adapter_ref=ADAPTER_REF), {SOLUTION_PATH: raw},
    )
    sealed = build_financial_evaluation(
        SimpleNamespace(evaluation={
            "evaluation_years": evaluation.evaluation_years,
            "construction_period": evaluation.construction_period,
            "operation_years": evaluation.operation_years,
            "discount_rate": evaluation.discount_rate,
        }),
        finance, adapted,
    )
    assert sealed is not None
    return adapted.outputs["financial_facts"], sealed, bundle


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


@pytest.mark.parametrize("second_rate", [None, "0"])
def test_zero_rate_npv_real_solve_retains_real_peak_and_zero_bill(tmp_path, second_rate):
    inputs = _inputs(rate="0", second_rate=second_rate, dispatch=True, configure=_npv)
    assert not _certificate(inputs)
    facts, sealed, bundle = _solved(inputs, tmp_path)
    assert _shape(bundle)[b"bit"]
    rows = facts["monthly_demand"]
    assert len(rows) == (12 if second_rate is None else 24)
    for row in rows:
        assert float(row["peak_power"]) == pytest.approx(POWER_KW, abs=1e-5)
        assert float(row["charge"]) == pytest.approx(0.0, abs=1e-6)
    assert float(facts["annual_operation"][0]["demand_charge"]) == pytest.approx(0.0, abs=1e-6)
    assert sealed["monthly_demand"] == list(rows)


def test_shared_zero_and_positive_rate_real_solve_matches_exact(tmp_path, monkeypatch):
    inputs = _inputs(
        start_year=2023, rate="0", second_rate="4", vat=True, income=True,
        sales=True, dispatch=True, configure=_npv,
    )
    assert _certificate(inputs) == {("grid1", "supply")}
    facts, sealed, bundle = _solved(inputs, tmp_path / "envelope")
    shape = _shape(bundle)
    assert not shape[b"bit"] and not shape[b"select_"]
    monkeypatch.setattr(project, "monotone_demand_meters", lambda *_: frozenset())
    exact, exact_sealed, exact_bundle = _solved(inputs, tmp_path / "exact")
    assert _shape(exact_bundle)[b"bit"]
    first, second = facts["monthly_demand"][:12], facts["monthly_demand"][12:]
    assert all(
        float(row["peak_power"]) == pytest.approx(POWER_KW, abs=1e-5)
        and float(row["charge"]) == pytest.approx(0.0, abs=1e-6)
        for row in first
    )
    assert all(
        float(row["peak_power"]) == pytest.approx(POWER_KW, abs=1e-5)
        and float(row["charge"]) == pytest.approx(4.0 * POWER_KW, abs=1e-4)
        for row in second
    )
    for field in ("monthly_demand", "annual_operation", "tax_cash", "annual_net_flows"):
        _assert_same_facts(facts[field], exact[field])
    _assert_same_facts(sealed["evaluation"], exact_sealed["evaluation"])


def test_shared_zero_rate_constrained_keeps_exact_encoding():
    def configure(doc, _profile):
        _npv(doc, _profile)
        doc["planning"]["constraints"] = {
            "zero_floor": {
                "type": "annual_purchase_cost_limit", "enabled": True,
                "expression": "first__period_demand_charge >= 0 CNY",
            },
        }

    inputs = _inputs(rate="0", second_rate="4", configure=configure)
    assert not _certificate(inputs)
    assert _shape(HIGHS_MILP_PROVIDER.generate(*inputs))[b"bit"]


def test_negative_margin_counterexamples_keep_exact_peak():
    # g=1, c=1/11, k=11, h=0.25 => margin = 1 - 12/11 - 0.25*(10/11) = -3.5/11 < 0
    inputs = _inputs(
        vat=True, income=True, sales=True,
        configure=lambda doc, profile: _surcharge(doc, profile, rate="11"),
    )
    assert not _certificate(inputs)

    # Shared meter where charge_a has margin < 0 and charge_b has margin > 0 stays exact.
    shared = _inputs(
        rate="12", second_rate="12", vat=True, income=True, sales=True,
        configure=lambda doc, profile: _surcharge(doc, profile, rate="11"),
    )
    assert not _certificate(shared)
    assert _shape(HIGHS_MILP_PROVIDER.generate(*shared))[b"bit"]


def test_special_references_assets_cashflows_taxes_and_disabled_constraints():
    # 1. Consumed asset basis_from referencing demand via movement keeps exact encoding.
    def with_asset(doc, profile):
        _npv(doc, profile)
        profile["amounts"] = {"fee": _amount(_demand_source())}
        profile["cash_movements"] = {
            "demand_cap": {
                "amount": {"kind": "amount", "id": "fee", "period": {"kind": "scalar"}},
                "price_basis": "tax_inclusive", "direction": "outflow",
                "recognition": {"kind": "at", "at": {"kind": "financial_year", "year": 2}},
                "payments": [{"offset": 0, "ratio": "1"}],
            },
        }
        profile["assets"] = {
            "demand_asset": {
                "basis_from": {
                    "kind": "movement", "id": "demand_cap", "basis": "net_basis",
                    "period": {"kind": "financial_year", "year": 2},
                },
                "capitalized_at": {"kind": "financial_year", "year": 2},
                "available_from": 2,
            },
        }

    asset_inputs = _inputs(configure=with_asset)
    assert not _certificate(asset_inputs)
    assert _shape(HIGHS_MILP_PROVIDER.generate(*asset_inputs))[b"bit"]

    # 2. Consumed net_cashflows (amount_ref) referencing demand or affected tax keeps exact encoding.
    for index, source in enumerate((
        _demand_source(),
        {"kind": "tax", "source": {"kind": "pool", "id": "pool"},
         "measure": "paid", "period": {"kind": "tax_month", "month": 13}},
    )):
        def with_ncf(doc, profile, src=source):
            _npv(doc, profile)
            profile["amounts"] = {"fee": _amount(src)}
            profile["net_cashflows"] = {
                "refund": {
                    "at": {"kind": "evaluation_end"},
                    "direction": "inflow",
                    "amount": {"kind": "amount_ref", "id": "fee"},
                },
            }

        ncf_inputs = _inputs(vat=True, income=True, sales=True, configure=with_ncf)
        assert not _certificate(ncf_inputs)
        if index == 0:
            assert _shape(HIGHS_MILP_PROVIDER.generate(*ncf_inputs))[b"bit"]

    # 3. Disabled constraints (enabled: False) do not block epigraph certification.
    def with_disabled(doc, profile):
        _npv(doc, profile)
        profile["amounts"] = {"fee": _amount(_demand_source())}
        doc["planning"]["constraints"] = {
            "off_amount": {
                "type": "amount_relation", "enabled": False,
                "expression": "bill >= 100 CNY",
                "references": {"bill": {"kind": "amount", "id": "fee", "period": {"kind": "scalar"}}},
            },
            "off_scalar": {
                "type": "annual_purchase_cost_limit", "enabled": False,
                "expression": "first__period_demand_charge >= 100 CNY",
            },
        }

    assert _certificate(_inputs(configure=with_disabled)) == {("grid1", "supply")}

    # 4. Tax measures credit_balance / deductible_input / loss_used / derived.paid block epigraph.
    for source in (
        {"kind": "tax", "source": {"kind": "pool", "id": "pool"},
         "measure": "credit_balance", "period": {"kind": "tax_month", "month": 13}},
        {"kind": "tax", "source": {"kind": "rule", "id": "vat_demand"},
         "measure": "deductible_input", "period": {"kind": "tax_month", "month": 13}},
        {"kind": "tax", "source": {"kind": "rule", "id": "income_demand"},
         "measure": "loss_used", "period": {"kind": "financial_year", "year": 2}},
        {"kind": "tax", "source": {"kind": "rule", "id": "surcharge"},
         "measure": "paid", "period": {"kind": "tax_month", "month": 13}},
    ):
        def with_tax_ref(doc, profile, src=source):
            _surcharge(doc, profile)
            profile["amounts"] = {"tax_ref": _amount(src)}
            doc["planning"]["constraints"] = {
                "tax_floor": {
                    "type": "amount_relation", "enabled": True,
                    "expression": "tax_val >= 0 CNY",
                    "references": {
                        "tax_val": {"kind": "amount", "id": "tax_ref", "period": {"kind": "scalar"}},
                    },
                },
            }

        assert not _certificate(_inputs(vat=True, income=True, sales=True, configure=with_tax_ref))

    # 5. Derived tax on credit_balance and nested derived tax on surcharge.paid block epigraph.
    def with_credit_surcharge(doc, profile):
        _surcharge(doc, profile)
        profile["tax_rules"]["surcharge"]["base_from"] = {
            "kind": "tax", "source": {"kind": "pool", "id": "pool"},
            "measure": "credit_balance", "period": {"kind": "current"},
        }

    assert not _certificate(_inputs(vat=True, income=True, sales=True, configure=with_credit_surcharge))

    def with_nested_surcharge(doc, profile):
        _surcharge(doc, profile)
        profile["tax_rules"]["surcharge_nested"] = {
            "kind": "derived_tax", "label_ref": "demand_label", "axis": "tax_month",
            "base_from": {
                "kind": "tax", "source": {"kind": "rule", "id": "surcharge"},
                "measure": "paid", "period": {"kind": "current"},
            },
            "rate": _money("0.05", "1"), "payment_lag": 0,
        }

    assert not _certificate(_inputs(vat=True, income=True, sales=True, configure=with_nested_surcharge))

    # 6. Refund with zero credit (deductible="0") does not block epigraph.
    assert _certificate(_inputs(vat=True, deductible="0", configure=_refund)) == {("grid1", "supply")}


def test_lagged_surcharge_and_discounted_cross_year_hand_check(tmp_path, monkeypatch):
    """Cross-year (2023 + leap 2024), r=0.07, opening credit & loss, lagged surcharge."""
    def configure(doc, profile):
        _surcharge(doc, profile, rate="0.1", yearly=False)
        profile["tax_rules"]["surcharge"]["payment_lag"] = 1
        profile["credit_pools"]["pool"]["opening_credit"] = _money("10000")
        income = profile["tax_rules"]["income_demand"]
        income["loss_carryforward_years"] = 3
        income["opening_losses"] = [{"amount": _money("20000"), "last_usable_year": 3}]

    inputs = _inputs(
        start_year=2023, vat=True, income=True, sales=True, dispatch=True,
        operation_years=2, discount_rate=0.07, configure=configure,
    )
    assert _certificate(inputs) == {("grid1", "supply")}
    facts, sealed, bundle = _solved(inputs, tmp_path / "envelope")
    assert not _shape(bundle)[b"bit"]
    monkeypatch.setattr(project, "monotone_demand_meters", lambda *_: frozenset())
    exact, exact_sealed, _ = _solved(inputs, tmp_path / "exact")
    for field in ("monthly_demand", "annual_operation", "tax_cash", "annual_net_flows"):
        _assert_same_facts(facts[field], exact[field])
    _assert_same_facts(sealed["evaluation"], exact_sealed["evaluation"])

    # Hand-calculable verification across 2023 (8760h) and leap 2024 (8784h):
    demand_yr = 12 * POWER_KW * 12.0  # 1120.0 CNY/yr
    in_vat_yr = demand_yr * 0.1 / 1.1
    rev_y2, rev_y3 = 7.0 * 8760, 7.0 * 8784
    util_y2, util_y3 = 0.01 * POWER_KW * 8760, 0.01 * POWER_KW * 8784
    op_y2, op_y3 = (rev_y2 - util_y2) - demand_yr, (rev_y3 - util_y3) - demand_yr
    out_vat_y2, out_vat_y3 = rev_y2 * 0.13 / 1.13, rev_y3 * 0.13 / 1.13
    carry_y2 = 10000.0 + in_vat_yr - out_vat_y2
    vat_paid_y3 = out_vat_y3 - in_vat_yr - carry_y2
    assert float(facts["annual_operation"][0]["net"]) == pytest.approx(op_y2, abs=1e-4)
    assert float(facts["annual_operation"][1]["net"]) == pytest.approx(op_y3, abs=1e-4)
    vat_y2 = [row for row in facts["tax_cash"]["vat"] if 13 <= row["month"] <= 24]
    vat_y3 = [row for row in facts["tax_cash"]["vat"] if 25 <= row["month"] <= 36]
    assert sum(float(row["paid"]) for row in vat_y2) == pytest.approx(0.0, abs=1e-5)
    assert float(vat_y2[-1]["carry"]) == pytest.approx(carry_y2, abs=1e-4)
    assert sum(float(row["paid"]) for row in vat_y3) == pytest.approx(vat_paid_y3, abs=1e-4)
    # Lagged surcharge from month 36 pays at period 37 (outside horizon, retained in derived).
    derived_y3 = [row for row in facts["tax_cash"]["derived"] if 25 <= row["period"] <= 36]
    surcharge_assessed_y3 = 0.1 * vat_paid_y3
    surcharge_paid_in_y3 = 0.1 * sum(float(row["paid"]) for row in vat_y3[:-1])
    assert sum(float(row["assessed"]) for row in derived_y3) == pytest.approx(surcharge_assessed_y3, abs=1e-4)
    assert derived_y3[-1]["payment_period"] == 37
    # Income tax: net_rev - demand_net - surcharge_assessed - opening_loss(20000 in yr2).
    taxable_y2 = (rev_y2 - out_vat_y2) - (demand_yr - in_vat_yr)
    taxable_y3 = (rev_y3 - out_vat_y3) - (demand_yr - in_vat_yr) - surcharge_assessed_y3
    inc_y2 = next(r for r in facts["tax_cash"]["income"] if r["year"] == 2)
    inc_y3 = next(r for r in facts["tax_cash"]["income"] if r["year"] == 3)
    assert float(inc_y2["loss_used"]) == pytest.approx(20000.0, abs=1e-4)
    assert float(inc_y2["tax"]) == pytest.approx((taxable_y2 - 20000.0) * 0.25, abs=1e-4)
    assert float(inc_y3["loss_used"]) == pytest.approx(0.0, abs=1e-5)
    assert float(inc_y3["tax"]) == pytest.approx(taxable_y3 * 0.25, abs=1e-4)
    cf2 = op_y2 - float(inc_y2["tax"])
    cf3 = op_y3 - vat_paid_y3 - surcharge_paid_in_y3 - float(inc_y3["tax"])
    assert [float(v) for v in facts["annual_net_flows"]] == pytest.approx([0.0, 0.0, cf2, cf3], abs=1e-3)
    hand_npv = cf2 / (1.07 ** 2) + cf3 / (1.07 ** 3)
    assert float(sealed["evaluation"]["npv"]) == pytest.approx(hand_npv, abs=1e-3)


def test_mixed_certified_and_uncertified_meters_isolated_in_same_model(tmp_path, monkeypatch):
    """One meter uses epigraph while an uncertified sibling meter keeps binary selection."""
    def configure(doc, profile):
        _npv(doc, profile)
        doc["demand_bindings"]["gen_dem"] = {
            "charge": "charge_b", "instance": "gen1", "interface": "supply",
        }
        profile["demand_charges"]["charge_b"] = {"rate": _money("2", "CNY/kW")}
        profile["finance_types"]["local_generator"]["period_variable_om"]["linear"][
            "throughput"]["unit_cost"]["value"] = "0.005"
        profile["amounts"] = {
            "gen_fee": _amount({
                "kind": "demand", "charge": "charge_b", "bindings": ["gen_dem"],
                "basis": "raw", "period": {"kind": "tax_month", "month": 13},
            }),
        }
        doc["planning"]["constraints"] = {
            "gen_floor": {
                "type": "amount_relation", "enabled": True,
                "expression": "bill >= 0 CNY",
                "references": {"bill": {"kind": "amount", "id": "gen_fee", "period": {"kind": "scalar"}}},
            },
        }

    inputs = _inputs(
        start_year=2023, rate="12", vat=True, income=True, sales=True,
        dispatch=True, configure=configure,
    )
    assert _certificate(inputs) == {("grid1", "supply")}
    facts, sealed, bundle = _solved(inputs, tmp_path / "mixed")
    mps_raw = materialized_file(bundle, "input/problem.mps")
    assert not re.findall(rb"grid1__supply__demand_peak_m\d+__bit\d+", mps_raw)
    assert re.findall(rb"grid1__supply__demand_peak_m\d+__floor_\d+", mps_raw)
    assert re.findall(rb"gen1__supply__demand_peak_m\d+__bit\d+", mps_raw)
    assert re.findall(rb"gen1__supply__demand_peak_m\d+__select_\d+", mps_raw)
    col_names = []
    epigraph_peaks = []
    for line in text_lines(bundle.files[RECORDS_PATH]):
        rec = json.loads(line)
        if rec[0] == "column_names":
            col_names.extend(rec[1])
        elif rec[0] == "epigraph":
            epigraph_peaks.append(col_names[rec[1]])
    assert len(epigraph_peaks) == 12
    assert all(name.startswith("grid1__supply__demand_peak_m") for name in epigraph_peaks)

    monkeypatch.setattr(project, "monotone_demand_meters", lambda *_: frozenset())
    exact, exact_sealed, _ = _solved(inputs, tmp_path / "exact")
    for field in ("monthly_demand", "annual_operation", "tax_cash", "annual_net_flows"):
        _assert_same_facts(facts[field], exact[field])
    _assert_same_facts(sealed["evaluation"], exact_sealed["evaluation"])
    gen_rows = [r for r in facts["monthly_demand"] if r["billing_id"] == "gen_dem"]
    grid_rows = [r for r in facts["monthly_demand"] if r["billing_id"] == "first"]
    assert all(float(r["peak_power"]) == pytest.approx(POWER_KW, abs=1e-5) for r in gen_rows)
    assert all(float(r["peak_power"]) == pytest.approx(0.0, abs=1e-5) for r in grid_rows)


def test_real_highs_inflated_epigraph_incumbent_rejected_at_adapter(tmp_path):
    """Real HiGHS VAT carry branch admits an inflated peak; adapter rejects publishing it."""
    import math

    import highspy

    from iesplan.application import worker as worker_app

    def configure(doc, profile):
        _npv(doc, profile)
        profile["energy_prices"]["heat_sale"]["value"] = _money("0.2", "CNY/kWh")
        profile["finance_types"]["local_generator"]["period_variable_om"]["linear"][
            "throughput"]["unit_cost"]["value"] = "0.005"

    inputs = _inputs(
        start_year=2023, rate="2000", vat=True, income=True, sales=True,
        dispatch=True, operation_years=1, configure=configure,
    )
    bundle = HIGHS_MILP_PROVIDER.generate(*inputs)
    mps_path = tmp_path / "problem.mps"
    mps_path.write_bytes(materialized_file(bundle, "input/problem.mps"))

    # Solve with VAT carry branch forced (vat__pool__m*__mutex = 0): real HiGHS finds a
    # relaxed incumbent whose peak_m13 is inflated (~659 W) while flow_m13 is 0 W.
    h = highspy.Highs()
    h.setOptionValue("output_flag", False)
    h.setOptionValue("random_seed", 0)
    h.readModel(str(mps_path))
    lp = h.getLp()
    for idx, name in enumerate(lp.col_names_):
        if name.startswith("vat__") and name.endswith("__mutex"):
            h.changeColBounds(idx, 0.0, 0.0)
    h.run()
    sol = h.getSolution()
    info = h.getInfo()
    assert h.getModelStatus() == highspy.HighsModelStatus.kOptimal
    assert info.primal_solution_status == highspy.SolutionStatus.kSolutionStatusFeasible
    vals = dict(zip(lp.col_names_, sol.col_value, strict=True))
    max_flow_m13 = max(vals[f"grid1__supply__t{t}"] for t in range(0, 744))
    assert vals["grid1__supply__demand_peak_m13"] - max_flow_m13 > 100.0

    # Package the real HiGHS solution as a kTimeLimit incumbent and verify adapter rejection.
    best_bound = info.mip_dual_bound if math.isfinite(info.mip_dual_bound) else None
    gap = info.mip_gap if math.isfinite(info.mip_gap) else None
    lines = [
        "HIGHS_SOLUTION_V1",
        "model_status: HighsModelStatus.kTimeLimit",
        "has_feasible_solution: true",
        f"objective_value: {repr(float(h.getObjectiveValue()))}",
        f"best_bound: {'none' if best_bound is None else repr(float(best_bound))}",
        f"relative_gap: {'none' if gap is None else repr(float(gap))}",
        f"num_columns: {lp.num_col_}",
        "primal:",
        *(f"{i}: {repr(float(v))}" for i, v in enumerate(sol.col_value)),
    ]
    rejected = HIGHS_ADAPTER.adapt(
        bundle, _receipt(result_adapter_ref=ADAPTER_REF),
        {SOLUTION_PATH: ("\n".join(lines) + "\n").encode("utf-8")},
    )
    assert rejected.execution_status == "completed"
    assert rejected.math_status == "not_solved"
    assert set(rejected.outputs) == {"solver_summary"}
    assert rejected.outputs["solver_summary"]["has_feasible_solution"] is False
    assert rejected.outputs["solver_summary"]["objective_value"] is None
    assert worker_app.business_outcome_for(rejected) == "insufficient_evidence"

    # Normal unrestricted solve publishes tight solved result via the real bundle.
    _, _, raw_opt = _solve_internal(bundle, tmp_path)
    solved = HIGHS_ADAPTER.adapt(
        bundle, _receipt(result_adapter_ref=ADAPTER_REF), {SOLUTION_PATH: raw_opt},
    )
    assert solved.math_status == "solved"
    assert all(
        float(row["peak_power"]) == pytest.approx(0.0, abs=1e-5)
        for row in solved.outputs["financial_facts"]["monthly_demand"]
    )
