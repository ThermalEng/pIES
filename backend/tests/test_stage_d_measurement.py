"""Small diagnostic arithmetic tests; no annual benchmark on pytest import."""
import json

from stage_d_measurement import matrix_summary, public_inputs

from iesplan.computation.generators.linear import (
    LinearConstraint,
    LinearExpression,
    LinearModel,
    LinearVariable,
)


def test_matrix_counts_and_explicit_sources_do_not_imply_zero_cost_binary_removal():
    z, y = LinearVariable("z", 0, 1, "binary"), LinearVariable("y", 0, 2)
    link = LinearConstraint("link", LinearExpression(terms=((("y", 0), 1.), (("z", 0), -2.))), "le", 0.)
    model = LinearModel((z, y), (link,), LinearExpression(terms=((("y", 0), 1.),)), "maximize")
    sources = {
        "sources": {"explicit_relation": {"kind": "technical"}},
        "groups": [
            {"source": "explicit_relation", "role": "column:binary", "targets": ["z"]},
            {"source": "explicit_relation", "role": "row:link", "targets": ["link"]},
        ],
    }
    before = model.to_dict()
    out = matrix_summary(model, sources)
    assert out["columns"] == 2 and out["rows"] == 1 and out["row_nonzeros"] == 2
    assert out["binary_columns"] == 1
    assert out["binary_by_source_kind"] == {"technical": 1}
    assert out["binary_without_source_mapping"] == 0
    assert out["binary_without_direct_objective_coefficient"] == 1
    assert out["densest_rows"] == [{"row": "link", "nonzeros": 2, "source": "explicit_relation"}]
    assert out["row_nonzero_histogram"] == {"0..8": 1}
    assert model.to_dict() == before


def test_explicit_budget_input_variant_does_not_change_devices_or_time_resolution():
    original, _, materialized, _, _, evaluation = public_inputs(2)
    alternative = public_inputs(2, purchase_budget_factor=2)[0]
    a, b = json.loads(original.canonical_text), json.loads(alternative.canonical_text)
    assert a["planning"]["constraints"]["annual_purchase_cap"]["expression"].endswith("900000 CNY")
    assert b["planning"]["constraints"]["annual_purchase_cap"]["expression"].endswith("1800000 CNY")
    del a["planning"]["constraints"]["annual_purchase_cap"]
    del b["planning"]["constraints"]["annual_purchase_cap"]
    assert a == b
    assert evaluation.operation_years == 2
    assert materialized.operation_years == 2
    assert {series.point_count for series in materialized.series} == {17520}
