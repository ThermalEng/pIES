"""GenerationInputs 生成输入边界契约测试(0.8f-A)。

覆盖深度不可变、确定性顺序、类型/结构边界与确定性序列化。
"""

from __future__ import annotations

from collections import Counter

import pytest  # type: ignore[import-untyped]

from iesplan.computation import GenerationInputs
from iesplan.core.contracts import (
    PlanningVariable,
    build_planning_program,
    compile_planning_expression,
)
from iesplan.core.yamlmini import load as yaml_load
from iesplan.devices.parser2 import parse_device_model_v2
from iesplan.finance import FinanceProfile, merge_effective
from iesplan.modeling import build_math_contribution

DEVICE_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.heater, names: {zh-CN: 加热器, en-US: Heater}}
properties:
  efficiency: {value: 0.9, unit: "1", valid_range: {minimum: 0, maximum: 1}}
interfaces:
  electricity_in: {type: in, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
  heat_out: {type: out, carrier: heat, unit: kW, valid_range: {minimum: 0, maximum: null}}
equations:
  variables: {}
  relations:
    - id: conversion
      expression: "heat_out[t] = electricity_in[t] * efficiency"
"""

FINANCE_PAYLOAD = {
    "schema": "ies.finance-profile",
    "schema_version": "1.0.0",
    "profile": {
        "id": "cn-north-demo",
        "region": "CN-North",
        "currency": "CNY",
        "base_year": 2025,
        "price_basis": "tax_inclusive",
        "cost_method": "fixed_plus_linear",
    },
    "finance_types": {
        "pv_system": {
            "upfront_capex": {"fixed": {"value": "0", "unit": "CNY"}},
        }
    },
    "energy_prices": {
        "grid_import": {
            "carrier": "electricity",
            "direction": "purchase",
            "kind": "constant",
            "value": {"value": "0.7", "unit": "CNY/kWh"},
        }
    },
}

SYMBOLS = {"pv1__upfront_capex": Counter({"currency": 1})}


def _finance():
    return merge_effective(FinanceProfile.from_dict(FINANCE_PAYLOAD), None)


def _contribution():
    result = parse_device_model_v2(yaml_load(DEVICE_YAML), file="test.yaml")
    assert result.ok, result.diagnostics
    built = build_math_contribution(result.document)
    assert built.ok, built.diagnostics
    return built.contribution


def _program():
    objective = compile_planning_expression("pv1__upfront_capex", SYMBOLS)
    return build_planning_program(
        sense="minimize",
        objective=objective,
        variables={
            "pv_cap": PlanningVariable(device_ref="pv1", parameter="rated_capacity_kwp")
        },
        variable_dims={"pv_cap": Counter({"power": 1})},
    )


def _inputs(**order):
    resources = {"obj-b": b"\x02", "obj-a": b"\x01"}
    contributions = {"heater1": _contribution()}
    if order.get("reversed"):
        resources = dict(reversed(list(resources.items())))
    return GenerationInputs(
        resources=resources,
        contributions=contributions,
        effective_finance=_finance(),
        planning_program=_program(),
    )


def test_inputs_freeze_and_serialize_deterministically() -> None:
    first = _inputs().to_dict()
    second = _inputs(reversed=True).to_dict()
    assert first == second
    assert list(first["resources"]) == ["obj-a", "obj-b"]
    assert first["resources"]["obj-a"] == b"\x01".hex()
    assert first["effective_finance"]["currency"] == "CNY"


def test_inputs_deeply_immutable() -> None:
    inputs = _inputs()
    with pytest.raises((TypeError, AttributeError)):
        inputs.resources["obj-c"] = b"\x03"  # type: ignore[index]
    with pytest.raises((TypeError, AttributeError)):
        inputs.resources = {}  # type: ignore[assignment]


def test_inputs_type_boundaries() -> None:
    good = _inputs()
    with pytest.raises(TypeError):
        GenerationInputs(
            resources={"obj-a": "not-bytes"},  # type: ignore[dict-item]
            contributions=good.contributions,
            effective_finance=good.effective_finance,
            planning_program=good.planning_program,
        )
    with pytest.raises(TypeError):
        GenerationInputs(
            resources=good.resources,
            contributions={"heater1": object()},  # type: ignore[dict-item]
            effective_finance=good.effective_finance,
            planning_program=good.planning_program,
        )
    with pytest.raises(TypeError):
        GenerationInputs(
            resources=good.resources,
            contributions=good.contributions,
            effective_finance=object(),  # type: ignore[arg-type]
            planning_program=good.planning_program,
        )
    assert good.planning_program.variables["pv_cap"].lower_bound is None
