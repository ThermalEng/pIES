"""P1-S3: repeatable full two-year/two-instance resource comparison.

All measurements run in the same Docker test image, with the same parsed
synthetic models, finance input, actual 17,520 hourly steps and one enabled
step_relation. Old S2 modeling/step kernels exist *only* in a test-reference
subclass; both paths use the same S2 inventory, domains, cash semantics and
the same S3 column allocation (documented limit of this comparison).
"""
from __future__ import annotations

import gc
import json
import logging
import statistics
import time
import tracemalloc

from iesplan.computation.generators.mps import write_mps
from iesplan.computation.generators.project import _ProjectBuilder
from iesplan.finance import EvaluationInput, FinanceProfile, merge_effective

from test_p1_s3_templates import _S2ReferenceBuilder
from test_linear_lowering import SOC_YAML
from test_project_model import _contribution, _finance_payload


def _full_two_year_builder(builder_type, *, block_size: int):
    tank = _contribution(SOC_YAML)
    models = {"a": tank, "b": tank}
    devices = {
        inst: {
            "definition": {"id": tank.device_id},
            "asset_origin": "existing",
            "properties": {
                "soc0": {"value": initial, "unit": "1"},
                "step_c": {"value": coeff, "unit": "1"},
            },
        }
        for inst, initial, coeff in (("a", 0.2, 1.0), ("b", 0.7, 0.5))
    }
    doc = {
        "project_baseline": {
            "start_year": 2026, "resolution": "1h", "scenario_mode": "single",
        },
        "planning": {
            "variables": {},
            "objective": {"sense": "minimize", "expression": "0"},
            "constraints": {
                "peer_flow": {
                    "type": "step_relation",
                    "references": {
                        "left": {"instance": "a", "interface": "flow"},
                        "right": {"instance": "b", "interface": "flow"},
                    },
                    "expression": "left[t] == right[t]",
                    "enabled": True,
                },
            },
        },
        "devices": devices,
        "connections": {},
        "finance_binding": {"instances": {
            "a": {"type": "none"}, "b": {"type": "none"},
        }},
        "tariff_bindings": {},
        "demand_bindings": {},
    }
    finance = merge_effective(
        FinanceProfile.from_dict(_finance_payload()), None,
    )
    evaluation = EvaluationInput(
        evaluation_years=2, construction_period=0, operation_years=2,
        discount_rate=0.0, construction_payments=(), maintenance_expenses=(),
    )
    builder = builder_type(
        doc=doc, devices=devices, models=models, series=(),
        finance=finance, evaluation=evaluation, point_count=17520,
        step_s=3600.0,
    )
    builder.template_block_size = block_size
    return builder


def test_cross_year_two_instance_resource_repeats_and_block_choices(tmp_path):
    # AB / BA repeated pairs quantify order noise. Single 128/512 runs
    # contextualize default 256; no timing threshold or "winner" assumption.
    trials = (
        ("s2_reference", 256, "pair1_a"),
        ("s3_production", 256, "pair1_b"),
        ("s3_production", 256, "pair2_a"),
        ("s2_reference", 256, "pair2_b"),
        ("s3_production", 128, "block128"),
        ("s3_production", 512, "block512"),
    )
    mps_path = tmp_path / "frozen-s2-two-year.mps"
    unit_path = tmp_path / "frozen-s2-two-year-units.json"
    facts_path = tmp_path / "frozen-s2-two-year-facts.json"
    series_path = tmp_path / "frozen-s2-two-year-series.json"
    reports: list[dict] = []
    expected_shape = None
    for kind, block_size, position in trials:
        gc.collect()
        builder_type = (
            _S2ReferenceBuilder if kind == "s2_reference" else _ProjectBuilder
        )
        tracemalloc.start()
        try:
            started = time.perf_counter()
            builder = _full_two_year_builder(builder_type, block_size=block_size)
            model, units, _unit = builder.build()
            wall_ms = round((time.perf_counter() - started) * 1000, 3)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert builder.inventory.sealed
        assert len(builder.analysis.relations("planning_step")) == 1
        # Production must compile one step relation, not 17,520 copies;
        # every execution still binds 17,520 distinct actual time rows.
        if kind == "s3_production":
            assert builder.template_stats["technical_compiles"] == 1
            assert builder.template_stats["technical_binds"] == 2 * 17520
            assert builder.template_stats["step_compiles"] == 1
            assert builder.template_stats["step_binds"] == 17520
        else:
            assert builder.template_stats["s2_ast_lower_calls"] == 2 * 17520
            assert builder.template_stats["s2_step_row_bindings"] == 17520

        name = "a__soc_step__t8760"
        (cross_year,) = (row for row in model.constraints if row.name == name)
        assert dict(cross_year.expression.terms)[("a__soc__t8759", 0)] == -1.0
        for edge in (255, 256, 8759, 8760, 17519):
            assert builder._step_columns[("a", "soc")][edge] == (
                f"a__soc__t{edge}"
            )
        assert any(
            row.name == "step__peer_flow__t17519"
            for row in model.constraints
        )
        shape = {
            "point_count": builder.point_count,
            "instances": 2,
            "planning_step_relations": 1,
            "columns": len(model.variables),
            "rows": len(model.constraints),
            "binary_columns": sum(v.kind == "binary" for v in model.variables),
            "row_nonzeros": sum(len(row.expression.terms) for row in model.constraints),
            "objective_nonzeros": len(model.objective.terms),
        }
        if expected_shape is None:
            expected_shape = shape
        assert shape == expected_shape

        mps_text, offset = write_mps(model)
        if not mps_path.exists():
            mps_path.write_text(mps_text, encoding="utf-8")
            expected_offset = offset
        else:
            assert offset == expected_offset
            assert mps_text == mps_path.read_text(encoding="utf-8")
        unit_json = json.dumps(dict(units), sort_keys=True)
        fact_json = json.dumps(dict(builder.financial_facts()), sort_keys=True)
        series_json = json.dumps(list(builder.step_series()), sort_keys=True)
        for path, content in (
            (unit_path, unit_json), (facts_path, fact_json),
            (series_path, series_json),
        ):
            if not path.exists():
                path.write_text(content, encoding="utf-8")
            else:
                assert content == path.read_text(encoding="utf-8")
        report = {
            "kind": kind,
            "order": position,
            "block_size": block_size,
            "wall_ms": wall_ms,
            "traced_peak_bytes": peak,
            "stage_wall_ms": dict(builder.template_stats),
            "mps_bytes": len(mps_text.encode("utf-8")),
            "step_series_declarations_bytes": len(series_json.encode("utf-8")),
            "finance_fact_declarations_bytes": len(fact_json.encode("utf-8")),
            **shape,
        }
        reports.append(report)
        # Do not keep any old full model/AST or its trace in the next sample.
        del builder, model, units, mps_text, unit_json, fact_json, series_json
        gc.collect()

    repeated = {
        kind: [report for report in reports
               if report["kind"] == kind and report["block_size"] == 256]
        for kind in ("s2_reference", "s3_production")
    }
    assert all(len(rows) == 2 for rows in repeated.values())
    summary = {
        "two_year_hours": 17520,
        "input": "synthetic two instances, 2026+2027, one enabled step_relation",
        "ordered_samples": reports,
        "default_block_size": 256,
        "default_block_choice_note":
            "Compare 128/256/512 timings and traced peaks; 256 is a balance, "
            "not an asserted machine-independent optimum",
        "paired_repeats": {
            kind: {
                "wall_ms_median": statistics.median(x["wall_ms"] for x in rows),
                "wall_ms_min": min(x["wall_ms"] for x in rows),
                "wall_ms_max": max(x["wall_ms"] for x in rows),
                "traced_peak_bytes_median":
                    statistics.median(x["traced_peak_bytes"] for x in rows),
            }
            for kind, rows in repeated.items()
        },
        "reference_scope":
            "Frozen original S2 technical AST and step row loops in test only; "
            "S3 column allocation, S2 semantic inventory, cash and writer shared; "
            "not a separate checkout of S2, not worker/solver and no metadata Bundle",
        "measurement_scope":
            "Full model build with tracemalloc overhead; MPS and output comparisons "
            "are after timer; peaks not isolated process RSS; AB/BA repeats "
            "control part of order effect",
    }
    logging.warning("P1_S3_MULTIYEAR_REPEATS %s", json.dumps(summary, sort_keys=True))
