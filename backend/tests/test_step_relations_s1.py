"""S1 跨实例逐步技术约束：仅输入、规范化、持久形状与提交能力门禁。"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from iesplan.assembly import parse_assembly_doc, validate_assembly_doc
from iesplan.core.contracts import Constraint, PlanningConfig, PlanningConfigError
from iesplan.core.yamlmini import dump as yaml_dump, load as yaml_load
from iesplan.application.tasks.submissions import _check_step_constraint_capability
from iesplan.application.tasks.submissions import InvalidRequestError
from iesplan.computation.generators.highs_milp import DESCRIPTOR

SAMPLES = Path(__file__).resolve().parents[1] / "iesplan" / "assembly" / "samples"
VALID = SAMPLES / "valid" / "campus.assembly.yaml"


@pytest.fixture
def doc():
    from iesplan.devices import init_registry
    init_registry()
    parsed = parse_assembly_doc(VALID.read_text(encoding="utf-8"))
    assert parsed.ok, parsed.diagnostics
    return parsed.doc


def _relation(enabled=True):
    return {
        "type": "step_relation",
        "references": {
            "boundary": {"instance": "grid", "interface": "electricity_export"},
            "supply": {"instance": "pv1", "interface": "electric_out"},
        },
        "expression": "boundary[t] <= supply[t]",
        "enabled": enabled,
    }


def _validate(doc):
    return validate_assembly_doc(doc, package_dir=SAMPLES)


def _insert(doc, rel=None):
    doc["planning"]["constraints"]["port_limit"] = _relation() if rel is None else rel
    return doc


def test_contract_roundtrip_and_deep_immutability():
    rel = Constraint.from_dict(_relation(enabled=False))
    assert rel.to_dict() == _relation(enabled=False)
    with pytest.raises(TypeError):
        rel.references["supply"]["interface"] = "other"
    with pytest.raises(TypeError):
        rel.references["other"] = {"instance": "grid", "interface": "electricity_export"}
    with pytest.raises(PlanningConfigError):
        Constraint.from_dict({"type": "step_relation", "expression": "x[t] <= y[t]"})
    with pytest.raises(PlanningConfigError):
        Constraint.from_dict({"type": "ratio", "expression": "1 <= 2", "references": {}})


def test_valid_assembly_canonical_roundtrip(doc):
    original = _insert(doc)
    result = _validate(original)
    assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]
    restored = json.loads(result.artifact.canonical_text)
    assert restored["schema_version"] == "5.0.0"
    assert restored["planning"]["constraints"]["port_limit"] == _relation()
    assert result.artifact.receipt.canonical_algorithm == "ies.assembly.canonical@5.0.0" if hasattr(result.artifact.receipt, "canonical_algorithm") else True
    cfg = PlanningConfig.from_dict(restored["planning"])
    plain = cfg.to_dict()
    assert PlanningConfig.from_dict(yaml_load(yaml_dump(plain))).to_dict() == plain


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("instance", "missing_device", "step_unknown_instance"),
    ("interface", "fake_port", "step_unknown_interface"),
    ("interface", "solar_irradiance", "step_port_not_connectable"),
])
def test_bad_endpoint_rejected_in_public_assembly(doc, field, value, reason):
    rel = _relation()
    rel["references"]["supply"] = dict(rel["references"]["supply"])
    rel["references"]["supply"][field] = value
    result = _validate(_insert(doc, rel))
    assert result.artifact is None
    assert any(d.params.get("reason") == reason and "references.supply" in str(d.location) for d in result.diagnostics)


@pytest.mark.parametrize(("expression", "reason"), [
    ("boundary[t-1] <= supply[t]", "step_invalid_index"),
    ("boundary[0] <= supply[t]", "step_invalid_index"),
    ("boundary <= supply[t]", "step_invalid_index"),
    ("boundary[t] <= 1 kWh", "dimension_mismatch"),
    ("boundary[t] * supply[t] <= supply[t] * 1 kW", "step_expression_invalid"),
    ("boundary[t] < supply[t]", "step_expression_invalid"),
])
def test_invalid_expression_rejected(doc, expression, reason):
    rel = _relation()
    rel["expression"] = expression
    result = _validate(_insert(doc, rel))
    assert result.artifact is None
    assert any(d.params.get("reason") == reason for d in result.diagnostics), [d.to_dict() for d in result.diagnostics]


@pytest.mark.parametrize("alias", ["pv_cap", "step_duration", "qconst1"])
def test_reserved_local_alias_rejected(doc, alias):
    rel = _relation()
    rel["references"][alias] = rel["references"].pop("supply")
    rel["expression"] = f"boundary[t] <= {alias}[t]"
    result = _validate(_insert(doc, rel))
    assert result.artifact is None
    assert any(d.params.get("reason") == "alias_reserved_collision" for d in result.diagnostics)


def test_disabled_relation_preserved_and_no_generator_capability_required(doc):
    rel = _relation(enabled=False)
    result = _validate(_insert(doc, rel))
    assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]
    frozen = json.loads(result.artifact.canonical_text)
    assert frozen["planning"]["constraints"]["port_limit"] == rel
    _check_step_constraint_capability(result.artifact, DESCRIPTOR.ref, DESCRIPTOR.capabilities)


def test_enabled_relation_requires_advertised_capability_and_freezes(doc):
    result = _validate(_insert(doc))
    assert result.artifact is not None
    assert DESCRIPTOR.version == "7.0.0"
    assert DESCRIPTOR.assembly_schema_version == "5.0.0"
    assert "technical.step_constraints" in DESCRIPTOR.capabilities
    _check_step_constraint_capability(result.artifact, DESCRIPTOR.ref, DESCRIPTOR.capabilities)
    # Any other provider without the capability still fails at TASK-REQ-007.
    with pytest.raises(InvalidRequestError) as exc:
        _check_step_constraint_capability(result.artifact, "test.generator@1.0.0", ("affine",))
    assert exc.value.code == "TASK-REQ-007"
    assert exc.value.params["missing_capabilities"] == ["technical.step_constraints"]
    assert "planning.constraints.port_limit" in exc.value.params["fields"]
    before = json.loads(result.artifact.canonical_text)
    doc["planning"]["constraints"]["port_limit"]["expression"] = "boundary[t] == supply[t]"
    assert json.loads(result.artifact.canonical_text) == before


def test_scalar_planning_still_passes_unchanged(doc):
    result = _validate(doc)
    assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]
    assert not any(c.get("type") == "step_relation"
                   for c in json.loads(result.artifact.canonical_text)["planning"]["constraints"].values())


def test_reserved_primitive_cannot_be_shadowed_to_pass_nonlinear_relation(doc):
    """主审复现：错误的装机变量名不允许冒充固定时间步长。"""
    variable = doc["planning"]["variables"].pop("pv_cap")
    doc["planning"]["variables"]["step_duration"] = variable
    relation = _relation()
    relation["expression"] = "boundary[t] * step_duration <= boundary[t] * 1 kW"
    result = _validate(_insert(doc, relation))
    assert result.artifact is None
    assert any(
        d.params.get("reason") == "variable_reserved_collision"
        and "planning.variables.step_duration" in str(d.location)
        for d in result.diagnostics
    ), [d.to_dict() for d in result.diagnostics]
