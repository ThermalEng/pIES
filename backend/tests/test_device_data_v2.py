"""ies.device-data 2.0.0 契约测试（周期/预测结果文件校验 + 临时文件 contract）。

纯协议测试：不依赖设备注册表/数据库，直接调用 datacontract2 纯函数，并
用 parser2/contracts2 产出的 2.0 DeviceModelDocument 作为目标设备绑定。

覆盖：合法/非法 CSV、元数据、方言、周期行数、错列/错单位、时间轴缺口、
偏移越界、设备内容摘要绑定、来源模式不匹配、摘要确定性、PendingDataFile。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

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
    TIMESTAMP_COL,
    canonicalize_device_data_v2,
    parse_data_file_v2,
    parse_metadata_v2,
    pending_from_result,
    summary_json_v2,
)

# ---------------------------------------------------------------------------
# 目标设备文档（与 parser2 契约同构；直接构造以隔离本切片）
# ---------------------------------------------------------------------------


def _device_doc() -> DeviceModelDocument:
    """混合设备：data_repeat 电负荷 + data_predict 环境温度 + constant + in。"""
    return DeviceModelDocument(
        device=DeviceInfo(id="acme.device.electric_load", names={"zh-CN": "电负荷"}),
        interfaces={
            "electric_demand": InterfaceSpec(
                id="electric_demand",
                type="predefined",
                carrier="electricity",
                unit="kW",
                valid_range=(0.0, 1000.0),
                source=SourceSpec(mode="data_repeat", data_ref="typical_day_load"),
            ),
            "ambient_temperature": InterfaceSpec(
                id="ambient_temperature",
                type="predefined",
                carrier="environment",
                unit="°C",
                valid_range=(-50.0, 60.0),
                source=SourceSpec(mode="data_predict", data_ref="weather_prediction"),
            ),
            "fixed_temperature": InterfaceSpec(
                id="fixed_temperature",
                type="predefined",
                carrier="environment",
                unit="°C",
                valid_range=(-50.0, 60.0),
                source=SourceSpec(mode="constant", value=25.0),
            ),
            "electricity_in": InterfaceSpec(
                id="electricity_in",
                type="in",
                carrier="electricity",
                unit="kW",
                valid_range=(0.0, None),
            ),
        },
    )


def _csv_text(
    *,
    source_mode: str = "data_predict",
    resolution: str = "1h",
    timestamp_mode: str = "fixed_offset",
    offset: int = 480,
    period: str | None = None,
    device_id: str = "acme.device.electric_load",
    device_content_sha256: str | None = None,
    dataset_id: str = "ambient_temp_2025",
    units: dict[str, str] | None = None,
    columns: list[str] | None = None,
    rows: list[str] | None = None,
) -> str:
    if device_content_sha256 is None:
        device_content_sha256 = content_sha256(_device_doc())
    if columns is None:
        columns = ["ambient_temperature"] if source_mode == "data_predict" else ["electric_demand"]
    if units is None:
        units = (
            {"ambient_temperature": "°C"}
            if source_mode == "data_predict"
            else {"electric_demand": "kW"}
        )
    lines = [
        f"# schema: {SCHEMA_ID}",
        f"# schema_version: {SCHEMA_VERSION}",
        f"# dataset_id: {dataset_id}",
        f"# device_id: {device_id}",
        f"# device_content_sha256: {device_content_sha256}",
        f"# source_mode: {source_mode}",
        f"# resolution: {resolution}",
        f"# timestamp_mode: {timestamp_mode}",
    ]
    if timestamp_mode == "fixed_offset":
        lines.append(f"# fixed_utc_offset_minutes: {offset}")
    if period is not None:
        lines.append(f"# period: {period}")
    for col in columns:
        lines.append(f"# unit.{col}: {units[col]}")
    lines.append("timestamp," + ",".join(columns))
    if rows is not None:
        lines.extend(rows)
    return "\n".join(lines) + "\n"


def _predict_rows(n: int = 3, start_hour: int = 0, value_base: float = 15.0) -> list[str]:
    """fixed_offset 本地时间行（1h 步长），如 2025-01-01T00:00:00,15.0。"""
    return [
        f"2025-01-01T{start_hour + h:02d}:00:00,{value_base + h}"
        for h in range(n)
    ]


def _repeat_rows(n: int = 24) -> list[str]:
    """data_repeat 模板行（1h 步长）；n 可为 24/168/8760，自动跨日。"""
    out: list[str] = []
    for h in range(n):
        day = h // 24
        hour = h % 24
        date = (datetime(2025, 1, 1) + timedelta(days=day)).strftime("%Y-%m-%d")
        out.append(f"{date}T{hour:02d}:00:00,{10.0 + (h % 24)}")
    return out


def _blocking_codes(result) -> list[str]:
    return [d.code for d in result.diagnostics if d.blocking]


# ---------------------------------------------------------------------------
# 元数据
# ---------------------------------------------------------------------------


class TestMetadata:
    def test_valid_metadata(self) -> None:
        text = "\n".join(
            [
                "# schema: ies.device-data",
                "# schema_version: 2.0.0",
                "# dataset_id: campus_electric_load_2025",
                "# device_id: acme.device.electric_load",
                "# device_content_sha256: " + "a" * 64,
                "# source_mode: data_repeat",
                "# resolution: 1h",
                "# timestamp_mode: fixed_offset",
                "# fixed_utc_offset_minutes: 480",
                "# period: day",
                "# unit.electric_demand: kW",
                "# note.source: test",
            ]
        )
        meta, diags = parse_metadata_v2(text.splitlines())
        assert diags == []
        assert meta.schema_id == SCHEMA_ID
        assert meta.schema_version == SCHEMA_VERSION
        assert meta.device_id == "acme.device.electric_load"
        assert meta.device_content_sha256 == "a" * 64
        assert meta.source_mode == "data_repeat"
        assert meta.period == "day"
        assert meta.units == {"electric_demand": "kW"}
        assert meta.notes == {"source": "test"}

    def test_missing_required_keys(self) -> None:
        text = "\n".join(["# schema: ies.device-data", "# schema_version: 2.0.0"])
        _, diags = parse_metadata_v2(text.splitlines())
        codes = [d.code for d in diags]
        assert "DATA-META-002" in codes
        assert all(d.blocking for d in diags)
        # 2.0 必需键含 device_id / device_content_sha256 / source_mode
        for key in ("dataset_id", "device_id", "device_content_sha256", "source_mode"):
            assert any(d.params.get("key") == key for d in diags)

    def test_duplicate_key(self) -> None:
        text = "\n".join(
            [
                "# schema: ies.device-data",
                "# schema_version: 2.0.0",
                "# dataset_id: a",
                "# dataset_id: b",
                "# device_id: acme.device.electric_load",
                "# device_content_sha256: " + "a" * 64,
                "# source_mode: data_predict",
                "# resolution: 1h",
                "# timestamp_mode: fixed_offset",
                "# fixed_utc_offset_minutes: 480",
            ]
        )
        _, diags = parse_metadata_v2(text.splitlines())
        assert any(d.code == "DATA-META-001" for d in diags)

    def test_unknown_core_field_rejected(self) -> None:
        text = "\n".join(
            [
                "# schema: ies.device-data",
                "# schema_version: 2.0.0",
                "# dataset_id: a",
                "# device_id: acme.device.electric_load",
                "# device_content_sha256: " + "a" * 64,
                "# source_mode: data_predict",
                "# resolution: 1h",
                "# timestamp_mode: fixed_offset",
                "# fixed_utc_offset_minutes: 480",
                "# device_model: acme.device.electric_load@1.2.0",
            ]
        )
        _, diags = parse_metadata_v2(text.splitlines())
        assert any(d.code == "DATA-META-002" and "device_model" in str(d.params.get("key")) for d in diags)

    def test_wrong_schema_or_version(self) -> None:
        text = _csv_text().replace(f"# schema_version: {SCHEMA_VERSION}", "# schema_version: 1.0.0")
        _, diags = parse_metadata_v2(text.splitlines())
        assert any(d.code == "DATA-META-003" for d in diags)

    def test_bad_source_mode_enum(self) -> None:
        text = _csv_text().replace("# source_mode: data_predict", "# source_mode: constant")
        _, diags = parse_metadata_v2(text.splitlines())
        assert any(d.code == "DATA-META-004" and "source_mode" in str(d.params.get("field")) for d in diags)

    def test_bad_device_id_format(self) -> None:
        text = _csv_text().replace("# device_id: acme.device.electric_load", "# device_id: Electric Load!")
        _, diags = parse_metadata_v2(text.splitlines())
        assert any(d.code == "DATA-META-004" and "device_id" in str(d.params.get("field")) for d in diags)

    def test_bad_content_sha_format(self) -> None:
        text = re.sub(
            r"# device_content_sha256: [0-9a-f]{64}",
            "# device_content_sha256: not-a-sha",
            _csv_text(),
        )
        _, diags = parse_metadata_v2(text.splitlines())
        assert any(d.code == "DATA-META-004" and "sha256" in str(d.params.get("field")) for d in diags)

    def test_fixed_offset_requires_offset(self) -> None:
        text = _csv_text().replace("# fixed_utc_offset_minutes: 480\n", "")
        _, diags = parse_metadata_v2(text.splitlines())
        assert any(d.code == "DATA-META-005" for d in diags)

    def test_offset_out_of_range(self) -> None:
        text = _csv_text(offset=900)
        _, diags = parse_metadata_v2(text.splitlines())
        assert any(d.code == "DATA-META-007" for d in diags)

    def test_repeat_requires_period(self) -> None:
        text = _csv_text(source_mode="data_repeat", columns=["electric_demand"], rows=_repeat_rows(24))
        text = text.replace("# period: day\n", "")
        _, diags = parse_metadata_v2(text.splitlines())
        assert any(d.code == "DATA-META-006" for d in diags)

    def test_bad_period_enum(self) -> None:
        text = _csv_text(
            source_mode="data_repeat",
            columns=["electric_demand"],
            period="month",
            rows=_repeat_rows(24),
        )
        _, diags = parse_metadata_v2(text.splitlines())
        assert any(d.code == "DATA-META-004" and "period" in str(d.params.get("field")) for d in diags)


# ---------------------------------------------------------------------------
# 方言
# ---------------------------------------------------------------------------


class TestDialect:
    def test_crlf_warns(self) -> None:
        text = _csv_text(rows=_predict_rows()).replace("\n", "\r\n")
        parsed, diags = parse_data_file_v2(text.encode("utf-8"))
        assert any(d.code == "DATA-DIAL-001" and not d.blocking for d in diags)

    def test_bom_warns(self) -> None:
        text = "﻿" + _csv_text(rows=_predict_rows())
        parsed, diags = parse_data_file_v2(text.encode("utf-8"))
        assert any(d.code == "DATA-DIAL-001" and not d.blocking for d in diags)

    def test_row_width_mismatch_blocking(self) -> None:
        text = _csv_text(rows=_predict_rows())
        text = text.replace("\n2025-01-01T00:00:00,15.0", "\n2025-01-01T00:00:00")
        parsed, diags = parse_data_file_v2(text.encode("utf-8"))
        assert any(d.code == "DATA-DIAL-001" and d.blocking for d in diags)

    def test_nan_cell_blocking(self) -> None:
        text = _csv_text(rows=["2025-01-01T00:00:00,nan"])
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.code == "DATA-DIAL-001" and d.blocking for d in result.diagnostics)

    def test_infinity_cell_blocking(self) -> None:
        text = _csv_text(rows=["2025-01-01T00:00:00,Infinity"])
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.code == "DATA-DIAL-001" and d.blocking for d in result.diagnostics)

    def test_formula_prefix_warns(self) -> None:
        text = _csv_text(rows=["2025-01-01T00:00:00,=SUM(A1)"])
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.code == "DATA-DIAL-001" and not d.blocking for d in result.diagnostics)

    def test_empty_data_rejected(self) -> None:
        """正式数据集至少一行；空文件只能用于非法样例测试。"""
        text = _csv_text(rows=[])
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.code == "DATA-DIAL-001" and d.blocking for d in result.diagnostics)

    def test_thousands_separator_rejected(self) -> None:
        text = _csv_text(rows=['2025-01-01T00:00:00,"1,000"'])
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.code == "DATA-VAL-001" for d in result.diagnostics)

    def test_overflow_number_rejected(self) -> None:
        """1e999 溢出为 inf，必须在数值阶段阻断（不允许非有限值进入规范表格）。"""
        text = _csv_text(rows=["2025-01-01T00:00:00,1e999"])
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.code == "DATA-VAL-001" and d.blocking for d in result.diagnostics)


# ---------------------------------------------------------------------------
# data_predict 时间轴
# ---------------------------------------------------------------------------


class TestPredictTimeline:
    def test_valid_fixed_offset(self) -> None:
        result = canonicalize_device_data_v2(
            _csv_text(rows=_predict_rows()).encode("utf-8"), _device_doc()
        )
        assert not any(d.blocking for d in result.diagnostics), [d.to_dict() for d in result.diagnostics]
        assert result.raw_sha256
        assert result.canonical_sha256
        # UTC = 本地 - 偏移(480)：本地 00:00(+8) → UTC 前一天 16:00
        assert result.utc_timestamps[0].hour == 16
        assert result.rows[0][TIMESTAMP_COL] == result.utc_timestamps[0]

    def test_valid_utc_mode(self) -> None:
        text = _csv_text(
            timestamp_mode="utc",
            rows=[
                "2024-12-31T16:00:00Z,15.0",
                "2024-12-31T17:00:00Z,16.0",
                "2024-12-31T18:00:00Z,17.0",
            ],
        )
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert not any(d.blocking for d in result.diagnostics), [d.to_dict() for d in result.diagnostics]
        assert result.utc_timestamps[0].tzinfo is not None

    def test_utc_mode_rejects_naive_timestamp(self) -> None:
        text = _csv_text(timestamp_mode="utc", rows=["2025-01-01T00:00:00,15.0"])
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.code == "DATA-TIME-006" and d.blocking for d in result.diagnostics)

    def test_fixed_offset_rejects_z_timestamp(self) -> None:
        text = _csv_text(rows=["2024-12-31T16:00:00Z,15.0"])
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.code == "DATA-TIME-006" and d.blocking for d in result.diagnostics)

    def test_fixed_offset_rejects_inline_numeric_offset(self) -> None:
        text = _csv_text(rows=["2025-01-01T09:00:00+09:00,15.0"])
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.code == "DATA-TIME-006" and d.blocking for d in result.diagnostics)

    def test_invalid_timestamp_format_rejected(self) -> None:
        text = _csv_text(rows=["01/02/2025 08:00,15.0"])
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.code == "DATA-TIME-005" for d in result.diagnostics)

    def test_out_of_order_rejected(self) -> None:
        text = _csv_text(
            rows=[
                "2025-01-01T02:00:00,17.0",
                "2025-01-01T00:00:00,15.0",
                "2025-01-01T01:00:00,16.0",
            ]
        )
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.code == "DATA-TIME-001" for d in result.diagnostics)

    def test_duplicate_timestamp_rejected(self) -> None:
        text = _csv_text(
            rows=[
                "2025-01-01T00:00:00,15.0",
                "2025-01-01T00:00:00,16.0",
            ]
        )
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.code == "DATA-TIME-001" for d in result.diagnostics)

    def test_time_gap_rejected(self) -> None:
        """跳过一小时：相邻步长不对齐 → 阻断并定位缺口，不补零。"""
        text = _csv_text(
            rows=[
                "2025-01-01T00:00:00,15.0",
                "2025-01-01T02:00:00,16.0",
            ]
        )
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.code == "DATA-TIME-002" and d.blocking for d in result.diagnostics)

    def test_expected_rows_check(self) -> None:
        text = _csv_text(rows=_predict_rows(3))
        result = canonicalize_device_data_v2(
            text.encode("utf-8"), _device_doc(), expected_rows=8760
        )
        assert any(d.code == "DATA-TS-004" for d in result.diagnostics)


# ---------------------------------------------------------------------------
# data_repeat 周期行数
# ---------------------------------------------------------------------------


class TestRepeatPeriod:
    def _canonicalize(self, period: str, n: int):
        text = _csv_text(
            source_mode="data_repeat",
            period=period,
            columns=["electric_demand"],
            rows=_repeat_rows(n),
        )
        return canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())

    def test_day_24_rows_valid(self) -> None:
        result = self._canonicalize("day", 24)
        assert not any(d.blocking for d in result.diagnostics), [d.to_dict() for d in result.diagnostics]

    def test_week_168_rows_valid(self) -> None:
        result = self._canonicalize("week", 168)
        assert not any(d.blocking for d in result.diagnostics), [d.to_dict() for d in result.diagnostics]

    def test_year_8760_rows_valid(self) -> None:
        result = self._canonicalize("year", 8760)
        assert not any(d.blocking for d in result.diagnostics), [d.to_dict() for d in result.diagnostics]
        assert result.canonical_sha256
        assert len(result.rows) == 8760

    def test_day_row_count_mismatch_blocked(self) -> None:
        result = self._canonicalize("day", 23)
        assert any(d.code == "DATA-TIME-004" and d.blocking for d in result.diagnostics)

    def test_week_row_count_mismatch_blocked(self) -> None:
        result = self._canonicalize("week", 169)
        assert any(d.code == "DATA-TIME-004" and d.blocking for d in result.diagnostics)

    def test_repeat_out_of_order_blocked(self) -> None:
        rows = _repeat_rows(24)
        rows[3], rows[1] = rows[1], rows[3]
        text = _csv_text(
            source_mode="data_repeat",
            period="day",
            columns=["electric_demand"],
            rows=rows,
        )
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.code == "DATA-TIME-001" for d in result.diagnostics)

    def test_repeat_gap_blocked(self) -> None:
        rows = _repeat_rows(24)
        rows.insert(2, "2025-01-01T01:30:00,10.5")  # 插入非对齐点使相邻步长失配
        text = _csv_text(
            source_mode="data_repeat",
            period="day",
            columns=["electric_demand"],
            rows=rows,
        )
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.code == "DATA-TIME-002" for d in result.diagnostics)

    def test_periodic_rows_table(self) -> None:
        from iesplan.devices.datacontract2 import periodic_rows

        assert periodic_rows("1h", "day") == 24
        assert periodic_rows("30min", "day") == 48
        assert periodic_rows("15min", "day") == 96
        assert periodic_rows("1h", "week") == 168
        assert periodic_rows("1h", "year") == 8760
        assert periodic_rows("1h", "month") is None


# ---------------------------------------------------------------------------
# 列与单位
# ---------------------------------------------------------------------------


class TestColumnsAndUnits:
    def test_unknown_column_rejected(self) -> None:
        text = _csv_text(rows=_predict_rows())
        text = text.replace("timestamp,ambient_temperature", "timestamp,ambient_temperature,extra_col")
        text = text.replace("\n2025-01-01T00:00:00,15.0", "\n2025-01-01T00:00:00,15.0,1")
        text = text.replace("\n2025-01-01T01:00:00,16.0", "\n2025-01-01T01:00:00,16.0,1")
        text = text.replace("\n2025-01-01T02:00:00,17.0", "\n2025-01-01T02:00:00,17.0,1")
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.code == "DATA-COL-003" for d in result.diagnostics)

    def test_duplicate_column_rejected(self) -> None:
        text = _csv_text(rows=_predict_rows())
        text = text.replace(
            "timestamp,ambient_temperature", "timestamp,ambient_temperature,ambient_temperature"
        )
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.code == "DATA-COL-004" for d in result.diagnostics)

    def test_required_column_missing_rejected(self) -> None:
        text = _csv_text(rows=_predict_rows())
        text = text.replace("timestamp,ambient_temperature", "timestamp")
        for i in range(3):
            text = text.replace(f"\n2025-01-01T0{i}:00:00,{15.0 + i}", f"\n2025-01-01T0{i}:00:00")
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.code == "DATA-COL-005" for d in result.diagnostics)

    def test_constant_interface_column_rejected(self) -> None:
        """constant 直接写在设备接口中，不使用 CSV：声明其列 → DATA-COL-003。"""
        text = _csv_text(
            rows=["2025-01-01T00:00:00,25.0"],
            columns=["fixed_temperature"],
            units={"fixed_temperature": "°C"},
        )
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.code == "DATA-COL-003" for d in result.diagnostics)

    def test_unit_dimension_mismatch_rejected(self) -> None:
        """kW 与 kWh 量纲不兼容，不能仅因都是数字而接受。"""
        text = _csv_text(
            source_mode="data_repeat",
            period="day",
            columns=["electric_demand"],
            units={"electric_demand": "kWh"},
            rows=_repeat_rows(24),
        )
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.code == "DATA-COL-006" and d.blocking for d in result.diagnostics)

    def test_unit_dimension_compatible_passes(self) -> None:
        """kW 与 W 量纲兼容：显式换算由生成器边界完成，这里只验量纲。"""
        text = _csv_text(
            source_mode="data_repeat",
            period="day",
            columns=["electric_demand"],
            units={"electric_demand": "W"},
            rows=_repeat_rows(24),
        )
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert not any(d.blocking for d in result.diagnostics), [d.to_dict() for d in result.diagnostics]

    def test_missing_unit_declaration_blocked(self) -> None:
        text = _csv_text(rows=_predict_rows()).replace("# unit.ambient_temperature: °C\n", "")
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.code == "DATA-COL-007" and d.blocking for d in result.diagnostics)

    def test_canonical_column_order_follows_device(self) -> None:
        """规范输出按设备模型 interface 声明顺序排列。"""
        text = _csv_text(rows=_predict_rows())
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert result.column_order == (TIMESTAMP_COL, "ambient_temperature")


# ---------------------------------------------------------------------------
# 数值与值域
# ---------------------------------------------------------------------------


class TestValues:
    def test_range_out_blocked_no_truncate(self) -> None:
        """越出 valid_range 是阻断错误，不自动截断。"""
        text = _csv_text(rows=["2025-01-01T00:00:00,70.0"])
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.code == "DATA-VAL-001" and d.blocking for d in result.diagnostics)
        assert result.rows[0]["ambient_temperature"] == 70.0  # 原始值保留，不截断

    def test_range_below_minimum_blocked(self) -> None:
        text = _csv_text(rows=["2025-01-01T00:00:00,-60.0"])
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.code == "DATA-VAL-001" and d.blocking for d in result.diagnostics)

    def test_missing_value_blocked(self) -> None:
        """2.0 模型未声明缺失策略：缺失值一律阻断。"""
        text = _csv_text(rows=["2025-01-01T00:00:00,"])
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.code == "DATA-VAL-002" and d.blocking for d in result.diagnostics)

    def test_regionalized_number_rejected(self) -> None:
        """区域化数字（千分位逗号在引号字段内）必须拒绝，不静默去逗号。"""
        text = _csv_text(rows=['2025-01-01T00:00:00,"1.234,56"'])
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.code == "DATA-VAL-001" for d in result.diagnostics)

    def test_boundary_values_accepted(self) -> None:
        """valid_range 是闭区间边界。"""
        text = _csv_text(
            rows=[
                "2025-01-01T00:00:00,-50.0",
                "2025-01-01T01:00:00,60.0",
            ]
        )
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert not any(d.blocking for d in result.diagnostics), [d.to_dict() for d in result.diagnostics]


# ---------------------------------------------------------------------------
# 设备绑定（device_id / device_content_sha256 / source_mode）
# ---------------------------------------------------------------------------


class TestDeviceBinding:
    def test_device_id_mismatch_blocked(self) -> None:
        text = _csv_text(rows=_predict_rows(), device_id="acme.device.heat_pump")
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.code == "DATA-META-008" and d.blocking for d in result.diagnostics)

    def test_content_sha_mismatch_blocked(self) -> None:
        text = _csv_text(rows=_predict_rows(), device_content_sha256="b" * 64)
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        diag = next((d for d in result.diagnostics if d.code == "DATA-META-010"), None)
        assert diag is not None and diag.blocking
        assert diag.params["expected"] == content_sha256(_device_doc())

    def test_source_mode_mismatch_blocked(self) -> None:
        """data_repeat 文件声明 data_predict 接口的列 → DATA-META-011 阻断。"""
        text = _csv_text(
            source_mode="data_repeat",
            period="day",
            columns=["ambient_temperature"],
            units={"ambient_temperature": "°C"},
            rows=_repeat_rows(24),
        )
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        diag = next((d for d in result.diagnostics if d.code == "DATA-META-011"), None)
        assert diag is not None and diag.blocking
        assert diag.params["column"] == "ambient_temperature"
        assert diag.params["mode"] == "data_predict"

    def test_predict_file_without_matching_interface_blocked(self) -> None:
        """设备无 data_predict 来源接口时，data_predict 文件整体阻断。"""
        doc = DeviceModelDocument(
            device=DeviceInfo(id="acme.device.pure_repeat", names={}),
            interfaces={
                "electric_demand": InterfaceSpec(
                    id="electric_demand",
                    type="predefined",
                    carrier="electricity",
                    unit="kW",
                    valid_range=(0.0, 1000.0),
                    source=SourceSpec(mode="data_repeat", data_ref="typical_day_load"),
                ),
            },
        )
        text = _csv_text(
            device_id="acme.device.pure_repeat",
            device_content_sha256=content_sha256(doc),
            rows=_predict_rows(1),
        )
        result = canonicalize_device_data_v2(text.encode("utf-8"), doc)
        assert any(d.code == "DATA-META-011" and d.blocking for d in result.diagnostics)


# ---------------------------------------------------------------------------
# 摘要确定性与质量报告
# ---------------------------------------------------------------------------


class TestSummaryDeterminism:
    def test_same_input_same_summary(self) -> None:
        text = _csv_text(rows=_predict_rows())
        r1 = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        r2 = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert r1.canonical_sha256 == r2.canonical_sha256
        assert summary_json_v2(r1) == summary_json_v2(r2)

    def test_utc_and_offset_same_instant_same_summary(self) -> None:
        """同一 UTC 瞬时（UTC 直接书写 vs fixed_offset 换算）→ 同一规范摘要。"""
        offset_text = _csv_text(rows=["2025-01-01T00:00:00,15.0"])
        utc_text = _csv_text(
            timestamp_mode="utc",
            rows=["2024-12-31T16:00:00Z,15.0"],
        )
        r1 = canonicalize_device_data_v2(offset_text.encode("utf-8"), _device_doc())
        r2 = canonicalize_device_data_v2(utc_text.encode("utf-8"), _device_doc())
        assert not any(d.blocking for d in r1.diagnostics), [d.to_dict() for d in r1.diagnostics]
        assert not any(d.blocking for d in r2.diagnostics), [d.to_dict() for d in r2.diagnostics]
        assert r1.canonical_sha256 == r2.canonical_sha256

    def test_semantics_change_changes_summary(self) -> None:
        r1 = canonicalize_device_data_v2(
            _csv_text(rows=["2025-01-01T00:00:00,15.0"]).encode("utf-8"), _device_doc()
        )
        r2 = canonicalize_device_data_v2(
            _csv_text(rows=["2025-01-01T00:00:00,16.0"]).encode("utf-8"), _device_doc()
        )
        assert r1.canonical_sha256 != r2.canonical_sha256

    def test_quality_report_fields(self) -> None:
        result = canonicalize_device_data_v2(
            _csv_text(rows=_predict_rows()).encode("utf-8"), _device_doc()
        )
        report = json.loads(summary_json_v2(result))
        assert report["schema"] == SCHEMA_ID
        assert report["schema_version"] == SCHEMA_VERSION
        assert report["device_id"] == "acme.device.electric_load"
        assert len(report["device_content_sha256"]) == 64
        assert report["source_mode"] == "data_predict"
        assert report["resolution"] == "1h"
        assert len(report["raw_sha256"]) == 64
        assert len(report["canonical_sha256"]) == 64
        assert report["has_blocking_errors"] is False
        assert report["transformations"] == ["time_to_utc", "units_declared", "values_checked"]

    def test_blocking_diags_surface_in_report(self) -> None:
        text = _csv_text(rows=["2025-01-01T00:00:00,70.0"])
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        report = json.loads(summary_json_v2(result))
        assert report["has_blocking_errors"] is True
        assert any(d["code"] == "DATA-VAL-001" for d in report["diagnostics"])

    def test_canonical_table_uses_utc_z(self) -> None:
        result = canonicalize_device_data_v2(
            _csv_text(rows=["2025-01-01T00:00:00,15.0"]).encode("utf-8"), _device_doc()
        )
        canonical = result.canonical_csv_bytes().decode("utf-8")
        assert "2024-12-31T16:00:00Z" in canonical
        assert "# timestamp_mode: utc" in canonical
        # fixed_offset 偏移信息不进规范表格（同一瞬时唯一形态）
        assert "fixed_utc_offset_minutes" not in canonical


# ---------------------------------------------------------------------------
# 临时文件契约（PendingDataFile，纯类型，不实现上传）
# ---------------------------------------------------------------------------


class TestPendingDataFile:
    def test_valid_result_produces_pending(self) -> None:
        result = canonicalize_device_data_v2(
            _csv_text(rows=_predict_rows()).encode("utf-8"), _device_doc()
        )
        pending = pending_from_result(result)
        assert pending is not None
        assert pending.dataset_id == "ambient_temp_2025"
        assert pending.device_id == "acme.device.electric_load"
        assert pending.device_content_sha256 == content_sha256(_device_doc())
        assert pending.source_mode == "data_predict"
        assert pending.resolution == "1h"
        assert pending.raw_sha256 == result.raw_sha256
        assert pending.canonical_sha256 == result.canonical_sha256
        assert pending.row_count == 3
        assert pending.column_order == (TIMESTAMP_COL, "ambient_temperature")

    def test_repeat_pending_keeps_period(self) -> None:
        result = canonicalize_device_data_v2(
            _csv_text(
                source_mode="data_repeat",
                period="day",
                columns=["electric_demand"],
                rows=_repeat_rows(24),
            ).encode("utf-8"),
            _device_doc(),
        )
        pending = pending_from_result(result)
        assert pending is not None
        assert pending.period == "day"
        assert pending.row_count == 24

    def test_blocking_result_produces_none(self) -> None:
        """阻断错误不得成为可提交的临时文件（宪法 §2.5 状态完整）。"""
        text = _csv_text(rows=["2025-01-01T00:00:00,70.0"])
        result = canonicalize_device_data_v2(text.encode("utf-8"), _device_doc())
        assert any(d.blocking for d in result.diagnostics)
        assert pending_from_result(result) is None


# ---------------------------------------------------------------------------
# 与 parser2 联动
# ---------------------------------------------------------------------------


class TestParser2Linkage:
    def test_csv_validated_against_parsed_document(self) -> None:
        """真实链路：YAML 经 parser2 解析 → 内容摘要 → CSV 校验。"""
        from iesplan.core.yamlmini import load as yaml_load
        from iesplan.devices.parser2 import parse_device_model_v2

        yaml_text = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.electric_load, names: {zh-CN: 电负荷, en-US: Electric Load}}
properties: {}
interfaces:
  electric_demand:
    type: predefined
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: null}
    source: {mode: data_repeat, data_ref: typical_day_load}
equations: {variables: {}, relations: []}
"""
        r = parse_device_model_v2(yaml_load(yaml_text))
        assert r.ok, [d.params.get("detail") for d in r.diagnostics]
        doc = r.document
        assert doc is not None
        text = _csv_text(
            source_mode="data_repeat",
            period="day",
            columns=["electric_demand"],
            units={"electric_demand": "kW"},
            device_content_sha256=content_sha256(doc),
            rows=_repeat_rows(24),
        )
        result = canonicalize_device_data_v2(text.encode("utf-8"), doc)
        assert not any(d.blocking for d in result.diagnostics), [d.to_dict() for d in result.diagnostics]

    def test_parsed_document_sha_binding(self) -> None:
        """用错误内容摘要绑定同一解析文档 → DATA-META-010 阻断。"""
        from iesplan.core.yamlmini import load as yaml_load
        from iesplan.devices.parser2 import parse_device_model_v2

        yaml_text = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.electric_load, names: {zh-CN: 电负荷, en-US: Electric Load}}
properties: {}
interfaces:
  electric_demand:
    type: predefined
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: null}
    source: {mode: data_repeat, data_ref: typical_day_load}
equations: {variables: {}, relations: []}
"""
        r = parse_device_model_v2(yaml_load(yaml_text))
        doc = r.document
        assert doc is not None
        text = _csv_text(
            source_mode="data_repeat",
            period="day",
            columns=["electric_demand"],
            units={"electric_demand": "kW"},
            device_content_sha256="f" * 64,  # 与 doc 的实际摘要不一致
            rows=_repeat_rows(24),
        )
        result = canonicalize_device_data_v2(text.encode("utf-8"), doc)
        assert any(d.code == "DATA-META-010" and d.blocking for d in result.diagnostics)
