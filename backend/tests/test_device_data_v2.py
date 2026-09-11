"""ies.device-data 2.0.0 的 step 契约测试。

0.6.5 条目 3 口径: 原始输入(``prepared: false``)必须 0..N-1 严格连续,
与基线同分辨率; ``data_repeat`` 的完整来源序列整体即重复基线, 只能是
完整日/周/年(不设周期字段); 稀疏 step 一律结构化拒绝。
"""

from __future__ import annotations

import json

from iesplan.devices.contracts2 import (
    DeviceInfo,
    DeviceModelDocument,
    InterfaceSpec,
    SourceSpec,
)
from iesplan.devices.datacontract2 import (
    SCHEMA_ID,
    SCHEMA_VERSION,
    STEP_COL,
    canonicalize_device_data_v2,
    parse_data_file_v2,
    parse_metadata_v2,
    pending_from_result,
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
    columns: list[str] | None = None,
    units: dict[str, str] | None = None,
    rows: list[str] | None = None,
    device_id: str = "acme.device.electric_load",
    prepared: bool = False,
    point_count: int | None = None,
) -> str:
    if columns is None:
        columns = ["ambient_temperature"] if source_mode == "data_predict" else [
            "fixed_temperature" if source_mode == "constant" else "electric_demand"
        ]
    if units is None:
        units = {column: ("°C" if "temperature" in column else "kW") for column in columns}
    lines = [
        f"# schema: {SCHEMA_ID}",
        f"# schema_version: {SCHEMA_VERSION}",
        "# dataset_id: campus.data.series",
        f"# device_id: {device_id}",
        f"# source_mode: {source_mode}",
        f"# resolution: {resolution}",
    ]
    if prepared:
        lines.extend([
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
        # 仅元信息行(不含 CSV 表头; 生产调用方同样只传 # 行)
        lines = [ln for ln in _csv_text().splitlines() if ln.startswith("#")]
        meta, diags = parse_metadata_v2(lines)
        assert not any(diag.blocking for diag in diags)
        assert meta.resolution == "1h"
        assert not hasattr(meta, "timestamp_mode")

    def test_unknown_timezone_metadata_rejected(self) -> None:
        text = _csv_text().replace("# resolution: 1h", "# resolution: 1h\n# timestamp_mode: utc")
        result = canonicalize_device_data_v2(text.encode(), _device_doc())
        assert "DATA-META-002" in _codes(result)

    def test_repeat_declares_no_period_field(self) -> None:
        # 0.6.5 条目 3: 不设周期字段; 声明 period 属未知核心字段
        text = _csv_text(source_mode="data_repeat").replace(
            "# resolution: 1h", "# resolution: 1h\n# period: year"
        )
        result = canonicalize_device_data_v2(text.encode(), _device_doc())
        assert "DATA-META-002" in _codes(result)

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

    def test_raw_sparse_steps_are_blocked(self) -> None:
        # 0.6.5: 装配前不重采样/插值/补齐, 原始输入必须 0..N-1 严格连续
        result = canonicalize_device_data_v2(
            _csv_text(rows=["0,15", "2,16", "5,17"]).encode(), _device_doc()
        )
        assert "DATA-STEP-005" in _codes(result)

    def test_step_must_be_nonnegative_integer(self) -> None:
        result = canonicalize_device_data_v2(_csv_text(rows=["0.5,15"]).encode(), _device_doc())
        assert "DATA-STEP-001" in _codes(result)

    def test_raw_step_must_strictly_increase(self) -> None:
        for rows in (["0,15", "0,16"], ["2,15", "1,16"]):
            result = canonicalize_device_data_v2(_csv_text(rows=list(rows)).encode(), _device_doc())
            assert "DATA-STEP-002" in _codes(result)
            assert "DATA-STEP-005" in _codes(result)  # 非 0..N-1 同时被连续性规则阻断

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



class TestPreassemblyCadence:
    """0.6.5 条目 3: data_repeat 完整来源序列只能是完整日/周/年(无周期字段)。"""

    def _repeat_csv(
        self,
        *,
        source_mode: str = "data_repeat",
        resolution: str = "1h",
        n: int = 8760,
    ) -> bytes:
        return _csv_text(
            source_mode=source_mode, resolution=resolution,
            rows=[f"{i},{15 + (i % 10)}" for i in range(n)],
        ).encode()

    def test_1h_day_week_year_accepted(self) -> None:
        # 1h: 完整日 24 / 完整周 168 / 完整年 8760 通过
        for n in (24, 168, 8760):
            ok = canonicalize_device_data_v2(
                self._repeat_csv(n=n), _device_doc(),
                baseline_resolution="1h",
            )
            assert not _codes(ok), n
        assert len(canonicalize_device_data_v2(
            self._repeat_csv(n=168), _device_doc(),
            baseline_resolution="1h",
        ).steps) == 168

    def test_1h_partial_lengths_rejected(self) -> None:
        # 非完整周期(23/25/167/169/4380/8759/8761/17520)一律阻断;
        # 17520(两整年)也不再接受: 重复基线只能是一个完整周期
        for n in (23, 25, 167, 169, 4380, 8759, 8761, 17520):
            result = canonicalize_device_data_v2(
                self._repeat_csv(n=n), _device_doc(),
                baseline_resolution="1h",
            )
            assert "DATA-STEP-006" in _codes(result), n

    def test_leap_year_baseline_counts(self) -> None:
        # 基线闰年: 完整年为 8784; 8760(非闰年)拒绝, 日/周点数不变
        wrong = canonicalize_device_data_v2(
            self._repeat_csv(n=8760), _device_doc(),
            baseline_resolution="1h", baseline_leap_year=True,
        )
        assert "DATA-STEP-006" in _codes(wrong)
        for n in (24, 168, 8784):
            ok = canonicalize_device_data_v2(
                self._repeat_csv(n=n), _device_doc(),
                baseline_resolution="1h", baseline_leap_year=True,
            )
            assert not _codes(ok), n

    def test_15min_and_30min_cycle_counts(self) -> None:
        # 15min: 日 96 / 周 672 / 年 35040(闰年 35136);
        # 30min: 日 48 / 周 336 / 年 17520(闰年 17568)
        for resolution, day, week, year in (
            ("15min", 96, 672, 35040), ("30min", 48, 336, 17520),
        ):
            for n in (day, week, year):
                ok = canonicalize_device_data_v2(
                    self._repeat_csv(resolution=resolution, n=n), _device_doc(),
                    baseline_resolution=resolution,
                )
                assert not _codes(ok), (resolution, n)
            bad = canonicalize_device_data_v2(
                self._repeat_csv(resolution=resolution, n=year - 1), _device_doc(),
                baseline_resolution=resolution,
            )
            assert "DATA-STEP-006" in _codes(bad), resolution
        leap_15 = canonicalize_device_data_v2(
            self._repeat_csv(resolution="15min", n=35136), _device_doc(),
            baseline_resolution="15min", baseline_leap_year=True,
        )
        assert not _codes(leap_15)

    def test_resolution_must_match_project_baseline(self) -> None:
        # 不同采样间隔不在导入时自动对齐: 结构化拒绝(用户导入前自行整理)
        result = canonicalize_device_data_v2(
            self._repeat_csv(resolution="30min", n=17520), _device_doc(),
            baseline_resolution="15min",
        )
        assert "DATA-META-004" in _codes(result)
        assert result.meta.resolution == "30min"

    def test_without_baseline_cycle_rule_still_applies(self) -> None:
        # 纯单文件校验: 周期规则是文件内在口径, 无基线参数仍执行;
        # 24 行(1h 完整日)通过, 25 行阻断
        ok = canonicalize_device_data_v2(
            self._repeat_csv(n=24), _device_doc(),
        )
        assert not _codes(ok)
        bad = canonicalize_device_data_v2(
            self._repeat_csv(n=25), _device_doc(),
        )
        assert "DATA-STEP-006" in _codes(bad)


class TestBindingColumnsAndValues:
    def test_device_id_mismatch_is_rejected(self) -> None:
        wrong_id = canonicalize_device_data_v2(
            _csv_text(device_id="acme.device.other").encode(), _device_doc()
        )
        assert "DATA-META-008" in _codes(wrong_id)

    def test_source_mode_mismatch_is_rejected(self) -> None:
        # 显式绑定上下文声明 data_predict，CSV 字头声明 data_repeat → 阻断
        result = canonicalize_device_data_v2(
            _csv_text(
                source_mode="data_repeat", columns=["ambient_temperature"],
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
            source_mode="data_repeat", units={"electric_demand": "W"},
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
    def test_same_semantics_same_canonical_output(self) -> None:
        text = _csv_text(rows=["0,15", "1,16", "2,17"])
        first = canonicalize_device_data_v2(text.encode(), _device_doc())
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
