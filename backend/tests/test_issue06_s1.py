"""ISSUE-06 S1: 有序维护来源、装配引用、冻结恢复与生产能力拒收。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from auth_helpers import make_user
from test_task_config_revision import (  # noqa: F401
    _h,
    _prepare_project,
    _save_config,
    client,
    db,
    engine,
    factory,
)

from iesplan.application.tasks.submissions import (
    _check_maintenance_capability,
    _check_maintenance_refs,
    _evaluation_identity,
)
from iesplan.finance import (
    CostComponentShareTerm,
    EvaluationInput,
    MaintenanceUnitCost,
    ScaleUnitCostTerm,
    maintenance_term_to_dict,
)
from iesplan.tasks.contracts import InvalidRequestError, parse_evaluation_input


def _input(items: list[dict]) -> dict:
    return {
        "evaluation_years": 2,
        "construction_period": 0,
        "operation_years": 2,
        "discount_rate": 0.08,
        "construction_payments": [{"t": 0, "ratio": 1}],
        "maintenance_expenses": items,
    }


def _scale(inst: str = "bat_a", prop: str = "capacity_kwh", unit: str = "CNY/kWh") -> dict:
    return {
        "kind": "scale_unit_cost", "instance_id": inst, "property": prop,
        "unit_cost": {"value": "3.2500", "unit": unit},
    }


def _share(inst: str = "bat_a", ratio: float = 0.2) -> dict:
    return {
        "kind": "cost_component_share", "instance_id": inst,
        "component": "upfront_capex", "ratio": ratio,
    }


def _expense(*terms: dict, amount: str = "0", year: int = 2) -> dict:
    return {"year": year, "amount": amount, "currency": "CNY", "terms": list(terms)}


def _facts():
    """仅提取提交边界所需的已签发事实形态，不模拟第二套装配校验。"""
    doc = {
        "devices": {
            "bat_a": {"asset_origin": "new"},
            "bat_b": {"asset_origin": "new"},
            "old_bat": {"asset_origin": "existing"},
            "bare": {"asset_origin": "new"},
        },
        "planning": {"variables": {"cap_a": {"device_ref": "bat_a", "parameter": "capacity_kwh"}}},
        "finance_binding": {"instances": {
            "bat_a": {"type": "costed", "finance_type": "battery_system"},
            "bat_b": {"type": "costed", "finance_type": "battery_system"},
            "old_bat": {"type": "costed", "finance_type": "battery_system"},
            "bare": {"type": "none"},
        }},
    }
    models = {
        "bat_a": {"variables": {
            "capacity_kwh": {"kind": "property", "unit": "kWh", "value": 200},
            "operation_mode": {"kind": "property", "unit": "1", "value": "auto"},
        }},
        "bat_b": {"variables": {"rated_power_kw": {"kind": "property", "unit": "kW", "value": 90}}},
        "old_bat": {"variables": {"capacity_kwh": {"kind": "property", "unit": "kWh", "value": 50}}},
        "bare": {"variables": {"capacity_kwh": {"kind": "property", "unit": "kWh", "value": -20}}},
    }
    effective = SimpleNamespace(
        currency="CNY",
        finance_types={"battery_system": SimpleNamespace(upfront_capex=object())},
    )
    return SimpleNamespace(canonical_text=json.dumps(doc)), models, effective


def test_affine_parse_order_and_fixed_amount_are_independent() -> None:
    parsed = parse_evaluation_input(_input([
        _expense(_scale(), _share(), _scale("bat_b", "rated_power_kw", "CNY/kW"), amount="19.5000"),
        {"year": 2, "amount": "6.00", "currency": "CNY"},
        _expense(_share("bat_b", 0.1), year=1),
    ]))
    first, second, third = parsed.maintenance_expenses
    assert first.amount == "19.5" and second.amount == "6"
    assert [term.kind for term in first.terms] == [
        "scale_unit_cost", "cost_component_share", "scale_unit_cost",
    ]
    assert first.terms[0] == ScaleUnitCostTerm(
        kind="scale_unit_cost", instance_id="bat_a", property="capacity_kwh",
        unit_cost=MaintenanceUnitCost(value="3.25", unit="CNY/kWh"),
    )
    assert first.terms[1] == CostComponentShareTerm(
        kind="cost_component_share", instance_id="bat_a",
        component="upfront_capex", ratio=0.2,
    )
    assert second.terms == () and third.year == 1


@pytest.mark.parametrize("term,field", [
    ({"kind": "unknown", "instance_id": "bat_a"}, ".kind"),
    ({**_scale(), "extra": 5}, "terms[0]"),
    ({**_scale(), "instance_id": ""}, ".instance_id"),
    ({**_scale(), "unit_cost": {"value": 3.25, "unit": "CNY/kWh"}}, ".unit_cost.value"),
    ({**_scale(), "unit_cost": {"value": "-1", "unit": "CNY/kWh"}}, ".unit_cost.value"),
    ({**_scale(), "unit_cost": {"value": "NaN", "unit": "CNY/kWh"}}, ".unit_cost.value"),
    ({**_scale(), "unit_cost": {"value": "3", "unit": ""}}, ".unit_cost.unit"),
    ({**_share(), "component": "annual_fixed_om"}, ".component"),
    ({**_share(), "ratio": "0.2"}, ".ratio"),
    ({**_share(), "ratio": 1.01}, ".ratio"),
    ({**_share(), "ratio": True}, ".ratio"),
])
def test_reject_invalid_term_shape_and_amount(term, field) -> None:
    with pytest.raises(InvalidRequestError) as raised:
        parse_evaluation_input(_input([_expense(term)]))
    assert raised.value.code == "TASK-REQ-015"
    assert (raised.value.location or {})["field"].endswith(field)


def test_reject_non_list_terms() -> None:
    with pytest.raises(InvalidRequestError) as raised:
        parse_evaluation_input(_input([{**_expense(), "terms": {"a": 1}}]))
    assert raised.value.code == "TASK-REQ-015"
    assert (raised.value.location or {})["field"] == "evaluation.maintenance_expenses[0].terms"


def test_assembly_reference_scope_currency_and_units() -> None:
    artifact, models, effective = _facts()
    # 同类别两个实例按自己的属性，规划接管的 bat_a 与固定的 old_bat 均可引用。
    ok = parse_evaluation_input(_input([
        _expense(_scale(), _scale("bat_b", "rated_power_kw", "CNY/kW"), _share("bat_a")),
        _expense(_scale("old_bat")),
    ]))
    _check_maintenance_refs(ok, artifact, models, effective)

    bad = [
        (_scale("missing"), ".instance_id"),
        (_scale("bat_b"), ".property"),
        (_scale("bat_a", "operation_mode", "CNY"), ".property"),
        (_scale("bare"), ".property"),
        (_scale("bat_a", "capacity_kwh", "CNY/kW"), ".unit_cost.unit"),
        (_scale("bat_a", "capacity_kwh", "USD/kWh"), ".unit_cost.unit"),
        (_scale("bat_a", "capacity_kwh", "invalid-unit"), ".unit_cost.unit"),
        (_share("old_bat"), ".component"),
        (_share("bare"), ".component"),
    ]
    for term, field in bad:
        evaluation = parse_evaluation_input(_input([_expense(term)]))
        with pytest.raises(InvalidRequestError) as raised:
            _check_maintenance_refs(evaluation, artifact, models, effective)
        assert raised.value.code == "TASK-REQ-015"
        assert (raised.value.location or {})["field"].endswith(field)

    wrong_currency = parse_evaluation_input(_input([{**_expense(_scale()), "currency": "USD"}]))
    with pytest.raises(InvalidRequestError) as raised:
        _check_maintenance_refs(wrong_currency, artifact, models, effective)
    assert (raised.value.location or {})["field"].endswith(".currency")


def test_affine_capability_only_required_for_nonempty_terms() -> None:
    plain = parse_evaluation_input(_input([{"year": 2, "amount": "10", "currency": "CNY"}]))
    _check_maintenance_capability(plain, "ies.generator.highs_milp@6.0.0", ())
    affine = parse_evaluation_input(_input([_expense(_scale())]))
    with pytest.raises(InvalidRequestError) as raised:
        _check_maintenance_capability(affine, "ies.generator.highs_milp@6.0.0", ())
    assert raised.value.code == "TASK-REQ-007"
    assert raised.value.params["missing_capabilities"] == ["finance.affine_maintenance"]
    _check_maintenance_capability(affine, "candidate", ("finance.affine_maintenance",))


def test_ordered_terms_frozen_roundtrip_and_identity() -> None:
    parsed = parse_evaluation_input(_input([
        _expense(_scale(), _share(), amount="10"), _expense(_share("bat_b")),
    ]))
    raw = _input([])
    raw["construction_payments"] = [{"t": p.t, "ratio": p.ratio} for p in parsed.construction_payments]
    raw["maintenance_expenses"] = [
        {"year": e.year, "amount": e.amount, "currency": e.currency,
         "terms": [maintenance_term_to_dict(t) for t in e.terms]}
        for e in parsed.maintenance_expenses
    ]
    restored = EvaluationInput.from_frozen(raw)
    assert restored == parsed
    assert [term.kind for term in restored.maintenance_expenses[0].terms] == [
        "scale_unit_cost", "cost_component_share",
    ]
    original = {**raw, "confirmed_by": 7, "confirmed_at": "first"}
    assert _evaluation_identity(original) == _evaluation_identity({**original, "confirmed_at": "second"})
    reversed_terms = json.loads(json.dumps(original))
    reversed_terms["maintenance_expenses"][0]["terms"].reverse()
    assert _evaluation_identity(original) != _evaluation_identity(reversed_terms)


def test_public_submit_accepts_supported_affine_and_fixed(client, db) -> None:
    owner = make_user(db, "owner_issue06_gate")
    pid = _prepare_project(client, db, owner)
    rev = _save_config(client, pid, owner)

    def submit(expenses, key):
        return client.post(
            f"/api/projects/{pid}/tasks",
            json={"task_type": "calc", "config": {"config_revision": rev},
                  "idempotency_key": key, "evaluation": _input(expenses)},
            headers=_h(client, owner),
        )

    fixed = submit([{"year": 2, "amount": "7.50", "currency": "CNY"}], "s1-fixed")
    assert fixed.status_code == 201, fixed.text
    affine = submit([_expense(_scale("bat1"))], "s1-affine")
    assert affine.status_code == 201, affine.text
    bad_ref = submit([_expense(_scale("bat1", "ghost"))], "s1-bad-ref")
    assert bad_ref.status_code == 400, bad_ref.text
    assert bad_ref.json()["error"]["code"] == "TASK-REQ-015"
