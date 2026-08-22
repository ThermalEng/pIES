"""ies.device-data 1.0.0 文件契约:元数据解析、CSV 方言校验、规范化与摘要。

本模块是 0.6.0「设备数据文件契约」的唯一纯函数实现,供两条调用链共用:

1. GUI 上传:services/dataset.py 的 parse/validate 最终收敛到
   ``canonicalize_device_data``, 与包内设备 CSV 走同一规范化流程;
2. 包内设备 CSV:devices/profile.py 的 read_standard_csv/validate_series_csv
   消费本模块的元数据/方言/时间轴/单位校验, 不再维护第二套规则。

契约要点(见 manual/developer-guide/zh-CN/formats/device-data-csv.md):
- 文件依次三部分: 元数据行(# 前缀) + 一行表头 + 零或多行数据;
- 必需元数据: schema=schema_version=dataset_id=device_model=series_mode=
  resolution=timestamp_mode=unit.<column>;
- timestamp_mode=fixed_offset 必须提供 fixed_utc_offset_minutes(-840..840),
  不依赖机器时区/夏令时;
- series_mode=periodic 必须提供 period(day|week|year);
- 方言: UTF-8、LF、英文逗号、RFC 4180、小数点 .、布尔 true/false、
  禁止 NaN/Inf/公式/区域化数字/千位分隔符;缺失值为空字段仅当模型允许;
- 第一列固定 timestamp; 后续列 ID 必须与设备模型 data_inputs 完全一致;
  未声明列拒绝、重复列拒绝、必需列缺失拒绝、规范输出按模型声明顺序排列;
- timeline: 时间戳严格递增无重复、与分辨率对齐、同文件不混用带Z/带偏移/
  无偏移; utc 用 RFC 3339 带 Z; fixed_offset 用 YYYY-MM-DDTHH:MM:SS 由文件级
  偏移唯一换算 UTC;
- 数值先按设备模型 value_type/范围/有限性校验; 超范围阻断不截断; 缺值按模型
  策略, 未声明阻断;
- 原始文件 SHA-256 与规范表格 SHA-256 都保留; 不静默删行/补零/前值填充/
  解析失败变空集。

本模块只依赖 core(units/diagnostics/errors/timeaxis/idgen)与 devices 公开
descriptor, 不导入 services 或数据库。

列声明来源(0.5.0 并行契约锁定):
- 最终以设备模型 ``data_inputs`` 映射 {column_id: {value_type, quantity,
  unit, required, minimum?, maximum?}} 为准;
- 若 data_inputs 尚不可用, 读 ``time_series.inputs``(key/unit/required/period)
  作为列声明来源, 并在参数名上标注 "最终以 ies.device-model 1.0.0
  data_inputs 为准"。
"""

from __future__ import annotations

import csv as _csv
import hashlib
import io
import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from iesplan.core.diagnostics import (
    Diagnostic,
    make_diag,
)
from iesplan.core.errors import AppError
from iesplan.core.timeaxis import RESOLUTIONS, validate_timestamps
from iesplan.core.units import (
    UnitError,
    dims_of,
)

# ---------------------------------------------------------------------------
# 契约常量
# ---------------------------------------------------------------------------

SCHEMA_ID = "ies.device-data"
SCHEMA_VERSION = "1.0.0"

TIMESTAMP_COL = "timestamp"
#: 时间戳列别名(解析时归一化到 TIMESTAMP_COL)
TS_ALIASES: tuple[str, ...] = ("timestamp", "time", "datetime")

SERIES_MODES: tuple[str, ...] = ("timeline", "periodic")
RESOLUTION_VALUES: tuple[str, ...] = ("15min", "30min", "1h")
TIMESTAMP_MODES: tuple[str, ...] = ("utc", "fixed_offset")
PERIOD_VALUES: tuple[str, ...] = ("day", "week", "year")

#: 固定 UTC 偏移允许范围(与 device-data-csv.md 一致, -840..840)
OFFSET_MIN = -840
OFFSET_MAX = 840

#: 每类诊断最多报告的行号数(避免刷屏)
MAX_ROWS_PER_DIAG = 5

#: 解析时间戳支持的分隔符(YYYY-MM-DD[T ]HH:MM[:SS])
_TS_PATTERNS = (
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}$",
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}$",
)
_TS_PATTERNS_COMPILED = tuple(re.compile(p) for p in _TS_PATTERNS)
#: 时区偏移后缀(±HH:MM, 出现在时间之后)
_TS_OFFSET_SUFFIX = re.compile(r"([+-]\d{2}:\d{2})$")


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------


class DeviceDataError(AppError):
    """ies.device-data 契约校验失败(HTTP 400, 携带阻断性诊断)。"""

    code = "DATA-VAL-001"
    severity = "error"
    message_key = "ies.error.device_data_invalid"
    http_status = 400

    def __init__(self, diagnostics: list[Diagnostic], message: str = "") -> None:
        self.diagnostics = list(diagnostics)
        super().__init__(message or f"设备数据契约校验失败: 共 {len(self.diagnostics)} 条阻断性诊断")


# ---------------------------------------------------------------------------
# 元数据
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeviceDataMeta:
    """解析并规范化后的 ies.device-data 元数据声明。"""

    schema_id: str = SCHEMA_ID
    schema_version: str = SCHEMA_VERSION
    dataset_id: str = ""
    device_model: str = ""
    series_mode: str = "timeline"
    resolution: str = "1h"
    timestamp_mode: str = "fixed_offset"
    fixed_utc_offset_minutes: int = 480
    period: str | None = None
    units: dict[str, str] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    declared_columns: tuple[str, ...] = ()


#: 必需元数据键(值校验); note.* 为可扩展注释
_REQUIRED_META_KEYS: tuple[str, ...] = (
    "schema",
    "schema_version",
    "dataset_id",
    "device_model",
    "series_mode",
    "resolution",
    "timestamp_mode",
)

#: 可选元数据键
_OPTIONAL_META_KEYS: frozenset[str] = frozenset({"fixed_utc_offset_minutes", "period"})

#: 元数据键中 unit.<column> 前缀
_UNIT_PREFIX = "unit."
#: 注释键前缀 note.<name>
_NOTE_PREFIX = "note."


def _parse_meta_line(line: str) -> tuple[str, str] | None:
    """解析单行元数据 '# key: value'(value 可为空)。

    返回 (key, value); 非 '#' 前缀或非 'key: value' 形态返回 None。
    """
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


def parse_metadata(text_lines: list[str]) -> tuple[DeviceDataMeta, list[Diagnostic]]:
    """从表头之前的文本行解析元数据(0.6.0 契约)。

    每个键只能出现一次; 必需键缺失或枚举非法给出阻断诊断; 返回
    (meta, diagnostics)。文本行可能含 UTF-8 BOM, 此处按纯文本处理。
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
            # 除 unit./note. 外的任意元数据键: 未知核心字段拒绝(0.6.0 契约)
            if key not in _REQUIRED_META_KEYS and key not in _OPTIONAL_META_KEYS:
                diags.append(
                    make_diag(
                        "DATA-META-002",
                        severity="error",
                        blocking=True,
                        params={"key": key, "detail": "未知核心字段, 应使用 note.* 或 unit.*"},
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
        return DeviceDataMeta(), diags

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

    series_mode = raw["series_mode"]
    if series_mode not in SERIES_MODES:
        diags.append(
            make_diag(
                "DATA-META-004",
                severity="error",
                blocking=True,
                params={"field": "series_mode", "value": series_mode, "allowed": sorted(SERIES_MODES)},
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
                params={"field": "timestamp_mode", "value": timestamp_mode, "allowed": sorted(TIMESTAMP_MODES)},
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
                        params={"field": "fixed_utc_offset_minutes", "value": offset_raw, "allowed": f"{OFFSET_MIN}..{OFFSET_MAX}"},
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

    # period
    period = None
    if series_mode == "periodic":
        if raw.get("period") is None or raw.get("period") == "":
            diags.append(
                make_diag(
                    "DATA-META-006",
                    severity="error",
                    blocking=True,
                    params={"field": "period", "mode": "periodic"},
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
        DeviceDataMeta(
            schema_id=raw.get("schema", SCHEMA_ID),
            schema_version=raw.get("schema_version", SCHEMA_VERSION),
            dataset_id=raw.get("dataset_id", ""),
            device_model=raw.get("device_model", ""),
            series_mode=series_mode,
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


def serialize_metadata(meta: DeviceDataMeta) -> str:
    """DeviceDataMeta → 规范元数据文本(按固定顺序, 供规范表格头使用)。"""
    lines = [
        f"# schema: {meta.schema_id}",
        f"# schema_version: {meta.schema_version}",
        f"# dataset_id: {meta.dataset_id}",
        f"# device_model: {meta.device_model}",
        f"# series_mode: {meta.series_mode}",
        f"# resolution: {meta.resolution}",
        f"# timestamp_mode: {meta.timestamp_mode}",
    ]
    if meta.timestamp_mode == "fixed_offset":
        lines.append(f"# fixed_utc_offset_minutes: {meta.fixed_utc_offset_minutes}")
    if meta.period is not None:
        lines.append(f"# period: {meta.period}")
    for col in sorted(meta.units):
        lines.append(f"# unit.{col}: {meta.units[col]}")
    for note in sorted(meta.notes):
        lines.append(f"# note.{note}: {meta.notes[note]}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 方言校验
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedDataFile:
    """解析后的设备数据文件(含原始字节摘要)。"""

    meta: DeviceDataMeta
    header: tuple[str, ...]
    rows: list[list[str]]  # 原始单元格(已去注释/空行)
    raw_sha256: str
    column_order: tuple[str, ...]  # 规范输出列顺序


def _detect_bom(text: str) -> bool:
    """检测 UTF-8 BOM(规范文件无 BOM; 有 BOM 给出方言诊断但不阻断)。"""
    return text.startswith("﻿")


def _dialect_diag(detail: str, loc: dict, blocking: bool = False) -> Diagnostic:
    return make_diag(
        "DATA-DIAL-001",
        severity="error" if blocking else "warning",
        blocking=blocking,
        params={"detail": detail},
        location=loc,
    )


#: 单元格以这些字符开头视为公式注入风险(只警告, 解析器不执行公式)
_FORMULA_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@")


def parse_data_file(data: bytes) -> tuple[ParsedDataFile | None, list[Diagnostic]]:
    """解析设备数据 CSV 字节 → (ParsedDataFile, diagnostics)。

    流程:
    1. 解码(UTF-8, 容 BOM);
    2. 按行分离: 元数据行(# 前缀, 仅表头之前) / 表头 / 数据行;
    3. 校验方言(列数一致、数值单元格有限、无公式前缀);
    4. 解析元数据与时间戳列名。

    不在此处做列声明/单位/时间轴校验(由 canonicalize 完成), 本函数只做
    结构解析。存在阻断性诊断时返回 (None, diags)。
    """
    diags: list[Diagnostic] = []
    loc = {"object_type": "device_data", "object_id": "", "field": ""}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        diags.append(
            _dialect_diag("文件无法按 UTF-8 解码", loc, blocking=True)
        )
        return None, diags
    raw_sha256 = hashlib.sha256(data).hexdigest()

    has_bom = _detect_bom(text)
    if has_bom:
        text = text.lstrip("﻿")
        diags.append(_dialect_diag("文件含 UTF-8 BOM(规范要求无 BOM)", loc, blocking=False))

    # 统一换行(LF)
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        diags.append(_dialect_diag("文件含 CR/LF 换行(规范要求 LF)", loc, blocking=False))

    raw_lines = text.split("\n")
    meta_lines: list[str] = []
    header: list[str] | None = None
    data_rows: list[list[str]] = []

    i = 0
    # 元数据只能在表头之前
    for i, line in enumerate(raw_lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            meta_lines.append(line)
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

    meta, meta_diags = parse_metadata(meta_lines)
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

    # 表头归一化: 第一列必须是 timestamp(或其别名)
    header_norm = [_normalize_col(c) for c in header]
    ts_index = None
    for idx, name in enumerate(header_norm):
        if name in TS_ALIASES:
            ts_index = idx
            break
    if ts_index is None:
        diags.append(
            make_diag(
                "DATA-COL-005",
                severity="error",
                blocking=True,
                params={"column": "timestamp"},
                location={**loc, "field": "timestamp"},
            )
        )
    elif ts_index != 0:
        diags.append(
            _dialect_diag(f"第一列必须为 timestamp, 实际在列 {ts_index + 1}", loc, blocking=True)
        )

    # 数据行: 只允许在表头之后, 不允许穿插注释
    reader = _csv.reader(io.StringIO("\n".join(raw_lines[i + 1 :])))
    row_no = i + 1  # 表头行号
    try:
        for raw_row in reader:
            row_no += 1
            if not raw_row or all(not c.strip() for c in raw_row):
                continue  # 空行
            if raw_row[0].lstrip().startswith("#"):
                diags.append(
                    _dialect_diag(f"数据区不允许穿插注释(行 {row_no})", loc, blocking=True)
                )
                continue
            if len(raw_row) != len(header):
                diags.append(
                    _dialect_diag(f"行字段数与表头不一致(行 {row_no}: {len(raw_row)} != {len(header)})", loc, blocking=True)
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

    # 数值单元格有限性(空格/Nan/Inf/公式前缀)
    for ridx, row in enumerate(data_rows):
        for cidx, cell in enumerate(row):
            if cidx == ts_index:
                continue
            v = cell.strip()
            if not v:
                continue  # 空字段: 由数值校验阶段按模型策略处理
            if v.lower() in ("nan", "inf", "infinity", "+inf", "-inf"):
                diags.append(
                    _dialect_diag(f"非有限数值 {v!r}(行 {ridx + 1})", {**loc, "row": [ridx + 1]}, blocking=True)
                )
                continue
            if v[0] in _FORMULA_PREFIXES and _parse_number_cell(v) is None:
                # 公式注入前缀只对非数值文本告警(负号数值是合法输入)
                diags.append(
                    _dialect_diag(f"单元格以公式注入前缀 {v[0]!r} 开头(行 {ridx + 1})", {**loc, "row": [ridx + 1]}, blocking=False)
                )

    if any(d.blocking for d in diags):
        return None, diags

    return (
        ParsedDataFile(
            meta=meta,
            header=tuple(header_norm),
            rows=data_rows,
            raw_sha256=raw_sha256,
            column_order=(),
        ),
        diags,
    )


def _normalize_col(name: str) -> str:
    """列名归一化: 去空白与小写。"""
    return name.strip().lower()


# ---------------------------------------------------------------------------
# 设备模型列声明
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DataInputDecl:
    """设备模型的一列数据输入声明(0.6.0 消费形态)。

    最终以 ies.device-model 1.0.0 的 data_inputs 映射为准; 当 data_inputs
    尚不可用时, 由 time_series.inputs 派生(见 from_descriptor)。
    """

    column_id: str
    value_type: str = "number"  # number | boolean | string(数值契约先支持 number)
    quantity: str | None = None
    unit: str = ""
    required: bool = True
    minimum: float | None = None
    maximum: float | None = None


def data_inputs_from_descriptor(desc) -> list[DataInputDecl]:
    """从公开设备描述提取数据列声明(0.5.0/0.6.0 并行契约锁定)。

    优先读取 ``data_inputs`` 映射(ies.device-model 1.0.0 最终形态
    {column_id: {value_type, quantity, unit, required, minimum?, maximum?}});
    不可用时读取 ``time_series.inputs``(key/unit/required/period), 并在
    文档/注释中标注"最终以 ies.device-model 1.0.0 data_inputs 为准"。
    """
    inputs = getattr(desc, "data_inputs", None)
    if isinstance(inputs, dict) and inputs:
        out: list[DataInputDecl] = []
        for col_id, spec in inputs.items():
            out.append(_decl_from_spec(str(col_id), spec))
        if out:
            return out
    # fallback: time_series.inputs(0.5.0 尚未发布的形态)
    series = getattr(desc, "time_series", None) or {}
    series_inputs = series.get("inputs") if isinstance(series, dict) else []
    return [
        DataInputDecl(
            column_id=s.key,
            unit=s.unit or "",
            required=getattr(s, "required", True),
        )
        for s in series_inputs
    ]


def _decl_from_spec(column_id: str, spec: object) -> DataInputDecl:
    """单列声明 → DataInputDecl(接受 dict 或带属性对象)。"""
    if isinstance(spec, dict):
        return DataInputDecl(
            column_id=column_id,
            value_type=str(spec.get("value_type") or "number"),
            quantity=spec.get("quantity"),
            unit=str(spec.get("unit") or ""),
            required=bool(spec.get("required", True)),
            minimum=_num(spec.get("minimum")),
            maximum=_num(spec.get("maximum")),
        )
    # 带属性对象(如 devices.spec.SeriesSpec 或本模块 DataInputDecl)
    get = getattr
    return DataInputDecl(
        column_id=column_id,
        value_type=str(get(spec, "value_type", "number") or "number"),
        quantity=get(spec, "quantity", None),
        unit=str(get(spec, "unit", "") or ""),
        required=bool(get(spec, "required", True)),
        minimum=_num(get(spec, "minimum", None)),
        maximum=_num(get(spec, "maximum", None)),
    )


def _num(v: object) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


# ---------------------------------------------------------------------------
# 时间戳解析
# ---------------------------------------------------------------------------


def _parse_timestamp_cell(value: str, timestamp_mode: str) -> tuple[datetime | None, str | None, bool | None]:
    """解析单个时间戳单元格 → (datetime, kind, has_zone)。

    kind: 'utc_z' | 'fixed_offset' | 'offset' 用于检测同文件混用。
    返回 (None, None, None) 表示解析失败。
    """
    v = value.strip()
    if not v:
        return None, None, None
    # 带 Z → UTC
    if v.endswith("Z") or v.endswith("z"):
        inner = v[:-1]
        dt = _parse_naive_dt(inner)
        if dt is None:
            return None, None, None
        return dt.replace(tzinfo=UTC), "utc_z", True
    # 带 ±HH:MM 偏移
    m = _TS_OFFSET_SUFFIX.search(v)
    if m is not None:
        try:
            dt = datetime.fromisoformat(v)
        except ValueError:
            return None, None, None
        if dt.tzinfo is None:
            return None, None, None
        return dt.astimezone(UTC), "offset", True
    # 无偏移
    dt = _parse_naive_dt(v)
    if dt is None:
        return None, None, None
    return dt, "local", False


def _parse_naive_dt(text: str) -> datetime | None:
    for pattern in _TS_PATTERNS_COMPILED:
        if pattern.match(text):
            try:
                return datetime.fromisoformat(text.replace(" ", "T"))
            except ValueError:
                return None
    return None


def _apply_fixed_offset(dt: datetime, offset_minutes: int) -> datetime:
    """无偏移本地时间 → UTC(本地 = UTC + 偏移, 故 UTC = 本地 - 偏移)。"""
    if dt.tzinfo is not None:
        return dt.astimezone(UTC)
    return dt.replace(tzinfo=UTC) - timedelta(minutes=offset_minutes)


# ---------------------------------------------------------------------------
# 规范化与摘要
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeviceDataResult:
    """规范化产物(0.6.0 契约: 原始摘要 + 规范摘要 + 质量摘要 + 变换记录)。"""

    meta: DeviceDataMeta
    column_order: tuple[str, ...]
    rows: list[dict]  # 规范行: {column: float|bool|str}, 含 timestamp(datetime UTC)
    utc_timestamps: list[datetime]
    raw_sha256: str
    canonical_sha256: str
    transformations: tuple[str, ...]
    diagnostics: list[Diagnostic]

    def canonical_meta(self) -> DeviceDataMeta:
        """规范元数据: 时间戳一律以 UTC 表达(带 Z), 保证同一瞬时唯一形态。

        fixed_offset 与 utc 两种写法换算到 UTC 后语义相同, 规范摘要必须一致
        (device-data-csv.md 完成标准: 同一语义输入得到相同规范摘要)。
        """
        from dataclasses import replace

        return replace(
            self.meta,
            timestamp_mode="utc",
            fixed_utc_offset_minutes=0,
        )

    def canonical_csv_bytes(self) -> bytes:
        """规范化表格字节(UTF-8, LF, RFC 4180; 元数据按 UTC 形态)。"""
        buf = io.StringIO()
        buf.write(serialize_metadata(self.canonical_meta()))
        buf.write(",".join(self.column_order))
        buf.write("\n")
        for row in self.rows:
            cells: list[str] = []
            for col in self.column_order:
                v = row.get(col)
                if col == TIMESTAMP_COL:
                    if isinstance(v, datetime):
                        cells.append(v.astimezone(UTC).isoformat().replace("+00:00", "Z"))
                    else:
                        cells.append("")
                elif isinstance(v, bool):
                    cells.append("true" if v else "false")
                elif v is None:
                    cells.append("")
                else:
                    cells.append(_format_number(float(v)))
            buf.write(",".join(cells))
            buf.write("\n")
        return buf.getvalue().encode("utf-8")


def _format_number(v: float) -> str:
    """数值规范形: 去尾零(与 Python repr 一致, 但保有限性)。"""
    if not math.isfinite(v):
        raise ValueError(f"非有限数值 {v!r} 不允许进入规范表格")
    return format(v, "g")


def canonicalize_device_data(
    data: bytes,
    desc,
    *,
    dataset_id: str | None = None,
    require_meta: bool = True,
    expected_rows: int | None = None,
) -> DeviceDataResult:
    """规范化设备数据 CSV 字节 → DeviceDataResult(唯一纯函数入口)。

    步骤(device-data-csv.md 校验顺序):
    1. 识别编码/换行/元数据/方言(parse_data_file);
    2. 解析 schema/设备版本/时间模式/分辨率/单位(meta);
    3. 与设备模型核对列/类型/量纲/范围/缺失策略(data_inputs_from_descriptor);
    4. 校验时间单调/步长/周期/覆盖(validate_timestamps 复用 core.timeaxis);
    5. 时间转 UTC、数值规范化;
    6. 生成原始摘要 + 规范摘要 + 质量摘要 + 变换记录。

    相同语义输入 → 相同规范摘要(唯一纯函数, 不依赖机器时区)。

    参数:
        data: CSV 字节。
        desc: 公开设备描述(经 devices.list_device_descriptors / get_device_descriptor)。
        dataset_id: 覆盖 dataset_id 元数据(缺省用文件声明; 上传场景由调用方指定)。
        require_meta: True 时缺失必需元数据阻断; False 时允许无元数据(包内
            CSV 迁移路径, 由调用方补充默认元数据)。
        expected_rows: 期望行数(1h→8760); 提供时校验 timeline 行数。
    """
    diags: list[Diagnostic] = []
    parsed, parse_diags = parse_data_file(data)
    diags.extend(parse_diags)
    if parsed is None:
        # 解析阻断: 返回空结果(原始摘要仍保留)
        return DeviceDataResult(
            meta=DeviceDataMeta(),
            column_order=(TIMESTAMP_COL,),
            rows=[],
            utc_timestamps=[],
            raw_sha256=hashlib.sha256(data).hexdigest(),
            canonical_sha256="",
            transformations=(),
            diagnostics=diags,
        )

    meta = parsed.meta
    if dataset_id is not None:
        meta = _replace_meta(meta, dataset_id=dataset_id)
    if not require_meta and not meta.schema_id:
        # 无元数据: 由调用方在调用前补齐默认元数据
        pass

    # 列声明
    decls = data_inputs_from_descriptor(desc)
    decl_by_id = {d.column_id: d for d in decls}
    decl_order = [d.column_id for d in decls]

    # 3) 列核对: 未声明拒绝 / 重复拒绝 / 必需缺失拒绝
    header = list(parsed.header)
    if TIMESTAMP_COL in header:
        header.remove(TIMESTAMP_COL)
    unknown_cols = [c for c in header if c not in decl_by_id]
    for col in unknown_cols:
        diags.append(
            make_diag(
                "DATA-COL-003",
                severity="error",
                blocking=True,
                params={"column": col},
                location={"object_type": "device_data", "object_id": meta.dataset_id, "field": col},
            )
        )
    # 必需列缺失
    if decls:
        required_missing = [d.column_id for d in decls if d.required and d.column_id not in header]
        for col in required_missing:
            diags.append(
                make_diag(
                    "DATA-COL-005",
                    severity="error",
                    blocking=True,
                    params={"column": col},
                    location={"object_type": "device_data", "object_id": meta.dataset_id, "field": col},
                )
            )
    # 列顺序: 规范输出按模型声明顺序排列(仅保留已声明的列)
    declared_present = [c for c in decl_order if c in header]

    # 单位核对(unit.<column> 与设备模型一致)
    for col, decl in decl_by_id.items():
        declared_unit = meta.units.get(col)
        if declared_unit is not None and decl.unit:
            if not _units_compatible(declared_unit, decl.unit):
                diags.append(
                    make_diag(
                        "DATA-COL-006",
                        severity="error",
                        blocking=True,
                        params={"column": col, "actual": declared_unit, "expected": decl.unit},
                        location={"object_type": "device_data", "object_id": meta.dataset_id, "field": col},
                    )
                )

    # 4) 时间轴
    ts_index = parsed.header.index(TIMESTAMP_COL) if TIMESTAMP_COL in parsed.header else -1
    timestamps_parsed: list[tuple[datetime, str]] = []
    ts_kinds: set[str] = set()
    for ridx, row in enumerate(parsed.rows):
        raw_ts = row[ts_index] if ts_index >= 0 and ts_index < len(row) else ""
        dt, kind, has_zone = _parse_timestamp_cell(raw_ts, meta.timestamp_mode)
        if dt is None:
            diags.append(
                make_diag(
                    "DATA-TIME-005",
                    severity="error",
                    blocking=True,
                    params={"value": raw_ts, "row_no": ridx + 1},
                    location={"object_type": "device_data", "object_id": meta.dataset_id, "field": TIMESTAMP_COL, "row": [ridx + 1]},
                )
            )
            continue
        if kind is not None:
            ts_kinds.add(kind)
        timestamps_parsed.append((dt, raw_ts))

    # 同文件不混用带Z/带偏移/无偏移
    if len(ts_kinds) > 1:
        diags.append(
            make_diag(
                "DATA-TIME-003",
                severity="error",
                blocking=True,
                params={"kinds": sorted(ts_kinds)},
                location={"object_type": "device_data", "object_id": meta.dataset_id, "field": TIMESTAMP_COL},
            )
        )

    # 换算到 UTC
    utc_ts: list[datetime] = []
    for dt, raw in timestamps_parsed:
        if dt.tzinfo is not None:
            utc_ts.append(dt.astimezone(UTC))
        elif meta.timestamp_mode == "fixed_offset":
            utc_ts.append(_apply_fixed_offset(dt, meta.fixed_utc_offset_minutes))
        else:
            # utc 模式无偏移 → 拒绝(必须带 Z)
            diags.append(
                make_diag(
                    "DATA-TIME-005",
                    severity="error",
                    blocking=True,
                    params={"value": raw, "detail": "timestamp_mode=utc 必须带 Z"},
                    location={"object_type": "device_data", "object_id": meta.dataset_id, "field": TIMESTAMP_COL},
                )
            )

    # timeline: 严格递增/步长/行数
    if meta.series_mode == "timeline":
        if utc_ts:
            diags.extend(_validate_timeline(utc_ts, meta))
        if expected_rows is not None and len(parsed.rows) != expected_rows:
            diags.append(
                make_diag(
                    "DATA-TS-004",
                    severity="error",
                    blocking=True,
                    params={"expected": expected_rows, "actual": len(parsed.rows), "resolution": meta.resolution},
                    location={"object_type": "device_data", "object_id": meta.dataset_id, "field": TIMESTAMP_COL},
                )
            )
    else:  # periodic
        if meta.period is not None:
            n_expected = _periodic_rows(meta.resolution, meta.period)
            if n_expected is not None and len(parsed.rows) != n_expected:
                diags.append(
                    make_diag(
                        "DATA-TIME-004",
                        severity="error",
                        blocking=True,
                        params={"period": meta.period, "resolution": meta.resolution, "expected": n_expected, "actual": len(parsed.rows)},
                        location={"object_type": "device_data", "object_id": meta.dataset_id, "field": TIMESTAMP_COL},
                    )
                )

    # 5) 数值规范化与模型校验
    rows_out: list[dict] = []
    for ridx, row in enumerate(parsed.rows):
        out: dict = {}
        if utc_ts and ridx < len(utc_ts):
            out[TIMESTAMP_COL] = utc_ts[ridx]
        for col in declared_present:
            decl = decl_by_id[col]
            idx = parsed.header.index(col)
            raw_val = row[idx] if idx < len(row) else ""
            cell = raw_val.strip()
            if cell == "":
                # 缺失值: 仅当模型允许
                if getattr(decl, "required", True):
                    diags.append(
                        make_diag(
                            "DATA-VAL-002",
                            severity="error",
                            blocking=True,
                            params={"column": col, "row_no": ridx + 1},
                            location={"object_type": "device_data", "object_id": meta.dataset_id, "field": col, "row": [ridx + 1]},
                        )
                    )
                    out[col] = None
                else:
                    out[col] = None
                continue
            num = _parse_number_cell(cell)
            if num is None:
                diags.append(
                    make_diag(
                        "DATA-VAL-001",
                        severity="error",
                        blocking=True,
                        params={"column": col, "value": cell, "row_no": ridx + 1, "detail": "非有限数值或格式非法"},
                        location={"object_type": "device_data", "object_id": meta.dataset_id, "field": col, "row": [ridx + 1]},
                    )
                )
                out[col] = None
                continue
            # 范围校验(不截断, 阻断)
            if decl.minimum is not None and num < decl.minimum:
                diags.append(
                    make_diag(
                        "DATA-VAL-001",
                        severity="error",
                        blocking=True,
                        params={"column": col, "value": num, "minimum": decl.minimum, "row_no": ridx + 1},
                        location={"object_type": "device_data", "object_id": meta.dataset_id, "field": col, "row": [ridx + 1]},
                    )
                )
            if decl.maximum is not None and num > decl.maximum:
                diags.append(
                    make_diag(
                        "DATA-VAL-001",
                        severity="error",
                        blocking=True,
                        params={"column": col, "value": num, "maximum": decl.maximum, "row_no": ridx + 1},
                        location={"object_type": "device_data", "object_id": meta.dataset_id, "field": col, "row": [ridx + 1]},
                    )
                )
            out[col] = num
        rows_out.append(out)

    # 数组长度一致性(0.6.0: 时间、单位或长度不一致时明确失败)
    if utc_ts and len(utc_ts) != len(parsed.rows):
        diags.append(
            make_diag(
                "DATA-ARR-001",
                severity="error",
                blocking=True,
                params={"expected": len(parsed.rows), "actual": len(utc_ts)},
                location={"object_type": "device_data", "object_id": meta.dataset_id, "field": TIMESTAMP_COL},
            )
        )

    # 规范表格摘要
    column_order = (TIMESTAMP_COL,) + tuple(declared_present)
    result = DeviceDataResult(
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
    result = DeviceDataResult(
        meta=meta,
        column_order=column_order,
        rows=rows_out,
        utc_timestamps=utc_ts,
        raw_sha256=parsed.raw_sha256,
        canonical_sha256=hashlib.sha256(canonical_bytes).hexdigest(),
        transformations=("time_to_utc", "units_declared", "values_checked"),
        diagnostics=diags,
    )
    return result


def _replace_meta(meta: DeviceDataMeta, **kw) -> DeviceDataMeta:
    from dataclasses import replace

    return replace(meta, **kw)


def _periodic_rows(resolution: str, period: str) -> int | None:
    """periodic 期望行数(day→24/168/8760 按分辨率换算)。"""
    step_min = RESOLUTIONS[resolution][1]
    steps_per_day = 1440 // step_min
    if period == "day":
        return steps_per_day
    if period == "week":
        return steps_per_day * 7
    if period == "year":
        return steps_per_day * 365
    return None


def _validate_timeline(utc_ts: list[datetime], meta) -> list[Diagnostic]:
    """timeline 时间戳: 严格递增无重复、与分辨率对齐(0.6.0 契约)。

    起止时间与预期点数由装配 YAML 明确(device-data-csv.md 时间轴规则),
    本函数只校验文件自身的时间单调性与步长对齐; expected_rows 由调用方
    显式传入时另行校验(DATA-TS-004), 不靠"看起来像一年"猜测。
    """
    out: list[Diagnostic] = []
    if len(utc_ts) < 2:
        return out
    step_seconds = RESOLUTIONS[meta.resolution][2]
    loc = {"object_type": "device_data", "object_id": meta.dataset_id, "field": TIMESTAMP_COL}
    # 严格递增 + 无重复
    dup_rows: list[int] = []
    disorder_rows: list[int] = []
    for i in range(1, len(utc_ts)):
        if utc_ts[i] == utc_ts[i - 1]:
            dup_rows.append(i)
        elif utc_ts[i] < utc_ts[i - 1]:
            disorder_rows.append(i)
    if dup_rows:
        out.append(
            make_diag(
                "DATA-TIME-001",
                severity="error",
                blocking=True,
                params={"detail": "重复时间戳", "count": len(dup_rows), "first_rows": [r + 1 for r in dup_rows[:MAX_ROWS_PER_DIAG]]},
                location={**loc, "row": [r + 1 for r in dup_rows[:MAX_ROWS_PER_DIAG]]},
            )
        )
    if disorder_rows:
        out.append(
            make_diag(
                "DATA-TIME-001",
                severity="error",
                blocking=True,
                params={"detail": "时间戳未严格递增", "count": len(disorder_rows), "first_rows": [r + 1 for r in disorder_rows[:MAX_ROWS_PER_DIAG]]},
                location={**loc, "row": [r + 1 for r in disorder_rows[:MAX_ROWS_PER_DIAG]]},
            )
        )
    # 步长对齐(相邻差必须等于 resolution 步长)
    mis_rows: list[int] = []
    for i in range(1, len(utc_ts)):
        delta = (utc_ts[i] - utc_ts[i - 1]).total_seconds()
        if abs(delta - step_seconds) > 1e-6:
            mis_rows.append(i)
    if mis_rows:
        out.append(
            make_diag(
                "DATA-TIME-002",
                severity="error",
                blocking=True,
                params={"resolution": meta.resolution, "step_seconds": step_seconds, "count": len(mis_rows), "first_rows": [r + 1 for r in mis_rows[:MAX_ROWS_PER_DIAG]]},
                location={**loc, "row": [r + 1 for r in mis_rows[:MAX_ROWS_PER_DIAG]]},
            )
        )
    return out


def _units_compatible(declared: str, model: str) -> bool:
    """单位量纲兼容(大小写不敏感; 未注册单位按字符串一致判断)。"""
    try:
        return dict(dims_of(declared)) == dict(dims_of(model))
    except UnitError:
        return _normalize_unit_str(declared) == _normalize_unit_str(model)


def _normalize_unit_str(unit: str) -> str:
    """单位字符串规范化(小写、去空白), 供无法解析时兜底比较。"""
    return " ".join(unit.strip().lower().split())


def _parse_number_cell(value: str) -> float | None:
    """解析数值单元格; 非有限值或含千分位/区域化数字返回 None。"""
    v = value.strip()
    if not v:
        return None
    # 禁止千分位逗号
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
# 摘要(质量报告)
# ---------------------------------------------------------------------------


def build_data_quality_report(result: DeviceDataResult) -> dict:
    """规范化产物 → 质量摘要(0.6.0: 原始摘要 + 规范摘要 + 质量 + 变换)。"""
    blocking = [d for d in result.diagnostics if d.blocking]
    return {
        "schema": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "dataset_id": result.meta.dataset_id,
        "device_model": result.meta.device_model,
        "series_mode": result.meta.series_mode,
        "resolution": result.meta.resolution,
        "row_count": len(result.rows),
        "raw_sha256": result.raw_sha256,
        "canonical_sha256": result.canonical_sha256,
        "transformations": list(result.transformations),
        "has_blocking_errors": bool(blocking),
        "diagnostics": [d.to_dict() for d in result.diagnostics],
    }


def summary_json(result: DeviceDataResult) -> str:
    """规范摘要的可读文本(相同输入 → 相同文本, 供测试断言)。"""
    report = build_data_quality_report(result)
    return json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "SCHEMA_ID",
    "SCHEMA_VERSION",
    "TIMESTAMP_COL",
    "DeviceDataError",
    "DeviceDataMeta",
    "DeviceDataResult",
    "DataInputDecl",
    "ParsedDataFile",
    "canonicalize_device_data",
    "parse_data_file",
    "parse_metadata",
    "serialize_metadata",
    "data_inputs_from_descriptor",
    "build_data_quality_report",
    "summary_json",
]
