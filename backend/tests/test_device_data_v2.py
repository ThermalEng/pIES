"""ies.device-data 2.0.0 的 step 契约测试。"""

from __future__ import annotations

import json

from iesplan.devices.contracts2 import (
    DeviceInfo,
    DeviceModelDocument,
    InterfaceSpec,
    SourceSpec,
    content_sha256,
)
from iesplan.devices.datacontract2 import (
    SCHEMA_ID,
    SCHEMA_VERSION,
    STEP_COL,
    canonicalize_device_data_v2,
    parse_data_file_v2,
    parse_metadata_v2,
    pending_from_result,
    periodic_rows,
    summary_json_v2,
)


def _device_doc() -> DeviceModelDocument:
    return DeviceModelDocument(
        device=DeviceInfo(id="acme.device.electric_load", names={"zh-CN": "电负荷"}),
        interfaces={
            "electric_demand": InterfaceSpec(
                id="electric_demand", type="predefined", carrier="electricity", unit="kW",
                valid_range=(0.0, 1000.0),
                source=SourceSpec(mode="data_repeat", data_ref="typical_day_load"),
            ),
            "ambient_temperature": InterfaceSpec(
                id="ambient_temperature", type="predefined", carrier="environment", unit="°C",
                valid_range=(-50.0, 60.0),
                source=SourceSpec(mode="data_predict", data_ref="weather_prediction"),
            ),
            "fixed_temperature": InterfaceSpec(
                id="fixed_temperature", type="predefined", carrier="environment", unit="°C",
                valid_range=(-50.0, 60.0), source=SourceSpec(mode="constant", value=25.0),
            ),
        },
    )


def _csv_text(
    *,
    source_mode: str = "data_predict",
    resolution: str = "1h",
    period: str | None = None,
    columns: list[str] | None = None,
    units: dict[str, str] | None = None,
    rows: list[str] | None = None,
    device_id: str = "acme.device.electric_load",
    device_sha: str | None = None,
    prepared: bool = False,
    baseline_sha: str = "a" * 64,
    point_count: int | None = None,
) -> str:
    if columns is None:
        columns = ["ambient_temperature"] if source_mode == "data_predict" else [
            "fixed_temperature" if source_mode == "constant" else "electric_demand"
        ]
    if units is None:
        units = {column: ("°C" if "temperature" in column else "kW") for column in columns}
    if device_sha is None:
        device_sha = content_sha256(_device_doc())
    lines = [
        f"# schema: {SCHEMA_ID}",
        f"# schema_version: {SCHEMA_VERSION}",
        "# dataset_id: campus.data.series",
        f"# device_id: {device_id}",
        f"# device_content_sha256: {device_sha}",
        f"# source_mode: {source_mode}",
        f"# resolution: {resolution}",
    ]
    if period:
        lines.append(f"# period: {period}")
    if prepared:
        lines.extend([
            f"# project_baseline_sha256: {baseline_sha}",
            f"# point_count: {point_count if point_count is not None else len(rows or [])}",
            "# prepared: true",
        ])
    for column in columns:
        lines.append(f"# unit.{column}: {units[column]}")
    lines.append("step," + ",".join(columns))
    lines.extend(rows or ["0,15", "1,16", "2,17"])
    return "\n".join(lines) + "\n"


def _codes(result) -> set[str]:
    return {diag.code for diag in result.diagnostics if diag.blocking}


class TestMetadata:
    def test_valid_raw_metadata_has_no_time_fields(self) -> None:
        lines = _csv_text().splitlines()[:8]
        meta, diags = parse_metadata_v2(lines)
        assert not any(diag.blocking for diag in diags)
        assert meta.resolution == "1h"
        assert not hasattr(meta, "timestamp_mode")

    def test_unknown_timezone_metadata_rejected(self) -> None:
        text = _csv_text().replace("# resolution: 1h", "# resolution: 1h\n# timestamp_mode: utc")
        result = canonicalize_device_data_v2(text.encode(), _device_doc())
        assert "DATA-META-002" in _codes(result)

    def test_repeat_requires_period(self) -> None:
        result = canonicalize_device_data_v2(
            _csv_text(source_mode="data_repeat", rows=[f"{i},{i}" for i in range(24)]).encode(),
            _device_doc(),
        )
        assert "DATA-META-006" in _codes(result)

    def test_prepared_requires_baseline_and_point_count(self) -> None:
        text = _csv_text().replace("# resolution: 1h", "# resolution: 1h\n# prepared: true")
        result = canonicalize_device_data_v2(text.encode(), _device_doc())
        assert "DATA-META-002" in _codes(result)

    def test_constant_only_allowed_after_preparation(self) -> None:
        raw = canonicalize_device_data_v2(_csv_text(source_mode="constant").encode(), _device_doc())
        prepared = canonicalize_device_data_v2(
            _csv_text(source_mode="constant", prepared=True, point_count=3).encode(), _device_doc()
        )
        assert "DATA-META-011" in _codes(raw)
        assert not _codes(prepared)


class TestDialectAndSteps:
    def test_header_must_start_with_step(self) -> None:
        text = _csv_text().replace("step,ambient_temperature", "timestamp,ambient_temperature")
        parsed, diags = parse_data_file_v2(text.encode())
        assert parsed is None
        assert any(diag.code == "DATA-COL-005" for diag in diags)

    def test_raw_sparse_steps_are_valid(self) -> None:
        result = canonicalize_device_data_v2(
            _csv_text(rows=["0,15", "2,16", "5,17"]).encode(), _device_doc()
        )
        assert not _codes(result)
        assert result.steps == [0, 2, 5]

    def test_step_must_be_nonnegative_integer(self) -> None:
        result = canonicalize_device_data_v2(_csv_text(rows=["0.5,15"]).encode(), _device_doc())
        assert "DATA-STEP-001" in _codes(result)

    def test_raw_step_must_strictly_increase(self) -> None:
        for rows in (["0,15", "0,16"], ["2,15", "1,16"]):
            result = canonicalize_device_data_v2(_csv_text(rows=list(rows)).encode(), _device_doc())
            assert "DATA-STEP-002" in _codes(result)

    def test_prepared_steps_are_zero_based_and_contiguous(self) -> None:
        result = canonicalize_device_data_v2(
            _csv_text(prepared=True, rows=["0,15", "2,16"], point_count=2).encode(), _device_doc()
        )
        assert "DATA-STEP-003" in _codes(result)

    def test_prepared_point_count_matches_rows(self) -> None:
        result = canonicalize_device_data_v2(
            _csv_text(prepared=True, rows=["0,15", "1,16"], point_count=3).encode(), _device_doc()
        )
        assert "DATA-STEP-004" in _codes(result)

    def test_expected_baseline_sha_is_enforced(self) -> None:
        result = canonicalize_device_data_v2(
            _csv_text(prepared=True, point_count=3).encode(), _device_doc(),
            expected_project_baseline_sha256="b" * 64,
        )
        assert "DATA-META-012" in _codes(result)

    def test_repeat_period_row_count(self) -> None:
        rows = [f"{i},{i}" for i in range(24)]
        valid = canonicalize_device_data_v2(
            _csv_text(source_mode="data_repeat", period="day", rows=rows).encode(), _device_doc()
        )
        invalid = canonicalize_device_data_v2(
            _csv_text(source_mode="data_repeat", period="day", rows=rows[:-1]).encode(), _device_doc()
        )
        assert not _codes(valid)
        assert "DATA-STEP-004" in _codes(invalid)
        assert periodic_rows("30min", "week") == 336


class TestBindingColumnsAndValues:
    def test_device_id_and_digest_are_fixed(self) -> None:
        wrong_id = canonicalize_device_data_v2(
            _csv_text(device_id="acme.device.other").encode(), _device_doc()
        )
        wrong_sha = canonicalize_device_data_v2(
            _csv_text(device_sha="b" * 64).encode(), _device_doc()
        )
        assert "DATA-META-008" in _codes(wrong_id)
        assert "DATA-META-010" in _codes(wrong_sha)

    def test_source_mode_mismatch_is_rejected(self) -> None:
        result = canonicalize_device_data_v2(
            _csv_text(
                source_mode="data_repeat", period="day", columns=["ambient_temperature"],
                units={"ambient_temperature": "°C"}, rows=[f"{i},{i}" for i in range(24)],
            ).encode(),
            _device_doc(),
        )
        assert "DATA-META-011" in _codes(result)

    def test_unknown_duplicate_and_missing_columns_are_rejected(self) -> None:
        unknown = _csv_text().replace(
            "step,ambient_temperature", "step,ambient_temperature,extra"
        ).replace("0,15", "0,15,1").replace("1,16", "1,16,1").replace("2,17", "2,17,1")
        duplicate = _csv_text().replace(
            "step,ambient_temperature", "step,ambient_temperature,ambient_temperature"
        )
        missing = (
            _csv_text()
            .replace("step,ambient_temperature", "step")
            .replace(",15", "")
            .replace(",16", "")
            .replace(",17", "")
        )
        assert "DATA-COL-003" in _codes(canonicalize_device_data_v2(unknown.encode(), _device_doc()))
        assert "DATA-COL-004" in _codes(canonicalize_device_data_v2(duplicate.encode(), _device_doc()))
        assert "DATA-COL-005" in _codes(canonicalize_device_data_v2(missing.encode(), _device_doc()))

    def test_units_check_dimension(self) -> None:
        good = _csv_text(
            source_mode="data_repeat", period="day", units={"electric_demand": "W"},
            rows=[f"{i},{i}" for i in range(24)],
        )
        bad = good.replace("unit.electric_demand: W", "unit.electric_demand: kWh")
        assert not _codes(canonicalize_device_data_v2(good.encode(), _device_doc()))
        assert "DATA-COL-006" in _codes(canonicalize_device_data_v2(bad.encode(), _device_doc()))

    def test_missing_nonfinite_and_out_of_range_values_block(self) -> None:
        for value, code in (("", "DATA-VAL-002"), ("NaN", "DATA-VAL-001"), ("70", "DATA-VAL-001")):
            result = canonicalize_device_data_v2(_csv_text(rows=[f"0,{value}"]).encode(), _device_doc())
            assert code in _codes(result)


class TestCanonicalAndPending:
    def test_same_semantics_same_sha_and_step_output(self) -> None:
        text = _csv_text(rows=["0,15", "2,16"])
        first = canonicalize_device_data_v2(text.encode(), _device_doc())
        second = canonicalize_device_data_v2(text.encode(), _device_doc())
        assert first.canonical_sha256 == second.canonical_sha256
        canonical = first.canonical_csv_bytes().decode()
        assert "step,ambient_temperature" in canonical
        assert "timestamp" not in canonical
        assert "timezone" not in canonical

    def test_summary_and_pending_preserve_prepared_binding(self) -> None:
        result = canonicalize_device_data_v2(
            _csv_text(prepared=True, point_count=3).encode(), _device_doc()
        )
        report = json.loads(summary_json_v2(result))
        pending = pending_from_result(result)
        assert report["column_order"] == [STEP_COL, "ambient_temperature"]
        assert report["transformations"] == ["steps_validated", "units_declared", "values_checked"]
        assert pending is not None
        assert pending.prepared is True
        assert pending.point_count == 3
        assert pending.column_order == (STEP_COL, "ambient_temperature")

    def test_blocking_result_cannot_be_pending(self) -> None:
        result = canonicalize_device_data_v2(_csv_text(rows=["0,70"]).encode(), _device_doc())
        assert pending_from_result(result) is None
