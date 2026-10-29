"""S2 synthetic same-solve step constraints: solver rows, port identity, units and HiGHS."""
from __future__ import annotations

import json
from copy import deepcopy

import pytest

from iesplan.assembly import validate_assembly_doc
from iesplan.computation.generators.highs_milp import HIGHS_MILP_PROVIDER
from iesplan.computation.generators.project import build_project_model_package
from iesplan.computation.generators.mps import write_mps
from test_computation_chain import (
    SAMPLES, _adapt, _attempt, _campus_inputs, _runtime,
)


@pytest.fixture(scope="module")
def campus():
    return _campus_inputs()


def _relation(expression, *, enabled=True, references=None):
    return {
        "type": "step_relation",
        "references": references or {
            "boundary": {"instance": "grid", "interface": "electricity_export"},
            "supply": {"instance": "pv1", "interface": "electric_out"},
        },
        "expression": expression,
        "enabled": enabled,
    }


def _with_constraints(campus, constraints):
    artifact, models, materialized, finance, config, evaluation = campus
    doc = json.loads(artifact.canonical_text)
    doc["planning"]["constraints"].update(deepcopy(constraints))
    validated = validate_assembly_doc(doc, package_dir=SAMPLES)
    assert validated.artifact is not None, [
        (d.code, d.params) for d in validated.diagnostics if d.blocking
    ]
    return validated.artifact, models, materialized, finance, config, evaluation


def _model(inputs):
    return build_project_model_package(*inputs)


def _terms(row):
    return {name: coeff for (name, offset), coeff in row.expression.terms if offset == 0}


def _rows(model, relation_id):
    return [r for r in model.constraints if r.name.startswith(f"step__{relation_id}__t")]


def test_full_horizon_math_uses_same_ports_and_scalar_capacity(campus):
    model, units, _, _, _, series = _model(_with_constraints(campus, {
        "limit": _relation("boundary[t] + 0.5 kW <= pv_cap"),
        "equal": _relation("boundary[t] == supply[t]"),
        "floor": _relation("boundary[t] >= 500 W"),
    }))
    n = len(next(s["columns"] for s in series if s["series_id"] == "grid.electricity_export"))
    assert n >= 8760
    for ident in ("limit", "equal", "floor"):
        assert len(_rows(model, ident)) == n  # includes final step and calendar boundary
    limit = _rows(model, "limit")
    assert limit[0].sense == "le"
    assert _terms(limit[0]) == {"grid__electricity_export__t0": 1, "pv_cap": -1}
    assert limit[0].expression.constant == pytest.approx(500)
    final_row = next(r for r in limit if r.name == f"step__limit__t{n - 1}")
    assert _terms(final_row) == {f"grid__electricity_export__t{n - 1}": 1, "pv_cap": -1}
    assert _terms(_rows(model, "equal")[0]) == {
        "grid__electricity_export__t0": 1, "pv1__electric_out__t0": -1,
    }
    assert _rows(model, "equal")[0].sense == "eq"
    assert _rows(model, "floor")[0].sense == "ge"
    assert _rows(model, "floor")[0].expression.constant == pytest.approx(-500)
    assert units["grid__electricity_export__t0"] == "kW"
    assert units["pv_cap"] == "kWp"


def test_compatible_unit_literals_have_identical_math(campus):
    lhs = _model(_with_constraints(campus, {"a": _relation("boundary[t] <= 0.5 kW")}))
    rhs = _model(_with_constraints(campus, {"a": _relation("boundary[t] <= 500 W")}))
    assert write_mps(lhs[0]) == write_mps(rhs[0])


def test_disabled_relation_is_mathematically_identical_to_absent(campus):
    no_relation = _model(campus)
    disabled = _model(_with_constraints(campus, {
        "disabled": _relation("boundary[t] >= 999 MW", enabled=False),
    }))
    assert no_relation[0].to_dict() == disabled[0].to_dict()
    assert no_relation[1:] == disabled[1:]
    assert write_mps(no_relation[0]) == write_mps(disabled[0])


def test_same_type_instances_are_not_cross_wired(campus):
    artifact, models, materialized, finance, config, evaluation = campus
    doc = json.loads(artifact.canonical_text)
    doc["devices"]["grid2"] = deepcopy(doc["devices"]["grid"])
    doc["finance_binding"]["instances"]["grid2"] = {"type": "none"}
    doc["planning"]["constraints"]["only_grid2"] = _relation(
        "boundary[t] <= supply[t]",
        references={
            "boundary": {"instance": "grid2", "interface": "electricity_export"},
            "supply": {"instance": "pv1", "interface": "electric_out"},
        },
    )
    result = validate_assembly_doc(doc, package_dir=SAMPLES)
    assert result.artifact is not None, [
        (d.code, d.params) for d in result.diagnostics if d.blocking
    ]
    augmented = dict(models)
    augmented["grid2"] = models["grid"]
    model = _model((result.artifact, augmented, materialized, finance, config, evaluation))[0]
    terms = _terms(_rows(model, "only_grid2")[0])
    assert terms == {
        "grid2__electricity_export__t0": 1,
        "pv1__electric_out__t0": -1,
    }
    assert "grid__electricity_export__t0" not in terms


def _solve(inputs, suffix):
    bundle = HIGHS_MILP_PROVIDER.generate(*inputs)
    receipt, outputs = _runtime().run(bundle, _attempt(suffix))
    assert receipt.status == "succeeded", receipt
    return _adapt(bundle, receipt, outputs)


def test_true_highs_changes_optimum_and_same_solution_series(campus):
    baseline = _solve(campus, "step-base")
    constrained = _solve(_with_constraints(campus, {
        "min_capacity": _relation("boundary[t] + 25 kW <= pv_cap"),
    }), "step-enforced")
    assert baseline.math_status == constrained.math_status == "solved"
    def capacity(result):
        return next(v["value"] for v in result.outputs["solution"]["variables"] if v["name"] == "pv_cap")
    assert capacity(baseline) == pytest.approx(0, abs=1e-5)
    cap = capacity(constrained)
    assert cap >= 25 - 1e-5
    assert cap > capacity(baseline) + 24.9
    values = constrained.outputs["series"]["grid.electricity_export"]["values"]
    assert len(values) >= 8760
    assert all(value + 25 <= cap + 1e-5 for value in values)
    assert values[0] == pytest.approx(next(
        v["value"] for v in constrained.outputs["solution"]["variables"]
        if v["name"] == "grid__electricity_export__t0"
    ))


def test_true_highs_keeps_conflicting_rows_and_reports_infeasible(campus):
    inputs = _with_constraints(campus, {
        "zero": _relation("boundary[t] == 0 kW"),
        "positive": _relation("boundary[t] >= 1 kW"),
    })
    model = _model(inputs)[0]
    assert _rows(model, "zero") and _rows(model, "positive")
    result = _solve(inputs, "step-conflict")
    assert result.math_status == "infeasible"
    assert result.execution_status == "completed"
    assert dict(result.outputs) == {}
