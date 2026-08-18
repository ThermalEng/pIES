"""时间轴:标准非闰年 365 天,固定 UTC 偏移,无夏令时(02 §1)。

- 支持 3 种步长:15min(n=35040) / 30min(n=17520) / 1h(n=8760)。
- 日历年表按 365 天结构计算(day_of_year/season 与 t0 的年份无关),
  不接受闰日(2 月 29 日 → DATA-TS-003)。
- 所有时间戳以 UTC 存储(数据库 TIMESTAMPTZ),按项目固定偏移解释为本地时间。
- validate_timestamps 返回 Diagnostic 列表:行数不匹配/乱序/重复/越界/闰日。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np

from iesplan.core.diagnostics import (
    DATA_TS_DUP,
    DATA_TS_GAP,
    DATA_TS_LEAP,
    Diagnostic,
    make_diag,
)

#: 标准年天数(非闰年)
DAYS_IN_YEAR: int = 365
#: 每月天数表(非闰年)
_MONTH_DAYS: tuple[int, ...] = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
#: 每月起始的年内天偏移(0 基)
_MONTH_START: tuple[int, ...] = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)

#: 步长配置:分辨率 → (年步数 n, 步长分钟, 步长秒)
RESOLUTIONS: dict[str, tuple[int, int, int]] = {
    "15min": (35040, 15, 900),
    "30min": (17520, 30, 1800),
    "1h": (8760, 60, 3600),
}

#: 季节编号:0=冬 1=春 2=夏 3=秋(02 §1.3:春 3-5 月、夏 6-8 月、秋 9-11 月、冬 12-2 月)
SEASON_WINTER = 0
SEASON_SPRING = 1
SEASON_SUMMER = 2
SEASON_AUTUMN = 3


def _season_of_month(month: int) -> int:
    """月份 → 季节编号(1 基月份)。"""
    if month in (3, 4, 5):
        return SEASON_SPRING
    if month in (6, 7, 8):
        return SEASON_SUMMER
    if month in (9, 10, 11):
        return SEASON_AUTUMN
    return SEASON_WINTER  # 12, 1, 2


def _month_of_day(day_of_year: int) -> int:
    """年内天(0..364) → 月份(1..12),按非闰年 365 天表计算。"""
    for m in range(11, -1, -1):
        if day_of_year >= _MONTH_START[m]:
            return m + 1
    return 1  # 不可达,防御


def _is_leap_day(ts: datetime) -> bool:
    """判断时间戳是否为闰日(2 月 29 日)。"""
    return ts.month == 2 and ts.day == 29


@dataclass(frozen=True, slots=True)
class TimeAxis:
    """逐时/分时时间轴(365 天标准年,固定 UTC 偏移)。

    属性:
        resolution: '15min' | '30min' | '1h'。
        n: 年步数 35040 / 17520 / 8760。
        step_minutes: 步长分钟 15 / 30 / 60。
        utc_offset_minutes: 项目固定 UTC 偏移(默认 480 = +08:00)。
        t0_utc: 非闰年 1 月 1 日 00:00 UTC(默认 2025-01-01T00:00:00Z)。
        hour_of_year: (n,) int,0..8759。
        day_of_year: (n,) int,0..364。
        season: (n,) int,0=冬 1=春 2=夏 3=秋。
    """

    resolution: str
    n: int
    step_minutes: int
    utc_offset_minutes: int
    t0_utc: datetime
    hour_of_year: np.ndarray
    day_of_year: np.ndarray
    season: np.ndarray

    @property
    def step_seconds(self) -> int:
        """步长秒数。"""
        return self.step_minutes * 60

    def timestamp(self, i: int) -> datetime:
        """第 i 步的 UTC 时间戳(2024-01-01 基准年可配置,见 build_axis)。"""
        if not 0 <= i < self.n:
            raise IndexError(f"步索引越界: {i},有效范围 0..{self.n - 1}")
        return self.t0_utc + timedelta(seconds=i * self.step_seconds)

    def local_time(self, i: int) -> datetime:
        """第 i 步的项目本地显示时间(ts + 固定偏移,02 §1.2)。"""
        return self.timestamp(i) + timedelta(minutes=self.utc_offset_minutes)


def build_axis(
    resolution: str,
    utc_offset_minutes: int = 480,
    t0_utc: datetime | None = None,
) -> TimeAxis:
    """构建一年标准时间轴(纯确定性函数,02 §1.5)。

    参数:
        resolution: '15min' | '30min' | '1h'。
        utc_offset_minutes: 项目固定 UTC 偏移(默认 480 = +08:00,无夏令时)。
        t0_utc: 基准年 1 月 1 日 00:00 UTC(naive 视为 UTC);默认 2025-01-01
            (非闰年,满足 CONTRACT 第 2 节"非闰年 1 月 1 日")。
    返回:
        TimeAxis;hour_of_year/day_of_year/season 长度均为 n。
    异常:
        ValueError: 非法分辨率。
    """
    if resolution not in RESOLUTIONS:
        raise ValueError(f"非法分辨率: {resolution!r},允许值 {sorted(RESOLUTIONS)}")
    n, step_min, _ = RESOLUTIONS[resolution]
    if t0_utc is None:
        t0 = datetime(2025, 1, 1, tzinfo=UTC)
    elif t0_utc.tzinfo is None:
        t0 = t0_utc.replace(tzinfo=UTC)
    else:
        t0 = t0_utc.astimezone(UTC)

    idx = np.arange(n, dtype=np.int64)
    hour_of_year = idx // (60 // step_min)  # 每步小时偏移
    day_of_year = idx // (1440 // step_min)  # 年内天 0..364
    month = np.array([_month_of_day(int(d)) for d in day_of_year], dtype=np.int64)
    season = np.array([_season_of_month(int(m)) for m in month], dtype=np.int64)

    return TimeAxis(
        resolution=resolution,
        n=n,
        step_minutes=step_min,
        utc_offset_minutes=utc_offset_minutes,
        t0_utc=t0,
        hour_of_year=hour_of_year,
        day_of_year=day_of_year,
        season=season,
    )


def validate_timestamps(timestamps: list[datetime], resolution: str) -> list[Diagnostic]:
    """校验时序列时间戳(02 §1.1,04 §5)。

    检查项(每项至多产出一条诊断,避免刷屏):
        1. 行数不匹配:长度 ≠ 期望 n → DATA-TS-004(new,见 NEW_DIAG_CODES)。
        2. 闰日:含 2 月 29 日 → DATA-TS-003(04 已登记)。
        3. 越界:超出 [t0, t0+365 天) 或跨年份 → DATA-TS-006(new)。
        4. 重复:相邻重复时间戳 → DATA-TS-001。
        5. 乱序:非严格递增 → DATA-TS-005(new)。
        6. 步长不对齐:与 n 对齐后的时间戳偏差超 1 秒 → DATA-TS-007(new)。

    参数:
        timestamps: 待校验时间戳列表(naive 视为 UTC;允许混合时区)。
        resolution: '15min' | '30min' | '1h'(决定期望行数)。
    返回:
        诊断列表(无问题返回空列表)。
    """
    diags: list[Diagnostic] = []
    if resolution not in RESOLUTIONS:
        raise ValueError(f"非法分辨率: {resolution!r}")
    expected_n = RESOLUTIONS[resolution][0]
    loc = {"object_type": "time_series", "object_id": "", "field": "timestamps"}

    # 1) 行数不匹配
    if len(timestamps) != expected_n:
        diags.append(
            make_diag(
                "DATA-TS-004",
                severity="error",
                params={"expected": expected_n, "actual": len(timestamps), "resolution": resolution},
                location=loc,
            )
        )
        return diags  # 行数错误时其余检查无意义,尽早返回

    if not timestamps:
        return diags

    # 归一化:naive 视为 UTC
    def _as_utc(ts: datetime) -> datetime:
        if ts.tzinfo is None:
            return ts.replace(tzinfo=UTC)
        return ts.astimezone(UTC)

    utc_list = [_as_utc(ts) for ts in timestamps]
    year0 = utc_list[0].year
    t0 = datetime(year0, 1, 1, tzinfo=UTC)
    t_end = t0 + timedelta(days=DAYS_IN_YEAR)  # 左闭右开 [t0, t0+365 天)

    # 2) 闰日(02 §1.1:不接受 366 天日历)
    leap_rows = [i for i, ts in enumerate(utc_list) if _is_leap_day(ts)]
    if leap_rows:
        diags.append(
            make_diag(
                DATA_TS_LEAP,
                severity="error",
                blocking=True,
                params={"series_name": "timestamps", "first_rows": leap_rows[:5], "count": len(leap_rows)},
                location={**loc, "row": leap_rows[:5]},
                ref_ids=["help.import.csv_general"],
            )
        )
        return diags

    # 3) 越界/跨年
    bad_rows = [i for i, ts in enumerate(utc_list) if ts < t0 or ts >= t_end]
    if bad_rows:
        diags.append(
            make_diag(
                "DATA-TS-006",
                severity="error",
                params={"year": year0, "first_rows": bad_rows[:5], "count": len(bad_rows)},
                location={**loc, "row": bad_rows[:5]},
            )
        )
        return diags

    # 4) 重复(相邻相等)
    dup_rows: list[int] = []
    for i in range(1, len(utc_list)):
        if utc_list[i] == utc_list[i - 1]:
            dup_rows.append(i)
    if dup_rows:
        diags.append(
            make_diag(
                DATA_TS_DUP,
                severity="error",
                params={"series_name": "timestamps", "count": len(dup_rows), "first_rows": dup_rows[:5]},
                location={**loc, "row": dup_rows[:5]},
                ref_ids=["help.import.duplicate_rows"],
            )
        )

    # 5) 乱序(非严格递增)
    disorder_rows: list[int] = []
    for i in range(1, len(utc_list)):
        if utc_list[i] < utc_list[i - 1]:
            disorder_rows.append(i)
    if disorder_rows:
        diags.append(
            make_diag(
                "DATA-TS-005",
                severity="error",
                params={"first_rows": disorder_rows[:5], "count": len(disorder_rows)},
                location={**loc, "row": disorder_rows[:5]},
            )
        )

    # 6) 步长不对齐:期望网格 t0 + k·Δt 与实际时间戳比对
    step_sec = RESOLUTIONS[resolution][2]
    grid = [t0 + timedelta(seconds=k * step_sec) for k in range(expected_n)]
    pairs = zip(utc_list, grid, strict=True)  # 长度已校验相等
    mis_rows = [i for i, (a, b) in enumerate(pairs) if abs((a - b).total_seconds()) > 1.0]
    if mis_rows:
        diags.append(
            make_diag(
                "DATA-TS-007",
                severity="warning",
                params={"resolution": resolution, "first_rows": mis_rows[:5], "count": len(mis_rows)},
                location={**loc, "row": mis_rows[:5]},
            )
        )

    # 缺口诊断:对齐后检测缺失/重复时段(04 已登记 DATA-TS-002,仅 warn 提示)
    present = {i for i, ts in enumerate(utc_list) if ts == grid[i]}
    if len(present) < expected_n:
        gap_rows = sorted(set(range(expected_n)) - present)
        diags.append(
            make_diag(
                DATA_TS_GAP,
                severity="warning",
                params={
                    "series_name": "timestamps",
                    "from": str(grid[gap_rows[0]]),
                    "to": str(grid[gap_rows[-1]]),
                    "count": len(gap_rows),
                },
                location={**loc, "row": gap_rows[:5]},
            )
        )
    return diags
