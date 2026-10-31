"""Source closure through real generation; no private engineering input."""
import json
from dataclasses import replace

import pytest
from test_highs_milp_provider import _campus_inputs
from test_project_model import TANK_YAML, _contribution, _inputs

from iesplan.computation import HIGHS_MILP_PROVIDER, ComputationUnavailableError
from iesplan.computation.generators.highs_milp import HighsMilpProvider
from iesplan.computation.generators.linear import LoweringUnsupportedError
from iesplan.computation.generators.project import _ProjectBuilder, build_project_model_package
from iesplan.computation.generators.sources import SourceTrace
from iesplan.finance import ConstructionPayment


@pytest.fixture(scope="module")
def generated():
    inputs = _campus_inputs()
    model, units, _, _, _, _, sources = build_project_model_package(*inputs)
    bundle = HIGHS_MILP_PROVIDER.generate(*inputs)
    published = json.loads(bundle.files["input/parameters.json"])["model_sources"]
    assert published == sources
    return model, units, published, bundle


def _indices(group):
    value = group["indices"]
    if isinstance(value, list):
        return value
    return [value["start"] + i * value["stride"] for i in range(value["count"])]


def test_every_actual_row_and_column_has_a_frozen_source(generated):
    model, _, sources, _ = generated
    assert sources["schema"] == "ies.model-sources"
    rows, columns = set(), set()
    for group in sources["groups"]:
        assert group["source"] in sources["sources"]
        assert len(_indices(group)) == len(group["targets"])
        (rows if group["role"].startswith("row:") else columns).update(group["targets"])
    assert rows == {row.name for row in model.constraints}
    assert columns == {column.name for column in model.variables}
    for source in sources["sources"].values():
        assert all(ref in sources["sources"] for ref in source["depends_on"])
    symbols = {symbol for source in sources["sources"].values() for symbol in source["outputs"]}
    for source in sources["sources"].values():
        if source["kind"] == "technical":
            assert set(source["inputs"]) <= symbols


def test_technical_capacity_bus_and_transformed_mutex_are_distinct(generated):
    _, _, sources, _ = generated
    kinds = {entry["kind"] for entry in sources["sources"].values()}
    assert {"technical", "bus", "cost", "tariff", "objective"} <= kinds
    mutex = next(group for group in sources["groups"] if group["role"] == "column:mutex")
    source = sources["sources"][mutex["source"]]
    assert source["location"]["instance_id"]
    assert source["location"]["relation_id"]
    assert set(_indices(mutex)) == set(range(8760))
    assert "expression" not in source  # no repeated model document/AST payload


def test_series_components_and_cash_use_explicit_source_references(generated):
    _, _, sources, _ = generated
    assert any(out["path"][:1] == ["series"] for out in sources["outputs"])
    assert any(out["path"][:2] == ["solution", "components"] for out in sources["outputs"])
    assert any(out["path"] == ["financial_facts", "annual_net_flows"] for out in sources["outputs"])
    for output in sources["outputs"]:
        assert all(ref in sources["sources"] for ref in output["sources"])


def test_late_actual_coefficient_failure_preserves_source_and_index():
    artifact, models, materialized, finance, config, evaluation = _inputs()
    models["tank1"] = _contribution(TANK_YAML.replace(
        "soc[t-1] + flow[t] * step_c", "soc[t-1] + step_c * (flow[t] / flow[t])",
    ))
    series = []
    for entry in materialized.series:
        if entry.device == "tank1":
            values = list(entry.values)
            values[256] = 0.0
            entry = replace(entry, values=tuple(values), source={"object_id": "frozen-test-series"})
        series.append(entry)
    materialized = replace(materialized, series=tuple(series))
    with pytest.raises(LoweringUnsupportedError, match="除零") as raised:
        HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
    error = raised.value
    assert isinstance(error, ComputationUnavailableError)
    assert error.reason == "unsupported-model"
    assert error.model_source["index"] == 256
    assert error.model_source["axis"] == "step"
    assert error.model_source["location"]["instance_id"] == "tank1"
    assert error.model_source["location"]["model_id"] == "acme.device.tank"
    assert error.model_source["location"]["relation_id"] == "soc_step"
    assert error.model_source["coefficients"]["tank1.flow"]["data_object_ref"] == "frozen-test-series"


def test_unexpected_programming_error_is_not_relabelled(monkeypatch):
    def bug(*args):
        raise RuntimeError("synthetic programming failure")
    monkeypatch.setattr(_ProjectBuilder, "_build_relations", bug)
    with pytest.raises(RuntimeError, match="programming failure") as raised:
        HIGHS_MILP_PROVIDER.generate(*_inputs())
    assert not isinstance(raised.value, ComputationUnavailableError)


def test_source_indices_are_compact_without_losing_irregular_periods():
    assert SourceTrace._indices([0, 1, 2]) == {"start": 0, "stride": 1, "count": 3}
    assert SourceTrace._indices([4, 4]) == {"start": 4, "stride": 0, "count": 2}
    assert SourceTrace._indices([0, 0, 1, 1]) == [0, 0, 1, 1]


def test_bundle_source_bytes_are_deterministic(generated):
    _, _, _, original = generated
    repeated = HIGHS_MILP_PROVIDER.generate(*_campus_inputs())
    assert dict(original.files) == dict(repeated.files)


def test_same_time_payments_retain_distinct_frozen_positions():
    inputs = list(_campus_inputs())
    inputs[-1] = replace(inputs[-1], construction_payments=(
        ConstructionPayment(t=0, ratio="0.6"), ConstructionPayment(t=0, ratio="0.4"),
    ))
    _, _, _, facts, _, _, sources = build_project_model_package(*inputs)
    assert len(facts["payments_applied"]) == 2
    refs = [entry for entry in sources["outputs"]
            if entry["path"][:2] == ["financial_facts", "payments_applied"]]
    assert len(refs) == 2 and refs[0]["sources"] != refs[1]["sources"]
    assert [sources["sources"][entry["sources"][0]]["location"]["field_path"]
            for entry in refs] == ["evaluation.construction_payments.0", "evaluation.construction_payments.1"]


def test_same_model_instances_and_cross_year_state_have_separate_origins():
    from test_p1_s3_multiscale import _full_two_year_builder
    builder = _full_two_year_builder(_ProjectBuilder, block_size=127)
    builder.build()
    sources = builder.model_sources()
    for inst in ("a", "b"):
        source = sources["sources"][f"technical:{inst}.soc_step"]
        assert source["location"]["instance_id"] == inst
        assert source["depends_on"] == [f"relation:{inst}.soc.initial"]
        group = next(g for g in sources["groups"] if g["source"] == f"technical:{inst}.soc_step")
        assert group["targets"][8760] == f"{inst}__soc_step__t8760"
        assert _indices(group)[8760] == 8760


@pytest.mark.parametrize("reason", ["cancelled", "fenced"])
def test_stop_during_source_serialization_does_not_publish_bundle(monkeypatch, reason):
    stopped = False
    original = HighsMilpProvider._metadata

    def serialize(*args, **kwargs):
        nonlocal stopped
        metadata = original(*args, **kwargs)
        stopped = True
        return metadata

    monkeypatch.setattr(HighsMilpProvider, "_metadata", serialize)
    with pytest.raises(ComputationUnavailableError) as raised:
        HIGHS_MILP_PROVIDER.generate_with_stop(*_campus_inputs(), stop=lambda: reason if stopped else None)
    assert raised.value.reason == reason
