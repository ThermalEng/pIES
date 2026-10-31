"""C route: declared settlement direction is independent of a device's port direction.

All input is synthetic.  The assembly gate, generated financial components,
per-step tariffs, annual cash and sealed-result declarations share one binding.
"""

from __future__ import annotations

from copy import deepcopy

import pytest
from test_npv_objective import (
    GRID_YAML,
    HEATER_YAML,
    LOAD_YAML,
    TANK_YAML,
    _contribution,
    _doc,
    _evaluation,
    _finance_payload,
)

from iesplan.assembly import validate_assembly_doc
from iesplan.computation import CalculationConfig, materialize_non_predictive_series
from iesplan.computation.generators.highs_milp import GENERATOR_REF
from iesplan.computation.generators.project import build_project_model_package
from iesplan.core.yamlmini import dump as yaml_dump, load as yaml_load
from iesplan.devices import init_registry
from iesplan.devices.parser2 import parse_device_model_v2
from iesplan.finance import FinanceProfile, merge_effective


def _case(interface: str, direction: str, price: str = "0.7", *, bad_unit=False,
          negative_port=False, property_floor=None, planned_floor=False,
          planning_lower="0"):
    carrier = "heat" if interface == "heat_out" else "electricity"
    doc = _doc(expression=f"meter__period_energy_{direction}", sense="minimize")
    doc["tariff_bindings"] = {
        "meter": {
            "price": "meter_price",
            "instance": "heater1",
            "interface": interface,
            "aggregation": "integrate_positive",
        }
    }
    payload = deepcopy(_finance_payload())
    payload["energy_prices"] = {
        "meter_price": {
            "carrier": carrier,
            "direction": direction,
            "kind": "constant",
            "value": {"value": price, "unit": "CNY/kW" if bad_unit else "CNY/kWh"},
        }
    }
    finance = merge_effective(FinanceProfile.from_dict(payload), None)
    device_yamls = {
        "heater1": HEATER_YAML,
        "tank1": TANK_YAML,
        "load1": LOAD_YAML,
        "grid1": GRID_YAML,
    }
    if negative_port:
        device_yamls["heater1"] = HEATER_YAML.replace(
            "heat_out: {type: out, carrier: heat, unit: kW, valid_range: {minimum: 0,",
            "heat_out: {type: out, carrier: heat, unit: kW, valid_range: {minimum: -1,",
        )
        assert device_yamls["heater1"] != HEATER_YAML
    if property_floor is not None:
        heater = yaml_load(device_yamls["heater1"])
        heater["properties"]["flow_floor"] = {
            "value": 0, "unit": "kW",
            "valid_range": {"minimum": -10, "maximum": 10},
        }
        heater["interfaces"]["heat_out"]["valid_range"]["minimum"] = {
            "property_ref": "flow_floor",
        }
        device_yamls["heater1"] = yaml_dump(heater)
        doc["devices"]["heater1"]["properties"]["flow_floor"] = {
            "value": property_floor, "unit": "kW",
        }
        if planned_floor:
            doc["planning"]["variables"]["floor_plan"] = {
                "device_ref": "heater1", "parameter": "flow_floor",
                "unit": "kW", "lower_bound": planning_lower,
                "upper_bound": "1",
            }
    documents = {}
    for yaml_text in device_yamls.values():
        result = parse_device_model_v2(yaml_load(yaml_text), file="synthetic.yaml")
        assert result.ok, result.diagnostics
        documents[result.document.device.id] = result.document
    init_registry()
    checked = validate_assembly_doc(
        doc,
        finance_snapshots={"obj-finance": finance},
        device_documents=documents,
    )
    return checked, finance, device_yamls


@pytest.mark.parametrize("interface", ["heat_out", "electricity_in"])
@pytest.mark.parametrize("direction", ["purchase", "sale"])
def test_four_port_and_settlement_combinations_reach_cashflow(interface, direction):
    checked, finance, yamls = _case(interface, direction)
    assert checked.artifact is not None, [d.to_dict() for d in checked.diagnostics]

    models = {name: _contribution(text) for name, text in yamls.items()}
    materialized = materialize_non_predictive_series(
        checked.artifact, {}, operation_years=2
    )
    config = CalculationConfig(
        mode="planning", generator=GENERATOR_REF, solver="ies.solver.highs@1.7.2",
        precision="medium", time_limit_seconds=600, outputs=["solution"],
    )
    model, _, unit, facts, components, _, _ = build_project_model_package(
        checked.artifact, models, materialized, finance, config, _evaluation()
    )
    assert unit == "CNY"
    name = f"meter__period_energy_{direction}"
    assert name in components
    assert f"meter__period_energy_{'sale' if direction == 'purchase' else 'purchase'}" not in components
    sign = 1 if direction == "purchase" else -1
    # 1h kW → kWh: LP watts×0.0007 CNY/W; zero/negative price handled below.
    expected = sign * 0.0007
    target = f"heater1__{interface}__t0"
    own_terms = components[name]["terms"]
    assert len(own_terms) == 17520
    assert own_terms[target] == pytest.approx(expected)
    assert all(key.startswith(f"heater1__{interface}__t") for key in own_terms)
    assert dict(model.objective.terms)[(target, 0)] == pytest.approx(expected)

    first_year = facts["annual_operation"][0]
    assert first_year["financial_year"] == 2
    assert first_year["energy_net"]["terms"][target] == pytest.approx(-expected)
    assert len(first_year["energy_net"]["terms"]) == 8760
    # Even if an entirely different input is nonzero, no implicit 'all inputs' sale.
    other = "electricity_in" if interface == "heat_out" else "heat_out"
    assert all(not key.startswith(f"heater1__{other}__") for key in own_terms)
    # Cash contains this period's net, including any independently priced O&M.
    assert facts["annual_net_flows"][2]["terms"][target] == pytest.approx(
        first_year["net"]["terms"][target]
    )


@pytest.mark.parametrize(("price", "expected"), [("1.2", -0.0012), ("0", 0), ("-0.4", 0.0004)])
def test_source_out_sale_keeps_signed_positive_zero_negative_prices(price, expected):
    checked, finance, yamls = _case("heat_out", "sale", price)
    assert checked.artifact is not None, [d.to_dict() for d in checked.diagnostics]
    models = {name: _contribution(text) for name, text in yamls.items()}
    materialized = materialize_non_predictive_series(checked.artifact, {}, operation_years=2)
    config = CalculationConfig(
        mode="planning", generator=GENERATOR_REF, solver="ies.solver.highs@1.7.2",
        precision="medium", time_limit_seconds=600, outputs=["solution"],
    )
    _, _, _, facts, components, _, _ = build_project_model_package(
        checked.artifact, models, materialized, finance, config, _evaluation()
    )
    source = components["meter__period_energy_sale"]
    assert source["terms"].get("heater1__heat_out__t0", 0) == pytest.approx(expected)
    assert facts["annual_operation"][0]["energy_net"]["terms"].get(
        "heater1__heat_out__t0", 0
    ) == pytest.approx(-expected)


@pytest.mark.parametrize(
    ("options", "reason"),
    [({"bad_unit": True}, "meter_price_unit_mismatch"),
     ({"negative_port": True}, "interface_not_nonnegative")],
)
def test_direction_freedom_does_not_waive_meter_guards(options, reason):
    checked, _, _ = _case("heat_out", "sale", **options)
    assert checked.artifact is None
    assert any(
        d.code == "ASM-TAR-001" and d.params.get("reason") == reason
        for d in checked.diagnostics
    )


@pytest.mark.parametrize(
    ("old_floor", "planned", "lower", "accepted"),
    [(-1, False, "0", False), (0, False, "0", True),
     (-1, True, "0", True), (0, True, "0", True),
     (-1, True, None, True)],
)
def test_property_ref_meter_uses_effective_planning_floor(
    old_floor, planned, lower, accepted,
):
    """A legal negative old property loses authority only after planning takeover."""
    checked, finance, yamls = _case(
        "heat_out", "sale", property_floor=old_floor,
        planned_floor=planned, planning_lower=lower,
    )
    if not accepted:
        assert checked.artifact is None
        assert any(
            d.code == "ASM-TAR-001"
            and d.params.get("reason") == "interface_not_nonnegative"
            for d in checked.diagnostics
        )
        return
    assert checked.artifact is not None, [d.to_dict() for d in checked.diagnostics]
    if not planned:
        return

    models = {name: _contribution(text) for name, text in yamls.items()}
    materialized = materialize_non_predictive_series(
        checked.artifact, {}, operation_years=2,
    )
    config = CalculationConfig(
        mode="planning", generator=GENERATOR_REF, solver="ies.solver.highs@1.7.2",
        precision="medium", time_limit_seconds=600, outputs=["solution"],
    )
    model, _, _, _, _, _, _ = build_project_model_package(
        checked.artifact, models, materialized, finance, config, _evaluation(),
    )
    variables = {var.name: var for var in model.variables}
    assert variables["floor_plan"].lower == 0
    assert variables["floor_plan"].upper == 1000
    # Missing explicit planning lower keeps the generator's step bound None,
    # with the nonnegative planned capacity enforced by its linked row.
    assert variables["heater1__heat_out__t0"].lower == (0 if lower is not None else None)
    rows = {row.name: row for row in model.constraints}
    linked = rows["heater1__heat_out__plan_min__t0"]
    assert (linked.sense, linked.rhs) == ("ge", 0)
    assert dict(linked.expression.terms) == {
        ("heater1__heat_out__t0", 0): 1.0, ("floor_plan", 0): -1.0,
    }
