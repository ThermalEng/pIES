"""Synthetic bound references survive canonical storage and math conversion."""
from __future__ import annotations

import json

import pytest

from iesplan.devices import canonical_bytes, from_dict, parse_device_model_v2, to_dict
from iesplan.modeling import build_math_contribution


@pytest.fixture
def model():
    return {
        "schema": "ies.device-model", "schema_version": "1.0.0",
        "device": {"id": "acme.device.limited_output", "names": {"en-US": "Limited output"}},
        "properties": {
            "floor": {"value": 0, "unit": "kW"},
            "limit": {"value": 8, "unit": "kW", "valid_range": {"minimum": 0, "maximum": None}},
        },
        "interfaces": {
            "output": {"type": "out", "carrier": "electricity", "unit": "kW"},
        },
        "equations": {
            "variables": {
                "flow": {"unit": "kW", "initial": {"property_ref": "floor"}},
                "unbounded": {"unit": "kW", "initial": {"expression": "limit - floor"}},
            },
            "relations": [{"id": "output_flow", "expression": "output[t] = flow[t]"}],
        },
    }


@pytest.mark.parametrize("minimum,maximum", [
    ({"property_ref": "floor"}, {"property_ref": "limit"}),
    (0, {"property_ref": "limit"}),
    ({"property_ref": "floor"}, None),
    (0, 8),
    (None, None),
])
def test_canonical_roundtrip_preserves_bound_semantics(model, minimum, maximum):
    bounds = {"minimum": minimum, "maximum": maximum}
    model["interfaces"]["output"]["valid_range"] = bounds
    model["equations"]["variables"]["flow"]["valid_range"] = bounds
    parsed = parse_device_model_v2(model)
    assert parsed.ok, parsed.diagnostics
    raw = json.loads(canonical_bytes(parsed.document))
    assert raw["interfaces"]["output"]["valid_range"] == bounds
    assert raw["equations"]["variables"]["flow"]["valid_range"] == bounds
    assert "valid_range" not in raw["equations"]["variables"]["unbounded"]

    restored = from_dict(raw)
    assert to_dict(restored) == raw
    assert canonical_bytes(restored) == canonical_bytes(parsed.document)
    assert restored.interfaces["output"] == parsed.document.interfaces["output"]
    assert restored.equations.variables == parsed.document.equations.variables
    assert restored.properties == parsed.document.properties

    math = build_math_contribution(restored)
    assert math.ok, math.diagnostics
    for original, actual in (
        (restored.interfaces["output"], math.contribution.interfaces["output"]),
        (restored.equations.variables["flow"], math.contribution.variables["flow"]),
    ):
        assert actual.minimum_property_ref == original.minimum_property_ref
        assert actual.maximum_property_ref == original.maximum_property_ref
        assert actual.valid_range == original.valid_range


def test_from_dict_restores_reference_bounds_without_reparsing(model):
    bounds = {"minimum": {"property_ref": "floor"}, "maximum": {"property_ref": "limit"}}
    model["interfaces"]["output"]["valid_range"] = bounds
    model["equations"]["variables"]["flow"]["valid_range"] = bounds
    restored = from_dict(model)
    for actual in (restored.interfaces["output"], restored.equations.variables["flow"]):
        assert actual.valid_range == (None, None)
        assert actual.minimum_property_ref == "floor"
        assert actual.maximum_property_ref == "limit"
    assert restored.equations.variables["unbounded"].valid_range is None
