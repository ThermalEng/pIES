"""ies.device-data 1.0.0 契约测试(0.6.0 事项 1)。

覆盖:
- 机器可读 schema 存在且声明版本/必需字段;
- 合法/非法样例的规范化与诊断;
- 同一语义输入产生同一规范摘要(唯一纯函数);
- 时间/单位/长度不一致时明确失败。

本测试直接调用纯函数 canonicalize_device_data / parse_metadata, 不依赖 DB。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from iesplan.devices.datacontract import (
    SCHEMA_ID,
    SCHEMA_VERSION,
    canonicalize_device_data,
    parse_metadata,
    parse_data_file,
    summary_json,
)
from iesplan.core.diagnostics import Diagnostic, make_diag

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "iesplan/devices/schema/device-data-v1.0.0.schema.json"


@dataclass(frozen=True)
class _DataInput:
    column_id: str
    value_type: str = "number"
    quantity: str | None = None
    unit: str = ""
    required: bool = True
    minimum: float | None = None
    maximum: float | None = None


@dataclass(frozen=True)
class _Series:
    key: str
    unit: str = "kWh"
    resolution: str = "1h"
    required: bool = True
    period: str | None = None


@dataclass(frozen=True)
class _FakeDescriptor:
    """最小公开设备描述(与 devices.DeviceModelDescriptor 字段签名一致)。"""

    type_id: str
    version: str
    data_inputs: dict | None = None
    time_series: dict | None = None


def _desc(*cols: _DataInput, legacy_series: list[_Series] | None = None) -> _FakeDescriptor:
    return _FakeDescriptor(
        type_id="ies.device.test",
        version="1.0.0",
        data_inputs={c.column_id: c for c in cols},
        time_series={"inputs": legacy_series or [], "outputs": []},
    )


def _e_load_desc() -> _FakeDescriptor:
    return _desc(
        _DataInput("e_load", unit="kWh", minimum=0.0),
    )


def _valid_csv_text(*, mode: str = "fixed_offset", offset: int = 480, n: int = 3, rows: list[str] | None = None) -> str:
    lines = [
        "# schema: ies.device-data",
        "# schema_version: 1.0.0",
        "# dataset_id: campus_electric_load_2025",
        "# device_model: ies.device.electric_load@1.2.0",
        "# series_mode: timeline",
        "# resolution: 1h",
        f"# timestamp_mode: {mode}",
    ]
    if mode == "fixed_offset":
        lines.append(f"# fixed_utc_offset_minutes: {offset}")
    lines.append("# unit.e_load: kWh")
    lines.append("timestamp,e_load")
    if rows is None:
        for i in range(n):
            ts = f"2025-01-01T{i:02d}:00:00"
            lines.append(f"{ts},{100.0 + i}")
    else:
        lines.extend(rows)
    return "\n".join(lines) + "\n"


def _valid_csv_bytes(*args, **kw) -> bytes:
    return _valid_csv_text(*args, **kw).encode("utf-8")


# ---------------------------------------------------------------------------
# 机器可读 schema
# ---------------------------------------------------------------------------


class TestMachineReadableSchema:
    def test_schema_file_exists_and_valid_json(self) -> None:
        assert SCHEMA_PATH.exists()
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        assert schema["$id"] == "ies.device-data/1.0.0"
        assert schema["title"] == "ies.device-data 1.0.0"
        assert schema["properties"]["schema"]["const"] == "ies.device-data"
        assert schema["properties"]["schema_version"]["const"] == "1.0.0"
        assert "required" in schema

    def test_schema_required_fields(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        required = schema["required"]
        for key in (
            "schema",
            "schema_version",
            "dataset_id",
            "device_model",
            "series_mode",
            "resolution",
            "timestamp_mode",
            "units",
            "columns",
        ):
            assert key in required


# ---------------------------------------------------------------------------
# 元数据解析
# ---------------------------------------------------------------------------


class TestParseMetadata:
    def test_valid_metadata(self) -> None:
        text = "\n".join(
            [
                "# schema: ies.device-data",
                "# schema_version: 1.0.0",
                "# dataset_id: abc",
                "# device_model: ies.device.electric_load@1.2.0",
                "# series_mode: timeline",
                "# resolution: 1h",
                "# timestamp_mode: fixed_offset",
                "# fixed_utc_offset_minutes: 480",
                "# unit.e_load: kWh",
                "# note.source: test",
            ]
        )
        meta, diags = parse_metadata(text.splitlines())
        assert diags == []
        assert meta.schema_id == "ies.device-data"
        assert meta.dataset_id == "abc"
        assert meta.fixed_utc_offset_minutes == 480
        assert meta.units == {"e_load": "kWh"}
        assert meta.notes == {"source": "test"}

    def test_missing_required_key(self) -> None:
        text = "\n".join(["# schema: ies.device-data", "# schema_version: 1.0.0"])
        meta, diags = parse_metadata(text.splitlines())
        codes = [d.code for d in diags]
        assert "DATA-META-002" in codes
        assert all(d.blocking for d in diags)

    def test_unknown_core_field_rejected(self) -> None:
        text = "\n".join(
            [
                "# schema: ies.device-data",
                "# schema_version: 1.0.0",
                "# dataset_id: abc",
                "# device_model: ies.device.electric_load@1.2.0",
                "# series_mode: timeline",
                "# resolution: 1h",
                "# timestamp_mode: fixed_offset",
                "# fixed_utc_offset_minutes: 480",
                "# unknown_field: x",
            ]
        )
        _, diags = parse_metadata(text.splitlines())
        assert any(d.code == "DATA-META-002" for d in diags)

    def test_duplicate_key(self) -> None:
        text = "\n".join(
            [
                "# schema: ies.device-data",
                "# schema_version: 1.0.0",
                "# dataset_id: a",
                "# dataset_id: b",
                "# device_model: ies.device.electric_load@1.2.0",
                "# series_mode: timeline",
                "# resolution: 1h",
                "# timestamp_mode: fixed_offset",
                "# fixed_utc_offset_minutes: 480",
            ]
        )
        _, diags = parse_metadata(text.splitlines())
        assert any(d.code == "DATA-META-001" for d in diags)

    def test_wrong_schema_id(self) -> None:
        text = "\n".join(
            [
                "# schema: ies.device-model",
                "# schema_version: 1.0.0",
                "# dataset_id: a",
                "# device_model: ies.device.electric_load@1.2.0",
                "# series_mode: timeline",
                "# resolution: 1h",
                "# timestamp_mode: fixed_offset",
                "# fixed_utc_offset_minutes: 480",
            ]
        )
        _, diags = parse_metadata(text.splitlines())
        assert any(d.code == "DATA-META-003" for d in diags)

    def test_fixed_offset_required(self) -> None:
        text = "\n".join(
            [
                "# schema: ies.device-data",
                "# schema_version: 1.0.0",
                "# dataset_id: a",
                "# device_model: ies.device.electric_load@1.2.0",
                "# series_mode: timeline",
                "# resolution: 1h",
                "# timestamp_mode: fixed_offset",
            ]
        )
        _, diags = parse_metadata(text.splitlines())
        assert any(d.code == "DATA-META-005" for d in diags)

    def test_offset_out_of_range(self) -> None:
        text = "\n".join(
            [
                "# schema: ies.device-data",
                "# schema_version: 1.0.0",
                "# dataset_id: a",
                "# device_model: ies.device.electric_load@1.2.0",
                "# series_mode: timeline",
                "# resolution: 1h",
                "# timestamp_mode: fixed_offset",
                "# fixed_utc_offset_minutes: 900",
            ]
        )
        _, diags = parse_metadata(text.splitlines())
        assert any(d.code == "DATA-META-007" for d in diags)

    def test_periodic_requires_period(self) -> None:
        text = "\n".join(
            [
                "# schema: ies.device-data",
                "# schema_version: 1.0.0",
                "# dataset_id: a",
                "# device_model: ies.device.electric_load@1.2.0",
                "# series_mode: periodic",
                "# resolution: 1h",
                "# timestamp_mode: fixed_offset",
                "# fixed_utc_offset_minutes: 480",
            ]
        )
        _, diags = parse_metadata(text.splitlines())
        assert any(d.code == "DATA-META-006" for d in diags)

    def test_invalid_enum(self) -> None:
        text = "\n".join(
            [
                "# schema: ies.device-data",
                "# schema_version: 1.0.0",
                "# dataset_id: a",
                "# device_model: ies.device.electric_load@1.2.0",
                "# series_mode: bogus",
                "# resolution: 1h",
                "# timestamp_mode: fixed_offset",
                "# fixed_utc_offset_minutes: 480",
            ]
        )
        _, diags = parse_metadata(text.splitlines())
        assert any(d.code == "DATA-META-004" and "series_mode" in str(d.params.get("field")) for d in diags)


# ---------------------------------------------------------------------------
# 方言
# ---------------------------------------------------------------------------


class TestDialect:
    def test_crlf_warns(self) -> None:
        text = _valid_csv_text().replace("\n", "\r\n")
        parsed, diags = parse_data_file(text.encode("utf-8"))
        assert any(d.code == "DATA-DIAL-001" and not d.blocking for d in diags)

    def test_bom_warns(self) -> None:
        text = "﻿" + _valid_csv_text()
        parsed, diags = parse_data_file(text.encode("utf-8"))
        assert any(d.code == "DATA-DIAL-001" and not d.blocking for d in diags)

    def test_row_width_mismatch_blocking(self) -> None:
        text = _valid_csv_text()
        text = text.replace("\n2025-01-01T00:00:00,100.0", "\n2025-01-01T00:00:00")
        parsed, diags = parse_data_file(text.encode("utf-8"))
        assert any(d.code == "DATA-DIAL-001" and d.blocking for d in diags)

    def test_non_finite_cell_blocking(self) -> None:
        text = _valid_csv_text(rows=["2025-01-01T00:00:00,nan"])
        parsed, diags = parse_data_file(text.encode("utf-8"))
        assert any(d.code == "DATA-DIAL-001" and d.blocking for d in diags)

    def test_formula_prefix_warns(self) -> None:
        text = _valid_csv_text(rows=["2025-01-01T00:00:00,=SUM(A1)"])
        parsed, diags = parse_data_file(text.encode("utf-8"))
        assert any(d.code == "DATA-DIAL-001" and not d.blocking for d in diags)

    def test_thousands_separator_rejected(self) -> None:
        """带千位分隔符的数值(RFC 4180 引号字段)必须拒绝, 不静默去逗号。"""
        text = _valid_csv_text(rows=['2025-01-01T00:00:00,"1,000"'])
        result = canonicalize_device_data(text.encode("utf-8"), _e_load_desc())
        assert any(d.code == "DATA-VAL-001" for d in result.diagnostics)


# ---------------------------------------------------------------------------
# 规范化与摘要
# ---------------------------------------------------------------------------


class TestCanonicalize:
    def test_valid_timeline_fixed_offset(self) -> None:
        result = canonicalize_device_data(_valid_csv_bytes(), _e_load_desc())
        assert not any(d.blocking for d in result.diagnostics), [d.to_dict() for d in result.diagnostics]
        assert result.raw_sha256
        assert result.canonical_sha256
        # UTC = 本地 - 偏移(480)
        assert result.utc_timestamps[0].hour == 16  # 本地 00:00(+8) → UTC 前一天 16:00

    def test_valid_utc_mode(self) -> None:
        text = _valid_csv_text(mode="utc")
        rows = [
            "2024-12-31T16:00:00Z,100.0",
            "2024-12-31T17:00:00Z,101.0",
            "2024-12-31T18:00:00Z,102.0",
        ]
        text = "\n".join(text.splitlines()[:8] + ["timestamp,e_load"] + rows) + "\n"
        result = canonicalize_device_data(text.encode("utf-8"), _e_load_desc())
        assert not any(d.blocking for d in result.diagnostics), [d.to_dict() for d in result.diagnostics]
        assert result.utc_timestamps[0].tzinfo is not None

    def test_mixed_zone_rejected(self) -> None:
        text = _valid_csv_text(rows=[
            "2024-12-31T16:00:00Z,100.0",
            "2025-01-01T01:00:00,101.0",
        ])
        result = canonicalize_device_data(text.encode("utf-8"), _e_load_desc())
        assert any(d.code == "DATA-TIME-003" for d in result.diagnostics)

    def test_unknown_column_rejected(self) -> None:
        text = _valid_csv_text()
        text = text.replace("timestamp,e_load", "timestamp,e_load,extra_col")
        text = text.replace("\n2025-01-01T00:00:00,100.0", "\n2025-01-01T00:00:00,100.0,1")
        text = text.replace("\n2025-01-01T01:00:00,101.0", "\n2025-01-01T01:00:00,101.0,1")
        text = text.replace("\n2025-01-01T02:00:00,102.0", "\n2025-01-01T02:00:00,102.0,1")
        result = canonicalize_device_data(text.encode("utf-8"), _e_load_desc())
        assert any(d.code == "DATA-COL-003" for d in result.diagnostics)

    def test_duplicate_column_rejected(self) -> None:
        text = _valid_csv_text()
        text = text.replace("timestamp,e_load", "timestamp,e_load,e_load")
        result = canonicalize_device_data(text.encode("utf-8"), _e_load_desc())
        assert any(d.code == "DATA-COL-004" for d in result.diagnostics)

    def test_required_column_missing_rejected(self) -> None:
        text = _valid_csv_text()
        text = text.replace("timestamp,e_load", "timestamp")
        for i in range(3):
            text = text.replace(f"\n2025-01-01T0{i}:00:00,{100.0 + i}", f"\n2025-01-01T0{i}:00:00")
        result = canonicalize_device_data(text.encode("utf-8"), _e_load_desc())
        assert any(d.code == "DATA-COL-005" for d in result.diagnostics)

    def test_unit_mismatch_rejected(self) -> None:
        text = _valid_csv_text()
        text = text.replace("# unit.e_load: kWh", "# unit.e_load: MW")
        result = canonicalize_device_data(text.encode("utf-8"), _e_load_desc())
        assert any(d.code == "DATA-COL-006" for d in result.diagnostics)

    def test_range_out_rejected_no_truncate(self) -> None:
        text = _valid_csv_text(rows=["2025-01-01T00:00:00,-5.0"])
        result = canonicalize_device_data(text.encode("utf-8"), _e_load_desc())
        assert any(d.code == "DATA-VAL-001" for d in result.diagnostics)

    def test_missing_value_not_allowed_rejected(self) -> None:
        text = _valid_csv_text(rows=["2025-01-01T00:00:00,"])
        result = canonicalize_device_data(text.encode("utf-8"), _e_load_desc())
        assert any(d.code == "DATA-VAL-002" for d in result.diagnostics)

    def test_out_of_order_rejected(self) -> None:
        text = _valid_csv_text(rows=[
            "2025-01-01T02:00:00,102.0",
            "2025-01-01T00:00:00,100.0",
            "2025-01-01T01:00:00,101.0",
        ])
        result = canonicalize_device_data(text.encode("utf-8"), _e_load_desc())
        assert any(d.code == "DATA-TIME-001" or d.code == "DATA-TS-005" for d in result.diagnostics)

    def test_same_semantics_same_summary(self) -> None:
        """同一语义输入(同一文件两次) → 同一规范摘要。"""
        text = _valid_csv_text()
        r1 = canonicalize_device_data(text.encode("utf-8"), _e_load_desc())
        r2 = canonicalize_device_data(text.encode("utf-8"), _e_load_desc())
        assert r1.canonical_sha256 == r2.canonical_sha256
        assert summary_json(r1) == summary_json(r2)

    def test_utc_and_offset_same_instant_same_summary(self) -> None:
        """同一 UTC 瞬时(UTC 直接书写 vs fixed_offset 换算) → 同一规范摘要。"""
        desc = _e_load_desc()
        offset_text = _valid_csv_text(mode="fixed_offset", offset=480, n=1)
        utc_text = _valid_csv_text(mode="utc", n=1)
        utc_rows = ["2024-12-31T16:00:00Z,100.0"]
        utc_text = "\n".join(utc_text.splitlines()[:8] + ["timestamp,e_load"] + utc_rows) + "\n"
        r1 = canonicalize_device_data(offset_text.encode("utf-8"), desc)
        r2 = canonicalize_device_data(utc_text.encode("utf-8"), desc)
        assert not any(d.blocking for d in r1.diagnostics)
        assert not any(d.blocking for d in r2.diagnostics)
        assert r1.canonical_sha256 == r2.canonical_sha256

    def test_periodic_row_count_check(self) -> None:
        """periodic day 24 行合法; 行数不符阻断。"""
        desc = _desc(_DataInput("e_load", unit="kWh", minimum=0.0))
        lines = [
            "# schema: ies.device-data",
            "# schema_version: 1.0.0",
            "# dataset_id: periodic_day",
            "# device_model: ies.device.electric_load@1.2.0",
            "# series_mode: periodic",
            "# resolution: 1h",
            "# timestamp_mode: fixed_offset",
            "# fixed_utc_offset_minutes: 480",
            "# period: day",
            "# unit.e_load: kWh",
            "timestamp,e_load",
        ]
        for i in range(24):
            lines.append(f"2025-01-01T{i:02d}:00:00,{10.0 + i}")
        ok_text = "\n".join(lines) + "\n"
        result = canonicalize_device_data(ok_text.encode("utf-8"), desc)
        assert not any(d.blocking for d in result.diagnostics), [d.to_dict() for d in result.diagnostics]

        bad_text = "\n".join(lines[:11] + ["2025-01-01T00:00:00,1.0"] * 10) + "\n"
        result_bad = canonicalize_device_data(bad_text.encode("utf-8"), desc)
        assert any(d.code == "DATA-TIME-004" for d in result_bad.diagnostics)


# ---------------------------------------------------------------------------
# 样例文件(合法/非法)
# ---------------------------------------------------------------------------

SAMPLES_DIR = Path(__file__).resolve().parents[1] / "iesplan/devices/samples"


class TestSamples:
    def _electric_load_desc(self):
        """从内置 catalog 取真实设备描述(注册表已由 conftest 初始化)。"""
        from iesplan.devices import get_device_descriptor

        return get_device_descriptor("ies.device.electric_load")

    def test_valid_sample_canonicalizes_clean(self) -> None:
        path = SAMPLES_DIR / "electric_load_valid.data.csv"
        assert path.exists()
        result = canonicalize_device_data(path.read_bytes(), self._electric_load_desc())
        assert not any(d.blocking for d in result.diagnostics), [d.to_dict() for d in result.diagnostics]
        assert result.canonical_sha256

    def test_invalid_unit_sample_rejected(self) -> None:
        path = SAMPLES_DIR / "electric_load_invalid_unit.data.csv"
        assert path.exists()
        result = canonicalize_device_data(path.read_bytes(), self._electric_load_desc())
        assert any(d.code == "DATA-COL-006" for d in result.diagnostics)

    def test_invalid_time_sample_rejected(self) -> None:
        path = SAMPLES_DIR / "electric_load_invalid_time.data.csv"
        assert path.exists()
        result = canonicalize_device_data(path.read_bytes(), self._electric_load_desc())
        codes = [d.code for d in result.diagnostics]
        assert "DATA-TIME-001" in codes  # 乱序
        assert "DATA-TIME-002" in codes  # 步长不对齐(01:30)


class TestSummary:
    def test_quality_report_contains_both_hashes(self) -> None:
        result = canonicalize_device_data(_valid_csv_bytes(), _e_load_desc())
        report = json.loads(summary_json(result))
        assert report["schema"] == SCHEMA_ID
        assert report["schema_version"] == SCHEMA_VERSION
        assert len(report["raw_sha256"]) == 64
        assert len(report["canonical_sha256"]) == 64
        assert report["has_blocking_errors"] is False
        assert report["transformations"] == ["time_to_utc", "units_declared", "values_checked"]

    def test_blocking_diags_surface(self) -> None:
        text = _valid_csv_text(rows=["2025-01-01T00:00:00,-5.0"])
        result = canonicalize_device_data(text.encode("utf-8"), _e_load_desc())
        report = json.loads(summary_json(result))
        assert report["has_blocking_errors"] is True
        assert any(d["code"] == "DATA-VAL-001" for d in report["diagnostics"])
