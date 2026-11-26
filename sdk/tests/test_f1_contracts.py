"""Comprehensive valid & invalid fixture tests for F1 core contracts and streaming I/O."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from pies_sdk import (
    ENGINEERING_SOLUTION_SCHEMA,
    ENGINEERING_SOLUTION_VERSION,
    EVALUATION_REPORT_SCHEMA,
    EVALUATION_REPORT_VERSION,
    PRIMAL_SOLUTION_SCHEMA,
    PRIMAL_SOLUTION_VERSION,
    VARIABLE_MAP_RECORD_TAGS,
    VARIABLE_MAP_SCHEMA,
    VARIABLE_MAP_VERSION,
    ConstructionScale,
    ConstructionUnit,
    DeviceSeries,
    EngineeringSolutionExpectation,
    EngineeringSolutionPayload,
    EvaluationChart,
    EvaluationChartSeries,
    EvaluationMetric,
    EvaluationReportPayload,
    EvaluationTable,
    EvaluationTableColumn,
    ExpectedConstructionUnit,
    ExpectedDeviceSeries,
    ExpectedSystemSeries,
    PrimalSolutionHeader,
    PrimalSolutionPayload,
    SolverSummary,
    StageContext,
    StageIOError,
    SystemSeries,
    VariableMapHeader,
    VariableMapWriter,
    apply_linear_conversion,
    iter_primal_solution_values,
    iter_variable_map_records,
    load_engineering_solution_schema,
    load_evaluation_report_schema,
    load_primal_solution_schema,
    load_variable_map_header_schema,
    read_engineering_solution,
    read_evaluation_report,
    read_primal_solution,
    read_variable_map,
    validate_engineering_solution_completeness,
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
        "schema": VARIABLE_MAP_SCHEMA,
        "schema_version": VARIABLE_MAP_VERSION,
        "assembly": {"id": "asm-1", "schema_version": "1.0.0"},
        "generator": "ies.generator.highs_milp@1.0.0",
        "records": "input/parameters.jsonl",
        "column_count": column_count,
        "objective_offset": 100.0,
        "objective_scale": 0.001,
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
                    {
                        "id": "grid",
                        "label": "Grid Import",
                        "unit": "kgCO2",
                        "values": [10.0, 12.5, 8.0],
                    }
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
    payload_dict = _primal_feasible_dict()
    Draft202012Validator(load_primal_solution_schema()).validate(payload_dict)
    payload = PrimalSolutionPayload.from_dict(payload_dict)
    out_path = write_primal_solution(tmp_path / "primal_solution.json", payload)
    loaded = read_primal_solution(out_path)
    assert loaded == payload
    assert loaded.values == (1.5, 0.0, 42.25)

    # Large streaming write & read (O(1) memory) crossing 65,536-char buffer boundaries
    col_count = 32752
    stream_path = tmp_path / "stream_primal.json"
    header = PrimalSolutionHeader(
        math_status="feasible",
        termination_reason="time_limit",
        has_feasible_solution=True,
        column_count=col_count,
        objective_value=999.0,
        best_bound=None,
        relative_gap=None,
        receipt={"solver": "acme.solver.cbc@1.0.0"},
    )
    write_primal_solution_streaming(
        stream_path, header, (float(i) * 0.125 for i in range(col_count))
    )
    hdr, val_iter = iter_primal_solution_values(stream_path)
    assert hdr.column_count == col_count
    assert hdr.best_bound is None
    streamed_vals = list(val_iter)
    assert len(streamed_vals) == col_count
    assert streamed_vals[0] == 0.0
    assert streamed_vals[-1] == (col_count - 1) * 0.125

    # Explicit chunk-boundary regression: numbers crossing char 65,536 both in 'values'
    # AND in metadata placed AFTER 'values'
    boundary_path = tmp_path / "chunk_boundary_primal.json"
    prefix_vals = ", ".join("1.0" for _ in range(13100))
    prefix_json = '{"values": [' + prefix_vals + ", "
    # Pad inside the array whitespace so a fractional float crosses 65,536 exactly
    pad_len = max(0, 65533 - len(prefix_json))
    values_section = prefix_json + (" " * pad_len) + "123.456789]"
    # Put metadata right after values, with another number straddling 2*65,536
    after_prefix = (
        values_section
        + ', "schema": "ies.primal-solution", "schema_version": "1.0.0"'
        + ', "math_status": "solved", "termination_reason": "optimal"'
        + ', "has_feasible_solution": true, "column_count": 13101'
        + ', "best_bound": 987.654321, "relative_gap": 0.0'
        + ', "receipt": {"solver": "ies.solver.highs@1.7.2"}'
        + ', "objective_value": '
    )
    pad_meta = max(0, (2 * 65536 - 3) - len(after_prefix))
    full_text = after_prefix + (" " * pad_meta) + "12345.6789}\n"
    assert len(full_text) > 2 * 65536
    boundary_path.write_text(full_text, encoding="utf-8")

    b_hdr, b_iter = iter_primal_solution_values(boundary_path)
    b_vals = list(b_iter)
    assert b_hdr.objective_value == pytest.approx(12345.6789)
    assert len(b_vals) == 13101
    assert b_vals[-1] == pytest.approx(123.456789)

    # Chunk boundary splits immediately after '.', 'e', 'e+', 'e-', and 'E'
    # both inside 'values' AND inside metadata placed after 'values'
    for split_num, expected_num in (
        ("1.25", 1.25),
        ("1e25", 1e25),
        ("1E+5", 1e5),
        ("1e-25", 1e-25),
    ):
        for cut in (1, 2, 3):
            if cut >= len(split_num):
                continue
            split_path = tmp_path / f"split_{expected_num}_{cut}.json"
            base_arr = '{"values": [' + ", ".join("1.0" for _ in range(13100)) + ", "
            pad_arr = " " * max(0, (65536 - cut) - len(base_arr))
            after = (
                base_arr
                + pad_arr
                + split_num
                + '], "schema": "ies.primal-solution", "schema_version": "1.0.0"'
                + ', "math_status": "solved", "termination_reason": "optimal"'
                + ', "has_feasible_solution": true, "column_count": 13101'
                + ', "best_bound": 1.0, "relative_gap": 0.0'
                + ', "receipt": {"solver": "ies.solver.highs@1.7.2"}'
                + ', "objective_value": '
            )
            pad_meta_num = " " * max(0, (2 * 65536 - cut) - len(after))
            split_path.write_text(after + pad_meta_num + split_num + "}\n", encoding="utf-8")
            s_hdr, s_iter = iter_primal_solution_values(split_path)
            s_vals = list(s_iter)
            assert s_hdr.objective_value == pytest.approx(expected_num)
            assert s_vals[-1] == pytest.approx(expected_num)

    # Non-feasible primal solution
    inf_path = write_primal_solution(tmp_path / "infeasible.json", _primal_infeasible_dict())
    inf_loaded = read_primal_solution(inf_path)
    assert inf_loaded.has_feasible_solution is False
    assert inf_loaded.values == ()


def test_primal_solution_rejects_invalid_fixtures(tmp_path: Path) -> None:
    bad_unknown = _primal_feasible_dict()
    bad_unknown["extra"] = 1
    with pytest.raises(StageIOError, match="unknown fields"):
        PrimalSolutionPayload.from_dict(bad_unknown)

    bad_bool = _primal_feasible_dict()
    bad_bool["values"] = [True, 0.0, 1.0]
    with pytest.raises(StageIOError, match="finite number"):
        PrimalSolutionPayload.from_dict(bad_bool)

    bad_bool_col = _primal_feasible_dict()
    bad_bool_col["column_count"] = True
    with pytest.raises(StageIOError, match="integer"):
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
    with pytest.raises(StageIOError, match="unknown fields"):
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


def test_engineering_solution_dual_state_roundtrip_and_completeness(tmp_path: Path) -> None:
    eng_dict = _engineering_feasible_dict()
    eng_validator = Draft202012Validator(load_engineering_solution_schema())
    eng_validator.validate(eng_dict)

    feasible = EngineeringSolutionPayload.from_dict(eng_dict)
    path = write_engineering_solution(tmp_path / "solution.json", feasible)
    assert read_engineering_solution(path) == feasible

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
        system_series={
            "sys.reserve": SystemSeries("reserve_balance", "margin", "kW", (10.0, 10.0, 10.0))
        },
        metrics={"num_variables": 3},
        model_sources={"schema": "ies.model-sources", "schema_version": "1.0.0"},
    )
    assert direct == feasible

    # Completeness boundary check against frozen expectation
    expectation = EngineeringSolutionExpectation(
        expected_steps=3,
        objective_unit="CNY",
        construction_units=(
            ExpectedConstructionUnit(
                unit_id="unit_pv1",
                instance_id="pv1",
                mode="optional",
                planning_variable_units={"capacity": "kW"},
            ),
        ),
        series={"pv1.power_out": ExpectedDeviceSeries("pv1", "power_out", "kW")},
        system_series={"sys.reserve": ExpectedSystemSeries("reserve_balance", "margin", "kW")},
    )
    assert validate_engineering_solution_completeness(feasible, expectation) == feasible

    # Standalone planning_variables roundtrip & completeness (without construction_units)
    standalone_dict = _engineering_feasible_dict()
    standalone_dict["construction_units"] = []
    standalone_dict["planning_variables"] = [
        {"planning_variable_id": "hp_cap", "value": 420.0, "unit": "kW"}
    ]
    eng_validator.validate(standalone_dict)
    standalone = EngineeringSolutionPayload.from_dict(standalone_dict)
    assert standalone.planning_variables == (ConstructionScale("hp_cap", 420.0, "kW"),)
    assert EngineeringSolutionPayload.from_dict(standalone.to_dict()) == standalone

    standalone_exp = EngineeringSolutionExpectation(
        expected_steps=3,
        objective_unit="CNY",
        construction_units=(),
        series={"pv1.power_out": ExpectedDeviceSeries("pv1", "power_out", "kW")},
        system_series={"sys.reserve": ExpectedSystemSeries("reserve_balance", "margin", "kW")},
        planning_variables={"hp_cap": "kW"},
    )
    assert validate_engineering_solution_completeness(standalone, standalone_exp) == standalone

    # Non-feasible: preserves exemption against frozen expectation
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
    eng_validator.validate(non_feasible_omitted.to_dict())
    assert non_feasible_omitted.construction_units is None
    assert non_feasible_omitted.series is None
    assert validate_engineering_solution_completeness(non_feasible_omitted, expectation) == (
        non_feasible_omitted
    )


def test_engineering_solution_rejects_invalid_and_incomplete_fixtures() -> None:
    missing_units = _engineering_feasible_dict()
    del missing_units["construction_units"]
    with pytest.raises(StageIOError, match="construction_units"):
        EngineeringSolutionPayload.from_dict(missing_units)

    missing_series = _engineering_feasible_dict()
    missing_series["series"] = None
    with pytest.raises(StageIOError, match="series"):
        EngineeringSolutionPayload.from_dict(missing_series)

    # Unequal continuous step counts (3-step device series vs 1-step system series)
    unequal_steps = _engineering_feasible_dict()
    unequal_steps["system_series"]["sys.reserve"]["values"] = [10.0]
    with pytest.raises(StageIOError, match="equal continuous step count"):
        EngineeringSolutionPayload.from_dict(unequal_steps)

    # Empty series values rejected
    empty_vals = _engineering_feasible_dict()
    empty_vals["series"]["pv1.power_out"]["values"] = []
    with pytest.raises(StageIOError, match="non-empty list"):
        EngineeringSolutionPayload.from_dict(empty_vals)

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
    with pytest.raises(StageIOError, match="mode"):
        EngineeringSolutionPayload.from_dict(bad_mode)

    bad_bool_scale = _engineering_feasible_dict()
    bad_bool_scale["construction_units"][0]["scales"][0]["value"] = False
    with pytest.raises(StageIOError, match="finite number"):
        EngineeringSolutionPayload.from_dict(bad_bool_scale)

    bad_extra = _engineering_feasible_dict()
    bad_extra["unexpected"] = 1
    with pytest.raises(StageIOError, match="unknown fields"):
        EngineeringSolutionPayload.from_dict(bad_extra)

    # Expectation completeness failures
    feasible = EngineeringSolutionPayload.from_dict(_engineering_feasible_dict())
    wrong_steps = EngineeringSolutionExpectation(
        expected_steps=4,
        objective_unit="CNY",
        construction_units=(
            ExpectedConstructionUnit("unit_pv1", "pv1", "optional", {"capacity": "kW"}),
        ),
        series={"pv1.power_out": ExpectedDeviceSeries("pv1", "power_out", "kW")},
        system_series={"sys.reserve": ExpectedSystemSeries("reserve_balance", "margin", "kW")},
    )
    with pytest.raises(StageIOError, match="expected_steps"):
        validate_engineering_solution_completeness(feasible, wrong_steps)

    missing_expected_unit = EngineeringSolutionExpectation(
        expected_steps=3,
        objective_unit="CNY",
        construction_units=(
            ExpectedConstructionUnit("unit_pv1", "pv1", "optional", {"capacity": "kW"}),
            ExpectedConstructionUnit("unit_bat1", "bat1", "optional", {"capacity": "kWh"}),
        ),
        series={"pv1.power_out": ExpectedDeviceSeries("pv1", "power_out", "kW")},
        system_series={"sys.reserve": ExpectedSystemSeries("reserve_balance", "margin", "kW")},
    )
    with pytest.raises(StageIOError, match="construction_units mismatch"):
        validate_engineering_solution_completeness(feasible, missing_expected_unit)

    wrong_series_unit = EngineeringSolutionExpectation(
        expected_steps=3,
        objective_unit="CNY",
        construction_units=(
            ExpectedConstructionUnit("unit_pv1", "pv1", "optional", {"capacity": "kW"}),
        ),
        series={"pv1.power_out": ExpectedDeviceSeries("pv1", "power_out", "MW")},
        system_series={"sys.reserve": ExpectedSystemSeries("reserve_balance", "margin", "kW")},
    )
    with pytest.raises(StageIOError, match="identity/unit mismatch"):
        validate_engineering_solution_completeness(feasible, wrong_series_unit)

    bad_non_feasible_pvars = _engineering_feasible_dict()
    bad_non_feasible_pvars["solver_summary"] = {
        "termination_reason": "infeasible",
        "math_status": "infeasible",
        "has_feasible_solution": False,
        "objective_value": None,
        "objective_unit": None,
        "best_bound": None,
        "relative_gap": None,
    }
    bad_non_feasible_pvars["construction_units"] = None
    bad_non_feasible_pvars["series"] = None
    bad_non_feasible_pvars["system_series"] = None
    bad_non_feasible_pvars["planning_variables"] = [
        {"planning_variable_id": "hp_cap", "value": 100.0, "unit": "kW"}
    ]
    with pytest.raises(StageIOError, match="Non-feasible"):
        EngineeringSolutionPayload.from_dict(bad_non_feasible_pvars)

    bad_dup_pvars = _engineering_feasible_dict()
    bad_dup_pvars["planning_variables"] = [
        {"planning_variable_id": "hp_cap", "value": 100.0, "unit": "kW"},
        {"planning_variable_id": "hp_cap", "value": 200.0, "unit": "kW"},
    ]
    with pytest.raises(StageIOError, match="Duplicate planning_variable_id"):
        EngineeringSolutionPayload.from_dict(bad_dup_pvars)

    missing_pvar_exp = EngineeringSolutionExpectation(
        expected_steps=3,
        objective_unit="CNY",
        construction_units=(),
        series={"pv1.power_out": ExpectedDeviceSeries("pv1", "power_out", "kW")},
        system_series={"sys.reserve": ExpectedSystemSeries("reserve_balance", "margin", "kW")},
        planning_variables={"hp_cap": "kW"},
    )
    no_unit_feasible = _engineering_feasible_dict()
    no_unit_feasible["construction_units"] = []
    with pytest.raises(StageIOError, match="missing required planning variable 'hp_cap'"):
        validate_engineering_solution_completeness(
            EngineeringSolutionPayload.from_dict(no_unit_feasible),
            missing_pvar_exp,
        )

    wrong_pvar_unit = _engineering_feasible_dict()
    wrong_pvar_unit["construction_units"] = []
    wrong_pvar_unit["planning_variables"] = [
        {"planning_variable_id": "hp_cap", "value": 100.0, "unit": "MW"}
    ]
    with pytest.raises(StageIOError, match="unit mismatch"):
        validate_engineering_solution_completeness(
            EngineeringSolutionPayload.from_dict(wrong_pvar_unit),
            missing_pvar_exp,
        )


# ---------------------------------------------------------------------------
# Universal variable_map tests (all 19 documented record tags + linear units)
# ---------------------------------------------------------------------------


def test_variable_map_all_documented_tags_linear_units_and_streaming(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    hdr_dict = _variable_map_header_dict(column_count=4)
    Draft202012Validator(load_variable_map_header_schema()).validate(hdr_dict)
    header = VariableMapHeader.from_dict(hdr_dict)

    assert apply_linear_conversion(5000.0, scale=header.objective_scale, offset=header.objective_offset) == (
        pytest.approx(105.0)
    )

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
        ["units", [[0, "kW", 0.001, 0.0], [1, "kW", 0.001, 0.0], [3, "kWh", 1.0, 5.0]]],
        ["component", "upfront_capex", "CNY", "gross", 0],
        ["facts", {"currency": "CNY", "construction_total": {"expression": 0}}],
        [
            "series",
            {
                "series_id": "pv1.power_out",
                "instance_id": "pv1",
                "variable_id": "power_out",
                "unit": "kW",
                "scale": 0.001,
                "offset": 0.0,
            },
        ],
        ["positions", [1, 2]],
        ["series_end"],
        [
            "series",
            {
                "series_id": "sys.reserve",
                "block": "res",
                "variable": "margin",
                "unit": "kW",
                "scale": 0.001,
                "offset": 1.0,
                "extensions": {"acme.source": "reserve"},
            },
        ],
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
    streamed = list(
        iter_variable_map_records(bundle_dir / "input" / "parameters.jsonl", column_count=4)
    )
    assert len(streamed) == len(records)


def test_variable_map_rejects_invalid_headers_fallbacks_and_records(tmp_path: Path) -> None:
    missing_schema = _variable_map_header_dict()
    del missing_schema["schema"]
    with pytest.raises(StageIOError, match="missing required fields"):
        VariableMapHeader.from_dict(missing_schema)

    zero_scale = _variable_map_header_dict()
    zero_scale["objective_scale"] = 0.0
    with pytest.raises(StageIOError, match="non-zero"):
        VariableMapHeader.from_dict(zero_scale)

    bad_hdr = _variable_map_header_dict()
    bad_hdr["column_count"] = True
    with pytest.raises(StageIOError, match="integer"):
        VariableMapHeader.from_dict(bad_hdr)

    bad_hdr_nan = _variable_map_header_dict()
    bad_hdr_nan["objective_offset"] = float("inf")
    with pytest.raises(StageIOError, match="non-finite"):
        VariableMapHeader.from_dict(bad_hdr_nan)

    bad_hdr_path = _variable_map_header_dict()
    bad_hdr_path["records"] = "../escape.jsonl"
    with pytest.raises(StageIOError, match="escapes"):
        VariableMapHeader.from_dict(bad_hdr_path)

    bad_hdr_extra = _variable_map_header_dict()
    bad_hdr_extra["solver"] = "ies.solver.highs@1.7.2"
    with pytest.raises(StageIOError, match="unknown fields"):
        VariableMapHeader.from_dict(bad_hdr_extra)

    # No silent alternate-path fallback when canonical input/parameters.json or declared records is missing
    fallback_dir = tmp_path / "fallback_bundle"
    fallback_dir.mkdir()
    (fallback_dir / "parameters.json").write_text(
        json.dumps(_variable_map_header_dict()), encoding="utf-8"
    )
    with pytest.raises(StageIOError, match="does not exist"):
        read_variable_map(fallback_dir)

    missing_rec_dir = tmp_path / "missing_rec_bundle"
    (missing_rec_dir / "input").mkdir(parents=True)
    (missing_rec_dir / "input" / "parameters.json").write_text(
        json.dumps(_variable_map_header_dict()), encoding="utf-8"
    )
    (missing_rec_dir / "parameters.jsonl").write_text('["column_names", ["x"]]\n', encoding="utf-8")
    with pytest.raises(StageIOError, match="does not exist"):
        read_variable_map(missing_rec_dir)

    # Bad NDJSON records
    invalid_cases = [
        (["unknown_tag", 1], "Unknown variable_map record tag"),
        (["epigraph", 4], "out of range"),
        (["epigraph", True], "must be an integer"),
        (["expression", 1, 0.0], "strictly 0-based sequential"),
        (["terms", [[0, 1.0]]], "open 'expression' block"),
        (["units", [[0, "kW"]]], "4-tuple"),
        (["units", [[0, "kW", 0.0, 0.0]]], "non-zero"),
        (["component", "c", "CNY", "gross", 0], "undefined expression"),
        (["series", {"series_id": "s1", "unit": "kW"}], "either device keys"),
        (
            [
                "series",
                {
                    "series_id": "s1",
                    "instance_id": "pv1",
                    "variable_id": "p",
                    "unit": "kW",
                    "scale": 1.0,
                    "offset": 0.0,
                    "unexpected_core": 123,
                },
            ],
            "unknown core fields",
        ),
        (
            [
                "series",
                {
                    "series_id": "s1",
                    "instance_id": "pv1",
                    "variable_id": "p",
                    "unit": "kW",
                    "scale": 0.0,
                    "offset": 0.0,
                },
            ],
            "non-zero",
        ),
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
    rep_dict = _evaluation_report_dict()
    Draft202012Validator(load_evaluation_report_schema()).validate(rep_dict)
    report = EvaluationReportPayload.from_dict(rep_dict)
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
    with pytest.raises(StageIOError, match="unknown fields"):
        EvaluationReportPayload.from_dict(bad_extra)

    bad_bool_metric = deepcopy(_evaluation_report_dict())
    bad_bool_metric["metrics"][0]["value"] = True
    with pytest.raises(StageIOError, match="boolean"):
        EvaluationReportPayload.from_dict(bad_bool_metric)

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

    # Parser/schema parity: raw types and explicit null on non-nullable optional fields
    schema_validator = Draft202012Validator(load_evaluation_report_schema())
    parity_invalid_cases = []

    bad_series_str = deepcopy(_evaluation_report_dict())
    bad_series_str["charts"][0]["series"][0]["values"] = ""
    parity_invalid_cases.append(bad_series_str)

    bad_series_dict = deepcopy(_evaluation_report_dict())
    bad_series_dict["charts"][0]["series"][0]["values"] = {"a": 1}
    parity_invalid_cases.append(bad_series_dict)

    bad_null_chart_type = deepcopy(_evaluation_report_dict())
    bad_null_chart_type["charts"][0]["chart_type"] = None
    parity_invalid_cases.append(bad_null_chart_type)

    bad_null_x_values = deepcopy(_evaluation_report_dict())
    bad_null_x_values["charts"][0]["x_values"] = None
    parity_invalid_cases.append(bad_null_x_values)

    bad_null_extensions = deepcopy(_evaluation_report_dict())
    bad_null_extensions["extensions"] = None
    parity_invalid_cases.append(bad_null_extensions)

    for bad_case in parity_invalid_cases:
        assert not schema_validator.is_valid(bad_case)
        with pytest.raises(StageIOError):
            EvaluationReportPayload.from_dict(bad_case)

    # Also verify DeviceSeries / SystemSeries preserve raw values types
    bad_dev_series = _engineering_feasible_dict()
    bad_dev_series["series"]["pv1.power_out"]["values"] = "12"
    with pytest.raises(StageIOError, match="non-empty list"):
        EngineeringSolutionPayload.from_dict(bad_dev_series)

    bad_sys_series = _engineering_feasible_dict()
    bad_sys_series["system_series"]["sys.reserve"]["values"] = {"a": 1.0}
    with pytest.raises(StageIOError, match="non-empty list"):
        EngineeringSolutionPayload.from_dict(bad_sys_series)


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
            "compatibility": {"pies": ">=1.2.0,<2.0.0"},
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
