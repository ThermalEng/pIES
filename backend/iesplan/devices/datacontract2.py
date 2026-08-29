"""`ies.device-data` 2.0.0 文件契约：周期/预测结果文件校验与临时文件契约。

对应格式标准 [device-data-csv.md](../../../manual/developer-guide/zh-CN/formats/device-data-csv.md)
与宪法 §7.5/§7.8「公共文件契约」。本切片只实现纯协议校验与摘要，不接 API/ORM，
不实现上传；临时文件以纯类型 ``PendingDataFile`` 表达（候选模型门禁的受控临时
隔离区阶段，见 device-model-yaml.md「进入项目前的候选模型门禁」）。

2.0 与 1.0 的关键差异（2.0 不再绑定独立设备版本，改为内容摘要固定语义）：
- 必需元数据：schema/schema_version/dataset_id/device_id/
  device_content_sha256/source_mode/resolution/timestamp_mode/unit.<column>；
  ``device_content_sha256`` 是设备规范内容摘要，决定允许绑定的 predefined
  interfaces（宪法 §7.7：哈希、schema_version、来源与依赖共同固定语义）；
- source_mode 取值 data_repeat | data_predict（constant 直接写在设备接口中，
  不使用 CSV）；source_mode=data_repeat 还必须提供 period(day|week|year)；
- 列必须与所固定设备内容中 source.mode 与文件 source_mode 一致的
  type: predefined interface ID 完全一致：未声明列拒绝、重复列拒绝、必需列
  缺失拒绝、规范输出按设备模型 interface 声明顺序排列；一份文件只绑定一个
  稳定设备 ID 与一个精确内容摘要；
- 单位量纲兼容（kW 与 W 兼容，kW 与 kWh 不兼容）；值域越界阻断不截断；
  缺失值未声明即阻断（2.0 模型未声明缺失策略，一律阻断）；
- data_predict：时间戳严格递增无重复、与固定 resolution 对齐、同文件不混用
  带 Z/带偏移/无偏移形态；utc 模式使用带 Z 的 RFC 3339，fixed_offset 使用
  YYYY-MM-DDTHH:MM:SS 并由文件级偏移唯一换算到 UTC（不依赖机器时区/夏令时）；
- data_repeat：行数必须与周期和分辨率严格匹配；展开后的 UTC 时间轴与重复
  次数由装配阶段计算，不在本切片；
- 输出：原始摘要 + 规范表格摘要 + 质量摘要；相同语义输入 → 相同规范摘要。

与 parser2 联动：``canonicalize_device_data_v2`` 接受已解析的 2.0
``DeviceModelDocument``（parser2 产出，含 device.id 与规范内容摘要），据此
核对 predefined 列集合、单位量纲与值域。

本模块只依赖 core（units/diagnostics/timeaxis）与 devices.contracts2 的不可变
类型与纯函数，不导入 services/数据库，不实现上传/落盘。
"""

from __future__ import annotations

import csv as _csv
import hashlib
import io
import json
import math
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from iesplan.core.diagnostics import Diagnostic, make_diag
from iesplan.core.timeaxis import RESOLUTIONS
from iesplan.devices.contracts2 import (
    DeviceModelDocument,
    content_sha256,
    is_valid_id,
)
from iesplan.devices.datacontract import units_compatible

# ---------------------------------------------------------------------------
# 契约常量
# ---------------------------------------------------------------------------

SCHEMA_ID = "ies.device-data"
SCHEMA_VERSION = "2.0.0"

TIMESTAMP_COL = "timestamp"
#: 预定义来源模式（constant 直接写在设备接口中，不使用 CSV）
SOURCE_MODES: tuple[str, ...] = ("data_repeat", "data_predict")
RESOLUTION_VALUES: tuple[str, ...] = ("15min", "30min", "1h")
TIMESTAMP_MODES: tuple[str, ...] = ("utc", "fixed_offset")
PERIOD_VALUES: tuple[str, ...] = ("day", "week", "year")

#: 固定 UTC 偏移允许范围（device-data-csv.md，-840..840）
OFFSET_MIN = -840
OFFSET_MAX = 840

#: 每类诊断最多报告的行号数（避免刷屏）
MAX_ROWS_PER_DIAG = 5

#: 2.0 时间戳形态：YYYY-MM-DDTHH:MM:SS（固定偏移）或带 Z / ±HH:MM
_TS_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}$")
_TS_OFFSET_SUFFIX = re.compile(r"([+-]\d{2}:\d{2})$")

#: 单元格以这些字符开头视为公式注入风险（只警告，解析器不执行公式）
_FORMULA_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@")

#: 必需元数据键（值校验）；unit.* / note.* 为扩展键
_REQUIRED_META_KEYS: tuple[str, ...] = (
    "schema",
    "schema_version",
    "dataset_id",
    "device_id",
    "device_content_sha256",
    "source_mode",
    "resolution",
    "timestamp_mode",
)

#: 可选元数据键
_OPTIONAL_META_KEYS: frozenset[str] = frozenset({"fixed_utc_offset_minutes", "period"})

_UNIT_PREFIX = "unit."
_NOTE_PREFIX = "note."

#: 小写 64 位十六进制 SHA-256（宪法 §7.2）
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# 元数据
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeviceData2Meta:
    """解析并规范化后的 ies.device-data 2.0.0 元数据声明。"""

    schema_id: str = SCHEMA_ID
    schema_version: str = SCHEMA_VERSION
    dataset_id: str = ""
    device_id: str = ""
    device_content_sha256: str = ""
    source_mode: str = "data_predict"
    resolution: str = "1h"
    timestamp_mode: str = "fixed_offset"
    fixed_utc_offset_minutes: int = 480
    period: str | None = None
    units: dict[str, str] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    declared_columns: tuple[str, ...] = ()


def _parse_meta_line(line: str) -> tuple[str, str] | None:
    """解析单行元数据 '# key: value'（value 可为空）。"""
    stripped = line.strip()
    if not stripped.startswith("#"):
        return None
    body = stripped[1:].strip()
    if not body:
        return None
    key, sep, value = body.partition(":")
    if not sep:
        return None
    return key.strip(), value.strip()


def parse_metadata_v2(text_lines: list[str]) -> tuple[DeviceData2Meta, list[Diagnostic]]:
    """从表头之前的文本行解析 2.0.0 元数据。

    每个键只能出现一次；必需键缺失或枚举非法给出阻断诊断；返回
    (meta, diagnostics)。
    """
    diags: list[Diagnostic] = []
    raw: dict[str, str] = {}
    units: dict[str, str] = {}
    notes: dict[str, str] = {}
    declared: list[str] = []
    loc = {"object_type": "device_data", "object_id": "", "field": "metadata"}

    for line in text_lines:
        kv = _parse_meta_line(line)
        if kv is None:
            continue
        key, value = kv
        if key in raw:
            diags.append(
                make_diag(
                    "DATA-META-001",
                    severity="error",
                    blocking=True,
                    params={"key": key},
                    location=loc,
                )
            )
            continue
        raw[key] = value
        if key.startswith(_UNIT_PREFIX):
            col = key[len(_UNIT_PREFIX):]
            if not col:
                continue
            if col in units:
                diags.append(
                    make_diag(
                        "DATA-META-001",
                        severity="error",
                        blocking=True,
                        params={"key": key},
                        location=loc,
                    )
                )
                continue
            units[col] = value
            declared.append(col)
        elif key.startswith(_NOTE_PREFIX):
            notes[key[len(_NOTE_PREFIX):]] = value
        else:
            if key not in _REQUIRED_META_KEYS and key not in _OPTIONAL_META_KEYS:
                diags.append(
                    make_diag(
                        "DATA-META-002",
                        severity="error",
                        blocking=True,
                        params={"key": key, "detail": "未知核心字段，应使用 note.* 或 unit.*"},
                        location=loc,
                    )
                )

    # 必需键
    missing = [k for k in _REQUIRED_META_KEYS if k not in raw]
    if missing:
        for key in missing:
            diags.append(
                make_diag(
                    "DATA-META-002",
                    severity="error",
                    blocking=True,
                    params={"key": key},
                    location=loc,
                )
            )
        return DeviceData2Meta(), diags

    if raw.get("schema") != SCHEMA_ID:
        diags.append(
            make_diag(
                "DATA-META-003",
                severity="error",
                blocking=True,
                params={"schema": raw.get("schema"), "expected": SCHEMA_ID},
                location=loc,
            )
        )
    if raw.get("schema_version") != SCHEMA_VERSION:
        diags.append(
            make_diag(
                "DATA-META-003",
                severity="error",
                blocking=True,
                params={"schema_version": raw.get("schema_version"), "expected": SCHEMA_VERSION},
                location=loc,
            )
        )

    # 设备身份与内容摘要格式
    device_id = raw["device_id"]
    if not is_valid_id(device_id):
        diags.append(
            make_diag(
                "DATA-META-004",
                severity="error",
                blocking=True,
                params={
                    "field": "device_id",
                    "value": device_id,
                    "allowed": "小写命名空间 ID（如 acme.device.electric_load）",
                },
                location=loc,
            )
        )
    content_sha = raw["device_content_sha256"]
    if not _SHA256_PATTERN.fullmatch(content_sha):
        diags.append(
            make_diag(
                "DATA-META-004",
                severity="error",
                blocking=True,
                params={
                    "field": "device_content_sha256",
                    "value": content_sha,
                    "allowed": "64 位小写十六进制 SHA-256",
                },
                location=loc,
            )
        )

    source_mode = raw["source_mode"]
    if source_mode not in SOURCE_MODES:
        diags.append(
            make_diag(
                "DATA-META-004",
                severity="error",
                blocking=True,
                params={"field": "source_mode", "value": source_mode, "allowed": sorted(SOURCE_MODES)},
                location=loc,
            )
        )
    resolution = raw["resolution"]
    if resolution not in RESOLUTION_VALUES:
        diags.append(
            make_diag(
                "DATA-META-004",
                severity="error",
                blocking=True,
                params={"field": "resolution", "value": resolution, "allowed": sorted(RESOLUTION_VALUES)},
                location=loc,
            )
        )
    timestamp_mode = raw["timestamp_mode"]
    if timestamp_mode not in TIMESTAMP_MODES:
        diags.append(
            make_diag(
                "DATA-META-004",
                severity="error",
                blocking=True,
                params={
                    "field": "timestamp_mode",
                    "value": timestamp_mode,
                    "allowed": sorted(TIMESTAMP_MODES),
                },
                location=loc,
            )
        )

    # fixed_utc_offset_minutes
    offset_raw = raw.get("fixed_utc_offset_minutes")
    offset = 480
    if timestamp_mode == "fixed_offset":
        if offset_raw is None or offset_raw == "":
            diags.append(
                make_diag(
                    "DATA-META-005",
                    severity="error",
                    blocking=True,
                    params={"field": "fixed_utc_offset_minutes", "mode": "fixed_offset"},
                    location=loc,
                )
            )
        else:
            try:
                offset = int(offset_raw)
            except ValueError:
                diags.append(
                    make_diag(
                        "DATA-META-004",
                        severity="error",
                        blocking=True,
                        params={
                            "field": "fixed_utc_offset_minutes",
                            "value": offset_raw,
                            "allowed": f"整数 {OFFSET_MIN}..{OFFSET_MAX}",
                        },
                        location=loc,
                    )
                )
            else:
                if not (OFFSET_MIN <= offset <= OFFSET_MAX):
                    diags.append(
                        make_diag(
                            "DATA-META-007",
                            severity="error",
                            blocking=True,
                            params={"field": "fixed_utc_offset_minutes", "value": offset},
                            location=loc,
                        )
                    )
    elif offset_raw is not None and offset_raw != "":
        try:
            offset = int(offset_raw)
        except ValueError:
            diags.append(
                make_diag(
                    "DATA-META-004",
                    severity="error",
                    blocking=True,
                    params={"field": "fixed_utc_offset_minutes", "value": offset_raw, "allowed": "整数"},
                    location=loc,
                )
            )

    # period（source_mode=data_repeat 必需）
    period = None
    if source_mode == "data_repeat":
        if raw.get("period") is None or raw.get("period") == "":
            diags.append(
                make_diag(
                    "DATA-META-006",
                    severity="error",
                    blocking=True,
                    params={"field": "period", "mode": "data_repeat"},
                    location=loc,
                )
            )
        elif raw["period"] not in PERIOD_VALUES:
            diags.append(
                make_diag(
                    "DATA-META-004",
                    severity="error",
                    blocking=True,
                    params={"field": "period", "value": raw["period"], "allowed": sorted(PERIOD_VALUES)},
                    location=loc,
                )
            )
        else:
            period = raw["period"]

    return (
        DeviceData2Meta(
            schema_id=raw.get("schema", SCHEMA_ID),
            schema_version=raw.get("schema_version", SCHEMA_VERSION),
            dataset_id=raw.get("dataset_id", ""),
            device_id=device_id,
            device_content_sha256=content_sha,
            source_mode=source_mode,
            resolution=resolution,
            timestamp_mode=timestamp_mode,
            fixed_utc_offset_minutes=offset,
            period=period,
            units=units,
            notes=notes,
            declared_columns=tuple(declared),
        ),
        diags,
    )


def serialize_metadata_v2(meta: DeviceData2Meta, *, column_order: tuple[str, ...] | None = None) -> str:
    """DeviceData2Meta → 规范元数据文本（固定键序；unit.* 按 column_order 排列）。"""
    lines = [
        f"# schema: {meta.schema_id}",
        f"# schema_version: {meta.schema_version}",
        f"# dataset_id: {meta.dataset_id}",
        f"# device_id: {meta.device_id}",
        f"# device_content_sha256: {meta.device_content_sha256}",
        f"# source_mode: {meta.source_mode}",
        f"# resolution: {meta.resolution}",
        f"# timestamp_mode: {meta.timestamp_mode}",
    ]
    if meta.timestamp_mode == "fixed_offset":
        lines.append(f"# fixed_utc_offset_minutes: {meta.fixed_utc_offset_minutes}")
    if meta.period is not None:
        lines.append(f"# period: {meta.period}")
    cols = column_order if column_order is not None else sorted(meta.units)
    for col in cols:
        if col in meta.units:
            lines.append(f"# unit.{col}: {meta.units[col]}")
    for note in sorted(meta.notes):
        lines.append(f"# note.{note}: {meta.notes[note]}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 方言校验
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedDataFile2:
    """解析后的 2.0.0 设备数据文件（含原始字节摘要）。"""

    meta: DeviceData2Meta
    header: tuple[str, ...]
    rows: list[list[str]]  # 原始单元格（已去注释/空行）
    raw_sha256: str
    column_order: tuple[str, ...]  # 规范输出列顺序


def _detect_bom(text: str) -> bool:
    """检测 UTF-8 BOM（规范文件无 BOM；有 BOM 给出方言诊断但不阻断）。"""
    return text.startswith("﻿")


def _dialect_diag(detail: str, loc: dict, blocking: bool = False) -> Diagnostic:
    return make_diag(
        "DATA-DIAL-001",
        severity="error" if blocking else "warning",
        blocking=blocking,
        params={"detail": detail},
        location=loc,
    )


def _normalize_col(name: str) -> str:
    """列名归一化：去空白与小写（interface ID 按契约小写）。"""
    return name.strip().lower()


def parse_data_file_v2(data: bytes) -> tuple[ParsedDataFile2 | None, list[Diagnostic]]:
    """解析 2.0.0 设备数据 CSV 字节 → (ParsedDataFile2, diagnostics)。

    流程：
    1. 解码（UTF-8，容 BOM）；统一换行（LF）；
    2. 元数据行（# 前缀，仅表头之前）/ 表头 / 数据行分离；
    3. 校验方言（列数一致、数值单元格有限、无公式注入前缀）；
    4. 解析元数据与时间戳列名。

    不在此处做列声明/单位/时间轴校验（由 canonicalize 完成）。存在阻断性
    诊断时返回 (None, diags)。
    """
    diags: list[Diagnostic] = []
    loc = {"object_type": "device_data", "object_id": "", "field": ""}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        diags.append(_dialect_diag("文件无法按 UTF-8 解码", loc, blocking=True))
        return None, diags
    raw_sha256 = hashlib.sha256(data).hexdigest()

    has_bom = _detect_bom(text)
    if has_bom:
        text = text.lstrip("﻿")
        diags.append(_dialect_diag("文件含 UTF-8 BOM（规范要求无 BOM）", loc, blocking=False))

    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        diags.append(_dialect_diag("文件含 CR/LF 换行（规范要求 LF）", loc, blocking=False))

    raw_lines = text.split("\n")
    meta_lines: list[str] = []
    header: list[str] | None = None
    header_line_idx = 0
    for line in raw_lines:
        stripped = line.strip()
        if not stripped:
            header_line_idx += 1
            continue
        if stripped.startswith("#"):
            meta_lines.append(line)
            header_line_idx += 1
            continue
        header = [c.strip() for c in line.split(",")]
        break
    else:
        diags.append(
            make_diag(
                "DATA-META-002",
                severity="error",
                blocking=True,
                params={"key": "schema", "detail": "文件缺少表头"},
                location=loc,
            )
        )
        return None, diags

    meta, meta_diags = parse_metadata_v2(meta_lines)
    diags.extend(meta_diags)

    if header is None:
        diags.append(
            make_diag(
                "DATA-META-002",
                severity="error",
                blocking=True,
                params={"key": "schema", "detail": "文件缺少表头"},
                location=loc,
            )
        )
        return None, diags

    # 第一列固定为 timestamp（2.0 不提供 1.0 的别名）
    header_norm = [_normalize_col(c) for c in header]
    ts_index = header_norm.index(TIMESTAMP_COL) if TIMESTAMP_COL in header_norm else -1
    if ts_index < 0:
        diags.append(
            make_diag(
                "DATA-COL-005",
                severity="error",
                blocking=True,
                params={"column": TIMESTAMP_COL},
                location={**loc, "field": TIMESTAMP_COL},
            )
        )
    elif ts_index != 0:
        diags.append(
            _dialect_diag(f"第一列必须为 timestamp，实际在列 {ts_index + 1}", loc, blocking=True)
        )

    # 数据行：只允许在表头之后，不允许穿插注释
    reader = _csv.reader(io.StringIO("\n".join(raw_lines[header_line_idx + 1:])))
    data_rows: list[list[str]] = []
    row_no = header_line_idx + 1
    try:
        for raw_row in reader:
            row_no += 1
            if not raw_row or all(not c.strip() for c in raw_row):
                continue  # 空行
            if raw_row[0].lstrip().startswith("#"):
                diags.append(
                    _dialect_diag(f"数据区不允许穿插注释（行 {row_no}）", loc, blocking=True)
                )
                continue
            if len(raw_row) != len(header):
                diags.append(
                    _dialect_diag(
                        f"行字段数与表头不一致（行 {row_no}: {len(raw_row)} != {len(header)}）",
                        loc,
                        blocking=True,
                    )
                )
                continue
            data_rows.append(raw_row)
    except _csv.Error as exc:
        diags.append(_dialect_diag(f"CSV 结构错误: {exc}", loc, blocking=True))
        return None, diags

    if len(header_norm) != len(set(header_norm)):
        for idx, name in enumerate(header_norm):
            if header_norm.index(name) != idx:
                diags.append(
                    make_diag(
                        "DATA-COL-004",
                        severity="error",
                        blocking=True,
                        params={"column": name},
                        location={**loc, "field": name, "row": [1]},
                    )
                )

    # 数值单元格有限性（NaN/Inf/Infinity）与公式注入前缀（非数值文本）
    for ridx, row in enumerate(data_rows):
        for cidx, cell in enumerate(row):
            if cidx == ts_index:
                continue
            v = cell.strip()
            if not v:
                continue  # 空字段：由数值校验阶段按模型策略处理
            if v.lower() in ("nan", "inf", "infinity", "+inf", "-inf"):
                diags.append(
                    _dialect_diag(
                        f"非有限数值 {v!r}（行 {ridx + 1}）",
                        {**loc, "row": [ridx + 1]},
                        blocking=True,
                    )
                )
                continue
            if v[0] in _FORMULA_PREFIXES and _parse_number_cell(v) is None:
                diags.append(
                    _dialect_diag(
                        f"单元格以公式注入前缀 {v[0]!r} 开头（行 {ridx + 1}）",
                        {**loc, "row": [ridx + 1]},
                        blocking=False,
                    )
                )

    if any(d.blocking for d in diags):
        return None, diags

    return (
        ParsedDataFile2(
            meta=meta,
            header=tuple(header_norm),
            rows=data_rows,
            raw_sha256=raw_sha256,
            column_order=(),
        ),
        diags,
    )


# ---------------------------------------------------------------------------
# 时间戳解析
# ---------------------------------------------------------------------------


def _parse_timestamp_cell(value: str) -> tuple[datetime | None, str | None]:
    """解析单个 2.0 时间戳单元格 → (datetime_utc_or_naive, form)。

    form（时间戳形态）：'utc_z' | 'offset' | 'local'。返回 (None, None) 表示解析失败。
    """
    v = value.strip()
    if not v:
        return None, None
    # 带 Z → UTC
    if v.endswith("Z") or v.endswith("z"):
        inner = v[:-1]
        if not _TS_PATTERN.match(inner):
            return None, None
        try:
            dt = datetime.fromisoformat(inner.replace(" ", "T"))
        except ValueError:
            return None, None
        return dt.replace(tzinfo=UTC), "utc_z"
    # 带 ±HH:MM 偏移
    if _TS_OFFSET_SUFFIX.search(v) is not None:
        try:
            dt = datetime.fromisoformat(v.replace(" ", "T"))
        except ValueError:
            return None, None
        if dt.tzinfo is None:
            return None, None
        return dt.astimezone(UTC), "offset"
    # 无偏移本地时间（保持 naive；换算由调用方按声明模式执行）
    if not _TS_PATTERN.match(v):
        return None, None
    try:
        dt = datetime.fromisoformat(v.replace(" ", "T"))
    except ValueError:
        return None, None
    return dt, "local"


def _apply_fixed_offset(dt: datetime, offset_minutes: int) -> datetime:
    """无偏移本地时间 → UTC（本地 = UTC + 偏移，故 UTC = 本地 - 偏移）。"""
    if dt.tzinfo is not None:
        return dt.astimezone(UTC)
    return dt.replace(tzinfo=UTC) - timedelta(minutes=offset_minutes)


# ---------------------------------------------------------------------------
# 周期行数
# ---------------------------------------------------------------------------


def periodic_rows(resolution: str, period: str) -> int | None:
    """data_repeat 期望行数（day→24/48/96 按分辨率换算；week 为 7 倍；year 为 365 倍）。"""
    if resolution not in RESOLUTIONS or period not in PERIOD_VALUES:
        return None
    step_min = RESOLUTIONS[resolution][1]
    steps_per_day = 1440 // step_min
    if period == "day":
        return steps_per_day
    if period == "week":
        return steps_per_day * 7
    if period == "year":
        return steps_per_day * 365
    return None


# ---------------------------------------------------------------------------
# 规范化与摘要
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeviceData2Result:
    """规范化产物（原始摘要 + 规范摘要 + 质量摘要 + 变换记录）。"""

    meta: DeviceData2Meta
    column_order: tuple[str, ...]
    rows: list[dict]  # 规范行：{column: float}，含 timestamp(datetime UTC)
    utc_timestamps: list[datetime]
    raw_sha256: str
    canonical_sha256: str
    transformations: tuple[str, ...]
    diagnostics: list[Diagnostic]

    def canonical_meta(self) -> DeviceData2Meta:
        """规范元数据：时间戳一律以 UTC 表达（带 Z），保证同一瞬时唯一形态。

        fixed_offset 与 utc 两种写法换算到 UTC 后语义相同，规范摘要必须一致
        （device-data-csv.md 完成标准：同一语义输入得到相同规范摘要）。
        """
        return replace(self.meta, timestamp_mode="utc", fixed_utc_offset_minutes=0)

    def canonical_csv_bytes(self) -> bytes:
        """规范化表格字节（UTF-8、LF、RFC 4180；元数据按 UTC 形态）。"""
        return canonical_table_bytes_v2(
            self.utc_timestamps,
            self.column_order,
            self.rows,
            meta=self.canonical_meta(),
        )


def _format_number(v: float) -> str:
    """数值规范形：Python repr（最短往返表示），不得低位宽格式静默舍入。"""
    if not math.isfinite(v):
        raise ValueError(f"非有限数值 {v!r} 不允许进入规范表格")
    return repr(float(v))


def canonical_table_bytes_v2(
    utc_timestamps: list[datetime],
    columns: tuple[str, ...],
    rows: list[dict],
    *,
    meta: DeviceData2Meta | None = None,
) -> bytes:
    """规范化数据表字节（UTC 带 Z 时间戳 + 规范数值格式，UTF-8/LF）。"""
    buf = io.StringIO()
    if meta is not None:
        data_cols = tuple(c for c in columns if c != TIMESTAMP_COL)
        buf.write(serialize_metadata_v2(meta, column_order=data_cols))
    buf.write(",".join(columns))
    buf.write("\n")
    for ridx, row in enumerate(rows):
        cells: list[str] = []
        for col in columns:
            if col == TIMESTAMP_COL:
                ts = utc_timestamps[ridx] if ridx < len(utc_timestamps) else None
                if isinstance(ts, datetime):
                    cells.append(ts.astimezone(UTC).isoformat().replace("+00:00", "Z"))
                else:
                    cells.append("")
                continue
            v = row.get(col)
            if isinstance(v, bool):
                cells.append("true" if v else "false")
            elif v is None:
                cells.append("")
            else:
                cells.append(_format_number(float(v)))
        buf.write(",".join(cells))
        buf.write("\n")
    return buf.getvalue().encode("utf-8")


def canonicalize_device_data_v2(
    data: bytes,
    document: DeviceModelDocument | None,
    *,
    expected_rows: int | None = None,
) -> DeviceData2Result:
    """规范化 ies.device-data 2.0.0 CSV 字节 → DeviceData2Result（唯一纯函数入口）。

    ``document`` 为已解析并校验的 2.0 设备文档（parser2 产出）；其 device.id
    与规范内容摘要（contracts2.content_sha256）决定允许绑定的 predefined 列。
    ``expected_rows``：data_predict 期望行数（由装配 YAML 明确，不靠
    “看起来像一年”猜测）；None 时不校验。

    步骤（device-data-csv.md「校验与规范输出」）：
    1. 识别编码/换行/元数据/方言（parse_data_file_v2）；
    2. 解析 schema/设备 ID/内容摘要/来源模式/时间模式/分辨率/单位；
    3. 按 device_content_sha256 核对 predefined 列、单位量纲、值域与缺失策略；
    4. 校验时间单调、步长、周期行数与期望覆盖范围；
    5. 时间规范化为 UTC，数值表示规范化；
    6. 生成原始摘要、规范表格摘要、质量摘要与变换记录。
    """
    diags: list[Diagnostic] = []
    parsed, parse_diags = parse_data_file_v2(data)
    diags.extend(parse_diags)
    if parsed is None:
        return DeviceData2Result(
            meta=DeviceData2Meta(),
            column_order=(TIMESTAMP_COL,),
            rows=[],
            utc_timestamps=[],
            raw_sha256=hashlib.sha256(data).hexdigest(),
            canonical_sha256="",
            transformations=(),
            diagnostics=diags,
        )

    meta = parsed.meta
    loc = {"object_type": "device_data", "object_id": meta.dataset_id, "field": ""}

    # ---- 目标设备绑定（device_id + 精确内容摘要） ----
    if document is None or document.device is None:
        diags.append(
            make_diag(
                "SYS-CFG-001",
                severity="error",
                blocking=True,
                params={
                    "file": "<device-data>",
                    "detail": "未提供已校验的 2.0 设备文档（device_content_sha256 无法核对）",
                },
                location={**loc, "field": "device_content_sha256"},
            )
        )
        return DeviceData2Result(
            meta=meta,
            column_order=(TIMESTAMP_COL,),
            rows=[],
            utc_timestamps=[],
            raw_sha256=parsed.raw_sha256,
            canonical_sha256="",
            transformations=(),
            diagnostics=diags,
        )

    expected_sha = content_sha256(document)
    if meta.device_content_sha256 != expected_sha:
        diags.append(
            make_diag(
                "DATA-META-010",
                severity="error",
                blocking=True,
                params={"declared": meta.device_content_sha256, "expected": expected_sha},
                location={**loc, "field": "device_content_sha256"},
            )
        )
    if meta.device_id != document.device.id:
        diags.append(
            make_diag(
                "DATA-META-008",
                severity="error",
                blocking=True,
                params={"declared": meta.device_id, "expected": document.device.id},
                location={**loc, "field": "device_id"},
            )
        )

    # ---- 列声明：predefined + source.mode 与文件 source_mode 一致 ----
    header = [c for c in parsed.header if c != TIMESTAMP_COL]
    # 文件声明了列、但设备该接口的预定义来源模式与文件不一致 → 逐列明确诊断
    mode_mismatch: set[str] = set()
    for col in header:
        iface = document.interfaces.get(col)
        if (
            iface is not None
            and iface.type == "predefined"
            and iface.source is not None
            and iface.source.mode in SOURCE_MODES
            and iface.source.mode != meta.source_mode
        ):
            mode_mismatch.add(col)
            diags.append(
                make_diag(
                    "DATA-META-011",
                    severity="error",
                    blocking=True,
                    params={
                        "column": col,
                        "mode": iface.source.mode,
                        "source_mode": meta.source_mode,
                    },
                    location={**loc, "field": col},
                )
            )
    expected_cols = [
        iid
        for iid, iface in document.interfaces.items()
        if iface.type == "predefined"
        and iface.source is not None
        and iface.source.mode == meta.source_mode
    ]
    if not expected_cols:
        diags.append(
            make_diag(
                "DATA-META-011",
                severity="error",
                blocking=True,
                params={
                    "column": "",
                    "mode": "none",
                    "source_mode": meta.source_mode,
                    "detail": "设备无 source.mode 与文件 source_mode 一致的 predefined interface",
                },
                location={**loc, "field": "source_mode"},
            )
        )
    unknown_cols = [c for c in header if c not in expected_cols and c not in mode_mismatch]
    for col in unknown_cols:
        diags.append(
            make_diag(
                "DATA-COL-003",
                severity="error",
                blocking=True,
                params={"column": col},
                location={**loc, "field": col},
            )
        )
    missing_cols = [c for c in expected_cols if c not in header]
    for col in missing_cols:
        diags.append(
            make_diag(
                "DATA-COL-005",
                severity="error",
                blocking=True,
                params={"column": col},
                location={**loc, "field": col},
            )
        )
    # 规范输出按设备模型 interface 声明顺序排列（仅保留实际出现的列）
    declared_present = [c for c in expected_cols if c in header]

    # 单位核对：存在的数据列必须声明 unit.<column> 且与设备模型量纲兼容
    for col in declared_present:
        iface = document.interfaces[col]
        declared_unit = meta.units.get(col)
        if declared_unit is None:
            diags.append(
                make_diag(
                    "DATA-COL-007",
                    severity="error",
                    blocking=True,
                    params={"column": col},
                    location={**loc, "field": col},
                )
            )
            continue
        if not units_compatible(declared_unit, iface.unit):
            diags.append(
                make_diag(
                    "DATA-COL-006",
                    severity="error",
                    blocking=True,
                    params={"column": col, "actual": declared_unit, "expected": iface.unit},
                    location={**loc, "field": col},
                )
            )

    # ---- 时间轴 ----
    ts_index = parsed.header.index(TIMESTAMP_COL) if TIMESTAMP_COL in parsed.header else -1
    timestamps_parsed: list[tuple[datetime, str]] = []
    ts_forms: set[str] = set()
    for ridx, row in enumerate(parsed.rows):
        raw_ts = row[ts_index] if ts_index >= 0 and ts_index < len(row) else ""
        dt, form = _parse_timestamp_cell(raw_ts)
        if dt is None:
            diags.append(
                make_diag(
                    "DATA-TIME-005",
                    severity="error",
                    blocking=True,
                    params={"value": raw_ts, "row_no": ridx + 1},
                    location={**loc, "field": TIMESTAMP_COL, "row": [ridx + 1]},
                )
            )
            continue
        # 时间戳形态必须与声明 timestamp_mode 匹配：不允行内偏移绕过文件级
        # fixed_utc_offset_minutes；utc 模式只接受带 Z 形态
        if meta.timestamp_mode == "fixed_offset" and form != "local":
            diags.append(
                make_diag(
                    "DATA-TIME-006",
                    severity="error",
                    blocking=True,
                    params={
                        "value": raw_ts,
                        "timestamp_mode": meta.timestamp_mode,
                        "form": form,
                        "row_no": ridx + 1,
                    },
                    location={**loc, "field": TIMESTAMP_COL, "row": [ridx + 1]},
                )
            )
            continue
        if meta.timestamp_mode == "utc" and form != "utc_z":
            diags.append(
                make_diag(
                    "DATA-TIME-006",
                    severity="error",
                    blocking=True,
                    params={
                        "value": raw_ts,
                        "timestamp_mode": meta.timestamp_mode,
                        "form": form,
                        "row_no": ridx + 1,
                    },
                    location={**loc, "field": TIMESTAMP_COL, "row": [ridx + 1]},
                )
            )
            continue
        if form is not None:
            ts_forms.add(form)
        timestamps_parsed.append((dt, raw_ts))

    # 同文件不混用带 Z/带偏移/无偏移形态（防御：形态过滤后仍可能残留多种）
    if len(ts_forms) > 1:
        diags.append(
            make_diag(
                "DATA-TIME-003",
                severity="error",
                blocking=True,
                params={"kinds": sorted(ts_forms)},
                location={**loc, "field": TIMESTAMP_COL},
            )
        )

    # 换算到 UTC（fixed_offset 只有无偏移本地时间进入本段；utc 只带 Z）
    utc_ts: list[datetime] = []
    for dt, _raw in timestamps_parsed:
        if dt.tzinfo is not None:
            utc_ts.append(dt.astimezone(UTC))
        else:
            utc_ts.append(_apply_fixed_offset(dt, meta.fixed_utc_offset_minutes))

    # 正式数据集至少一行（空文件只能用于非法样例测试）
    if not parsed.rows:
        diags.append(
            _dialect_diag("正式数据集至少一行数据（空文件只能用于非法样例测试）", loc, blocking=True)
        )

    # 时间单调 / 步长对齐（两种来源模式共用）
    diags.extend(_validate_timeline2(utc_ts, meta, loc))

    # data_repeat：行数与周期/分辨率严格匹配
    if meta.source_mode == "data_repeat":
        n_expected = periodic_rows(meta.resolution, meta.period) if meta.period is not None else None
        if n_expected is not None and len(parsed.rows) != n_expected:
            diags.append(
                make_diag(
                    "DATA-TIME-004",
                    severity="error",
                    blocking=True,
                    params={
                        "period": meta.period,
                        "resolution": meta.resolution,
                        "expected": n_expected,
                        "actual": len(parsed.rows),
                    },
                    location={**loc, "field": TIMESTAMP_COL},
                )
            )
    # data_predict：期望行数由装配 YAML 明确（调用方显式传入）
    elif expected_rows is not None and len(parsed.rows) != expected_rows:
        diags.append(
            make_diag(
                "DATA-TS-004",
                severity="error",
                blocking=True,
                params={
                    "expected": expected_rows,
                    "actual": len(parsed.rows),
                    "resolution": meta.resolution,
                },
                location={**loc, "field": TIMESTAMP_COL},
            )
        )

    # ---- 数值规范化与值域校验 ----
    rows_out: list[dict] = []
    for ridx, row in enumerate(parsed.rows):
        out: dict = {}
        if utc_ts and ridx < len(utc_ts):
            out[TIMESTAMP_COL] = utc_ts[ridx]
        for col in declared_present:
            iface = document.interfaces[col]
            idx = parsed.header.index(col)
            raw_val = row[idx] if idx < len(row) else ""
            cell = raw_val.strip()
            if cell == "":
                # 缺失值：2.0 模型未声明缺失策略，一律阻断
                diags.append(
                    make_diag(
                        "DATA-VAL-002",
                        severity="error",
                        blocking=True,
                        params={"column": col, "row_no": ridx + 1},
                        location={**loc, "field": col, "row": [ridx + 1]},
                    )
                )
                out[col] = None
                continue
            num = _parse_number_cell(cell)
            if num is None:
                diags.append(
                    make_diag(
                        "DATA-VAL-001",
                        severity="error",
                        blocking=True,
                        params={
                            "column": col,
                            "value": cell,
                            "row_no": ridx + 1,
                            "detail": "非有限数值或格式非法",
                        },
                        location={**loc, "field": col, "row": [ridx + 1]},
                    )
                )
                out[col] = None
                continue
            # 范围校验（不截断，阻断）
            if iface.minimum is not None and num < iface.minimum:
                diags.append(
                    make_diag(
                        "DATA-VAL-001",
                        severity="error",
                        blocking=True,
                        params={
                            "column": col,
                            "value": num,
                            "minimum": iface.minimum,
                            "row_no": ridx + 1,
                        },
                        location={**loc, "field": col, "row": [ridx + 1]},
                    )
                )
            if iface.maximum is not None and num > iface.maximum:
                diags.append(
                    make_diag(
                        "DATA-VAL-001",
                        severity="error",
                        blocking=True,
                        params={
                            "column": col,
                            "value": num,
                            "maximum": iface.maximum,
                            "row_no": ridx + 1,
                        },
                        location={**loc, "field": col, "row": [ridx + 1]},
                    )
                )
            out[col] = num
        rows_out.append(out)

    # 数组长度一致性：时间与数据行数不一致时明确失败
    if utc_ts and len(utc_ts) != len(parsed.rows):
        diags.append(
            make_diag(
                "DATA-ARR-001",
                severity="error",
                blocking=True,
                params={"expected": len(parsed.rows), "actual": len(utc_ts)},
                location={**loc, "field": TIMESTAMP_COL},
            )
        )

    # ---- 规范表格摘要 ----
    column_order = (TIMESTAMP_COL,) + tuple(declared_present)
    result = DeviceData2Result(
        meta=meta,
        column_order=column_order,
        rows=rows_out,
        utc_timestamps=utc_ts,
        raw_sha256=parsed.raw_sha256,
        canonical_sha256="",
        transformations=("time_to_utc", "units_declared", "values_checked"),
        diagnostics=diags,
    )
    canonical_bytes = result.canonical_csv_bytes()
    return DeviceData2Result(
        meta=meta,
        column_order=column_order,
        rows=rows_out,
        utc_timestamps=utc_ts,
        raw_sha256=parsed.raw_sha256,
        canonical_sha256=hashlib.sha256(canonical_bytes).hexdigest(),
        transformations=("time_to_utc", "units_declared", "values_checked"),
        diagnostics=diags,
    )


def _validate_timeline2(utc_ts: list[datetime], meta: DeviceData2Meta, loc: dict) -> list[Diagnostic]:
    """时间戳：严格递增无重复、与声明 resolution 对齐（两种来源模式共用）。"""
    out: list[Diagnostic] = []
    if len(utc_ts) < 2:
        return out
    step_seconds = RESOLUTIONS[meta.resolution][2]
    loc_ts = {**loc, "field": TIMESTAMP_COL}
    dup_rows: list[int] = []
    disorder_rows: list[int] = []
    for i in range(1, len(utc_ts)):
        if utc_ts[i] == utc_ts[i - 1]:
            dup_rows.append(i)
        elif utc_ts[i] < utc_ts[i - 1]:
            disorder_rows.append(i)
    if dup_rows:
        first_dup = [r + 1 for r in dup_rows[:MAX_ROWS_PER_DIAG]]
        out.append(
            make_diag(
                "DATA-TIME-001",
                severity="error",
                blocking=True,
                params={"detail": "重复时间戳", "count": len(dup_rows), "first_rows": first_dup},
                location={**loc_ts, "row": first_dup},
            )
        )
    if disorder_rows:
        first_dis = [r + 1 for r in disorder_rows[:MAX_ROWS_PER_DIAG]]
        out.append(
            make_diag(
                "DATA-TIME-001",
                severity="error",
                blocking=True,
                params={
                    "detail": "时间戳未严格递增",
                    "count": len(disorder_rows),
                    "first_rows": first_dis,
                },
                location={**loc_ts, "row": first_dis},
            )
        )
    mis_rows: list[int] = []
    for i in range(1, len(utc_ts)):
        delta = (utc_ts[i] - utc_ts[i - 1]).total_seconds()
        if abs(delta - step_seconds) > 1e-6:
            mis_rows.append(i)
    if mis_rows:
        first_mis = [r + 1 for r in mis_rows[:MAX_ROWS_PER_DIAG]]
        out.append(
            make_diag(
                "DATA-TIME-002",
                severity="error",
                blocking=True,
                params={
                    "resolution": meta.resolution,
                    "step_seconds": step_seconds,
                    "count": len(mis_rows),
                    "first_rows": first_mis,
                },
                location={**loc_ts, "row": first_mis},
            )
        )
    return out


def _parse_number_cell(value: str) -> float | None:
    """解析数值单元格；非有限值或含千分位/区域化数字返回 None。"""
    v = value.strip()
    if not v:
        return None
    # 禁止千分位逗号（RFC 4180 引号字段也不放行）
    if "," in v:
        return None
    try:
        parsed = float(v)
    except ValueError:
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


# ---------------------------------------------------------------------------
# 摘要（质量报告）与临时文件契约
# ---------------------------------------------------------------------------


def build_data_quality_report_v2(result: DeviceData2Result) -> dict:
    """规范化产物 → 质量摘要（原始摘要 + 规范摘要 + 质量 + 变换记录）。"""
    blocking = [d for d in result.diagnostics if d.blocking]
    return {
        "schema": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "dataset_id": result.meta.dataset_id,
        "device_id": result.meta.device_id,
        "device_content_sha256": result.meta.device_content_sha256,
        "source_mode": result.meta.source_mode,
        "resolution": result.meta.resolution,
        "timestamp_mode": result.meta.timestamp_mode,
        "row_count": len(result.rows),
        "raw_sha256": result.raw_sha256,
        "canonical_sha256": result.canonical_sha256,
        "transformations": list(result.transformations),
        "has_blocking_errors": bool(blocking),
        "diagnostics": [d.to_dict() for d in result.diagnostics],
    }


def summary_json_v2(result: DeviceData2Result) -> str:
    """规范摘要的可读文本（相同输入 → 相同文本，供测试断言）。"""
    report = build_data_quality_report_v2(result)
    return json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class PendingDataFile:
    """候选设备数据文件（纯类型契约；本切片不实现上传/落盘）。

    对应 device-model-yaml.md「进入项目前的候选模型门禁」的受控临时隔离区
    阶段：文件只作为未落盘候选字节参与校验，不进入项目正式模型目录、设备
    目录、装配目录或可选择目录；校验通过后由调用方（未来 application 用例）
    原子保存并分配数据版本。阻断性校验失败不得由本类型代表任何状态。
    """

    dataset_id: str
    device_id: str
    device_content_sha256: str
    source_mode: str
    resolution: str
    timestamp_mode: str
    period: str | None
    raw_sha256: str
    canonical_sha256: str
    row_count: int
    column_order: tuple[str, ...]


def pending_from_result(result: DeviceData2Result) -> PendingDataFile | None:
    """从校验结果构造候选数据文件描述；存在阻断诊断时返回 None。

    失败可见、状态完整（宪法 §2.5）：阻断错误不得成为可提交的临时文件。
    """
    if any(d.blocking for d in result.diagnostics):
        return None
    return PendingDataFile(
        dataset_id=result.meta.dataset_id,
        device_id=result.meta.device_id,
        device_content_sha256=result.meta.device_content_sha256,
        source_mode=result.meta.source_mode,
        resolution=result.meta.resolution,
        timestamp_mode=result.meta.timestamp_mode,
        period=result.meta.period,
        raw_sha256=result.raw_sha256,
        canonical_sha256=result.canonical_sha256,
        row_count=len(result.rows),
        column_order=result.column_order,
    )


__all__ = [
    "SCHEMA_ID",
    "SCHEMA_VERSION",
    "TIMESTAMP_COL",
    "SOURCE_MODES",
    "RESOLUTION_VALUES",
    "TIMESTAMP_MODES",
    "PERIOD_VALUES",
    "OFFSET_MIN",
    "OFFSET_MAX",
    "DeviceData2Meta",
    "DeviceData2Result",
    "ParsedDataFile2",
    "PendingDataFile",
    "parse_metadata_v2",
    "parse_data_file_v2",
    "serialize_metadata_v2",
    "canonicalize_device_data_v2",
    "canonical_table_bytes_v2",
    "periodic_rows",
    "build_data_quality_report_v2",
    "summary_json_v2",
    "pending_from_result",
]
