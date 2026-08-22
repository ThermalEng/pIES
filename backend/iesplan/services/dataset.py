"""数据集服务(U05 数据集写入单元)。

设计约束见开发者指南 domain-model.md、contracts.md 及使用者指南的数据准备章节:
- 标准 CSV 模板生成(字段说明/单位/示例, 双语注释行, REQ-DATA-002);
- 上传解析: 错误定位到文件/字段/行号;
- 校验: 行数(35040/17520/8760)、时间戳严格递增无重复、无缺失值、单位与范围、
  固定 UTC 偏移(REQ-DATA-001/8.1/8.3); 存在 blocking 诊断即拒绝提交;
- 版本化写入: 内容寻址对象 + objects/object_refs 引用 + 质量报告 + 溯源/许可证/适用范围;
- 内置样例数据: 确定性伪随机合成 365 天(与上传数据同一校验与存储路径, REQ-DATA-003)。

对象存储统一委托 services/objects.py(U11 对象域唯一写入单元):
put_object / get_object_bytes / add_object_ref 为薄封装, 本模块不直接落盘。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from iesplan.core.diagnostics import (
    DATA_COL_MISSING,
    DATA_COL_UNIT_UNKNOWN,
    PARAM_RNG_OUT,
    PARAM_UNIT_MISMATCH,
    RES_NUM_INVALID,
    RES_RANGE_OUT,
    SEVERITY_ERROR,
    Diagnostic,
    make_diag,
)
from iesplan.core.errors import AppError, ConflictError, NotFoundError
from iesplan.core.idgen import sha256_hex
from iesplan.core.timeaxis import RESOLUTIONS, TimeAxis, build_axis, validate_timestamps
from iesplan.models.dataset import Dataset, DatasetFile, DatasetVersion
from iesplan.models.identity import User
from iesplan.models.project import Project
from iesplan.storage import (
    add_ref,
    get_object,
    object_info,
)
from iesplan.storage import (
    put_object as _storage_put_object,
)
from iesplan.storage.contracts import RefInfo

#: 时间戳诊断码(04 登记 DATA-TS-001..003; 004..007 为本实现新增, 见 NEW_DIAG_CODES)
DATA_TS_ROW_COUNT = "DATA-TS-004"
DATA_TS_OUT_OF_ORDER = "DATA-TS-005"
DATA_TS_OUT_CALENDAR = "DATA-TS-006"
DATA_TS_STEP_MISALIGNED = "DATA-TS-007"

# ---------------------------------------------------------------------------
# 常量与字段规格
# ---------------------------------------------------------------------------

#: 时间戳列名(CSV 第一列)
TIMESTAMP_COL: str = "timestamp"
#: 时间戳列别名(解析时归一化到 TIMESTAMP_COL)
_TIMESTAMP_ALIASES: tuple[str, ...] = ("timestamp", "time", "datetime")
#: 分辨率 → timeline(01 §5.2 CHECK 枚举)
TIMELINE_MAP: dict[str, str] = {"15min": "quarter_hourly", "30min": "custom", "1h": "hourly"}
#: 数据对象媒体类型
DATA_MEDIA_TYPE: str = "text/csv; charset=utf-8"
METADATA_MEDIA_TYPE: str = "application/json"
#: 内置样例数据许可证
SAMPLE_LICENSE: str = "CC-BY-4.0"
#: 上传数据默认来源类别
DEFAULT_SOURCE_CATEGORY: str = "user_upload"


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """标准数据字段规格(模板说明 / 单位 / 范围 / 示例)。"""

    key: str
    name_zh: str
    name_en: str
    unit: str
    min: float | None
    max: float | None
    example: str
    description_zh: str
    description_en: str


#: 标准字段注册表(顺序即模板列顺序; 范围依据 RPD 8.3 与 17.4 数据约束)
STANDARD_FIELDS: dict[str, FieldSpec] = {
    "e_load": FieldSpec(
        key="e_load",
        name_zh="电负荷",
        name_en="Electric load",
        unit="kWh",
        min=0.0,
        max=None,
        example="125.5",
        description_zh="时段内电负荷(含空调等用电设备)",
        description_en="Electric energy load in the period",
    ),
    "h_load": FieldSpec(
        key="h_load",
        name_zh="热负荷",
        name_en="Heating load",
        unit="kWh",
        min=0.0,
        max=None,
        example="85.2",
        description_zh="时段内热负荷(采暖/生活热水)",
        description_en="Thermal (heating/DHW) load in the period",
    ),
    "c_load": FieldSpec(
        key="c_load",
        name_zh="冷负荷",
        name_en="Cooling load",
        unit="kWh",
        min=0.0,
        max=None,
        example="60.0",
        description_zh="时段内冷负荷(制冷)",
        description_en="Cooling load in the period",
    ),
    "t_ambient": FieldSpec(
        key="t_ambient",
        name_zh="环境温度",
        name_en="Ambient temperature",
        unit="°C",
        min=-40.0,
        max=60.0,
        example="25.0",
        description_zh="时段平均环境温度(℃)",
        description_en="Average ambient temperature in Celsius",
    ),
    "ghi": FieldSpec(
        key="ghi",
        name_zh="水平总辐照",
        name_en="Global horizontal irradiance",
        unit="W/m²",
        min=0.0,
        max=1500.0,
        example="620.0",
        description_zh="时段平均水平面总辐照(W/m²)",
        description_en="Average GHI in W/m²",
    ),
    "electricity_price": FieldSpec(
        key="electricity_price",
        name_zh="购电价",
        name_en="Electricity purchase price",
        unit="元/kWh",
        min=0.0,
        max=None,
        example="0.58",
        description_zh="时段购电价格(元/kWh, 分时电价)",
        description_en="Electricity purchase price in CNY/kWh (TOU)",
    ),
    "grid_emission_factor": FieldSpec(
        key="grid_emission_factor",
        name_zh="电网排放因子",
        name_en="Grid emission factor",
        unit="kgCO₂/kWh",
        min=0.0,
        max=None,
        example="0.581",
        description_zh="时段电网平均碳排放因子(kgCO₂/kWh)",
        description_en="Average grid CO₂ emission factor in kgCO₂/kWh",
    ),
}

#: 必须存在的字段(timestamp 之外至少需要电负荷)
REQUIRED_FIELDS: tuple[str, ...] = ("e_load",)
#: 必须存在的列
REQUIRED_COLUMNS: tuple[str, ...] = (TIMESTAMP_COL, *REQUIRED_FIELDS)

#: 每类诊断最多报告的行号数(避免刷屏)
_MAX_ROWS_PER_DIAG: int = 5

# ---------------------------------------------------------------------------
# 数据文件诊断码注册(RPD 8.3: 错误必须定位到文件/字段/行号)
# 新码集中在 iesplan/core/diagnostics.py 的 NEW_DIAG_CODES 声明; 并行开发阶段
# 由各业务单元在导入时登记, 保持码目录可增量扩展。
# ---------------------------------------------------------------------------


def _register_diag_codes() -> None:
    """登记本单元新增诊断码(DATA-FILE-001..004)到共享诊断目录(幂等)。"""
    from iesplan.core import diagnostics as diag_mod

    codes = {
        "DATA-FILE-001": "CSV 文件无法解码(编码或格式错误)",
        "DATA-FILE-002": "CSV 行字段数与表头不一致",
        "DATA-FILE-003": "CSV 文件为空或没有数据行",
        "DATA-FILE-004": "CSV 时间戳列解析失败",
    }
    for code, desc in codes.items():
        diag_mod.NEW_DIAG_CODES.setdefault(code, desc)
    for code, key in {
        "DATA-FILE-001": "ies.diag.data.file_decode_error",
        "DATA-FILE-002": "ies.diag.data.file_row_width",
        "DATA-FILE-003": "ies.diag.data.file_empty",
        "DATA-FILE-004": "ies.diag.data.file_ts_parse",
    }.items():
        diag_mod.DIAG_MESSAGE_KEYS.setdefault(code, key)
        diag_mod.DIAG_FIX_HINT_KEYS.setdefault(code, "ies.fix.data.csv_general")


_register_diag_codes()

# 本单元新增码别名(供本模块引用)
DATA_FILE_DECODE = "DATA-FILE-001"
DATA_FILE_ROW_WIDTH = "DATA-FILE-002"
DATA_FILE_EMPTY = "DATA-FILE-003"
DATA_FILE_TS_PARSE = "DATA-FILE-004"

# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class DataValidationError(AppError):
    """数据集校验失败(携带阻断性诊断列表, HTTP 400)。

    API 层捕获后返回 400 + 诊断明细(字段/行号定位)。
    """

    code = "DATA-VAL-001"
    severity = SEVERITY_ERROR
    message_key = "ies.error.data_validation_failed"
    http_status = 400

    def __init__(self, diagnostics: list[Diagnostic], message: str = "") -> None:
        self.diagnostics = list(diagnostics)
        super().__init__(message or f"数据集校验失败: 共 {len(self.diagnostics)} 条阻断性诊断")


# ---------------------------------------------------------------------------
# CSV 模板
# ---------------------------------------------------------------------------


def _format_ts_local(ts: datetime) -> str:
    """时间戳格式化为模板展示的本地时间(分钟精度)。"""
    return ts.strftime("%Y-%m-%d %H:%M")


def get_template(resolution: str) -> bytes:
    """生成标准 CSV 模板(REQ-DATA-002)。

    模板含:
    - 双语注释行(以 ``#`` 开头): 字段说明 / 单位 / 示例;
    - 表头行(列名);
    - 若干示例数据行。

    参数:
        resolution: '15min' | '30min' | '1h'(决定示例时间戳步长与注释)。
    返回:
        UTF-8 编码的 CSV 字节(带 BOM, 便于 Excel 打开)。
    """
    if resolution not in RESOLUTIONS:
        raise ValueError(f"非法分辨率: {resolution!r},允许值 {sorted(RESOLUTIONS)}")
    lines: list[str] = []
    lines.append("# pIES 数据集模板 / pIES dataset template")
    lines.append(f"# 分辨率 resolution: {resolution}  年步数 steps/year: {RESOLUTIONS[resolution][0]}")
    lines.append("# 时间戳为项目本地时间(无时区), 固定 UTC 偏移在上传时声明; 标准非闰年 365 天")
    lines.append("# Timestamps are project-local naive times; fixed UTC offset is declared on upload.")
    lines.append("# 请勿修改表头与列顺序 / Do not modify the header row or column order.")
    lines.append("#")
    lines.append(f"# {'列 column':<22}{'含义 description':<38}{'单位 unit':<12}{'示例 example'}")
    lines.append(f"# {'-' * 22}{'-' * 38}{'-' * 12}{'-' * 16}")
    for spec in STANDARD_FIELDS.values():
        lines.append(
            f"# {spec.key:<22}{spec.name_zh + ' / ' + spec.name_en:<38}{spec.unit:<12}{spec.example}"
        )
    lines.append("#")
    header = ",".join([TIMESTAMP_COL, *STANDARD_FIELDS.keys()])
    lines.append(header)
    # 示例行: 以本地年 1 月 1 日 00:00 为起点, 连续若干步
    step_min = RESOLUTIONS[resolution][1]
    for i in range(3):
        ts_local = _format_ts_local(datetime(2025, 1, 1) + timedelta(minutes=i * step_min))
        values = [ts_local, "125.5", "85.2", "60.0", "25.0", "620.0", "0.58", "0.581"]
        lines.append(",".join(values[: 1 + len(STANDARD_FIELDS)]))
    return ("﻿" + "\n".join(lines) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# CSV 解析(错误定位到文件/字段/行号)
# ---------------------------------------------------------------------------


def _decode_csv(data: bytes) -> tuple[str, list[Diagnostic]]:
    """按 utf-8-sig → gbk 依次尝试解码, 均失败返回诊断 DATA-FILE-001。"""
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return data.decode(encoding), []
        except UnicodeDecodeError:
            continue
    diag = make_diag(
        DATA_FILE_DECODE,
        severity=SEVERITY_ERROR,
        blocking=True,
        params={"reason": "无法以 UTF-8 或 GBK 解码"},
        location={"object_type": "dataset_file", "object_id": "", "field": ""},
    )
    return "", [diag]


def _normalize_header(name: str) -> str:
    """表头归一化: 去空白与小写。"""
    return name.strip().lower()


def _parse_timestamp_cell(value: str) -> datetime | None:
    """解析时间戳单元格(本地无时区); 失败返回 None。

    时区感知值视为 UTC 绝对时刻并转为 naive UTC。
    """
    v = value.strip()
    if not v:
        return None
    text = v.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                dt = datetime.strptime(text, "%Y/%m/%d %H:%M")
            except ValueError:
                return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _parse_number_cell(value: str) -> float | None:
    """解析数值单元格(去除千分位逗号); 失败或非有限值返回 None(M-08)。

    Python 的 float() 可接受 "nan"/"inf"/"Infinity" 等非标准常量,
    非有限值会绕过范围校验并污染下游求解, 一律视为解析失败。
    """
    v = value.strip().replace(",", "")
    if not v:
        return None
    try:
        parsed = float(v)
    except ValueError:
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def parse_csv(data: bytes, resolution: str) -> tuple[list[dict], list[Diagnostic]]:
    """解析数据集 CSV, 错误定位到文件/字段/行号(REQ-DATA-002)。

    参数:
        data: CSV 字节(UTF-8 或 GBK)。
        resolution: '15min' | '30min' | '1h'(仅用于行数期望提示, 不在此校验行数)。
    返回:
        (rows, diagnostics):
        - rows: 数据行字典列表, 键为列名; timestamp 为 datetime|None,
          数值列为 float|None(解析失败为 None, 对应诊断已给出)。
        - diagnostics: 解析期诊断(文件/列/行级); 存在 blocking 时不可继续提交。
    异常:
        ValueError: 非法分辨率。
    """
    if resolution not in RESOLUTIONS:
        raise ValueError(f"非法分辨率: {resolution!r},允许值 {sorted(RESOLUTIONS)}")
    loc_file = {"object_type": "dataset_file", "object_id": "", "field": ""}
    diags: list[Diagnostic] = []

    text, decode_diags = _decode_csv(data)
    diags.extend(decode_diags)
    if decode_diags:
        return [], diags

    reader = csv.reader(io.StringIO(text))
    header: list[str] | None = None
    col_index: dict[str, int] = {}
    unknown_cols: list[str] = []
    rows: list[dict] = []
    row_no = 0
    try:
        for row_no, raw_row in enumerate(reader, start=1):
            if not raw_row or all(not cell.strip() for cell in raw_row):
                continue  # 空行
            if raw_row[0].lstrip().startswith("#"):
                continue  # 注释行(模板说明)
            if header is None:
                header = [_normalize_header(c) for c in raw_row]
                col_index = {}
                for idx, name in enumerate(header):
                    if name in _TIMESTAMP_ALIASES:
                        col_index[TIMESTAMP_COL] = idx
                    else:
                        col_index.setdefault(name, idx)  # 重复列保留首个
                unknown_cols = [c for c in header if c not in _TIMESTAMP_ALIASES and c not in STANDARD_FIELDS]
                for name in unknown_cols:
                    diags.append(
                        make_diag(
                            DATA_COL_UNIT_UNKNOWN,
                            severity="warning",
                            params={"column": name, "hint": "该列将忽略; 请使用标准列名"},
                            location={**loc_file, "field": name, "row": [1]},
                            ref_ids=["help.import.csv_general"],
                        )
                    )
                for name in (TIMESTAMP_COL, *REQUIRED_FIELDS):
                    if name not in col_index:
                        diags.append(
                            make_diag(
                                DATA_COL_MISSING,
                                severity=SEVERITY_ERROR,
                                blocking=True,
                                params={"column": name},
                                location={**loc_file, "field": name, "row": [1]},
                                ref_ids=["help.import.csv_general"],
                            )
                        )
                if not header:
                    break
                continue
            # 数据行: 字段数须与表头一致
            if len(raw_row) != len(header):
                diags.append(
                    make_diag(
                        DATA_FILE_ROW_WIDTH,
                        severity=SEVERITY_ERROR,
                        blocking=True,
                        params={
                            "expected": len(header),
                            "actual": len(raw_row),
                            "row_no": row_no,
                        },
                        location={**loc_file, "row": [row_no]},
                    )
                )
                continue
            if TIMESTAMP_COL not in col_index:
                # 表头缺时间戳列: DATA-COL-001 已给出, 数据行无从定位
                continue
            row: dict = {}
            ts_raw = raw_row[col_index[TIMESTAMP_COL]]
            ts = _parse_timestamp_cell(ts_raw)
            row[TIMESTAMP_COL] = ts
            if ts is None and ts_raw.strip():
                diags.append(
                    make_diag(
                        DATA_FILE_TS_PARSE,
                        severity=SEVERITY_ERROR,
                        blocking=True,
                        params={"value": ts_raw.strip(), "row_no": row_no},
                        location={**loc_file, "field": TIMESTAMP_COL, "row": [row_no]},
                    )
                )
            for name in STANDARD_FIELDS:
                if name not in col_index:
                    row[name] = None
                    continue
                raw = raw_row[col_index[name]]
                num = _parse_number_cell(raw)
                row[name] = num
                if num is None and raw.strip():
                    diags.append(
                        make_diag(
                            RES_NUM_INVALID,
                            severity=SEVERITY_ERROR,
                            blocking=True,
                            params={"value": raw.strip(), "row_no": row_no},
                            location={**loc_file, "field": name, "row": [row_no]},
                        )
                    )
            rows.append(row)
    except csv.Error as exc:
        diags.append(
            make_diag(
                DATA_FILE_DECODE,
                severity=SEVERITY_ERROR,
                blocking=True,
                params={"reason": f"CSV 结构错误: {exc}"},
                location={**loc_file, "row": [row_no]},
            )
        )
        return [], diags

    if header is None or not rows:
        diags.append(
            make_diag(
                DATA_FILE_EMPTY,
                severity=SEVERITY_ERROR,
                blocking=True,
                params={"resolution": resolution, "expected": RESOLUTIONS[resolution][0]},
                location=loc_file,
            )
        )
    return rows, diags


# ---------------------------------------------------------------------------
# 校验(返回 TimeAxis / 归一化 DataFrame / 诊断)
# ---------------------------------------------------------------------------


def _upgrade_blocking(diags: list[Diagnostic]) -> list[Diagnostic]:
    """将 error 严重度诊断升级为阻断(数据集提交场景: 数据错误必须修复后提交)。"""
    out: list[Diagnostic] = []
    for d in diags:
        if d.severity == SEVERITY_ERROR and not d.blocking:
            out.append(replace(d, blocking=True))
        else:
            out.append(d)
    return out


def _field_location(field: str, rows: list[int] | None = None) -> dict:
    """构造字段定位字典。"""
    loc = {"object_type": "time_series", "object_id": "", "field": field}
    if rows:
        loc["row"] = rows[:_MAX_ROWS_PER_DIAG]
    return loc


def validate_dataset(
    df: pd.DataFrame,
    resolution: str,
    utc_offset_minutes: int,
) -> tuple[TimeAxis, pd.DataFrame, list[Diagnostic]]:
    """校验并归一化数据集(RPD 8.3 / 17.4)。

    检查项:
    1. 必需列(timestamp + e_load)存在;
    2. 行数匹配分辨率期望(35040/17520/8760)与时间戳有效性(严格递增、无重复、无闰日、
       不越界、步长对齐)——复用 core.timeaxis.validate_timestamps;
    3. 无缺失值/非有限值;
    4. 数值在允许范围内(负荷≥0、温度 -40..60°C、GHI 0..1500 W/m²、电价≥0、排放因子≥0);
    5. UTC 偏移在 [-720, 840] 分钟内(固定偏移, 无夏令时)。

    参数:
        df: 输入 DataFrame; timestamp 列为 datetime(naive 视为本地时间,
            aware 视为 UTC 绝对时刻并转 naive), 数值列为 float。
        resolution: '15min' | '30min' | '1h'。
        utc_offset_minutes: 数据集固定 UTC 偏移(分钟)。
    返回:
        (axis, normalized_df, diagnostics):
        - axis: 构建的时间轴;
        - normalized_df: 归一化后数据(时间戳转为 UTC aware datetime, 数值列 float);
        - diagnostics: 校验诊断(error 级均已置 blocking=True)。
    异常:
        ValueError: 非法分辨率。
    """
    if resolution not in RESOLUTIONS:
        raise ValueError(f"非法分辨率: {resolution!r},允许值 {sorted(RESOLUTIONS)}")
    # 偏移越界: 给出阻断诊断, 归一化按夹紧后的偏移继续
    offset = utc_offset_minutes
    diags: list[Diagnostic] = []
    if not isinstance(offset, int) or not (-720 <= offset <= 840):
        diags.append(
            make_diag(
                PARAM_RNG_OUT,
                severity=SEVERITY_ERROR,
                blocking=True,
                params={"field": "utc_offset_minutes", "value": offset, "min": -720, "max": 840},
                location=_field_location("utc_offset_minutes"),
            )
        )
        offset = min(max(int(offset), -720), 840)
    axis = build_axis(resolution, offset)

    # 1) 必需列
    for col in REQUIRED_COLUMNS:
        if col not in df.columns:
            diags.append(
                make_diag(
                    DATA_COL_MISSING,
                    severity=SEVERITY_ERROR,
                    blocking=True,
                    params={"column": col},
                    location=_field_location(col),
                    ref_ids=["help.import.csv_general"],
                )
            )

    # 2) 时间戳: 解析 + 时间轴校验
    if TIMESTAMP_COL in df.columns:
        ts_series = pd.to_datetime(df[TIMESTAMP_COL], errors="coerce")
    else:
        ts_series = pd.Series(dtype="object")
    ts_list: list[datetime] = []
    for idx, ts in enumerate(ts_series.tolist()):
        if ts is None or pd.isna(ts):
            diags.append(
                make_diag(
                    RES_NUM_INVALID,
                    severity=SEVERITY_ERROR,
                    blocking=True,
                    params={"field": TIMESTAMP_COL, "row_no": idx + 1},
                    location=_field_location(TIMESTAMP_COL, [idx + 1]),
                )
            )
            continue
        if ts.tzinfo is not None:
            ts_list.append(ts.astimezone(UTC).replace(tzinfo=None))
        else:
            ts_list.append(ts)
    diags.extend(_upgrade_blocking(validate_timestamps(ts_list, resolution)))

    # 3) 缺失值/非有限值 + 4) 范围(逐标准字段, 仅检查数据中存在的列)
    for col in STANDARD_FIELDS:
        if col not in df.columns:
            continue
        values = pd.to_numeric(df[col], errors="coerce").to_numpy(dtype=np.float64)
        finite_mask = np.isfinite(values)
        if not finite_mask.all():
            bad_rows = [int(i) + 1 for i in np.flatnonzero(~finite_mask)]
            diags.append(
                make_diag(
                    RES_NUM_INVALID,
                    severity=SEVERITY_ERROR,
                    blocking=True,
                    params={"field": col, "count": len(bad_rows), "first_rows": bad_rows[:5]},
                    location=_field_location(col, bad_rows),
                )
            )
        spec = STANDARD_FIELDS[col]
        if spec.min is not None or spec.max is not None:
            lo = spec.min if spec.min is not None else -np.inf
            hi = spec.max if spec.max is not None else np.inf
            in_range = finite_mask & (values >= lo) & (values <= hi)
            if not in_range.all():
                # 越界明细只含有限值: 缺失/NaN 已由 RES-NUM-001 单独覆盖,
                # 不混入 first_values, 避免诊断序列化失败(JSON 不支持 NaN)
                bad_idx = np.flatnonzero(~in_range & finite_mask)
                bad_rows = [int(i) + 1 for i in np.flatnonzero(~in_range)]
                bad_values = [float(values[i]) for i in bad_idx[:5]]
                diags.append(
                    make_diag(
                        RES_RANGE_OUT,
                        severity=SEVERITY_ERROR,
                        blocking=True,
                        params={
                            "field": col,
                            "min": spec.min,
                            "max": spec.max,
                            "unit": spec.unit,
                            "count": len(bad_rows),
                            "first_rows": bad_rows[:5],
                            "first_values": bad_values,
                        },
                        location=_field_location(col, bad_rows),
                    )
                )

    # 归一化: 本地时间 → UTC(UTC = 本地 - 偏移), 输出 aware UTC 时间戳
    normalized = df.copy()
    if TIMESTAMP_COL in normalized.columns:
        normalized = normalized.dropna(subset=[TIMESTAMP_COL])
        utc_stamps = [
            t - timedelta(minutes=offset) if t.tzinfo is None else t.astimezone(UTC)
            for t in pd.to_datetime(normalized[TIMESTAMP_COL], errors="coerce").tolist()
            if t is not None and not pd.isna(t)
        ]
        normalized[TIMESTAMP_COL] = [
            t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC) for t in utc_stamps
        ]
    for col in STANDARD_FIELDS:
        if col in normalized.columns:
            normalized[col] = pd.to_numeric(normalized[col], errors="coerce")
    normalized = normalized.reset_index(drop=True)
    return axis, normalized, diags


# ---------------------------------------------------------------------------
# 质量报告
# ---------------------------------------------------------------------------


def build_quality_report(
    axis: TimeAxis,
    df: pd.DataFrame,
    diags: list[Diagnostic],
    content_hash: str,
) -> dict:
    """生成版本质量报告(01 §5.2 quality_report)。"""
    missing_by_field: dict[str, int] = {}
    range_by_field: dict[str, int] = {}
    ts_diags: list[str] = []
    for d in diags:
        field = (d.location or {}).get("field") or ""
        if d.code == RES_NUM_INVALID and field in STANDARD_FIELDS:
            missing_by_field[field] = missing_by_field.get(field, 0) + 1
        elif d.code == RES_RANGE_OUT and field in STANDARD_FIELDS:
            range_by_field[field] = range_by_field.get(field, 0) + 1
        if d.code.startswith("DATA-TS-"):
            ts_diags.append(d.code)
    missing_total = sum(missing_by_field.values())
    range_total = sum(range_by_field.values())
    return {
        "tool": "iesplan.services.dataset",
        "generated_at": datetime.now(UTC).isoformat(),
        "resolution": axis.resolution,
        "timeline": TIMELINE_MAP[axis.resolution],
        "row_count": axis.n,
        "fixed_utc_offset_minutes": axis.utc_offset_minutes,
        "content_hash": content_hash,
        "checks": {
            "row_count": {"expected": axis.n, "actual": axis.n, "ok": True},
            "timestamps": {
                "ok": not any(d.blocking for d in diags if d.code.startswith("DATA-TS-")),
                "codes": ts_diags,
            },
            "missing_values": {
                "ok": missing_total == 0,
                "total": missing_total,
                "by_field": missing_by_field,
            },
            "ranges": {"ok": range_total == 0, "total": range_total, "by_field": range_by_field},
            "units": {"ok": True},
        },
        "diagnostics": [d.to_dict() for d in diags],
        "has_blocking_errors": any(d.blocking for d in diags),
    }


# ---------------------------------------------------------------------------
# 对象存储(薄封装, 统一委托 services/objects.py, U11 唯一写入单元)
# ---------------------------------------------------------------------------


def put_object(
    db: Session,
    data: bytes,
    media_type: str,
    *,
    source_category: str = "dataset",
) -> dict:
    """写入内容寻址对象(01 §10.1, 委托 services/objects.py)。

    U11 对象服务负责临时区写入→原子 rename 提交→objects 记录→审计与存储门禁;
    本模块只做参数适配。数据集单元的文件引用在 DatasetFile 行建立后由
    add_object_ref 补充(对象先落盘, 后建业务引用, 符合 RPD 23.1)。
    """
    return _storage_put_object(db, data, media_type, source_category=source_category)


def get_object_bytes(db: Session, object_id: int) -> bytes:
    """读取对象字节并校验完整性(STO-05: 按公开对象 ID, 委托 storage 门面)。"""
    return get_object(db, object_id)


def add_object_ref(
    db: Session,
    obj: object,
    ref_type: str,
    ref_entity_type: str,
    ref_entity_id: int,
    purpose: str | None = None,
) -> RefInfo | None:
    """建立对象引用(01 §10.2, STO-05: 接受 ObjectHandle 或元数据 dict,
    委托门面)。"""
    object_id = obj["id"] if isinstance(obj, dict) else obj.id
    return add_ref(
        db, object_id, ref_type, ref_entity_id, ref_entity_type=ref_entity_type, purpose=purpose
    )


# ---------------------------------------------------------------------------
# 数据集 / 版本服务
# ---------------------------------------------------------------------------


def default_user(db: Session) -> User:
    """返回系统操作者(admin); 不存在则创建(认证接入前的占位实现)。"""
    user = db.execute(
        sa.select(User).where(User.username == "admin", User.status == "active").limit(1)
    ).scalar_one_or_none()
    if user is None:
        user = User(username="admin", display_name="系统管理员", is_system=True)
        db.add(user)
        db.flush()
    return user


def create_dataset(
    db: Session,
    project_id: int,
    name: str,
    source_category: str | None = None,
    license: str | None = None,
    provenance: dict | None = None,
    *,
    user_id: int | None = None,
    description: str | None = None,
) -> Dataset:
    """创建数据集元数据(01 §5.1)。

    注: source_category/provenance 按数据模型归属版本(5.2 列), 此处接受参数
    仅为接口完整与默认值透传; 实际入库发生在版本上传(见 upload_dataset_version)。
    """
    if db.execute(sa.select(Project.id).where(Project.id == project_id)).first() is None:
        raise NotFoundError(params={"entity_type": "project", "entity_id": project_id})
    actor_id = user_id if user_id is not None else default_user(db).id
    ds = Dataset(
        project_id=project_id,
        name=name,
        description=description,
        status="draft",
        default_license=license,
        created_by=actor_id,
    )
    db.add(ds)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(
            message_key="ies.error.duplicate_name",
            params={"entity_type": "dataset", "name": name, "project_id": project_id},
        ) from exc
    # 透传默认溯源(版本创建时若未显式给出则继承)
    if provenance:
        ds.source_category = source_category
        ds.default_provenance = dict(provenance)
    return ds


def _build_fields_info(
    df: pd.DataFrame,
    declared: dict | None,
) -> tuple[dict, dict, list[Diagnostic]]:
    """合并字段定义与单位(01 §5.2 fields/units), 校验声明单位一致性。"""
    declared = dict(declared or {})
    fields: dict = {
        TIMESTAMP_COL: {
            "type": "datetime",
            "unit": "ISO8601",
            "description_zh": "时间戳(项目本地时间)",
            "description_en": "Timestamp (project local time)",
        }
    }
    units: dict = {TIMESTAMP_COL: "ISO8601"}
    diags: list[Diagnostic] = []
    for col in df.columns:
        if col == TIMESTAMP_COL:
            continue
        spec = STANDARD_FIELDS.get(col)
        decl = declared.get(col)
        unit = ""
        if isinstance(decl, dict):
            unit = str(decl.get("unit", "")).strip()
        elif isinstance(decl, str):
            unit = decl.strip()
        if spec is not None:
            if unit and unit.lower() != spec.unit.lower():
                diags.append(
                    make_diag(
                        PARAM_UNIT_MISMATCH,
                        severity=SEVERITY_ERROR,
                        blocking=True,
                        params={"field": col, "expected": spec.unit, "actual": unit},
                        location=_field_location(col),
                    )
                )
            unit = unit or spec.unit
            fields[col] = {
                "type": "float",
                "unit": unit,
                "description_zh": spec.name_zh,
                "description_en": spec.name_en,
            }
        else:
            if not unit:
                diags.append(
                    make_diag(
                        DATA_COL_UNIT_UNKNOWN,
                        severity="warning",
                        params={"column": col, "hint": "非标准字段, 请补充单位声明"},
                        location=_field_location(col),
                    )
                )
            unit = unit or "unknown"
            fields[col] = {"type": "float", "unit": unit, "description_zh": col, "description_en": col}
        units[col] = unit
    return fields, units, diags


def _normalized_to_csv_bytes(df: pd.DataFrame) -> bytes:
    """归一化 DataFrame → 规范 CSV 字节(表头 + UTC ISO 时间戳)。"""
    out = df.copy()
    ts_col = out[TIMESTAMP_COL]
    out[TIMESTAMP_COL] = [ts.isoformat() for ts in ts_col.tolist()]
    cols = [TIMESTAMP_COL]
    cols += [c for c in STANDARD_FIELDS if c in out.columns]
    cols += [c for c in out.columns if c not in STANDARD_FIELDS and c != TIMESTAMP_COL]
    out = out[cols]
    return out.to_csv(index=False, lineterminator="\n").encode("utf-8")


def _commit_version(
    db: Session,
    dataset: Dataset,
    axis: TimeAxis,
    normalized_df: pd.DataFrame,
    diags: list[Diagnostic],
    declared_fields: dict | None,
    meta: dict,
    actor_id: int | None = None,
) -> DatasetVersion:
    """校验通过后执行版本写入(对象 + 版本行 + 文件行 + 引用, 单事务)。"""
    fields_info, units, unit_diags = _build_fields_info(normalized_df, declared_fields)
    all_diags = list(diags) + unit_diags
    if any(d.blocking for d in all_diags):
        raise DataValidationError(all_diags)

    canonical_csv = _normalized_to_csv_bytes(normalized_df)
    content_hash = sha256_hex(canonical_csv)
    quality_report = build_quality_report(axis, normalized_df, all_diags, content_hash)

    version_no = 1
    last = db.execute(
        sa.select(DatasetVersion.version_no)
        .where(DatasetVersion.dataset_id == dataset.id)
        .order_by(DatasetVersion.version_no.desc())
        .limit(1)
    ).scalar_one_or_none()
    if last is not None:
        version_no = last + 1

    source_category = (
        meta.get("source_category") or getattr(dataset, "source_category", None) or DEFAULT_SOURCE_CATEGORY
    )
    provenance = dict(meta.get("provenance") or {})
    provenance.setdefault("source_category", source_category)
    license = meta.get("license") or dataset.default_license
    created_reason = meta.get("created_reason") or "upload"

    version = DatasetVersion(
        dataset_id=dataset.id,
        version_no=version_no,
        timeline=TIMELINE_MAP[axis.resolution],
        resolution=axis.resolution,
        fixed_utc_offset_minutes=axis.utc_offset_minutes,
        fields=fields_info,
        units=units,
        quality_report=quality_report,
        provenance=provenance,
        license=license,
        content_hash=content_hash,
        created_by=actor_id if actor_id is not None else default_user(db).id,
        created_reason=created_reason,
    )
    db.add(version)
    db.flush()

    obj_data = put_object(db, canonical_csv, DATA_MEDIA_TYPE, source_category=source_category)
    metadata_json = json.dumps(
        {
            "dataset_id": dataset.id,
            "dataset_version_id": version.id,
            "version_no": version_no,
            "resolution": axis.resolution,
            "timeline": TIMELINE_MAP[axis.resolution],
            "fixed_utc_offset_minutes": axis.utc_offset_minutes,
            "row_count": axis.n,
            "fields": fields_info,
            "units": units,
            "content_hash": content_hash,
            "generated_at": datetime.now(UTC).isoformat(),
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    obj_meta = put_object(db, metadata_json, METADATA_MEDIA_TYPE, source_category=source_category)

    file_data = DatasetFile(
        dataset_version_id=version.id,
        object_id=obj_data.id,
        file_kind="data",
        format="csv",
        row_count=axis.n,
        size_bytes=len(canonical_csv),
    )
    file_meta = DatasetFile(
        dataset_version_id=version.id,
        object_id=obj_meta.id,
        file_kind="metadata",
        format="json",
        row_count=0,
        size_bytes=len(metadata_json),
    )
    db.add_all([file_data, file_meta])
    db.flush()
    add_object_ref(
        db, {"id": obj_data.id}, "dataset_file", "dataset_files", file_data.id,
        purpose="数据集版本数据本体",
    )
    add_object_ref(
        db, {"id": obj_meta.id}, "dataset_file", "dataset_files", file_meta.id,
        purpose="数据集版本元数据",
    )
    db.commit()
    return version


def upload_dataset_version(
    db: Session,
    dataset_id: int,
    resolution: str,
    utc_offset_minutes: int,
    fields: dict,
    data_bytes: bytes,
    meta: dict,
    *,
    user_id: int | None = None,
) -> DatasetVersion:
    """上传并校验数据集版本(RPD 8.3 / REQ-DATA-002)。

    流程: 解析 CSV → 校验(行数/时间戳/缺失/范围/偏移) → 有阻断诊断即抛
    DataValidationError → 内容寻址落盘 + 版本/文件/引用入库 + 质量报告。

    参数:
        db: 数据库会话。
        dataset_id: 数据集 id。
        resolution: '15min' | '30min' | '1h'。
        utc_offset_minutes: 固定 UTC 偏移(分钟)。
        fields: 字段描述 {"e_load": {"unit": "kWh", ...}}(可为空字典自动推断)。
        data_bytes: CSV 数据字节。
        meta: 元信息 {source_category, license, provenance, created_reason}。
    返回:
        新建的 DatasetVersion(已提交)。
    异常:
        DataValidationError: 存在阻断性诊断(携带诊断明细)。
        NotFoundError: 数据集不存在。
        ConflictError: 数据集已 deprecated, 禁止新建版本。
    """
    dataset = db.execute(sa.select(Dataset).where(Dataset.id == dataset_id)).scalar_one_or_none()
    if dataset is None:
        raise NotFoundError(params={"entity_type": "dataset", "entity_id": dataset_id})
    if dataset.status == "deprecated":
        raise ConflictError(
            message_key="ies.error.dataset_deprecated",
            params={"dataset_id": dataset_id},
        )

    rows, parse_diags = parse_csv(data_bytes, resolution)
    if any(d.blocking for d in parse_diags):
        raise DataValidationError(parse_diags)
    df = pd.DataFrame(rows)
    axis, normalized, validate_diags = validate_dataset(df, resolution, utc_offset_minutes)
    all_diags = parse_diags + validate_diags
    if any(d.blocking for d in all_diags):
        raise DataValidationError(all_diags)
    return _commit_version(db, dataset, axis, normalized, all_diags, fields, meta, actor_id=user_id)


def get_dataset(db: Session, dataset_id: int) -> Dataset | None:
    """按 id 获取数据集(不存在返回 None)。"""
    return db.execute(sa.select(Dataset).where(Dataset.id == dataset_id)).scalar_one_or_none()


def require_project(db: Session, project_id: int) -> None:
    """校验项目存在, 否则 NotFoundError。"""
    if db.execute(sa.select(Project.id).where(Project.id == project_id)).first() is None:
        raise NotFoundError(params={"entity_type": "project", "entity_id": project_id})


def version_files_summary(db: Session, version_id: int) -> list[dict]:
    """版本文件摘要(不含对象内容)。"""
    files: list[dict] = []
    for f in db.execute(sa.select(DatasetFile).where(DatasetFile.dataset_version_id == version_id)).scalars():
        files.append(
            {
                "file_kind": f.file_kind,
                "format": f.format,
                "row_count": f.row_count,
                "size_bytes": f.size_bytes,
            }
        )
    return files


def list_dataset_versions(db: Session, dataset_id: int) -> list[DatasetVersion]:
    """数据集版本列表(新版本在前)。"""
    if db.execute(sa.select(Dataset.id).where(Dataset.id == dataset_id)).first() is None:
        raise NotFoundError(params={"entity_type": "dataset", "entity_id": dataset_id})
    return list(
        db.execute(
            sa.select(DatasetVersion)
            .where(DatasetVersion.dataset_id == dataset_id)
            .order_by(DatasetVersion.version_no.desc())
        ).scalars()
    )


def get_dataset_version(
    db: Session,
    dataset_id: int,
    version_no: int | None = None,
) -> dict:
    """获取版本及其数据引用(01 §5.2/5.3)。

    参数:
        dataset_id: 数据集 id。
        version_no: 版本号; None 取最新版本。
    返回:
        {"version": DatasetVersion, "files": [{file_kind, format, row_count,
        size_bytes, sha256, media_type}], "data": 汇总引用}。
    """
    stmt = sa.select(DatasetVersion).where(DatasetVersion.dataset_id == dataset_id)
    if version_no is None:
        stmt = stmt.order_by(DatasetVersion.version_no.desc()).limit(1)
    else:
        stmt = stmt.where(DatasetVersion.version_no == version_no)
    version = db.execute(stmt).scalars().first()
    if version is None:
        raise NotFoundError(
            params={"entity_type": "dataset_version", "dataset_id": dataset_id, "version_no": version_no}
        )
    files: list[dict] = []
    for f in db.execute(sa.select(DatasetFile).where(DatasetFile.dataset_version_id == version.id)).scalars():
        try:
            obj = object_info(db, f.object_id)
        except NotFoundError:
            obj = None
        files.append(
            {
                "id": f.id,
                "file_kind": f.file_kind,
                "format": f.format,
                "row_count": f.row_count,
                "size_bytes": f.size_bytes,
                "sha256": obj["sha256"] if obj else None,
                "media_type": obj["media_type"] if obj else None,
            }
        )
    data_file = next((f for f in files if f["file_kind"] == "data"), None)
    return {
        "version": version,
        "files": files,
        "data": {
            "content_hash": version.content_hash,
            **(
                {}
                if data_file is None
                else {"row_count": data_file["row_count"], "size_bytes": data_file["size_bytes"]}
            ),
        },
    }


def list_datasets_with_latest(db: Session, project_id: int) -> list[dict]:
    """项目数据集列表, 附带最新版本摘要。"""
    datasets = list(
        db.execute(
            sa.select(Dataset).where(Dataset.project_id == project_id).order_by(Dataset.created_at)
        ).scalars()
    )
    if not datasets:
        return []
    latest_by_id: dict[int, DatasetVersion] = {}
    versions = db.execute(
        sa.select(DatasetVersion)
        .where(DatasetVersion.dataset_id.in_([d.id for d in datasets]))
        .order_by(DatasetVersion.dataset_id, DatasetVersion.version_no.desc())
    ).scalars()
    for v in versions:
        latest_by_id.setdefault(v.dataset_id, v)
    out: list[dict] = []
    for ds in datasets:
        out.append({"dataset": ds, "latest_version": latest_by_id.get(ds.id)})
    return out


# ---------------------------------------------------------------------------
# 内置样例数据(REQ-DATA-003)
# ---------------------------------------------------------------------------


def _sample_params(region: str) -> dict:
    """内置样例区域参数(未知区域回退上海)。"""
    table: dict[str, dict] = {
        "shanghai": {
            "t_base": 17.0,
            "t_amp": 11.0,
            "ghi_summer": 1000.0,
            "ghi_winter": 550.0,
            "name": "上海",
        },
        "beijing": {"t_base": 12.5, "t_amp": 15.0, "ghi_summer": 1100.0, "ghi_winter": 420.0, "name": "北京"},
        "guangzhou": {
            "t_base": 22.0,
            "t_amp": 8.0,
            "ghi_summer": 1050.0,
            "ghi_winter": 700.0,
            "name": "广州",
        },
    }
    return table.get(region, table["shanghai"])


def _generate_sample_rows(axis: TimeAxis, region: str, rng: np.random.Generator) -> list[dict]:
    """确定性生成 365 天合成数据(季节 + 日模式, RPD 8.2/REQ-DATA-004)。

    含: 电/热/冷负荷、环境温度、水平总辐照、分时电价、电网排放因子。
    全部计算只依赖 (hour_of_year, day_of_year, season) 与固定顺序的 rng 采样,
    同一种子结果完全一致。
    """
    p = _sample_params(region)
    n = axis.n
    hour = axis.hour_of_year % 24
    doy = axis.day_of_year
    season = axis.season
    weekday = (doy % 7) < 5  # 周一至周五
    wd = np.where(weekday, 1.08, 0.86)

    # 环境温度: 年周期 + 日周期 + 噪声
    t = (
        p["t_base"]
        - p["t_amp"] * np.cos(2 * np.pi * (doy - 15) / 365)
        + 3.2 * np.sin(2 * np.pi * (hour - 14) / 24)
        + rng.normal(0.0, 1.2, n)
    )
    # 水平总辐照: 日出日落包络 + 云系数(仅白天非零)
    daylight = np.maximum(0.0, np.sin(np.pi * np.clip((hour - 6.0) / 12.0, 0.0, 1.0))) ** 1.2
    gmax = np.where(
        season == 2,
        p["ghi_summer"],
        np.where(
            season == 3,
            p["ghi_summer"] * 0.85,
            np.where(season == 1, p["ghi_summer"] * 0.9, p["ghi_winter"]),
        ),
    )
    ghi = np.maximum(0.0, gmax * daylight * (0.75 + 0.25 * rng.uniform(size=n)))
    # 电负荷: 早晚双峰 + 季节(夏高冬中) + 周内差异
    hour_curve = (
        1.15 * np.exp(-((hour - 19.0) ** 2) / (2 * 3.5**2))
        + 0.90 * np.exp(-((hour - 9.0) ** 2) / (2 * 2.5**2))
        + 0.55 * np.exp(-((hour - 23.0) ** 2) / 8.0)
        + 0.45 * np.exp(-((hour - 4.0) ** 2) / 6.0)
    )
    season_elec = np.where(season == 2, 1.22, np.where(season == 0, 1.05, 0.90))
    e_load = np.maximum(0.0, 780.0 * wd * season_elec * hour_curve * (0.96 + 0.08 * rng.uniform(size=n)))
    # 热负荷: 冬季主导, 早晚高峰
    winter_w = np.clip(np.cos(2 * np.pi * (doy - 15) / 365) + 0.35, 0.0, 1.3)
    heat_curve = 0.75 + 0.6 * np.exp(-((hour - 21.0) ** 2) / 10.0) + 0.5 * np.exp(-((hour - 7.0) ** 2) / 8.0)
    h_load = np.maximum(0.0, 520.0 * winter_w * heat_curve * wd * (0.95 + 0.1 * rng.uniform(size=n)))
    # 冷负荷: 夏季主导, 午后峰值
    summer_w = np.clip(np.cos(2 * np.pi * (doy - 205) / 365) + 0.4, 0.0, 1.3)
    cool_curve = 0.5 + 0.9 * np.exp(-((hour - 15.0) ** 2) / (2 * 3.0**2))
    c_load = np.maximum(0.0, 460.0 * summer_w * cool_curve * wd * (0.95 + 0.1 * rng.uniform(size=n)))
    # 分时电价: 峰(10-12/18-21) 谷(23-7) 平; 夏季上浮
    peak = ((hour >= 10) & (hour < 12)) | ((hour >= 18) & (hour < 21))
    valley = (hour >= 23) | (hour < 7)
    price = np.where(valley, 0.30, np.where(peak, 0.95, 0.60))
    price = price * np.where(season == 2, 1.06, 1.0) * (0.99 + 0.02 * rng.uniform(size=n))
    # 电网排放因子: 基准 0.581 kgCO₂/kWh, 年周期微调
    emis = 0.581 * (1 + 0.04 * np.cos(2 * np.pi * (doy - 15) / 365))
    emis = np.maximum(0.4, emis * (0.99 + 0.02 * rng.uniform(size=n)))

    rows: list[dict] = []
    for i in range(n):
        local = axis.timestamp(i) + timedelta(minutes=axis.utc_offset_minutes)
        rows.append(
            {
                TIMESTAMP_COL: local.replace(tzinfo=None),
                "e_load": float(e_load[i]),
                "h_load": float(h_load[i]),
                "c_load": float(c_load[i]),
                "t_ambient": float(t[i]),
                "ghi": float(ghi[i]),
                "electricity_price": float(price[i]),
                "grid_emission_factor": float(emis[i]),
            }
        )
    return rows


def _sample_seed(region: str, resolution: str) -> int:
    """样例种子: 由 (region, resolution) 确定性派生(结果可复现, 种子进入快照)。"""
    return int(hashlib.sha256(f"iesplan:builtin_sample:{region}:{resolution}".encode()).hexdigest()[:16], 16)


def _get_or_create_sample_dataset(
    db: Session, project_id: int, region: str, resolution: str, dataset_id: int | None = None
) -> Dataset:
    """查找或创建样例数据集(按项目内唯一名称; 指定 dataset_id 时直接复用)。"""
    if dataset_id is not None:
        ds = db.execute(sa.select(Dataset).where(Dataset.id == dataset_id)).scalar_one_or_none()
        if ds is not None:
            return ds
    name = f"内置样例-{region}-{resolution}"
    ds = db.execute(
        sa.select(Dataset).where(Dataset.project_id == project_id, Dataset.name == name)
    ).scalar_one_or_none()
    if ds is None:
        ds = create_dataset(
            db,
            project_id,
            name,
            source_category="builtin_sample",
            license=SAMPLE_LICENSE,
            provenance={"source_category": "builtin_sample", "region": region, "resolution": resolution},
            description=f"内置合成样例数据({_sample_params(region)['name']}, {resolution})",
        )
    return ds


def create_builtin_sample(
    db: Session,
    project_id: int,
    resolution: str,
    region: str = "shanghai",
    *,
    user_id: int | None = None,
    dataset_id: int | None = None,
) -> DatasetVersion:
    """生成并保存内置样例数据版本(REQ-DATA-003)。

    与上传数据共用同一校验与存储路径; 记录地区/时间范围/分辨率/单位/许可证/溯源。
    同一种子生成内容完全一致(content_hash 相同, 对象存储去重)。

    参数:
        dataset_id: 目标数据集; 为 None 时按 "内置样例-{region}-{resolution}" 查找或创建。
    """
    if resolution not in RESOLUTIONS:
        raise ValueError(f"非法分辨率: {resolution!r},允许值 {sorted(RESOLUTIONS)}")
    seed = _sample_seed(region, resolution)
    rng = np.random.default_rng(seed)
    # 数据时间轴按项目本地年: 本地 2025-01-01 00:00 起点, 即 UTC 2024-12-31 16:00
    t0_utc = datetime(2024, 12, 31, 16, tzinfo=UTC)
    axis = build_axis(resolution, utc_offset_minutes=480, t0_utc=t0_utc)
    rows = _generate_sample_rows(axis, region, rng)
    df = pd.DataFrame(rows)
    _axis, normalized, diags = validate_dataset(df, resolution, axis.utc_offset_minutes)
    if any(d.blocking for d in diags):
        # 防御: 生成器本身不应产生阻断性诊断
        raise DataValidationError(diags)
    dataset = _get_or_create_sample_dataset(db, project_id, region, resolution, dataset_id=dataset_id)
    provenance = {
        "source_category": "builtin_sample",
        "generator": "iesplan.services.dataset.create_builtin_sample",
        "region": region,
        "region_name": _sample_params(region)["name"],
        "resolution": resolution,
        "utc_offset_minutes": axis.utc_offset_minutes,
        "seed": seed,
        "time_range": {
            "start": axis.timestamp(0).isoformat(),
            "end": axis.timestamp(axis.n - 1).isoformat(),
        },
        "description_zh": "内置合成样例数据, 仅用于演示/教学/测试",
    }
    meta = {
        "source_category": "builtin_sample",
        "license": SAMPLE_LICENSE,
        "provenance": provenance,
        "created_reason": "builtin_sample",
    }
    return _commit_version(db, dataset, axis, normalized, diags, None, meta, actor_id=user_id)
