"""Test-only exact-vs-on-demand mutex measurements on explicit small MILPs."""
from __future__ import annotations

import pytest

from iesplan.computation.generators.linear import (
    LinearConstraint, LinearExpression, LinearModel, LinearVariable,
)
from p2_mutex_measurement import compare_mutex_model


def _affine(**coefficients):
    return LinearExpression(
        terms=tuple(((key, 0), value) for key, value in sorted(coefficients.items())),
        constant=0.0,
    )


def _model(*, fixed_x: bool = False, infeasible: bool = False) -> LinearModel:
    rows = [
        LinearConstraint("pair__mutex_x", _affine(x=1, pair__mutex=-1), "le", 0),
        LinearConstraint("pair__mutex_y", _affine(y=1, pair__mutex=1), "le", 1),
    ]
    if fixed_x:
        rows.append(LinearConstraint("fix_x", _affine(x=1), "eq", 0))
    if infeasible:
        rows.extend((
            LinearConstraint("require_x", _affine(x=1), "ge", 1),
            LinearConstraint("require_y", _affine(y=1), "ge", 1),
        ))
    return LinearModel(
        variables=(
            LinearVariable("x", 0, 1, "continuous"),
            LinearVariable("y", 0, 1, "continuous"),
            LinearVariable("pair__mutex", 0, 1, "binary"),
        ),
        constraints=tuple(rows),
        objective=_affine(x=1, y=1),
        sense="maximize",
    )


def test_relaxation_already_satisfies_original_mutex(tmp_path):
    original = _model(fixed_x=True)
    result = compare_mutex_model(original, tmp_path)
    assert result.full_status == "HighsModelStatus.kOptimal"
    assert result.candidate_status == "HighsModelStatus.kOptimal"
    assert result.candidate_all_relations_feasible is True
    assert result.candidate_resolves == 1
    assert result.candidate_integers_added == 0
    assert result.full_integers == 1
    assert result.full_best_bound == pytest.approx(1)
    assert result.candidate_best_bound == pytest.approx(1)
    assert result.full_mps_bytes > 0 and result.candidate_mps_bytes > 0
    assert result.recommendation == "not_evaluated"


def test_relaxation_violates_original_mutex_and_requires_resolve(tmp_path):
    original = _model()
    result = compare_mutex_model(original, tmp_path)
    assert result.full_status == "HighsModelStatus.kOptimal"
    assert result.candidate_status == "HighsModelStatus.kOptimal"
    assert result.candidate_all_relations_feasible is True
    assert result.candidate_resolves == 2
    assert result.candidate_integers_added == 1
    assert result.full_best_bound == pytest.approx(1)
    assert result.candidate_best_bound == pytest.approx(1)
    assert result.candidate_peak_columns == 3


def test_genuinely_infeasible_original_not_reported_feasible(tmp_path):
    result = compare_mutex_model(_model(infeasible=True), tmp_path)
    assert result.full_status == "HighsModelStatus.kInfeasible"
    assert result.full_best_bound is None
    assert result.candidate_resolves == 2
    assert result.candidate_status == "HighsModelStatus.kInfeasible"
    assert result.candidate_all_relations_feasible is False
    assert result.candidate_best_bound is None
    assert result.recommendation == "not_evaluated"
