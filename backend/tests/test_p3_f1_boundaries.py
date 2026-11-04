"""F1's public planning/assembly/submit boundaries with public synthetic data."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

from iesplan.application.tasks.submissions import (
    InvalidRequestError,
    _check_amount_capabilities,
    _check_amount_evaluation_refs,
)
from iesplan.assembly import (
    ASSEMBLY_SCHEMA_PATH,
    canonicalize_assembly_doc,
    consumed_amounts,
    parse_assembly_doc,
    validate_assembly_doc,
)
from iesplan.assembly.amounts import check_amount_relations, check_amount_sources
from iesplan.assembly.rules.finance import linear_drivers, required_driver_names
from iesplan.core.contracts import Constraint, PlanningConfig, PlanningConfigError
from iesplan.core.yamlmini import dump as yaml_dump
from iesplan.devices import init_registry, list_devices
from iesplan.finance import EffectiveFinanceConfig, effective_from_yaml

SAMPLES = Path(__file__).resolve().parents[1] / "iesplan" / "assembly" / "samples"


def _amount(source=None, *, unit="CNY", axis="scalar", coefficient="CNY/kW"):
    return {
        "unit": unit,
        "axis": axis,
        "constant": {"value": "0", "unit": unit},
        "terms": {}
        if source is None
        else {
            "scale": {"coefficient": {"value": "2", "unit": coefficient}, "source": source},
        },
    }


def _driver(**changes):
    return {
        "kind": "driver",
        "finance_type": "pv_system",
        "driver": "capacity_kw",
        "scope": {"kind": "instances", "instances": ["pv1"]},
        "period": {"kind": "scalar"},
        **changes,
    }


def _constraint(*, period=None, enabled=True):
    return {
        "type": "amount_relation",
        "expression": "spend <= 6 CNY",
        "references": {"spend": {"kind": "amount", "id": "spend", "period": period or {"kind": "scalar"}}},
        "enabled": enabled,
    }


@pytest.fixture
def context():
    init_registry()
    doc = parse_assembly_doc((SAMPLES / "valid/campus.assembly.yaml").read_text()).doc
    finance = effective_from_yaml((SAMPLES / "finance/campus.effective-finance.yaml").read_text()).to_dict()
    finance["amounts"] = {"spend": _amount(_driver())}
    doc["finance"]["ref"] = {"kind": "object", "object_id": "test-finance"}
    doc["planning"]["constraints"]["spend_limit"] = _constraint()
    return doc, finance, {entry.device.id: entry for entry in list_devices()}


def _validate(doc, finance):
    return validate_assembly_doc(
        deepcopy(doc),
        package_dir=SAMPLES,
        finance_snapshots={"test-finance": EffectiveFinanceConfig.from_dict(finance)},
    )


def test_amount_constraint_roundtrip_deep_freeze_and_schema(context):
    doc, finance, _ = context
    config = PlanningConfig.from_dict(doc["planning"])
    frozen = config.constraints["spend_limit"]
    with pytest.raises(TypeError):
        frozen.references["spend"]["period"]["kind"] = "current"
    assert frozen.to_dict() == _constraint()
    schema = json.loads((SAMPLES.parent / ASSEMBLY_SCHEMA_PATH).read_text())
    jsonschema.validate(doc, schema)
    # The file parser and canonical output retain the same local reference.
    parsed = parse_assembly_doc(yaml_dump(doc))
    assert parsed.ok, parsed.diagnostics
    result = _validate(doc, finance)
    assert result.artifact, [d.to_dict() for d in result.diagnostics]
    restored = json.loads(result.artifact.canonical_text)
    assert restored["planning"]["constraints"]["spend_limit"] == _constraint()
    assert canonicalize_assembly_doc(restored) == result.artifact.canonical_text


@pytest.mark.parametrize(
    "period",
    [
        {"kind": "current"},
        {"kind": "year_months", "year": "current"},
        {"kind": "financial_year", "year": True},
        {"kind": "financial_year", "year": 0},
        {"kind": "range", "axis": "tax_month", "first": 3, "last": 2},
        {"kind": "scalar", "year": 1},
    ],
)
def test_scalar_constraint_requires_fixed_explicit_period(period):
    with pytest.raises(PlanningConfigError):
        Constraint.from_dict(_constraint(period=period))


@pytest.mark.parametrize(
    ("expression", "reason"),
    [
        ("spend <= 1 kWh", "dimension_mismatch"),
        ("spend * spend <= 6 CNY * 1 CNY", "amount_expression_invalid"),
        ("spend[t] <= 6 CNY", "amount_expression_invalid"),
        ("other <= 6 CNY", "amount_unknown_symbol"),
        ("spend < 6 CNY", "amount_expression_invalid"),
        ("spend <= 6 USD", "dimension_mismatch"),
    ],
)
def test_local_amount_expression_is_affine_and_unit_checked(context, expression, reason):
    doc, finance, _ = context
    doc["planning"]["constraints"]["spend_limit"]["expression"] = expression
    result = _validate(doc, finance)
    assert result.artifact is None
    assert any(d.params.get("reason") == reason for d in result.diagnostics), result.diagnostics


@pytest.mark.parametrize("alias", ["pv_cap", "step_duration", "qconst1", "pv1__upfront_capex"])
def test_local_alias_cannot_shadow_public_symbols(context, alias):
    doc, finance, _ = context
    relation = doc["planning"]["constraints"]["spend_limit"]
    relation["references"][alias] = relation["references"].pop("spend")
    relation["expression"] = f"{alias} <= 6 CNY"
    result = _validate(doc, finance)
    assert result.artifact is None
    assert any(d.params.get("reason") == "alias_reserved_collision" for d in result.diagnostics)


def test_named_items_share_one_driver_binding_and_occurrence_leaf_scope(context):
    doc, finance, _ = context
    finance["finance_types"]["pv_system"]["annual_fixed_om"] = {
        "items": {
            "materials": {"linear": {"capacity_kw": {"unit_cost": {"value": "2", "unit": "CNY/kW/a"}}}},
            "service": {
                "fixed": {"value": "10", "unit": "CNY/a"},
                "linear": {"capacity_kw": {"unit_cost": {"value": "5", "unit": "CNY/kW/a"}}},
            },
        },
    }
    effective = EffectiveFinanceConfig.from_dict(finance)
    assert required_driver_names("new", effective.finance_types["pv_system"]) == {
        "capacity_kw",
        "generated_kwh",
    }
    declared = linear_drivers(effective.finance_types["pv_system"])
    assert len(declared["capacity_kw"]) == 3  # initial capex and both O&M items
    binding = doc["finance_binding"]["instances"]["pv1"]
    binding["cost_occurrence"] = {"annual_fixed_om.items.service": {"kind": "always"}}
    result = _validate(doc, finance)
    assert result.artifact, [d.to_dict() for d in result.diagnostics]
    binding["cost_occurrence"]["annual_fixed_om"] = {"kind": "always"}
    result = _validate(doc, finance)
    assert result.artifact is None
    assert any(d.params.get("reason") == "occurrence_overlapping_paths" for d in result.diagnostics), [
        d.to_dict() for d in result.diagnostics
    ]


def test_used_driver_requires_binding_even_for_sunk_cost_instance(context):
    doc, finance, registry = context
    doc["devices"]["pv1"]["asset_origin"] = "existing"
    del doc["finance_binding"]["instances"]["pv1"]["drivers"]["capacity_kw"]
    diags = []
    check_amount_sources(doc, EffectiveFinanceConfig.from_dict(finance), registry, diags)
    assert [d.params["reason"] for d in diags] == ["amount_driver_binding_missing"]
    assert "spend.terms.scale.source" in diags[0].location["field"]


def test_source_binding_is_exact_not_first_matching_instance(context):
    doc, finance, registry = context
    finance["amounts"]["spend"]["terms"]["scale"]["source"]["scope"]["instances"] = ["bat1"]
    diags = []
    check_amount_sources(doc, EffectiveFinanceConfig.from_dict(finance), registry, diags)
    assert diags[0].params["reason"] == "amount_scope_wrong_instance"
    assert diags[0].params["instances"] == ("bat1",)
    finance["amounts"]["spend"]["terms"]["scale"]["source"] = _driver(scope={"kind": "all_new_instances"})
    doc["devices"]["pv1"]["asset_origin"] = "existing"
    diags = []
    check_amount_sources(doc, EffectiveFinanceConfig.from_dict(finance), registry, diags)
    assert diags == []  # an explicitly empty all-new scope is known zero


def test_used_tariff_binding_cannot_point_to_another_price(context):
    doc, finance, registry = context
    finance["amounts"]["spend"] = _amount(
        {
            "kind": "tariff",
            "price": "grid_import",
            "bindings": ["export_meter"],
            "basis": "raw",
            "period": {"kind": "financial_year", "year": 2},
        },
        coefficient="1",
    )
    diags = []
    check_amount_sources(doc, EffectiveFinanceConfig.from_dict(finance), registry, diags)
    assert diags[0].params["reason"] == "amount_binding_wrong_source"
    assert diags[0].params["binding"] == "export_meter"


def test_property_driver_period_is_not_guessed_from_amount_axis(context):
    doc, finance, registry = context
    finance["amounts"]["spend"]["terms"]["scale"]["source"]["period"] = {
        "kind": "financial_year",
        "year": 2,
    }
    diagnostics = []
    check_amount_sources(doc, EffectiveFinanceConfig.from_dict(finance), registry, diagnostics)
    assert diagnostics[0].params["reason"] == "amount_property_requires_scalar"
    assert diagnostics[0].params["instance"] == "pv1"


def test_system_quantity_uses_real_variable_unit_and_scope(context):
    doc, finance, registry = context
    doc["system_equations"] = {"plant": {"variables": {"output": {"unit": "kW"}}}}
    finance["amounts"]["spend"] = _amount(
        {
            "kind": "system_quantity",
            "block": "plant",
            "variable": "output",
            "aggregation": "integrate_positive",
            "period": {"kind": "financial_year", "year": 2},
        },
        coefficient="CNY/kWh",
    )
    diagnostics = []
    check_amount_sources(doc, EffectiveFinanceConfig.from_dict(finance), registry, diagnostics)
    assert diagnostics == []
    doc["system_equations"]["plant"]["variables"]["output"]["unit"] = "kg"
    check_amount_sources(doc, EffectiveFinanceConfig.from_dict(finance), registry, diagnostics)
    assert diagnostics[0].params["reason"] == "amount_source_dimension_mismatch"
    source = finance["amounts"]["spend"]["terms"]["scale"]["source"]
    source["variable"] = "not_published"
    diagnostics = []
    check_amount_sources(doc, EffectiveFinanceConfig.from_dict(finance), registry, diagnostics)
    assert diagnostics[0].params["reason"] == "amount_unknown_system_variable"


def test_consumption_closure_does_not_activate_unused_definitions(context):
    doc, finance, _ = context
    finance["amounts"]["idle"] = _amount(
        _driver(scope={"kind": "instances", "instances": ["not_in_project"]})
    )
    finance["amounts"]["spend"]["terms"]["scale"] = {
        "coefficient": {"value": "1", "unit": "1"},
        "source": {"kind": "amount", "id": "underlying", "period": {"kind": "scalar"}},
    }
    finance["amounts"]["underlying"] = _amount(_driver())
    effective = EffectiveFinanceConfig.from_dict(finance)
    assert set(consumed_amounts(doc, effective)) == {"spend", "underlying"}
    doc["planning"]["constraints"]["spend_limit"]["enabled"] = False
    assert consumed_amounts(doc, effective) == ()
    _check_amount_capabilities(
        SimpleNamespace(canonical_text=json.dumps(doc)), effective, "test.generator", ()
    )


def test_active_amount_requires_source_execution_and_same_solution_recovery(context):
    doc, finance, _ = context
    effective = EffectiveFinanceConfig.from_dict(finance)
    artifact = SimpleNamespace(canonical_text=json.dumps(doc))
    with pytest.raises(InvalidRequestError) as error:
        _check_amount_capabilities(artifact, effective, "test.generator", ())
    assert error.value.code == "TASK-REQ-007"
    assert error.value.params["missing_capabilities"] == [
        "finance.amount_constraints",
        "finance.amount_references",
        "finance.financial_facts",
    ]
    _check_amount_capabilities(
        artifact,
        effective,
        "test.generator",
        (
            "finance.amount_constraints",
            "finance.amount_references",
            "finance.financial_facts",
        ),
    )


def test_tax_source_requires_actual_rule_or_pool_capabilities(context):
    from test_finance_s1_triplet_contract import _valid_profile_dict

    from iesplan.finance import FinanceProfile, merge_effective

    doc, _, _ = context
    payload = _valid_profile_dict()
    payload["net_cashflows"] = {}
    payload["amounts"] = {
        "spend": _amount(
            {
                "kind": "tax",
                "source": {"kind": "pool", "id": "main_pool"},
                "measure": "paid",
                "period": {"kind": "tax_month", "month": 1},
            },
            coefficient="1",
        )
    }
    effective = merge_effective(FinanceProfile.from_dict(payload), None)
    doc["finance_binding"] = {"instances": {}}
    artifact = SimpleNamespace(canonical_text=json.dumps(doc))
    capabilities = ("finance.amount_constraints", "finance.amount_references", "finance.financial_facts")
    with pytest.raises(InvalidRequestError) as error:
        _check_amount_capabilities(artifact, effective, "test.generator", capabilities)
    assert error.value.code == "TASK-REQ-007"
    assert error.value.params["missing_capabilities"] == ["finance.credit_pools"]
    assert error.value.params["fields"] == ["effective_finance.amounts.spend.terms.scale.source"]
    _check_amount_capabilities(
        artifact, effective, "test.generator", capabilities + ("finance.credit_pools",),
    )
    doc["planning"]["constraints"]["spend_limit"]["enabled"] = False
    artifact.canonical_text = json.dumps(doc)
    _check_amount_capabilities(artifact, effective, "test.generator", ())


def test_named_item_capabilities_are_only_required_for_actual_incremental_costs(context):
    doc, finance, _ = context
    doc["planning"]["constraints"]["spend_limit"]["enabled"] = False
    finance["finance_types"]["pv_system"]["annual_fixed_om"] = {
        "items": {"service": {"fixed": {"value": "10", "unit": "CNY/a"}}},
    }
    effective = EffectiveFinanceConfig.from_dict(finance)
    artifact = SimpleNamespace(canonical_text=json.dumps(doc))
    with pytest.raises(InvalidRequestError) as error:
        _check_amount_capabilities(artifact, effective, "test.generator", ())
    assert error.value.params["missing_capabilities"] == [
        "finance.affine_annual_om",
        "finance.financial_facts",
        "finance.named_items",
    ]
    doc["devices"]["pv1"]["asset_origin"] = "existing"
    artifact.canonical_text = json.dumps(doc)
    _check_amount_capabilities(artifact, effective, "test.generator", ())


def test_fixed_periods_checked_once_with_actual_evaluation(context):
    doc, finance, _ = context
    finance["amounts"]["spend"] = _amount(
        {
            "kind": "maintenance",
            "year": 2,
            "position": 2,
            "basis": "gross",
        },
        coefficient="1",
    )
    effective = EffectiveFinanceConfig.from_dict(finance)
    artifact = SimpleNamespace(canonical_text=json.dumps(doc))
    from iesplan.finance import EvaluationInput, MaintenanceExpense

    evaluation = EvaluationInput(
        evaluation_years=3, construction_period=1, operation_years=2, discount_rate=0.08,
        construction_payments=(),
        maintenance_expenses=(MaintenanceExpense(year=2, amount="1", currency="CNY"),),
    )
    with pytest.raises(InvalidRequestError) as error:
        _check_amount_evaluation_refs(evaluation, artifact, effective)
    assert error.value.code == "TASK-REQ-015"
    assert error.value.params == {"year": 2, "position": 2}
    finance["amounts"]["spend"] = _amount(axis="financial_year")
    effective = EffectiveFinanceConfig.from_dict(finance)
    doc["planning"]["constraints"]["spend_limit"]["references"]["spend"]["period"] = {
        "kind": "financial_year",
        "year": 4,
    }
    artifact.canonical_text = json.dumps(doc)
    with pytest.raises(InvalidRequestError) as error:
        _check_amount_evaluation_refs(evaluation, artifact, effective)
    assert error.value.params["limit"] == 3
    assert error.value.location["field"] == "planning.constraints.spend_limit.references.spend.period"


def test_net_cashflow_amount_reference_is_a_consumption_root(context):
    doc, finance, _ = context
    doc["planning"]["constraints"]["spend_limit"]["enabled"] = False
    finance["net_cashflows"]["recovery"] = {
        "at": {"kind": "evaluation_end"},
        "direction": "inflow",
        "amount": {"kind": "amount_ref", "id": "spend"},
    }
    effective = EffectiveFinanceConfig.from_dict(finance)
    assert consumed_amounts(doc, effective) == ("spend",)


def test_actual_baseline_reference_is_rejected_not_guessed(context):
    doc, finance, _ = context
    doc["planning"]["constraints"]["spend_limit"]["references"]["spend"]["kind"] = "baseline"
    artifact = SimpleNamespace(canonical_text=json.dumps(doc))
    with pytest.raises(InvalidRequestError) as error:
        _check_amount_capabilities(
            artifact,
            EffectiveFinanceConfig.from_dict(finance),
            "test.generator",
            (
                "finance.amount_constraints",
                "finance.amount_references",
                "finance.financial_facts",
            ),
        )
    assert error.value.params["reason"] == "unimplemented_amount_source"
    assert error.value.params["source"]["kind"] == "baseline"
    assert error.value.params["fields"] == ["planning.constraints.spend_limit.references.spend"]


def test_planning_save_uses_effective_amount_units_without_full_topology(context):
    doc, finance, _ = context
    plan = deepcopy(doc["planning"])
    diags = []
    check_amount_relations(plan, EffectiveFinanceConfig.from_dict(finance), diags)
    assert diags == []
    plan["constraints"]["spend_limit"]["references"]["spend"]["period"] = {
        "kind": "financial_year",
        "year": 2,
    }
    check_amount_relations(plan, EffectiveFinanceConfig.from_dict(finance), diags)
    assert diags[0].params["reason"] == "amount_period_axis_mismatch"
    assert diags[0].location["field"].endswith("references.spend.period")


def test_planning_revision_persists_amount_references_and_rejects_bad_units(context, tmp_path, monkeypatch):
    from auth_helpers import make_user
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from iesplan.application.configuration import revisions
    from iesplan.application.projects import lifecycle
    from iesplan.config import settings
    from iesplan.db import Base
    from iesplan.identity.persistence import User  # noqa: F401 — register FK target for isolated schema
    from iesplan.tasks.persistence import Task  # noqa: F401 — register FK target for isolated schema

    doc, finance, _ = context
    profile_payload = {
        "schema": "ies.finance-profile",
        "schema_version": "1.0.0",
        "profile": {
            "id": "amount-save",
            "region": "test",
            "currency": "CNY",
            "base_year": 2026,
            "price_basis": "tax_inclusive",
            "cost_method": "fixed_plus_linear",
        },
        **{
            key: deepcopy(finance[key])
            for key in (
                "finance_types",
                "energy_prices",
                "demand_charges",
                "taxes",
                "tax_rules",
                "credit_pools",
                "net_cashflows",
                "amounts",
            )
        },
    }
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine, expire_on_commit=False) as session:
            owner = make_user(session, "amount_save_owner")
            project = lifecycle.create_project(
                session,
                owner,
                "Amount input",
                baseline_resolution="1h",
                baseline_start_year=2026,
                baseline_scenario_mode="single",
            )
            revisions.register_finance_profile(session, profile_payload, owner.id)
            revisions.set_project_finance_profile(session, project.id, "amount-save", owner.id)
            _, revision = revisions.save_planning_config(session, project.id, doc["planning"], None, owner.id)
            restored, read_revision, _ = revisions.get_planning_config(session, project.id)
            assert read_revision == revision
            assert restored.constraints["spend_limit"].to_dict() == _constraint()
            bad = deepcopy(doc["planning"])
            bad["constraints"]["spend_limit"]["expression"] = "spend <= 1 kWh"
            with pytest.raises(revisions.InvalidRequestError) as error:
                revisions.save_planning_config(session, project.id, bad, revision, owner.id)
            assert error.value.code == "PROJ-PLAN-003"
            assert error.value.params["diagnostics"][0]["field"].endswith("spend_limit.expression")
            restored, current, _ = revisions.get_planning_config(session, project.id)
            assert current == revision
            assert restored.constraints["spend_limit"].expression == "spend <= 6 CNY"
    finally:
        engine.dispose()
