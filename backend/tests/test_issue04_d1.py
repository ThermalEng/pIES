"""ISSUE-04 D1: public finance and assembly-v4, no solver or browser.

Synthetic inputs only. Test the existing production parsing, merge, validation and
capability gate. D1 validation and capability gating remain independently covered after D2.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from iesplan.assembly import validate_assembly_doc
from iesplan.core.yamlmini import load as yaml_load
from iesplan.application.tasks.submissions import _check_demand_capabilities
from iesplan.finance import SCHEMA_VERSION, EffectiveFinanceConfig, FinanceOverrides, FinanceProfile, FinanceTripletError, merge_effective
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
        "schema": "ies.finance-overrides", "schema_version": SCHEMA_VERSION,
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
        "schema": "ies.finance-overrides", "schema_version": SCHEMA_VERSION,
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
        "schema": "ies.finance-overrides", "schema_version": SCHEMA_VERSION,
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


def _check(doc, eff, **kwargs):
    return validate_assembly_doc(
        doc, package_dir=DIR, finance_snapshots={"fin:campus": eff}, **kwargs,
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
        _check_demand_capabilities(result.artifact, "ies.generator.highs_milp@6.0.0", ("affine",))
    assert exc.value.code == "TASK-REQ-007"
    assert exc.value.params["missing_capabilities"] == [
        "finance.financial_facts", "finance.monthly_demand",
    ]
    with pytest.raises(InvalidRequestError) as missing_facts:
        _check_demand_capabilities(
            result.artifact, "candidate-D2", ("finance.monthly_demand",)
        )
    assert missing_facts.value.params["missing_capabilities"] == [
        "finance.financial_facts"
    ]
    _check_demand_capabilities(
        result.artifact, "candidate-D2",
        ("finance.monthly_demand", "finance.financial_facts"),
    )


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
    _check_demand_capabilities(result.artifact, "ies.generator.highs_milp@6.0.0", ("affine",))


@pytest.mark.parametrize(
    "instance_floor,should_accept",
    [(-10, False), (0, True), (7, True)],
)
def test_demand_property_ref_uses_instance_minimum(instance_floor, should_accept):
    """Use the real 2.1 device parser + full assembly validation, not a fake spec.

    Declared minimum property accepts negative numbers (-100..100); only the
    demand meter requires the *actual instance* floor to be nonnegative.
    """
    from iesplan.devices.parser2 import parse_device_model_v2

    raw = yaml_load(
        (DIR.parent.parent / "devices" / "catalog" / "grid_connection.yaml").read_text("utf-8")
    )
    raw["properties"]["import_floor_kw"] = {
        "value": 12,
        "unit": "kW",
        "valid_range": {"minimum": -100, "maximum": 100},
    }
    raw["interfaces"]["electricity_import"]["valid_range"]["minimum"] = {
        "property_ref": "import_floor_kw"
    }
    parsed = parse_device_model_v2(raw, file="synthetic-grid.yaml")
    assert parsed.ok, [d.to_dict() for d in parsed.diagnostics]

    doc, effective = _assembly()
    doc["devices"]["grid"]["properties"]["import_floor_kw"] = {
        "value": instance_floor, "unit": "kW",
    }
    result = _check(doc, effective, device_documents={
        "ies.device.grid_connection": parsed.document,
    })
    if should_accept:
        assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]
    else:
        assert result.artifact is None
        assert any(
            d.params.get("reason") == "requires_nonnegative_electric_power_out"
            for d in result.diagnostics
        )


def test_demand_property_ref_default_is_used_without_override():
    """The unoverridden descriptor property is still an actual instance bound."""
    from iesplan.devices.parser2 import parse_device_model_v2

    raw = yaml_load(
        (DIR.parent.parent / "devices" / "catalog" / "grid_connection.yaml").read_text("utf-8")
    )
    raw["properties"]["import_floor_kw"] = {
        "value": -1, "unit": "kW",
        "valid_range": {"minimum": -100, "maximum": 100},
    }
    raw["interfaces"]["electricity_import"]["valid_range"]["minimum"] = {
        "property_ref": "import_floor_kw"
    }
    parsed = parse_device_model_v2(raw, file="synthetic-grid.yaml")
    assert parsed.ok, [d.to_dict() for d in parsed.diagnostics]
    doc, effective = _assembly()
    assert "import_floor_kw" not in doc["devices"]["grid"]["properties"]
    result = _check(doc, effective, device_documents={
        "ies.device.grid_connection": parsed.document,
    })
    assert result.artifact is None
    assert any(
        d.params.get("reason") == "requires_nonnegative_electric_power_out"
        for d in result.diagnostics
    )


@pytest.mark.parametrize("planning_lower", ["0", None])
def test_demand_property_ref_accepts_planning_takeover_of_negative_old_value(planning_lower):
    """Shared effective-floor check preserves demand's electricity/out contract."""
    from iesplan.devices.parser2 import parse_device_model_v2

    raw = yaml_load(
        (DIR.parent.parent / "devices" / "catalog" / "grid_connection.yaml").read_text("utf-8")
    )
    raw["properties"]["import_floor_kw"] = {
        "value": 1, "unit": "kW",
        "valid_range": {"minimum": -10, "maximum": 10},
    }
    raw["interfaces"]["electricity_import"]["valid_range"]["minimum"] = {
        "property_ref": "import_floor_kw",
    }
    parsed = parse_device_model_v2(raw, file="synthetic-demand-grid.yaml")
    assert parsed.ok, [d.to_dict() for d in parsed.diagnostics]

    doc, effective = _assembly()
    doc["devices"]["grid"]["properties"]["import_floor_kw"] = {
        "value": -1, "unit": "kW",
    }
    doc["planning"]["variables"]["floor_plan"] = {
        "device_ref": "grid", "parameter": "import_floor_kw", "unit": "kW",
        "lower_bound": planning_lower, "upper_bound": "1",
    }
    docs = {"ies.device.grid_connection": parsed.document}
    result = _check(doc, effective, device_documents=docs)
    assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]

    # The identical negative instance floor remains ineligible without takeover.
    # Existing demand-binding tests independently enforce its out/electricity/unit gate.
    # Validation materializes resource content into its input tree; a fresh
    # assembly is required for an independent public-entry comparison.
    unplanned, _ = _assembly()
    unplanned["devices"]["grid"]["properties"]["import_floor_kw"] = {
        "value": -1, "unit": "kW",
    }
    no_takeover = _check(unplanned, effective, device_documents=docs)
    assert no_takeover.artifact is None
    assert any(
        d.params.get("reason") == "requires_nonnegative_electric_power_out"
        for d in no_takeover.diagnostics
    )
