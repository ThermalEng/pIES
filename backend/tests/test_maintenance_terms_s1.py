"""0.15 ISSUE-06 S1: typed maintenance terms, frozen identity and reference gates."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from iesplan.application.tasks.submissions import (
    _check_affine_maintenance_capabilities,
    _check_maintenance_term_references,
)
from iesplan.finance import MaintenanceCostComponentShare, MaintenanceScaleUnitCost
from iesplan.tasks.contracts import InvalidRequestError, parse_evaluation_input


def _raw_evaluation(terms=None, *, include_terms: bool = True, currency: str = "CNY"):
    expense = {"year": 3, "amount": "0", "currency": currency}
    if include_terms:
        expense["terms"] = [] if terms is None else terms
    return {
        "evaluation_years": 3,
        "construction_period": 1,
        "operation_years": 2,
        "discount_rate": 0.08,
        "construction_payments": [{"t": 0, "ratio": 1}],
        "maintenance_expenses": [expense],
    }


def _facts():
    properties = {
        "capacity_kwh": {"unit": "kWh"},
        "rated_power_kw": {"unit": "kW"},
    }
    return {
        "devices": {
            "new_a": {
                "definition": {"id": "battery"},
                "asset_origin": "new",
                "properties": {
                    "capacity_kwh": {"value": "100", "unit": "kWh"},
                    "rated_power_kw": {"value": "50", "unit": "kW"},
                },
            },
            "new_b": {
                "definition": {"id": "battery"},
                "asset_origin": "new",
                "properties": {
                    "capacity_kwh": {"value": "200", "unit": "kWh"},
                    "rated_power_kw": {"value": "100", "unit": "kW"},
                },
            },
            "existing_a": {
                "definition": {"id": "battery"},
                "asset_origin": "existing",
                "properties": {
                    "capacity_kwh": {"value": "80", "unit": "kWh"},
                    "rated_power_kw": {"value": "40", "unit": "kW"},
                },
            },
        },
        "planning": {
            "variables": {
                "new_a_capacity": {
                    "device_ref": "new_a",
                    "parameter": "capacity_kwh",
                    "unit": "kWh",
                }
            }
        },
        "finance_binding": {
            "instances": {
                "new_a": {"type": "costed", "finance_type": "battery_system"},
                "new_b": {"type": "costed", "finance_type": "battery_system"},
                "existing_a": {"type": "costed", "finance_type": "battery_system"},
            }
        },
    }


def _validate_references(terms, *, facts=None, effective=None):
    evaluation = parse_evaluation_input(_raw_evaluation(terms))
    artifact = SimpleNamespace(canonical_text=json.dumps(facts or _facts()))
    device_documents = {
        "battery": {"properties": {
            "capacity_kwh": {"unit": "kWh"},
            "rated_power_kw": {"unit": "kW"},
        }}
    }
    if effective is None:
        effective = SimpleNamespace(
            finance_types={"battery_system": SimpleNamespace(upfront_capex=object())}
        )
    _check_maintenance_term_references(
        evaluation, artifact, device_documents, effective
    )
    return evaluation


def _scale(instance_id="new_a", property_name="capacity_kwh", unit="CNY/kWh"):
    return {
        "kind": "scale_unit_cost",
        "instance_id": instance_id,
        "property": property_name,
        "unit_cost": {"value": "2.50", "unit": unit},
    }


def _share(instance_id="new_b", ratio=0.2):
    return {
        "kind": "cost_component_share",
        "instance_id": instance_id,
        "component": "upfront_capex",
        "ratio": ratio,
    }


def test_omitted_and_empty_terms_have_the_same_frozen_shape():
    omitted = parse_evaluation_input(_raw_evaluation(include_terms=False))
    explicit = parse_evaluation_input(_raw_evaluation(terms=[]))
    assert omitted.maintenance_expenses[0].terms == ()
    assert omitted.maintenance_expenses[0].to_frozen() == {
        "year": 3, "amount": "0", "currency": "CNY", "terms": []
    }
    assert explicit.maintenance_expenses[0].to_frozen() == (
        omitted.maintenance_expenses[0].to_frozen()
    )


def test_typed_terms_freeze_restore_amount_references_and_order():
    raw_terms = [
        _scale("new_a", "capacity_kwh"),
        _share("new_b", 0.125),
        _scale("new_b", "rated_power_kw", "CNY/kW"),
    ]
    parsed = parse_evaluation_input(_raw_evaluation(raw_terms))
    expense = parsed.maintenance_expenses[0]
    assert isinstance(expense.terms[0], MaintenanceScaleUnitCost)
    assert isinstance(expense.terms[1], MaintenanceCostComponentShare)
    assert [term.to_frozen() for term in expense.terms] == [
        {
            "kind": "scale_unit_cost",
            "instance_id": "new_a",
            "property": "capacity_kwh",
            "unit_cost": {"value": "2.5", "unit": "CNY/kWh"},
        },
        {
            "kind": "cost_component_share",
            "instance_id": "new_b",
            "component": "upfront_capex",
            "ratio": "0.125",
        },
        {
            "kind": "scale_unit_cost",
            "instance_id": "new_b",
            "property": "rated_power_kw",
            "unit_cost": {"value": "2.5", "unit": "CNY/kW"},
        },
    ]
    restored = type(expense).from_frozen(expense.to_frozen())
    assert restored == expense
    assert [term.instance_id for term in restored.terms] == [
        "new_a", "new_b", "new_b"
    ]


@pytest.mark.parametrize(
    "term",
    [
        {"kind": "unknown"},
        {**_scale(), "extra": 1},
        {**_scale(), "unit_cost": {"value": 2.5, "unit": "CNY/kWh"}},
        {**_scale(), "unit_cost": {"value": "-1", "unit": "CNY/kWh"}},
        {**_scale(), "unit_cost": {"value": "2", "unit": ""}},
        {**_share(), "ratio": -0.01},
        {**_share(), "ratio": 1.01},
        {**_share(), "ratio": True},
        {**_share(), "component": "annual_fixed_om"},
    ],
)
def test_malformed_maintenance_terms_are_rejected(term):
    with pytest.raises(InvalidRequestError) as exc_info:
        parse_evaluation_input(_raw_evaluation([term]))
    assert exc_info.value.code == "TASK-REQ-015"


def test_scale_references_support_planned_and_explicit_fixed_properties():
    evaluation = _validate_references([
        _scale("new_a", "capacity_kwh"),
        _scale("new_b", "rated_power_kw", "CNY/kW"),
    ])
    assert evaluation.maintenance_expenses[0].terms[0].instance_id == "new_a"
    assert evaluation.maintenance_expenses[0].terms[1].instance_id == "new_b"


def test_shared_finance_type_keeps_cost_reference_on_the_selected_instance():
    evaluation = _validate_references([_share("new_b", 0.25)])
    term = evaluation.maintenance_expenses[0].terms[0]
    assert isinstance(term, MaintenanceCostComponentShare)
    assert term.instance_id == "new_b"


@pytest.mark.parametrize(
    "term",
    [
        _scale("missing", "capacity_kwh"),
        _scale("new_a", "unknown_property"),
        _scale("new_a", "capacity_kwh", "CNY/kW"),
    ],
)
def test_unknown_references_and_wrong_dimensions_are_rejected(term):
    with pytest.raises(InvalidRequestError) as exc_info:
        _validate_references([term])
    assert exc_info.value.code == "TASK-REQ-015"


def test_fixed_property_cannot_fall_back_to_device_default():
    facts = _facts()
    facts["devices"]["new_b"]["properties"].pop("rated_power_kw")
    with pytest.raises(InvalidRequestError) as exc_info:
        _validate_references([_scale("new_b", "rated_power_kw", "CNY/kW")], facts=facts)
    assert exc_info.value.code == "TASK-REQ-015"


def test_cost_share_rejects_existing_assets_and_missing_component():
    with pytest.raises(InvalidRequestError) as exc_info:
        _validate_references([_share("existing_a")])
    assert exc_info.value.code == "TASK-REQ-015"

    with pytest.raises(InvalidRequestError) as exc_info:
        _validate_references(
            [_share("new_b")],
            effective=SimpleNamespace(
                finance_types={"battery_system": SimpleNamespace(annual_fixed_om=object())}
            ),
        )
    assert exc_info.value.code == "TASK-REQ-015"


def test_associated_terms_require_explicit_generator_capability():
    associated = parse_evaluation_input(_raw_evaluation([_scale()]))
    with pytest.raises(InvalidRequestError) as exc_info:
        _check_affine_maintenance_capabilities(associated, "highs", ())
    assert exc_info.value.code == "TASK-REQ-007"
    _check_affine_maintenance_capabilities(
        associated, "test-only", ("finance.affine_maintenance",)
    )
    fixed_only = parse_evaluation_input(_raw_evaluation(terms=[]))
    _check_affine_maintenance_capabilities(fixed_only, "highs", ())
