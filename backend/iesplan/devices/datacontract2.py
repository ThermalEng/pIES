"""`ies.device-data` 2.0.0 连续 step 文件契约。

原始输入可用不同采样间隔，但统一以非负整数 ``step`` 表达，不含时间戳、时区
或 UTC 偏移。序列预备用例生成的计算文件还必须固定项目基线摘要，并使用从 0
开始的连续 step。本模块只实现纯协议解析、设备内容绑定、数值校验和规范摘要。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from iesplan.core.diagnostics import Diagnostic, make_diag
from iesplan.core.timeaxis import RESOLUTIONS
from iesplan.devices.contracts2 import DeviceModelDocument, content_sha256, is_valid_id
from iesplan.devices.datacontract import units_compatible

SCHEMA_ID = "ies.device-data"
SCHEMA_VERSION = "2.0.0"
STEP_COL = "step"
SOURCE_MODES = ("constant", "data_repeat", "data_predict")
RESOLUTION_VALUES = tuple(RESOLUTIONS)
PERIOD_VALUES = ("day", "week", "year")
MAX_ROWS_PER_DIAG = 5

_REQUIRED_META_KEYS = (
    "schema", "schema_version", "dataset_id", "device_id",
    "device_content_sha256", "source_mode", "resolution",
)
_OPTIONAL_META_KEYS = frozenset(
    {"period", "project_baseline_sha256", "point_count", "prepared"}
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[a-z0-9]+([._-][a-z0-9]+)*$")


@dataclass(frozen=True, slots=True)
class DeviceData2Meta:
    schema_id: str = SCHEMA_ID
    schema_version: str = SCHEMA_VERSION
    dataset_id: str = ""
    device_id: str = ""
    device_content_sha256: str = ""
    source_mode: str = "data_predict"
    resolution: str = "1h"
    period: str | None = None
    project_baseline_sha256: str | None = None
    point_count: int | None = None
    prepared: bool = False
    units: dict[str, str] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    declared_columns: tuple[str, ...] = ()


def _diag(
    code: str,
    params: dict[str, Any],
    *,
    field_name: str = "",
    blocking: bool = True,
    rows: list[int] | None = None,
) -> Diagnostic:
    location: dict[str, Any] = {"object_type": "device_data", "field": field_name}
    if rows:
        location["row"] = rows[:MAX_ROWS_PER_DIAG]
    return make_diag(
        code,
        severity="error" if blocking else "warning",
        blocking=blocking,
        params=params,
        location=location,
    )


def _parse_meta_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped.startswith("#"):
        return None
    body = stripped[1:].strip()
    if ":" not in body:
        return None
    key, value = body.split(":", 1)
    return key.strip(), value.strip()


def parse_metadata_v2(text_lines: list[str]) -> tuple[DeviceData2Meta, list[Diagnostic]]:
    raw: dict[str, str] = {}
    units: dict[str, str] = {}
    notes: dict[str, str] = {}
    declared: list[str] = []
    diags: list[Diagnostic] = []
    for line in text_lines:
        item = _parse_meta_line(line)
        if item is None:
            diags.append(_diag("DATA-META-001", {"key": line.strip()}))
            continue
        key, value = item
        if key in raw:
            diags.append(_diag("DATA-META-001", {"key": key}, field_name=key))
            continue
        raw[key] = value
        if key.startswith("unit."):
            column = key[5:]
            if column:
                units[column] = value
                declared.append(column)
            else:
                diags.append(_diag("DATA-META-001", {"key": key}, field_name=key))
        elif key.startswith("note."):
            notes[key[5:]] = value
        elif key not in _REQUIRED_META_KEYS and key not in _OPTIONAL_META_KEYS:
            diags.append(_diag(
                "DATA-META-002", {"key": key, "detail": "未知核心字段，应使用 note.*"},
                field_name=key,
            ))

    missing = [key for key in _REQUIRED_META_KEYS if key not in raw]
    for key in missing:
        diags.append(_diag("DATA-META-002", {"key": key}, field_name=key))
    if missing:
        return DeviceData2Meta(units=units, notes=notes), diags

    if raw["schema"] != SCHEMA_ID or raw["schema_version"] != SCHEMA_VERSION:
        diags.append(_diag("DATA-META-003", {
            "schema": raw["schema"], "schema_version": raw["schema_version"],
            "expected": f"{SCHEMA_ID} {SCHEMA_VERSION}",
        }))
    for name, valid, allowed in (
        ("dataset_id", bool(_ID_RE.fullmatch(raw["dataset_id"])), "稳定小写 ID"),
        ("device_id", is_valid_id(raw["device_id"]), "稳定设备 ID"),
        ("device_content_sha256", bool(_SHA256_RE.fullmatch(raw["device_content_sha256"])), "SHA-256"),
        ("source_mode", raw["source_mode"] in SOURCE_MODES, SOURCE_MODES),
        ("resolution", raw["resolution"] in RESOLUTION_VALUES, RESOLUTION_VALUES),
    ):
        if not valid:
            diags.append(_diag(
                "DATA-META-004", {"field": name, "value": raw[name], "allowed": allowed},
                field_name=name,
            ))

    source_mode = raw["source_mode"]
    period = raw.get("period") or None
    if source_mode == "data_repeat":
        if period is None:
            diags.append(_diag(
                "DATA-META-006", {"field": "period", "mode": source_mode}, field_name="period",
            ))
        elif period not in PERIOD_VALUES:
            diags.append(_diag(
                "DATA-META-004", {"field": "period", "value": period, "allowed": PERIOD_VALUES},
                field_name="period",
            ))
    elif period is not None:
        diags.append(_diag(
            "DATA-META-004", {"field": "period", "value": period, "allowed": "仅 data_repeat"},
            field_name="period",
        ))

    prepared_text = raw.get("prepared")
    prepared = prepared_text == "true"
    if prepared_text not in (None, "true", "false"):
        diags.append(_diag(
            "DATA-META-004", {"field": "prepared", "value": prepared_text, "allowed": ("true", "false")},
            field_name="prepared",
        ))
    baseline_sha = raw.get("project_baseline_sha256") or None
    point_count: int | None = None
    if raw.get("point_count"):
        try:
            point_count = int(raw["point_count"])
        except ValueError:
            pass
        if point_count is None or point_count < 1:
            diags.append(_diag(
                "DATA-META-004",
                {"field": "point_count", "value": raw.get("point_count"), "allowed": "正整数"},
                field_name="point_count",
            ))
    if prepared:
        if baseline_sha is None:
            diags.append(_diag(
                "DATA-META-002", {"key": "project_baseline_sha256"}, field_name="project_baseline_sha256",
            ))
        elif not _SHA256_RE.fullmatch(baseline_sha):
            diags.append(_diag(
                "DATA-META-004",
                {
                    "field": "project_baseline_sha256",
                    "value": baseline_sha,
                    "allowed": "SHA-256",
                },
                field_name="project_baseline_sha256",
            ))
        if point_count is None:
            diags.append(_diag("DATA-META-002", {"key": "point_count"}, field_name="point_count"))
    elif source_mode == "constant":
        diags.append(_diag(
            "DATA-META-011", {"column": "", "mode": "constant", "source_mode": source_mode,
                               "detail": "constant 只允许预备后的计算文件"},
            field_name="source_mode",
        ))
    elif baseline_sha is not None or point_count is not None:
        diags.append(_diag(
            "DATA-META-004", {"field": "prepared", "value": prepared_text,
                               "allowed": "基线摘要和点数仅用于 prepared: true"},
            field_name="prepared",
        ))

    return DeviceData2Meta(
        schema_id=raw["schema"], schema_version=raw["schema_version"],
        dataset_id=raw["dataset_id"], device_id=raw["device_id"],
        device_content_sha256=raw["device_content_sha256"], source_mode=source_mode,
        resolution=raw["resolution"], period=period,
        project_baseline_sha256=baseline_sha, point_count=point_count, prepared=prepared,
        units=units, notes=notes, declared_columns=tuple(declared),
    ), diags


def serialize_metadata_v2(
    meta: DeviceData2Meta, *, column_order: tuple[str, ...] | None = None,
) -> str:
    lines = [
        f"# schema: {meta.schema_id}", f"# schema_version: {meta.schema_version}",
        f"# dataset_id: {meta.dataset_id}", f"# device_id: {meta.device_id}",
        f"# device_content_sha256: {meta.device_content_sha256}",
        f"# source_mode: {meta.source_mode}", f"# resolution: {meta.resolution}",
    ]
    if meta.period is not None:
        lines.append(f"# period: {meta.period}")
    if meta.prepared:
        lines.extend([
            f"# project_baseline_sha256: {meta.project_baseline_sha256}",
            f"# point_count: {meta.point_count}", "# prepared: true",
        ])
    for column in column_order or tuple(sorted(meta.units)):
        if column != STEP_COL and column in meta.units:
            lines.append(f"# unit.{column}: {meta.units[column]}")
    for name in sorted(meta.notes):
        lines.append(f"# note.{name}: {meta.notes[name]}")
    return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class ParsedDataFile2:
    meta: DeviceData2Meta
    header: tuple[str, ...]
    rows: list[list[str]]
    raw_sha256: str
    column_order: tuple[str, ...] = ()


def parse_data_file_v2(data: bytes) -> tuple[ParsedDataFile2 | None, list[Diagnostic]]:
    diags: list[Diagnostic] = []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, [_diag("DATA-DIAL-001", {"detail": "文件无法按 UTF-8 解码"})]
    if text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")
        diags.append(_diag("DATA-DIAL-001", {"detail": "文件含 UTF-8 BOM"}, blocking=False))
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        diags.append(_diag("DATA-DIAL-001", {"detail": "文件使用非 LF 换行"}, blocking=False))
    lines = text.split("\n")
    meta_lines: list[str] = []
    header_index: int | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            meta_lines.append(line)
            continue
        header_index = index
        break
    if header_index is None:
        return None, diags + [_diag("DATA-DIAL-001", {"detail": "文件缺少 CSV 表头"})]
    meta, meta_diags = parse_metadata_v2(meta_lines)
    diags.extend(meta_diags)
    try:
        header = next(csv.reader([lines[header_index]]))
    except csv.Error as exc:
        return None, diags + [_diag("DATA-DIAL-001", {"detail": f"CSV 表头错误: {exc}"})]
    normalized = tuple(cell.strip().lower() for cell in header)
    if not normalized or normalized[0] != STEP_COL:
        diags.append(_diag("DATA-COL-005", {"column": STEP_COL}, field_name=STEP_COL))
    for name in set(normalized):
        if normalized.count(name) > 1:
            diags.append(_diag("DATA-COL-004", {"column": name}, field_name=name))
    rows: list[list[str]] = []
    try:
        reader = csv.reader(io.StringIO("\n".join(lines[header_index + 1:])))
        for row_no, row in enumerate(reader, start=header_index + 2):
            if not row or all(not cell.strip() for cell in row):
                continue
            if row[0].lstrip().startswith("#"):
                diags.append(_diag("DATA-DIAL-001", {"detail": "数据区不允许注释"}, rows=[row_no]))
                continue
            if len(row) != len(normalized):
                diags.append(_diag(
                    "DATA-DIAL-001", {"detail": f"行字段数 {len(row)} 与表头 {len(normalized)} 不一致"},
                    rows=[row_no],
                ))
                continue
            rows.append(row)
    except csv.Error as exc:
        diags.append(_diag("DATA-DIAL-001", {"detail": f"CSV 结构错误: {exc}"}))
    if not rows:
        diags.append(_diag("DATA-DIAL-001", {"detail": "正式数据文件至少需要一行"}))
    if any(diag.blocking for diag in diags):
        return None, diags
    return ParsedDataFile2(meta, normalized, rows, hashlib.sha256(data).hexdigest()), diags


def periodic_rows(resolution: str, period: str) -> int | None:
    if resolution not in RESOLUTIONS or period not in PERIOD_VALUES:
        return None
    per_day = 1440 // RESOLUTIONS[resolution][1]
    return {"day": per_day, "week": per_day * 7, "year": per_day * 365}[period]


@dataclass(frozen=True, slots=True)
class DeviceData2Result:
    meta: DeviceData2Meta
    column_order: tuple[str, ...]
    rows: list[dict[str, Any]]
    steps: list[int]
    raw_sha256: str
    canonical_sha256: str
    transformations: tuple[str, ...]
    diagnostics: list[Diagnostic]

    def canonical_csv_bytes(self) -> bytes:
        return canonical_table_bytes_v2(self.steps, self.column_order, self.rows, meta=self.meta)


def canonical_table_bytes_v2(
    steps: list[int], columns: tuple[str, ...], rows: list[dict[str, Any]], *, meta: DeviceData2Meta,
) -> bytes:
    data_columns = tuple(column for column in columns if column != STEP_COL)
    output = io.StringIO(newline="")
    output.write(serialize_metadata_v2(meta, column_order=data_columns))
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow((STEP_COL,) + data_columns)
    for index, step in enumerate(steps):
        row = rows[index] if index < len(rows) else {}
        writer.writerow([step] + [
            "" if row.get(column) is None else repr(float(row[column])) for column in data_columns
        ])
    return output.getvalue().encode("utf-8")


def _parse_step(value: str) -> int | None:
    stripped = value.strip()
    return int(stripped) if re.fullmatch(r"0|[1-9][0-9]*", stripped) else None


def _parse_number(value: str) -> float | None:
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def canonicalize_device_data_v2(
    data: bytes,
    document: DeviceModelDocument | None,
    *,
    expected_rows: int | None = None,
    expected_project_baseline_sha256: str | None = None,
    expected_data_ref: str | None = None,
) -> DeviceData2Result:
    parsed, diags = parse_data_file_v2(data)
    raw_sha = hashlib.sha256(data).hexdigest()
    if parsed is None:
        return DeviceData2Result(DeviceData2Meta(), (STEP_COL,), [], [], raw_sha, "", (), diags)
    meta = parsed.meta
    if document is None or document.device is None:
        diags.append(_diag(
            "SYS-CFG-001", {"detail": "缺少已校验的设备模型，无法核对数据绑定"},
            field_name="device_content_sha256",
        ))
        return DeviceData2Result(meta, (STEP_COL,), [], [], parsed.raw_sha256, "", (), diags)
    expected_sha = content_sha256(document)
    if meta.device_id != document.device.id:
        diags.append(_diag(
            "DATA-META-008",
            {"declared": meta.device_id, "expected": document.device.id},
            field_name="device_id",
        ))
    if meta.device_content_sha256 != expected_sha:
        diags.append(_diag(
            "DATA-META-010", {"declared": meta.device_content_sha256, "expected": expected_sha},
            field_name="device_content_sha256",
        ))
    if expected_project_baseline_sha256 is not None and (
        not meta.prepared or meta.project_baseline_sha256 != expected_project_baseline_sha256
    ):
        diags.append(_diag(
            "DATA-META-012", {"declared": meta.project_baseline_sha256,
                               "expected": expected_project_baseline_sha256},
            field_name="project_baseline_sha256",
        ))

    file_columns = list(parsed.header[1:])
    expected_columns = [
        iid for iid, iface in document.interfaces.items()
        if iface.type == "predefined"
        and iface.source is not None
        and iface.source.mode == meta.source_mode
        and (expected_data_ref is None or iface.source.data_ref == expected_data_ref)
    ]
    if expected_data_ref is not None and not expected_columns:
        diags.append(_diag(
            "DATA-META-011", {"column": "", "mode": "missing_data_ref",
                               "source_mode": meta.source_mode, "data_ref": expected_data_ref},
            field_name="source_mode",
        ))
    mismatched: set[str] = set()
    for column in file_columns:
        iface = document.interfaces.get(column)
        if iface is not None and iface.type == "predefined" and iface.source is not None \
                and iface.source.mode != meta.source_mode:
            mismatched.add(column)
            diags.append(_diag(
                "DATA-META-011", {"column": column, "mode": iface.source.mode,
                                   "source_mode": meta.source_mode}, field_name=column,
            ))
    for column in file_columns:
        if column not in expected_columns and column not in mismatched:
            diags.append(_diag("DATA-COL-003", {"column": column}, field_name=column))
    for column in expected_columns:
        if column not in file_columns:
            diags.append(_diag("DATA-COL-005", {"column": column}, field_name=column))
    present_columns = [column for column in expected_columns if column in file_columns]
    for column in present_columns:
        declared_unit = meta.units.get(column)
        if declared_unit is None:
            diags.append(_diag("DATA-COL-007", {"column": column}, field_name=column))
        elif not units_compatible(declared_unit, document.interfaces[column].unit):
            diags.append(_diag(
                "DATA-COL-006", {"column": column, "actual": declared_unit,
                                  "expected": document.interfaces[column].unit}, field_name=column,
            ))

    steps: list[int] = []
    rows_out: list[dict[str, Any]] = []
    for row_no, row in enumerate(parsed.rows, start=1):
        step = _parse_step(row[0])
        if step is None:
            diags.append(_diag("DATA-STEP-001", {"value": row[0]}, field_name=STEP_COL, rows=[row_no]))
        else:
            steps.append(step)
        values: dict[str, Any] = {}
        for column in present_columns:
            value = row[parsed.header.index(column)].strip()
            if not value:
                diags.append(_diag("DATA-VAL-002", {"column": column}, field_name=column, rows=[row_no]))
                values[column] = None
                continue
            number = _parse_number(value)
            if number is None:
                diags.append(_diag(
                    "DATA-VAL-001", {"column": column, "value": value}, field_name=column, rows=[row_no],
                ))
                values[column] = None
                continue
            iface = document.interfaces[column]
            if (iface.minimum is not None and number < iface.minimum) or \
                    (iface.maximum is not None and number > iface.maximum):
                diags.append(_diag(
                    "DATA-VAL-001", {"column": column, "value": number,
                                      "minimum": iface.minimum, "maximum": iface.maximum},
                    field_name=column, rows=[row_no],
                ))
            values[column] = number
        rows_out.append(values)

    if len(steps) == len(parsed.rows):
        bad_order = [index + 2 for index in range(len(steps) - 1) if steps[index + 1] <= steps[index]]
        if bad_order:
            diags.append(_diag(
                "DATA-STEP-002", {"detail": "step 必须严格递增且不重复"},
                field_name=STEP_COL, rows=bad_order,
            ))
        if meta.prepared:
            if steps != list(range(len(steps))):
                diags.append(_diag(
                    "DATA-STEP-003", {"expected": "0..point_count-1",
                                      "actual_start": steps[0], "actual_end": steps[-1]}, field_name=STEP_COL,
                ))
            if meta.point_count is not None and len(steps) != meta.point_count:
                diags.append(_diag(
                    "DATA-STEP-004",
                    {"expected": meta.point_count, "actual": len(steps)},
                    field_name=STEP_COL,
                ))
        elif meta.source_mode == "data_repeat" and meta.period is not None:
            expected = periodic_rows(meta.resolution, meta.period)
            if expected is not None and len(steps) != expected:
                diags.append(_diag(
                    "DATA-STEP-004", {"expected": expected, "actual": len(steps), "period": meta.period},
                    field_name=STEP_COL,
                ))
        if expected_rows is not None and len(steps) != expected_rows:
            diags.append(_diag(
                "DATA-STEP-004", {"expected": expected_rows, "actual": len(steps)}, field_name=STEP_COL,
            ))

    column_order = (STEP_COL,) + tuple(present_columns)
    transformations = ("steps_validated", "units_declared", "values_checked")
    provisional = DeviceData2Result(
        meta, column_order, rows_out, steps, parsed.raw_sha256, "", transformations, diags,
    )
    canonical_sha = ""
    if len(steps) == len(parsed.rows):
        canonical_sha = hashlib.sha256(provisional.canonical_csv_bytes()).hexdigest()
    return DeviceData2Result(
        meta, column_order, rows_out, steps, parsed.raw_sha256, canonical_sha, transformations, diags,
    )


def build_data_quality_report_v2(result: DeviceData2Result) -> dict[str, Any]:
    return {
        "schema": result.meta.schema_id, "schema_version": result.meta.schema_version,
        "dataset_id": result.meta.dataset_id, "device_id": result.meta.device_id,
        "device_content_sha256": result.meta.device_content_sha256,
        "source_mode": result.meta.source_mode, "resolution": result.meta.resolution,
        "prepared": result.meta.prepared,
        "project_baseline_sha256": result.meta.project_baseline_sha256,
        "point_count": result.meta.point_count, "row_count": len(result.rows),
        "column_order": list(result.column_order), "raw_sha256": result.raw_sha256,
        "canonical_sha256": result.canonical_sha256,
        "has_blocking_errors": any(diag.blocking for diag in result.diagnostics),
        "transformations": list(result.transformations),
        "diagnostics": [diag.to_dict() for diag in result.diagnostics],
    }


def summary_json_v2(result: DeviceData2Result) -> str:
    return json.dumps(
        build_data_quality_report_v2(result), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


@dataclass(frozen=True, slots=True)
class PendingDataFile:
    dataset_id: str
    device_id: str
    device_content_sha256: str
    source_mode: str
    resolution: str
    period: str | None
    prepared: bool
    project_baseline_sha256: str | None
    point_count: int | None
    raw_sha256: str
    canonical_sha256: str
    row_count: int
    column_order: tuple[str, ...]


def pending_from_result(result: DeviceData2Result) -> PendingDataFile | None:
    if any(diag.blocking for diag in result.diagnostics) or not result.canonical_sha256:
        return None
    return PendingDataFile(
        dataset_id=result.meta.dataset_id, device_id=result.meta.device_id,
        device_content_sha256=result.meta.device_content_sha256,
        source_mode=result.meta.source_mode, resolution=result.meta.resolution,
        period=result.meta.period, prepared=result.meta.prepared,
        project_baseline_sha256=result.meta.project_baseline_sha256,
        point_count=result.meta.point_count, raw_sha256=result.raw_sha256,
        canonical_sha256=result.canonical_sha256, row_count=len(result.rows),
        column_order=result.column_order,
    )


__all__ = [
    "SCHEMA_ID", "SCHEMA_VERSION", "STEP_COL", "DeviceData2Meta", "ParsedDataFile2",
    "DeviceData2Result", "PendingDataFile", "parse_metadata_v2", "serialize_metadata_v2",
    "parse_data_file_v2", "periodic_rows", "canonical_table_bytes_v2",
    "canonicalize_device_data_v2", "build_data_quality_report_v2", "summary_json_v2",
    "pending_from_result",
]
