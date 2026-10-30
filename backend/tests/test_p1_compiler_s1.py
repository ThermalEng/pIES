"""P1-S1: reproducible Docker baseline and public generator/adapter regressions.

Run (only inside the repository's existing Docker test container):
    python -m pytest -vv -ra --log-cli-level=WARNING tests/test_p1_compiler_s1.py
The P1_S1_BASELINE JSON log is a measured one-year synthetic input, not a
claimed before/after improvement or a customer case.
"""
from __future__ import annotations

import copy
from dataclasses import replace
import json
import logging
import resource
import time
import tracemalloc
from unittest.mock import patch

import pytest

from iesplan.assembly.contracts import ValidatedAssemblyArtifact, ValidationReceipt
from iesplan.computation import MaterializedSet, build_registry, write_mps
from iesplan.computation.generators import highs_milp as generator
from iesplan.computation.generators import project
from iesplan.computation.result_adapters.highs import HIGHS_ADAPTER, _mps_column_names
from iesplan.computation.sequences import materialize_non_predictive_series

from test_highs_milp_provider import _campus_inputs
from test_highs_result_adapter import _receipt
from test_project_model import _inputs, _contribution, HEATER_YAML, POINTS


@pytest.fixture(scope="module")
def measured_generate():
    """One generate, instrumented at the actual build/writer/metadata boundaries."""
    inputs = _campus_inputs()
    stages = {}
    captured = {}
    # reset_peak() is useful for stage-local peaks but erases its previous
    # high water. Preserve each interval high water before every reset and
    # aggregate them to cover the *entire* generate call.
    memory_peaks = {"whole_generate": 0}
    original_new = project._new_builder
    original_build = generator.build_project_model_package
    original_writer = generator.write_mps
    original_metadata = generator.HighsMilpProvider._metadata

    def capture_builder(*args, **kwargs):
        builder = original_new(*args, **kwargs)
        captured["builder"] = builder
        return builder

    def measure(label, fn):
        def wrapped(*args, **kwargs):
            # Capture uninstrumented work between the previous stage and this
            # one before restarting a stage-local high-water interval.
            _current, preceding_peak = tracemalloc.get_traced_memory()
            memory_peaks["whole_generate"] = max(
                memory_peaks["whole_generate"], preceding_peak,
            )
            tracemalloc.reset_peak()
            start = time.perf_counter()
            result = fn(*args, **kwargs)
            elapsed_ms = (time.perf_counter() - start) * 1000
            _current, peak = tracemalloc.get_traced_memory()
            memory_peaks["whole_generate"] = max(
                memory_peaks["whole_generate"], peak,
            )
            stages[label] = {
                "wall_ms": round(elapsed_ms, 3),
                "traced_peak_bytes": peak,
            }
            if label == "model_and_recovery_declarations":
                captured["model"] = result[0]
            return result
        return wrapped

    tracemalloc.start()
    try:
        start = time.perf_counter()
        with (
            patch.object(project, "_new_builder", capture_builder),
            patch.object(generator, "build_project_model_package",
                         measure("model_and_recovery_declarations", original_build)),
            patch.object(generator, "write_mps",
                         measure("mps_write", original_writer)),
            patch.object(generator.HighsMilpProvider, "_metadata",
                         measure("metadata_serialization", original_metadata)),
        ):
            bundle = generator.HIGHS_MILP_PROVIDER.generate(*inputs)
        total_ms = (time.perf_counter() - start) * 1000
        _current, last_interval_peak = tracemalloc.get_traced_memory()
        total_peak = max(memory_peaks["whole_generate"], last_interval_peak)
    finally:
        tracemalloc.stop()

    model = captured["model"]
    from iesplan.computation import export_bundle_files
    files = export_bundle_files(bundle)
    baseline = {
        "fixture": "assembly/samples/valid/campus.assembly.yaml (public synthetic)",
        "measurement_note": "wall time includes tracemalloc tracing overhead",
        "rss_note": "ru_maxrss is the cumulative Pytest-process high-water mark, not an isolated generate RSS",
        "generator": generator.GENERATOR_REF,
        "points": captured["builder"].point_count,
        "columns": len(model.variables),
        "rows": len(model.constraints),
        "row_nonzeros": sum(len(r.expression.terms) for r in model.constraints),
        "objective_nonzeros": len(model.objective.terms),
        "mps_bytes": len(bundle.files[generator.PROBLEM_PATH]),
        "metadata_bytes": len(bundle.files[generator.PARAMETERS_PATH]),
        "bundle_bytes": sum(len(blob) for blob in files.values()),
        "total_wall_ms": round(total_ms, 3),
        "bundle_assembly_and_checks_ms": round(
            max(0, total_ms - sum(v["wall_ms"] for v in stages.values())), 3,
        ),
        "full_generate_traced_peak_bytes": total_peak,
        "pytest_process_cumulative_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "stages": stages,
    }
    logging.warning("P1_S1_BASELINE %s", json.dumps(baseline, sort_keys=True))
    return bundle, captured["builder"], baseline


def test_reproducible_generator_measurement(measured_generate):
    bundle, builder, data = measured_generate
    assert data["points"] == 8760
    assert data["rows"] and data["columns"] and data["row_nonzeros"]
    assert data["mps_bytes"] == len(bundle.files[generator.PROBLEM_PATH])
    assert set(data["stages"]) == {
        "model_and_recovery_declarations", "mps_write", "metadata_serialization",
    }
    assert data["total_wall_ms"] > 0
    assert data["full_generate_traced_peak_bytes"] >= max(
        stage["traced_peak_bytes"] for stage in data["stages"].values()
    )
    assert "assembly_sha256" not in data
    assert builder.inventory.relations  # AST-based, not deferred builder callbacks


def test_source_inventory_is_inspectable_and_unpersisted(measured_generate):
    bundle, builder, _ = measured_generate
    inventory = builder.inventory
    assert inventory.symbols and inventory.outputs and inventory.relations
    assert len(inventory.property_bindings) >= 1
    for family in inventory.relations:
        assert family.domain.kind == "step"
        assert family.domain.count == builder.point_count
        assert family.source.instance_id in builder.devices
        assert family.source.model_id == builder.models[family.source.instance_id].device_id
        assert family.source.relation_id == family.relation.id
        assert family.relation.ast.lhs_root is not None
        assert family.relation.ast.rhs_root is not None
        assert family.referenced_symbols
        for local in family.known_coefficients:
            key = ("step_duration" if local == "step_duration"
                   else f"{family.source.instance_id}.{local}")
            assert key in inventory.coefficients  # never misclassify a planning decision
    for sym in inventory.symbols.values():
        assert sym.source.field_path
    metadata = json.loads(bundle.files[generator.PARAMETERS_PATH])
    # P1-S1 source identities are internal until the authoritative Bundle
    # and diagnostics contracts are revised by the owner.
    assert "compiler_inventory" not in metadata
    assert "source_map" not in metadata


def test_public_generate_adapter_reuses_one_bundle_mapping(measured_generate):
    """Synthetic full primal checks mapping only; actual solve stays in Docker integration tests."""
    bundle, _builder, _ = measured_generate
    names = _mps_column_names(bundle.files[generator.PROBLEM_PATH])
    params = json.loads(bundle.files[generator.PARAMETERS_PATH])
    series = next(s for s in params["step_series"] if s["columns"])
    first_column = series["columns"][0]
    values = [42.0 if name == first_column else 0.0 for name in names]
    solution = "\n".join([
        "HIGHS_SOLUTION_V1",
        "model_status: HighsModelStatus.kOptimal",
        "objective_value: 0.0",
        f"num_columns: {len(names)}",
        "primal:",
        *(f"{i}: {value}" for i, value in enumerate(values)),
        "",
    ]).encode()
    result = HIGHS_ADAPTER.adapt(
        bundle, _receipt(), {generator.SOLUTION_PATH: solution},
    )
    assert result.math_status == "solved"
    out = result.outputs
    assert out["series"][series["series_id"]]["instance_id"] == series["instance_id"]
    variable = next(v for v in out["solution"]["variables"]
                    if v["name"] == first_column)
    assert out["series"][series["series_id"]]["values"][0] == pytest.approx(
        variable["value"],
    )
    assert "annual_net_flows" in out["financial_facts"]
    assert "annual_operation" in out["financial_facts"]


def test_two_instances_planned_initial_and_varying_steps():
    """Instance order, fixed-vs-planned state and step coefficients are independent."""
    artifact, models, _materialized, finance, config, evaluation = _inputs()
    doc = json.loads(artifact.canonical_text)
    doc["devices"]["heater2"] = copy.deepcopy(doc["devices"]["heater1"])
    doc["devices"]["heater2"]["properties"] = {
        "efficiency": {"value": 0.8, "unit": "1"},
    }
    doc["finance_binding"]["instances"]["heater2"] = {"type": "none"}
    doc["devices"]["tank2"] = copy.deepcopy(doc["devices"]["tank1"])
    doc["devices"]["tank2"]["properties"] = {
        "soc0": {"value": 0.75, "unit": "1"},
        "rated_kw": {"value": 70, "unit": "kW"},
    }
    doc["finance_binding"]["instances"]["tank2"] = {"type": "none"}
    doc["planning"]["variables"]["soc_plan"] = {
        "device_ref": "tank1", "parameter": "soc0",
        "lower_bound": "0", "upper_bound": "1", "unit": "1",
    }
    models = {**models, "heater2": _contribution(HEATER_YAML),
              "tank2": models["tank1"]}

    def setup(document):
        art = ValidatedAssemblyArtifact(
            canonical_text=json.dumps(document), receipt=ValidationReceipt(),
        )
        materialized = materialize_non_predictive_series(
            art, {}, operation_years=1,
        )
        series = tuple(
            replace(s, values=(5.0, 7.0) + s.values[2:])
            if (s.device, s.interface) == ("tank1", "flow") else s
            for s in materialized.series
        )
        return art, MaterializedSet(
            operation_years=materialized.operation_years,
            series=series,
            pending_predict=materialized.pending_predict,
            prices=materialized.prices,
        )

    art, series = setup(doc)
    builder = project._new_builder(art, models, series, finance, evaluation)
    model, _units, _objective_unit = builder.build()
    rows = {row.name: row for row in model.constraints}
    coefficients = lambda row: {key[0]: value for key, value in row.expression.terms}
    first = rows["tank1__soc_step__t0"]
    assert coefficients(first) == {
        "tank1__soc__t0": 1.0, "soc_plan": -1.0,
    }
    assert first.expression.constant == pytest.approx(-5000.0)
    fixed = rows["tank2__soc_step__t0"]
    assert coefficients(fixed) == {"tank2__soc__t0": 1.0}
    assert fixed.expression.constant == pytest.approx(-5000.75)
    assert rows["tank1__soc_step__t1"].expression.constant == pytest.approx(-7000.0)
    assert coefficients(rows["heater1__conversion__t0"])["heater1__electricity_in__t0"] == pytest.approx(-0.9)
    assert coefficients(rows["heater2__conversion__t0"])["heater2__electricity_in__t0"] == pytest.approx(-0.8)
    assert sum(v.name == "soc_plan" for v in model.variables) == 1
    assert builder.inventory.property_bindings[("tank1", "soc0")] == "soc_plan"
    assert "tank1.soc0" not in builder.inventory.coefficients
    assert builder.inventory.property_bindings[("tank2", "soc0")] != "soc_plan"

    reversed_doc = copy.deepcopy(doc)
    reversed_doc["devices"] = dict(reversed(tuple(doc["devices"].items())))
    reversed_art, reversed_series = setup(reversed_doc)
    # The reordered case also uses the same PUBLIC provider generate boundary,
    # not a second/private runtime model or an adapter-side reconstruction.
    public_config = replace(config, generator=generator.GENERATOR_REF)
    provider = build_registry((generator.HIGHS_MILP_PROVIDER,)).resolve(public_config)
    bundle = provider.generate(
        reversed_art, dict(reversed(tuple(models.items()))), reversed_series,
        finance, public_config, evaluation,
    )
    mps_text, _offset = write_mps(model)
    assert bundle.files[generator.PROBLEM_PATH] == mps_text.encode("utf-8")
    metadata = json.loads(bundle.files[generator.PARAMETERS_PATH])
    assert "soc_plan" in metadata["unit_map"]
    assert len(next(s["columns"] for s in metadata["step_series"]
                    if s["series_id"] == "tank1.soc")) == POINTS
    assert len(next(s["columns"] for s in metadata["step_series"]
                    if s["series_id"] == "tank2.soc")) == POINTS
    assert metadata["financial_facts"]["annual_net_flows"]
