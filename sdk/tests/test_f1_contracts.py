"""Comprehensive valid & invalid fixture tests for F1 core contracts and streaming I/O."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from pies_sdk import (
    ENGINEERING_SOLUTION_SCHEMA,
    ENGINEERING_SOLUTION_VERSION,
    EVALUATION_REPORT_SCHEMA,
    EVALUATION_REPORT_VERSION,
    PRIMAL_SOLUTION_SCHEMA,
    PRIMAL_SOLUTION_VERSION,
    VARIABLE_MAP_RECORD_TAGS,
    ConstructionScale,
    ConstructionUnit,
    DeviceSeries,
    EngineeringSolutionPayload,
    EvaluationChart,
    EvaluationChartSeries,
    EvaluationMetric,
    EvaluationReportPayload,
    EvaluationTable,
    EvaluationTableColumn,
    PrimalSolutionHeader,
    PrimalSolutionPayload,
    SolverSummary,
    StageContext,
    StageIOError,
    SystemSeries,
    VariableMapHeader,
    VariableMapWriter,
    iter_primal_solution_values,
    iter_variable_map_records,
    read_engineering_solution,
    read_evaluation_report,
    read_primal_solution,
    read_variable_map,
    validate_plugin_manifest,
    write_engineering_solution,
    write_evaluation_report,
    write_primal_solution,
    write_primal_solution_streaming,
    write_variable_map,
)


def _primal_feasible_dict() -> dict:
    return {
        "schema": PRIMAL_SOLUTION_SCHEMA,
        "schema_version": PRIMAL_SOLUTION_VERSION,
        "math_status": "solved",
        "termination_reason": "optimal",
        "has_feasible_solution": True,
        "column_count": 3,
        "values": [1.5, 0.0, 42.25],
        "objective_value": 1234.5,
        "best_bound": 1234.5,
        "relative_gap": 0.0,
        "receipt": {
            "solver": "ies.solver.highs@1.7.2",
            "started_at": "2026-11-25T12:00:00Z",
            "ended_at": "2026-11-25T12:00:02Z",
            "elapsed_seconds": 2.0,
            "solve_domain": "original",
            "original_model_verified": True,
        },
    }


def _primal_infeasible_dict() -> dict:
    return {
        "schema": PRIMAL_SOLUTION_SCHEMA,
        "schema_version": PRIMAL_SOLUTION_VERSION,
        "math_status": "infeasible",
        "termination_reason": "infeasible",
        "has_feasible_solution": False,
        "column_count": 3,
        "values": [],
        "objective_value": None,
        "best_bound": None,
        "relative_gap": None,
        "receipt": {"solver": "ies.solver.highs@1.7.2"},
    }


def _engineering_feasible_dict() -> dict:
    return {
        "schema": ENGINEERING_SOLUTION_SCHEMA,
        "schema_version": ENGINEERING_SOLUTION_VERSION,
        "solver_summary": {
            "termination_reason": "optimal",
            "math_status": "solved",
            "has_feasible_solution": True,
            "objective_value": 5000.0,
            "objective_unit": "CNY",
            "best_bound": 5000.0,
            "relative_gap": 0.0,
        },
        "construction_units": [
            {
                "unit_id": "unit_pv1",
                "instance_id": "pv1",
                "mode": "optional",
                "built": True,
                "scales": [
                    {
                        "planning_variable_id": "capacity",
                        "value": 100.0,
                        "unit": "kW",
                    }
                ],
            }
        ],
        "series": {
            "pv1.power_out": {
                "instance_id": "pv1",
                "variable_id": "power_out",
                "unit": "kW",
                "values": [0.0, 50.0, 75.0],
            }
        },
        "system_series": {
            "sys.reserve": {
                "block": "reserve_balance",
                "variable": "margin",
                "unit": "kW",
                "values": [10.0, 10.0, 10.0],
            }
        },
        "metrics": {"num_variables": 3},
        "model_sources": {"schema": "ies.model-sources", "schema_version": "1.0.0"},
    }


def _variable_map_header_dict(column_count: int = 4) -> dict:
    return {
        "assembly": {"id": "asm-1", "schema_version": "1.0.0"},
        "generator": "ies.generator.highs_milp@1.0.0",
        "records": "input/parameters.jsonl",
        "column_count": column_count,
        "objective_offset": 100.0,
        "objective_unit": "CNY",
        "outputs": ["solution", "metrics"],
        "precision": "medium",
        "seed": 42,
    }


def _evaluation_report_dict() -> dict:
    return {
        "schema": EVALUATION_REPORT_SCHEMA,
        "schema_version": EVALUATION_REPORT_VERSION,
        "title": "Carbon & Reliability Report",
        "evaluator": "acme.evaluator.carbon@1.0.0",
        "summary": "Annual CO2 intensity evaluated.",
        "metrics": [
            {"id": "co2_total", "label": "Total Emissions", "value": 125.5, "unit": "tCO2"},
            {"id": "renewable_share", "label": "Renewable Share", "value": "0.68", "unit": "1"},
        ],
        "charts": [
            {
                "id": "hourly_emissions",
                "title": "Hourly CO2",
                "x_label": "Step",
                "y_label": "Emissions",
                "y_unit": "kgCO2",
                "x_unit": "step",
                "chart_type": "line",
                "x_values": [0, 1, 2],
                "series": [
                    {"id": "grid", "label": "Grid Import", "unit": "kgCO2", "values": [10.0, 12.5, 8.0]}
                ],
            }
        ],
        "tables": [
            {
                "id": "device_emissions",
                "title": "Device Breakdown",
                "columns": [
                    {"id": "instance_id", "label": "Instance"},
                    {"id": "emissions", "label": "Emissions", "unit": "tCO2"},
                ],
                "rows": [
                    ["chp1", 100.0],
                    ["boiler1", 25.5],
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# ies.primal-solution@1.0.0 tests
# ---------------------------------------------------------------------------


def test_primal_solution_roundtrip_and_streaming(tmp_path: Path) -> None:
    payload = PrimalSolutionPayload.from_dict(_primal_feasible_dict())
    out_path = write_primal_solution(tmp_path / "primal_solution.json", payload)
    loaded = read_primal_solution(out_path)
    assert loaded == payload
    assert loaded.values == (1.5, 0.0, 42.25)

    # Large streaming write & read (O(1) memory) with 5000 columns
    stream_path = tmp_path / "stream_primal.json"
    header = PrimalSolutionHeader(
        math_status="feasible",
        termination_reason="time_limit",
        has_feasible_solution=True,
        column_count=5000,
        objective_value=999.0,
        best_bound=None,
        relative_gap=None,
        receipt={"solver": "acme.solver.cbc@1.0.0"},
    )
    write_primal_solution_streaming(stream_path, header, (float(i) * 0.5 for i in range(5000)))
    hdr, val_iter = iter_primal_solution_values(stream_path)
    assert hdr.column_count == 5000
    assert hdr.best_bound is None
    streamed_vals = list(val_iter)
    assert len(streamed_vals) == 5000
    assert streamed_vals[0] == 0.0
    assert streamed_vals[4999] == 2499.5

    # Key-order independence: 'values' before metadata keys
    reordered_path = tmp_path / "reordered.json"
    reordered_doc = {
        "values": [2.0, 4.0, 6.0],
        "schema": PRIMAL_SOLUTION_SCHEMA,
        "schema_version": PRIMAL_SOLUTION_VERSION,
        "math_status": "solved",
        "termination_reason": "optimal",
        "has_feasible_solution": True,
        "column_count": 3,
        "objective_value": 12.0,
        "best_bound": 12.0,
        "relative_gap": 0.0,
        "receipt": {"solver": "ies.solver.highs@1.7.2"},
    }
    reordered_path.write_text(json.dumps(reordered_doc), encoding="utf-8")
    r_hdr, r_iter = iter_primal_solution_values(reordered_path)
    assert r_hdr.column_count == 3
    assert list(r_iter) == [2.0, 4.0, 6.0]

    # Non-feasible primal solution
    inf_path = write_primal_solution(tmp_path / "infeasible.json", _primal_infeasible_dict())
    inf_loaded = read_primal_solution(inf_path)
    assert inf_loaded.has_feasible_solution is False
    assert inf_loaded.values == ()


def test_primal_solution_rejects_invalid_fixtures(tmp_path: Path) -> None:
    bad_unknown = _primal_feasible_dict()
    bad_unknown["extra"] = 1
    with pytest.raises(StageIOError, match="Additional properties"):
        PrimalSolutionPayload.from_dict(bad_unknown)

    bad_bool = _primal_feasible_dict()
    bad_bool["values"] = [True, 0.0, 1.0]
    with pytest.raises(StageIOError, match="not of type 'number'"):
        PrimalSolutionPayload.from_dict(bad_bool)
    with pytest.raises(StageIOError, match="finite number"):
        PrimalSolutionPayload(
            math_status="solved",
            termination_reason="optimal",
            has_feasible_solution=True,
            column_count=1,
            values=(True,),  # type: ignore[arg-type]
            objective_value=1.0,
            best_bound=1.0,
            relative_gap=0.0,
            receipt={"solver": "ies.solver.highs@1.7.2"},
        )

    bad_bool_col = _primal_feasible_dict()
    bad_bool_col["column_count"] = True
    with pytest.raises(StageIOError):
        PrimalSolutionPayload.from_dict(bad_bool_col)

    bad_nan = _primal_feasible_dict()
    bad_nan["objective_value"] = float("nan")
    with pytest.raises(StageIOError, match="non-finite"):
        PrimalSolutionPayload.from_dict(bad_nan)

    bad_len = _primal_feasible_dict()
    bad_len["values"] = [1.0, 2.0]
    with pytest.raises(StageIOError, match="must equal 3"):
        PrimalSolutionPayload.from_dict(bad_len)

    bad_infeasible_vals = _primal_infeasible_dict()
    bad_infeasible_vals["values"] = [1.0, 2.0, 3.0]
    with pytest.raises(StageIOError, match="must equal 0"):
        PrimalSolutionPayload.from_dict(bad_infeasible_vals)

    bad_status_mix = _primal_feasible_dict()
    bad_status_mix["math_status"] = "infeasible"
    with pytest.raises(StageIOError, match="has_feasible_solution=True"):
        PrimalSolutionPayload.from_dict(bad_status_mix)

    bad_receipt = _primal_feasible_dict()
    bad_receipt["receipt"]["unknown"] = "x"
    with pytest.raises(StageIOError, match="Additional properties"):
        PrimalSolutionPayload.from_dict(bad_receipt)

    nan_file = tmp_path / "nan_primal.json"
    nan_file.write_text(
        json.dumps(_primal_feasible_dict()).replace("42.25", "NaN"),
        encoding="utf-8",
    )
    with pytest.raises(StageIOError, match="Non-finite"):
        read_primal_solution(nan_file)


# ---------------------------------------------------------------------------
# ies.engineering-solution@1.0.0 tests
# ---------------------------------------------------------------------------


def test_engineering_solution_dual_state_roundtrip(tmp_path: Path) -> None:
    feasible = EngineeringSolutionPayload.from_dict(_engineering_feasible_dict())
    path = write_engineering_solution(tmp_path / "solution.json", feasible)
    assert read_engineering_solution(path) == feasible

    # Construct directly from typed dataclasses
    direct = EngineeringSolutionPayload(
        solver_summary=SolverSummary(
            termination_reason="optimal",
            math_status="solved",
            has_feasible_solution=True,
            objective_value=5000.0,
            objective_unit="CNY",
            best_bound=5000.0,
            relative_gap=0.0,
        ),
        construction_units=(
            ConstructionUnit(
                unit_id="unit_pv1",
                instance_id="pv1",
                mode="optional",
                built=True,
                scales=(ConstructionScale("capacity", 100.0, "kW"),),
            ),
        ),
        series={"pv1.power_out": DeviceSeries("pv1", "power_out", "kW", (0.0, 50.0, 75.0))},
        system_series={"sys.reserve": SystemSeries("reserve_balance", "margin", "kW", (10.0, 10.0, 10.0))},
        metrics={"num_variables": 3},
        model_sources={"schema": "ies.model-sources", "schema_version": "1.0.0"},
    )
    assert direct == feasible

    # Non-feasible: omits construction_units/series/system_series OR allows empty
    non_feasible_omitted = EngineeringSolutionPayload.from_dict(
        {
            "schema": ENGINEERING_SOLUTION_SCHEMA,
            "schema_version": ENGINEERING_SOLUTION_VERSION,
            "solver_summary": {
                "termination_reason": "infeasible",
                "math_status": "infeasible",
                "has_feasible_solution": False,
                "objective_value": None,
                "objective_unit": "CNY",
                "best_bound": None,
                "relative_gap": None,
            },
        }
    )
    assert non_feasible_omitted.construction_units is None
    assert non_feasible_omitted.series is None

    non_feasible_empty = EngineeringSolutionPayload.from_dict(
        {
            "schema": ENGINEERING_SOLUTION_SCHEMA,
            "schema_version": ENGINEERING_SOLUTION_VERSION,
            "solver_summary": {
                "termination_reason": "time_limit",
                "math_status": "not_solved",
                "has_feasible_solution": False,
                "objective_value": None,
                "objective_unit": None,
                "best_bound": None,
                "relative_gap": None,
            },
            "construction_units": [],
            "series": {},
            "system_series": {},
        }
    )
    assert non_feasible_empty.construction_units == ()
    assert non_feasible_empty.series == {}


def test_engineering_solution_rejects_invalid_fixtures() -> None:
    missing_units = _engineering_feasible_dict()
    del missing_units["construction_units"]
    with pytest.raises(StageIOError, match="construction_units"):
        EngineeringSolutionPayload.from_dict(missing_units)

    missing_series = _engineering_feasible_dict()
    missing_series["series"] = None
    with pytest.raises(StageIOError):
        EngineeringSolutionPayload.from_dict(missing_series)

    duplicate_sid = _engineering_feasible_dict()
    duplicate_sid["system_series"]["pv1.power_out"] = {
        "block": "b",
        "variable": "v",
        "unit": "kW",
        "values": [1.0, 2.0, 3.0],
    }
    with pytest.raises(StageIOError, match="duplicate series_id"):
        EngineeringSolutionPayload.from_dict(duplicate_sid)

    bad_non_feasible = {
        "schema": ENGINEERING_SOLUTION_SCHEMA,
        "schema_version": ENGINEERING_SOLUTION_VERSION,
        "solver_summary": {
            "termination_reason": "infeasible",
            "math_status": "infeasible",
            "has_feasible_solution": False,
            "objective_value": None,
            "objective_unit": "CNY",
            "best_bound": None,
            "relative_gap": None,
        },
        "construction_units": _engineering_feasible_dict()["construction_units"],
    }
    with pytest.raises(StageIOError, match="Non-feasible"):
        EngineeringSolutionPayload.from_dict(bad_non_feasible)

    bad_mode = _engineering_feasible_dict()
    bad_mode["construction_units"][0]["mode"] = "invalid_mode"
    with pytest.raises(StageIOError):
        EngineeringSolutionPayload.from_dict(bad_mode)

    bad_bool_scale = _engineering_feasible_dict()
    bad_bool_scale["construction_units"][0]["scales"][0]["value"] = False
    with pytest.raises(StageIOError, match="not of type 'number'"):
        EngineeringSolutionPayload.from_dict(bad_bool_scale)
    with pytest.raises(StageIOError, match="finite number"):
        ConstructionScale("cap", False, "kW")  # type: ignore[arg-type]

    bad_extra = _engineering_feasible_dict()
    bad_extra["unexpected"] = 1
    with pytest.raises(StageIOError, match="Additional properties"):
        EngineeringSolutionPayload.from_dict(bad_extra)


# ---------------------------------------------------------------------------
# Universal variable_map tests (all 19 documented record tags)
# ---------------------------------------------------------------------------


def test_variable_map_all_documented_tags_and_streaming(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    header = VariableMapHeader.from_dict(_variable_map_header_dict(column_count=4))
    records = [
        ["column_names", ["c0", "c1"]],
        ["column_names", ["c2", "c3"]],
        ["row_names", ["r0", "r1"]],
        ["epigraph", 0],
        ["epigraph_flows", [1, 2]],
        ["epigraph_end"],
        ["expression", 0, 10.0],
        ["terms", [[0, 1.5], [3, -2.0]]],
        ["expression_end"],
        ["units", [[0, "kW"], [1, "kW"], [3, "kWh"]]],
        ["component", "upfront_capex", "CNY", "gross", 0],
        ["facts", {"currency": "CNY", "construction_total": {"expression": 0}}],
        ["series", {
            "series_id": "pv1.power_out",
            "instance_id": "pv1",
            "variable_id": "power_out",
            "unit": "kW",
        }],
        ["positions", [1, 2]],
        ["series_end"],
        ["series", {"series_id": "sys.reserve", "block": "res", "variable": "margin", "unit": "kW"}],
        ["positions", [3]],
        ["series_end"],
        ["sources", {"schema": "ies.model-sources", "schema_version": "1.0.0", "sources": {}, "outputs": []}],
        [
            "source_group",
            {"source": "src1", "role": "column:flow", "axis": "step"},
            {"start": 0, "stride": 1, "count": 2},
        ],
        ["targets", [1, 2]],
        ["source_group_end"],
        ["source_group", {"source": "src2", "role": "row:balance", "axis": "step"}, None],
        ["targets", [0, 1]],
        ["indices", [0, 1]],
        ["source_group_end"],
    ]
    assert {rec[0] for rec in records} == set(VARIABLE_MAP_RECORD_TAGS)
    write_variable_map(bundle_dir, header, records)
    loaded_header, loaded_records = read_variable_map(bundle_dir)
    assert loaded_header == header
    assert loaded_records == records

    # Streaming iteration directly
    streamed = list(iter_variable_map_records(bundle_dir / "input" / "parameters.jsonl", column_count=4))
    assert len(streamed) == len(records)


def test_variable_map_rejects_invalid_headers_and_records(tmp_path: Path) -> None:
    bad_hdr = _variable_map_header_dict()
    bad_hdr["column_count"] = True
    with pytest.raises(StageIOError):
        VariableMapHeader.from_dict(bad_hdr)

    bad_hdr_nan = _variable_map_header_dict()
    bad_hdr_nan["objective_offset"] = float("inf")
    with pytest.raises(StageIOError):
        VariableMapHeader.from_dict(bad_hdr_nan)

    bad_hdr_path = _variable_map_header_dict()
    bad_hdr_path["records"] = "../escape.jsonl"
    with pytest.raises(StageIOError):
        VariableMapHeader.from_dict(bad_hdr_path)

    bad_hdr_extra = _variable_map_header_dict()
    bad_hdr_extra["solver"] = "ies.solver.highs@1.7.2"
    with pytest.raises(StageIOError, match="Additional properties"):
        VariableMapHeader.from_dict(bad_hdr_extra)

    # Bad NDJSON records
    invalid_cases = [
        (["unknown_tag", 1], "Unknown variable_map record tag"),
        (["epigraph", 4], "out of range"),
        (["epigraph", True], "must be an integer"),
        (["expression", 1, 0.0], "strictly 0-based sequential"),
        (["terms", [[0, 1.0]]], "open 'expression' block"),
        (["component", "c", "CNY", "gross", 0], "undefined expression"),
        (["series", {"series_id": "s1", "unit": "kW"}], "either"),
    ]
    for rec, msg in invalid_cases:
        p = tmp_path / "bad.jsonl"
        with pytest.raises(StageIOError, match=msg):
            with VariableMapWriter(p, column_count=4) as writer:
                writer.write_record(rec)

    # Out-of-order section
    out_of_order = tmp_path / "order.jsonl"
    with pytest.raises(StageIOError, match="section order"):
        with VariableMapWriter(out_of_order, column_count=2) as writer:
            writer.write_record(["expression", 0, 1.0])
            writer.write_record(["expression_end"])
            writer.write_record(["column_names", ["c0", "c1"]])

    # Unclosed block
    unclosed = tmp_path / "unclosed.jsonl"
    with pytest.raises(StageIOError, match="Unclosed 'epigraph' block"):
        with VariableMapWriter(unclosed, column_count=2) as writer:
            writer.write_record(["epigraph", 0])

    # Column names count mismatch with header column_count
    mismatch = tmp_path / "mismatch.jsonl"
    with pytest.raises(StageIOError, match="does not match header column_count"):
        with VariableMapWriter(mismatch, column_count=3) as writer:
            writer.write_record(["column_names", ["c0", "c1"]])

    # Compressed source_group with explicit indices forbidden
    compressed_err = tmp_path / "compressed.jsonl"
    with pytest.raises(StageIOError, match="compressed indices"):
        with VariableMapWriter(compressed_err, column_count=2) as writer:
            writer.write_record(["sources", {}])
            writer.write_record(
                [
                    "source_group",
                    {"source": "s", "role": "column:x", "axis": "step"},
                    {"start": 0, "stride": 1, "count": 1},
                ]
            )
            writer.write_record(["indices", [0]])


# ---------------------------------------------------------------------------
# ies.evaluation-report@1.0.0 tests
# ---------------------------------------------------------------------------


def test_evaluation_report_roundtrip_and_dataclasses(tmp_path: Path) -> None:
    report = EvaluationReportPayload.from_dict(_evaluation_report_dict())
    out = write_evaluation_report(tmp_path / "evaluation_report.json", report)
    assert read_evaluation_report(out) == report

    direct = EvaluationReportPayload(
        title="Carbon & Reliability Report",
        evaluator="acme.evaluator.carbon@1.0.0",
        summary="Annual CO2 intensity evaluated.",
        metrics=(
            EvaluationMetric("co2_total", "Total Emissions", 125.5, "tCO2"),
            EvaluationMetric("renewable_share", "Renewable Share", "0.68", "1"),
        ),
        charts=(
            EvaluationChart(
                id="hourly_emissions",
                title="Hourly CO2",
                x_label="Step",
                y_label="Emissions",
                y_unit="kgCO2",
                x_unit="step",
                chart_type="line",
                x_values=(0, 1, 2),
                series=(
                    EvaluationChartSeries("grid", "Grid Import", (10.0, 12.5, 8.0), unit="kgCO2"),
                ),
            ),
        ),
        tables=(
            EvaluationTable(
                id="device_emissions",
                title="Device Breakdown",
                columns=(
                    EvaluationTableColumn("instance_id", "Instance"),
                    EvaluationTableColumn("emissions", "Emissions", unit="tCO2"),
                ),
                rows=(("chp1", 100.0), ("boiler1", 25.5)),
            ),
        ),
    )
    assert direct == report


def test_evaluation_report_rejects_invalid_fixtures() -> None:
    bad_extra = _evaluation_report_dict()
    bad_extra["extra"] = True
    with pytest.raises(StageIOError, match="Additional properties"):
        EvaluationReportPayload.from_dict(bad_extra)

    bad_bool_metric = deepcopy(_evaluation_report_dict())
    bad_bool_metric["metrics"][0]["value"] = True
    with pytest.raises(StageIOError, match="not of type 'number'"):
        EvaluationReportPayload.from_dict(bad_bool_metric)
    with pytest.raises(StageIOError, match="boolean"):
        EvaluationMetric("m1", "Metric", True, "1")  # type: ignore[arg-type]

    bad_dup_metric = deepcopy(_evaluation_report_dict())
    bad_dup_metric["metrics"].append(deepcopy(bad_dup_metric["metrics"][0]))
    with pytest.raises(StageIOError, match="Duplicate metric id"):
        EvaluationReportPayload.from_dict(bad_dup_metric)

    bad_chart_len = deepcopy(_evaluation_report_dict())
    bad_chart_len["charts"][0]["series"][0]["values"] = [1.0, 2.0]
    with pytest.raises(StageIOError, match="x_values length"):
        EvaluationReportPayload.from_dict(bad_chart_len)

    bad_table_row = deepcopy(_evaluation_report_dict())
    bad_table_row["tables"][0]["rows"][0] = ["chp1"]
    with pytest.raises(StageIOError, match="does not match columns"):
        EvaluationReportPayload.from_dict(bad_table_row)

    bad_nan_chart = deepcopy(_evaluation_report_dict())
    bad_nan_chart["charts"][0]["series"][0]["values"] = [float("nan"), 1.0, 2.0]
    with pytest.raises(StageIOError, match="non-finite"):
        EvaluationReportPayload.from_dict(bad_nan_chart)


def test_stage_context_f1_helpers(tmp_path: Path) -> None:
    manifest = validate_plugin_manifest(
        {
            "schema": "ies.algorithm-plugin",
            "schema_version": "1.0.0",
            "plugin_id": "acme.evaluator.carbon",
            "version": "1.0.0",
            "kind": "evaluator",
            "name": {"en_us": "Carbon"},
            "description": "Carbon evaluator.",
            "compatibility": {"pies": ">=1.1.5,<2.0.0"},
            "consumes": ["ies.engineering-solution@1.0.0"],
            "produces": ["ies.evaluation-report@1.0.0"],
            "runtime": {"language": "python", "python": "3.12"},
            "entrypoint": "main:evaluate",
            "parameters_schema": {"type": "object", "additionalProperties": False, "properties": {}},
            "resources": {
                "default": {"cpu": 1, "memory_mb": 128, "timeout_seconds": 30, "process_limit": 4},
                "maximum": {"cpu": 1, "memory_mb": 256, "timeout_seconds": 60, "process_limit": 8},
            },
        }
    )
    work_in = tmp_path / "in"
    work_out = tmp_path / "out"
    work_in.mkdir()
    work_out.mkdir()
    ctx = StageContext(manifest=manifest, work_in=work_in, work_out=work_out, parameters={})

    write_engineering_solution(work_in / "solution.json", _engineering_feasible_dict())
    sol = ctx.read_engineering_solution()
    assert sol.solver_summary.math_status == "solved"

    ctx.write_evaluation_report(_evaluation_report_dict())
    rep = read_evaluation_report(work_out / "evaluation_report.json")
    assert rep.title == "Carbon & Reliability Report"

    ctx.write_primal_solution(_primal_feasible_dict())
    primal = read_primal_solution(work_out / "primal_solution.json")
    assert primal.column_count == 3
