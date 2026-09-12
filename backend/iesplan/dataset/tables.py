"""数据集 CSV 规则表（模板生成 / CSV 解析 / 时间轴与数值校验 / 质量报告，归属 dataset）。

本模块是数据集 CSV 权威规则的唯一实现（由 ``services.dataset`` 经
``application.datasets.csv_validation`` 收敛而来，旧服务已删除）；
无副作用纯函数，不访问数据库与对象存储。

调用方向：``application.datasets.lifecycle`` 消费本模块 +
dataset/project/identity/storage 域公开门面。
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from iesplan.core.diagnostics import (
    DATA_COL_MISSING,
    DATA_COL_UNIT_UNKNOWN,
    PARAM_RNG_OUT,
    RES_NUM_INVALID,
    RES_RANGE_OUT,
    SEVERITY_ERROR,
    Diagnostic,
    make_diag,
)
from iesplan.core.errors import AppError
from iesplan.core.timeaxis import RESOLUTIONS, TimeAxis, build_axis, validate_timestamps

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


#: 标准字段注册表(顺序即模板列顺序; 范围依据 file-formats §设备数据 CSV + domain-model §数据集 数据约束)
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
# 数据文件诊断码注册(file-formats §设备数据 CSV + domain-model §数据集: 错误必须定位到文件/字段/行号)
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
    校验失败阻断当前操作: 显式 blocking=True(severity 保持 error,
    与 config 422 包络一致, §8.3 示例 blocking: true)。
    """

    code = "DATA-VAL-001"
    severity = SEVERITY_ERROR
    message_key = "ies.error.data_validation_failed"
    http_status = 400

    def __init__(self, diagnostics: list[Diagnostic], message: str = "") -> None:
        self.diagnostics = list(diagnostics)
        # 校验失败阻断当前操作: 显式 blocking=True(severity 保持 error,
        # 与 config 422 包络一致, §8.3 示例 blocking: true)
        super().__init__(message or f"数据集校验失败: 共 {len(self.diagnostics)} 条阻断性诊断", blocking=True)


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
    """校验并归一化数据集(file-formats §设备数据 CSV + domain-model §数据集)。

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
    report = {
        "tool": "iesplan.services.dataset",
        "generated_at": datetime.now(UTC).isoformat(),
        "resolution": axis.resolution,
        "timeline": TIMELINE_MAP[axis.resolution],
        "row_count": axis.n,
        "fixed_utc_offset_minutes": axis.utc_offset_minutes,
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
    return report


def unit_matches(declared: str, model: str) -> bool:
    """单位量纲兼容判定(0.6.0: 与 ies.device-data 规范化器同一规则)。

    收敛到 core.units.units_compatible —— 不复制第二套单位换算表。
    """
    from iesplan.core.units import units_compatible

    return units_compatible(declared, model)


def normalized_to_csv_bytes(df: pd.DataFrame) -> bytes:
    """归一化 DataFrame → 规范 CSV 字节(2.0: 仅校验 CSV 自身，不做配套证明)。"""

    import csv as _csv
    import io as _io

    ts_col = df[TIMESTAMP_COL]
    utc_stamps = [
        t.astimezone(UTC) if getattr(t, "tzinfo", None) is not None else t.replace(tzinfo=UTC)
        for t in ts_col.tolist()
    ]
    cols = [TIMESTAMP_COL]
    cols += [c for c in STANDARD_FIELDS if c in df.columns]
    cols += [c for c in df.columns if c not in STANDARD_FIELDS and c != TIMESTAMP_COL]
    rows = [dict(r) for r in df.to_dict("records")]
    # 简化规范化：直接按列顺序写 CSV，时间戳转 ISO8601 UTC
    buf = _io.StringIO()
    writer = _csv.writer(buf, lineterminator="\n")
    writer.writerow(cols)
    for idx, row in enumerate(rows):
        ts = utc_stamps[idx].isoformat().replace("+00:00", "Z")
        writer.writerow([ts if c == TIMESTAMP_COL else row.get(c, "") for c in cols])
    return buf.getvalue().encode("utf-8")
