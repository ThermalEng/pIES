"""标准 csv 时间序列:读取/校验/模板/周期曲线(02 §4;05 §7.6 csvio → profile)。

- 列 → 单位映射只认 yaml ``time_series`` 声明(设备列唯一权威)与标准列注册表;
  非标准单位列必须在 yaml 中给出 ``convert`` 换算声明, 否则报错而非静默透传(02 §4.2);
- 时间戳校验复用 core.timeaxis.validate_timestamps(行数 35040/17520/8760、闰日、
  越界、重复、乱序、步长对齐);
- 本模块为 1 层, 不得 import services(5 层)的 STANDARD_FIELDS, 标准列单位/范围
  在此以常量复刻(yaml 声明优先, 数值不做换算只做声明与校验)。
"""

from __future__ import annotations

import csv as _csv
import math
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from iesplan.core.diagnostics import (
    DATA_COL_MISSING,
    PARAM_RNG_OUT,
    PARAM_UNIT_MISMATCH,
    RES_NUM_INVALID,
    SYS_CFG_INVALID,
    Diagnostic,
    make_diag,
)
from iesplan.core.errors import AppError
from iesplan.core.timeaxis import RESOLUTIONS, validate_timestamps
from iesplan.devices.spec import PERIOD_VALUES, DeviceModelDescriptor, DeviceYamlSpec, SeriesSpec

#: 时间戳列名(CSV 第一列)
TIMESTAMP_COL = "timestamp"
#: 时间戳列别名(解析时归一化到 TIMESTAMP_COL)
_TS_ALIASES = ("timestamp", "time", "datetime")

#: 标准列注册表(镜像 services/dataset.py::STANDARD_FIELDS 的 unit/min/max;层内复刻)
#: 键 → (单位, 最小, 最大)
STANDARD_COLUMN_UNITS: dict[str, tuple[str, float | None, float | None]] = {
    "e_load": ("kWh", 0.0, None),
    "h_load": ("kWh", 0.0, None),
    "c_load": ("kWh", 0.0, None),
    "t_ambient": ("°C", -40.0, 60.0),
    "ghi": ("W/m²", 0.0, 1500.0),
    "electricity_price": ("元/kWh", 0.0, None),
    "grid_emission_factor": ("kgCO₂/kWh", 0.0, None),
}


def _normalize_unit(unit: str) -> str:
    """单位归一化(02 §4.2: 大小写不敏感)。"""
    return unit.strip().casefold()


def _all_series(spec: DeviceYamlSpec) -> list[SeriesSpec]:
    """yaml 声明的全部时间序列列(inputs + outputs, 保持声明顺序)。"""
    return list(spec.time_series.get("inputs", [])) + list(spec.time_series.get("outputs", []))


def load_profile_columns(path: Path, desc: DeviceModelDescriptor) -> dict[str, np.ndarray]:
    """公开接口: 读取 data_repeat 设备标准 csv → {列名: 一维数组}(BE-REG-01)。

    modeling 模块经本函数消费典型曲线, 不感知 csv 校验规则;
    必选列缺失/文件错误抛 AppError(原始文件与列错误可见)。
    """
    proxy = DeviceYamlSpec(
        type_id=desc.type_id, version=desc.version, name_zh=desc.name_zh,
        name_en=desc.name_en, model_method=desc.model_method, stateful=desc.stateful,
        energy_carriers=tuple(desc.energy_carriers), is_load=desc.is_load,
        capabilities=tuple(desc.capabilities), extends=desc.extends,
        help_topic=desc.help_topic, parameters=desc.parameters,
        ports=tuple(desc.ports), time_series=desc.time_series,
        states=tuple(desc.states), model_commands=desc.model_commands,
    )
    df = read_standard_csv(Path(path), proxy)
    return {
        col: df[col].to_numpy(dtype=np.float64)
        for col in df.columns
        if col != TIMESTAMP_COL
    }


def _loc(spec: DeviceYamlSpec, field: str) -> dict:
    return {"object_type": "device", "object_id": spec.type_id, "field": field}


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def read_standard_csv(path: Path, spec: DeviceYamlSpec) -> pd.DataFrame:
    """读取设备 csv:跳过注释行取表头 → 必选列存在性校验 → timestamp 解析归一化。

    必选列缺失抛 AppError(DATA-COL-001, 定位到文件与列名)。
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        raise _csv_error(path, "csv 为空(无表头)")
    header = [c.strip() for c in lines[0].split(",") if c.strip()]
    rows = list(_csv.reader(lines[1:]))
    rows = [(r + [""] * len(header))[: len(header)] for r in rows]

    required = [s.key for s in _all_series(spec) if s.required]
    missing = [c for c in required if c not in header]
    if missing:
        raise _csv_error(path, f"缺少必需列: {missing}", columns=missing)

    ts_col = next((c for c in header if c.strip().lower() in _TS_ALIASES), None)
    if ts_col is None:
        raise _csv_error(path, "缺少时间戳列(timestamp/time/datetime)", columns=["timestamp"])

    df = pd.DataFrame(rows, columns=header)
    df[TIMESTAMP_COL] = pd.to_datetime(df[ts_col], errors="coerce")
    return df


def _csv_error(path: Path, message: str, **params: object) -> AppError:
    return AppError(
        f"设备 csv 读取失败: {path} — {message}",
        code=DATA_COL_MISSING,
        message_key="ies.diag.data.col_missing",
        params={"file": str(path), **params},
    )


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def validate_series_csv(df: pd.DataFrame, spec: DeviceYamlSpec) -> list[Diagnostic]:
    """列/单位声明一致、时间戳行数/递增/步长、数值非空与范围;错误定位到文件/列/行。"""
    diags: list[Diagnostic] = []
    series = _all_series(spec)
    if not series:
        return diags
    resolutions = {s.resolution for s in series}
    if len(resolutions) > 1:
        diags.append(
            make_diag(
                SYS_CFG_INVALID,
                severity="warning",
                params={"resolution": sorted(resolutions)},
                location=_loc(spec, "time_series"),
            )
        )
    resolution = sorted(resolutions)[0]

    # 1) 必选列缺失
    missing = [s.key for s in series if s.required and s.key not in df.columns]
    if missing:
        diags.append(
            make_diag(
                DATA_COL_MISSING,
                blocking=True,
                params={"columns": missing},
                location=_loc(spec, "columns"),
            )
        )

    # 2) 单位声明一致性(大小写不敏感;非标准单位直接拒绝, 不再支持 convert 声明)
    for s in series:
        std = STANDARD_COLUMN_UNITS.get(s.key)
        if std is not None and s.key in df.columns:
            if _normalize_unit(s.unit) != _normalize_unit(std[0]):
                diags.append(
                    make_diag(
                        PARAM_UNIT_MISMATCH,
                        severity="error",
                        params={"field": s.key, "expected": std[0], "actual": s.unit},
                        location=_loc(spec, s.key),
                    )
                )

    # 3) 时间戳(行数/闰日/越界/重复/乱序/步长)
    if TIMESTAMP_COL in df.columns:
        bad_ts = [i for i, ts in enumerate(df[TIMESTAMP_COL]) if pd.isna(ts)]
        if bad_ts:
            diags.append(
                make_diag(
                    RES_NUM_INVALID,
                    params={"field": TIMESTAMP_COL, "rows": bad_ts[:10], "count": len(bad_ts)},
                    location=_loc(spec, TIMESTAMP_COL),
                )
            )
        else:
            timestamps = [ts.to_pydatetime() for ts in df[TIMESTAMP_COL]]
            diags.extend(validate_timestamps(timestamps, resolution))

    # 4) 数值非空与范围
    for s in series:
        if s.key not in df.columns:
            continue
        nums = pd.to_numeric(df[s.key], errors="coerce")
        null_rows = [i for i, v in enumerate(nums) if pd.isna(v)]
        if null_rows:
            diags.append(
                make_diag(
                    RES_NUM_INVALID,
                    params={"field": s.key, "rows": null_rows[:10], "count": len(null_rows)},
                    location=_loc(spec, s.key),
                )
            )
            continue
        lo: float | None = None
        hi: float | None = None
        if s.key in STANDARD_COLUMN_UNITS:
            _, lo, hi = STANDARD_COLUMN_UNITS[s.key]
        elif s.key in spec.parameters:
            lo, hi = spec.parameters[s.key].min, spec.parameters[s.key].max
        if lo is not None or hi is not None:
            bad = [
                i
                for i, v in enumerate(nums)
                if (lo is not None and v < lo) or (hi is not None and v > hi)
            ]
            if bad:
                diags.append(
                    make_diag(
                        PARAM_RNG_OUT,
                        params={"field": s.key, "rows": bad[:10], "count": len(bad)},
                        location=_loc(spec, s.key),
                    )
                )
    return diags


# ---------------------------------------------------------------------------
# 模板
# ---------------------------------------------------------------------------


def _sample_value(key: str, i: int) -> float:
    """模板示例值(确定性;范围与标准列注册表一致)。"""
    hour = i % 24
    weekday = (i // 24) % 7
    day_factor = 1.15 if weekday < 5 else 0.9
    if key == "e_load":
        profile = 0.35 + 0.65 * max(0.0, 1 - abs(hour - 18) / 10)
        return round(120.0 * day_factor * profile, 3)
    if key == "h_load":
        profile = 0.3 + 0.7 * max(0.0, 1 - abs(hour - 8) / 6)
        return round(85.0 * profile, 3)
    if key == "c_load":
        profile = 0.3 + 0.7 * max(0.0, 1 - abs(hour - 14) / 8)
        return round(60.0 * profile, 3)
    if key == "t_ambient":
        return round(20.0 + 6.0 * math.sin(2 * math.pi * (hour - 14) / 24), 2)
    if key == "ghi":
        daylight = max(0.0, math.sin(math.pi * min(1.0, max(0.0, (hour - 6.0) / 12.0)))) ** 1.2
        return round(620.0 * daylight, 3)
    if key == "electricity_price":
        return 0.7 if 8 <= hour < 22 else 0.3
    if key == "grid_emission_factor":
        return 0.581
    return 0.0


def make_template_csv(spec: DeviceYamlSpec, resolution: str = "1h", rows: int = 8760) -> str:
    """生成带双语注释行的设备标准时间序列模板(与 U05 数据集模板同风格)。

    rows 指定示例数据行数(默认全年;传负数则按该分辨率的全年行数)。
    """
    if resolution not in RESOLUTIONS:
        raise ValueError(f"非法分辨率: {resolution!r}, 允许值 {sorted(RESOLUTIONS)}")
    expected = RESOLUTIONS[resolution][0]
    if rows < 0 or rows == expected:
        rows = expected
    series = _all_series(spec)
    lines: list[str] = [
        "# pIES 设备标准时间序列模板 / pIES device time series template",
        f"# 设备 device: {spec.type_id}  name: {spec.name_zh} / {spec.name_en}",
        f"# 模型方法 model_method: {spec.model_method}  状态 stateful: {spec.stateful}",
        f"# 分辨率 resolution: {resolution}  年步数 steps/year: {expected}",
        "# 请勿修改表头与列顺序 / Do not modify the header row or column order.",
        "#",
        "# 列 column, 单位 unit, 必填 required, 周期 period(仅 data_repeat)",
    ]
    for s in series:
        period = f", period: {s.period}" if s.period else ""
        required = "是" if s.required else "否"
        lines.append(f"# {s.key},{s.unit},{required}{period}")
    lines.append("#")
    lines.append(",".join([TIMESTAMP_COL, *(s.key for s in series)]))
    t0 = datetime(2025, 1, 1)
    for i in range(rows):
        ts = (t0 + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%S")
        lines.append(",".join([ts, *(str(_sample_value(s.key, i)) for s in series)]))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 周期曲线(data_repeat)
# ---------------------------------------------------------------------------


def extract_period_curve(df: pd.DataFrame, period: str) -> np.ndarray:
    """data_repeat:按 period 聚合典型曲线(day→24 点、week→168 点、year→8760 点)。

    聚合列为数据帧内第一个非时间戳数值列(设备 csv 通常单数据列)。
    """
    if period not in PERIOD_VALUES:
        raise ValueError(f"非法周期粒度: {period!r}, 允许值 {PERIOD_VALUES}")
    ts = pd.to_datetime(df[TIMESTAMP_COL], errors="coerce")
    col = next(
        (
            c
            for c in df.columns
            if c.strip().lower() not in _TS_ALIASES
            and pd.to_numeric(df[c], errors="coerce").notna().any()
        ),
        None,
    )
    if col is None:
        raise ValueError("csv 无可用数值列, 无法提取周期曲线")
    hour = ts.dt.hour
    if period == "day":
        keys = hour
        n = 24
    elif period == "week":
        keys = ts.dt.dayofweek * 24 + hour
        n = 168
    else:
        keys = (ts.dt.dayofyear - 1) * 24 + hour
        n = 8760
    values = pd.to_numeric(df[col], errors="coerce").to_numpy(dtype=float)
    valid = ~np.isnan(values)
    out = np.full(n, np.nan, dtype=float)
    key_arr = keys.to_numpy()
    for k in range(n):
        mask = valid & (key_arr == k)
        if mask.any():
            out[k] = float(np.mean(values[mask]))
    return out
