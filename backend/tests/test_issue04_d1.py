"""ISSUE-04 D1: public finance-v4 and assembly-v4, no solver or browser.

Synthetic inputs only. Test the existing production parsing, merge, validation and
capability gate. D2 peak/tax/cashflow calculations are intentionally unimplemented.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from iesplan.assembly import validate_assembly_doc
from iesplan.core.yamlmini import load as yaml_load
from iesplan.application.tasks.submissions import _check_demand_capabilities
from iesplan.finance import EffectiveFinanceConfig, FinanceOverrides, FinanceProfile, FinanceTripletError, merge_effective
from iesplan.tasks.contracts import InvalidRequestError
from test_finance_triplet import _profile_payload


RATE = {"rate": {"value": "25", "unit": "CNY/kW"}}
DIR = Path(__file__).resolve().parent.parent / "iesplan" / "assembly" / "samples"


def _profile():
    payload = _profile_payload()
    payload["demand_charges"] = {"main_charge": deepcopy(RATE), "second_charge": deepcopy(RATE)}
    return payload


def test_demand_profile_effective_and_rate_override():
    payload = _profile()
    profile = FinanceProfile.from_dict(payload)
    assert profile.to_dict()["demand_charges"]["main_charge"] == RATE
    overrides = FinanceOverrides.from_dict({
        "schema": "ies.finance-overrides", "schema_version": "4.0.0",
        "profile_ref": {"id": profile.profile_id},
        "demand_charges": {"main_charge": {"rate": {"value": "38", "unit": "CNY/kW"}}},
    }, profile=profile)
    effective = merge_effective(profile, overrides)
    assert effective.demand_charges["main_charge"].rate.value == Decimal("38")
    assert effective.demand_charges["second_charge"].rate.value == Decimal("25")
    assert EffectiveFinanceConfig.from_dict(effective.to_dict()).to_dict() == effective.to_dict()
    assert FinanceOverrides.from_dict(overrides.to_dict(), profile=profile).to_dict() == overrides.to_dict()
    assert profile.demand_charges["main_charge"].rate.value == Decimal("25")


@pytest.mark.parametrize("rate", [
    {"value": "-1", "unit": "CNY/kW"},
    {"value": 1.0, "unit": "CNY/kW"},
    {"value": "10", "unit": "CNY/kWh"},
    {"value": "10", "unit": "USD/kW"},
    {"value": "10", "unit": "CNY/kW/月"},
])
def test_demand_rate_rejects_illegal_money(rate):
    payload = _profile()
    payload["demand_charges"]["main_charge"]["rate"] = rate
    with pytest.raises(FinanceTripletError):
        FinanceProfile.from_dict(payload)


def test_demand_missing_section_and_unknown_override_rejected():
    payload = _profile()
    payload.pop("demand_charges")
    with pytest.raises(FinanceTripletError):
        FinanceProfile.from_dict(payload)
    profile = FinanceProfile.from_dict(_profile())
    base = {
        "schema": "ies.finance-overrides", "schema_version": "4.0.0",
        "profile_ref": {"id": profile.profile_id},
    }
    with pytest.raises(FinanceTripletError):
        FinanceOverrides.from_dict({**base, "demand_charges": {"unknown": RATE}}, profile=profile)
    with pytest.raises(FinanceTripletError):
        FinanceOverrides.from_dict({**base, "demand_charges": {"main_charge": {"other": RATE}}}, profile=profile)
    with pytest.raises(FinanceTripletError):
        FinanceOverrides.from_dict({
            **base, "demand_charges": {"main_charge": {"rate": {"value": "35", "unit": "CNY/MW"}}}
        }, profile=profile)


def test_demand_tax_references_and_vat_override_are_input_only():
    payload = _profile()
    payload["taxes"] = {
        "vat_demand": {
            "display_name": "Synthetic demand VAT", "tax_type": "value_added_tax",
            "applies_to": "demand_charges.main_charge",
        },
        "income_demand": {
            "display_name": "Synthetic demand income", "tax_type": "income_tax",
            "applies_to": "demand_charges.main_charge",
        },
    }
    payload["credit_pools"] = {
        "pool": {
            "opening_credit": {"value": "0", "unit": "CNY"},
            "receivable_policy": "end_receivable", "refunds": [],
        },
    }
    payload["tax_rules"] = {
        "vat": {
            "kind": "vat", "label_ref": "vat_demand", "settlement": "monthly",
            "in_tax": [], "out_tax": [], "om_input_tax": [], "maintenance_input_tax": [],
            "demand_input_tax": [
                {"charge": "main_charge",
                 "rate": {"value": "0.06", "unit": "1"},
                 "deductible_ratio": {"value": "0.5", "unit": "1"}},
            ],
            "credit_pool": "pool", "confirmation": "same_period",
        },
        "income": {
            "kind": "income_tax", "label_ref": "income_demand",
            "rate": {"value": "0.25", "unit": "1"},
            "revenue": [], "energy_cost": [], "demand_cost": ["main_charge"],
            "annual_om": [], "variable_om": [],
            "deductible_maintenance": {"mode": "explicit_items", "items": []},
            "depreciation": [], "loss_carryforward_years": 0, "opening_losses": [],
        },
    }
    profile = FinanceProfile.from_dict(payload)
    overrides = FinanceOverrides.from_dict({
        "schema": "ies.finance-overrides", "schema_version": "4.0.0",
        "profile_ref": {"id": profile.profile_id},
        "tax_rules": {"vat": {"demand_input_tax": {
            "main_charge": {"deductible_ratio": {"value": "1", "unit": "1"}},
        }}},
    }, profile=profile)
    effective = merge_effective(profile, overrides)
    assert effective.tax_rules["vat"].demand_input_tax[0].deductible_ratio.value == Decimal("1")
    assert effective.tax_rules["income"].demand_cost == ("main_charge",)
    assert EffectiveFinanceConfig.from_dict(effective.to_dict()).to_dict() == effective.to_dict()
    bad = deepcopy(payload)
    bad["tax_rules"]["income"]["demand_cost"] = ["not_registered"]
    with pytest.raises(FinanceTripletError):
        FinanceProfile.from_dict(bad)


def _assembly():
    from iesplan.devices import init_registry
    init_registry()
    doc = yaml_load((DIR / "valid" / "campus.assembly.yaml").read_text("utf-8"))
    finance_doc = yaml_load((DIR / "finance" / "campus.effective-finance.yaml").read_text("utf-8"))
    finance_doc["demand_charges"] = {"main_charge": deepcopy(RATE), "second_charge": deepcopy(RATE)}
    effective = EffectiveFinanceConfig.from_dict(finance_doc)
    doc["finance"]["ref"] = {"kind": "object", "object_id": "fin:campus"}
    doc["demand_bindings"] = {"month_import": {
        "charge": "main_charge", "instance": "grid", "interface": "electricity_import",
    }}
    doc["planning"]["objective"] = {
        "sense": "minimize", "expression": "month_import__period_demand_charge",
    }
    doc["planning"]["constraints"] = {}
    # A power-only demand meter does not require buying energy via tariff_bindings.
    doc["tariff_bindings"] = {}
    return doc, effective


def _check(doc, eff):
    return validate_assembly_doc(
        doc, package_dir=DIR, finance_snapshots={"fin:campus": eff},
    )


def test_demand_bindings_validate_and_planning_symbol_and_freeze():
    doc, eff = _assembly()
    result = _check(doc, eff)
    assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]
    assert '"demand_bindings"' in result.artifact.canonical_text
    from iesplan.assembly.rules.planning import finance_identifiers
    dims, _ = finance_identifiers(doc, eff)
    assert dims["month_import__period_demand_charge"]["currency"] == 1
    # Reject unsupported D2 at the public task gate; even a valid, frozen D1
    # binding cannot result in an optimization which silently drops the bill.
    with pytest.raises(InvalidRequestError) as exc:
        _check_demand_capabilities(result.artifact, "ies.generator.highs_milp@3.4.0", ("affine",))
    assert exc.value.code == "TASK-REQ-007"
    assert exc.value.params["missing_capabilities"] == ["finance.monthly_demand"]
    _check_demand_capabilities(result.artifact, "candidate-D2", ("finance.monthly_demand",))


@pytest.mark.parametrize("mutate,reason", [
    (lambda d: d["demand_bindings"]["month_import"].update(charge="unknown"), "charge_unknown"),
    (lambda d: d["demand_bindings"]["month_import"].update(instance="absent"), "instance_unknown"),
    (lambda d: d["demand_bindings"]["month_import"].update(interface="electricity_export"), "requires_nonnegative_electric_power_out"),
    (lambda d: d["demand_bindings"]["month_import"].update(interface="absent"), "interface_unknown"),
    (lambda d: d["demand_bindings"].update({"another": dict(d["demand_bindings"]["month_import"])}), "duplicate_charge_meter"),
])
def test_demand_binding_errors_are_blocking(mutate, reason):
    doc, eff = _assembly()
    mutate(doc)
    result = _check(doc, eff)
    assert result.artifact is None
    assert any(d.params.get("reason") == reason for d in result.diagnostics)


def test_distinct_charge_at_same_power_meter_is_explicitly_allowed():
    doc, eff = _assembly()
    doc["demand_bindings"]["secondary"] = {
        "charge": "second_charge", "instance": "grid", "interface": "electricity_import",
    }
    result = _check(doc, eff)
    assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]


def test_zero_demand_keeps_empty_contract_without_generator_capability():
    doc, eff = _assembly()
    doc["demand_bindings"] = {}
    doc["planning"]["objective"] = {"sense": "minimize", "expression": "0 CNY"}
    result = _check(doc, eff)
    assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]
    _check_demand_capabilities(result.artifact, "ies.generator.highs_milp@3.4.0", ("affine",))
