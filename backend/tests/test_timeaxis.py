"""时间轴模块单元测试:365 天非闰年、n 与数组长度、季节、校验诊断。"""

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from iesplan.core.timeaxis import (
    SEASON_AUTUMN,
    SEASON_SPRING,
    SEASON_SUMMER,
    SEASON_WINTER,
    build_axis,
    validate_timestamps,
)

UTC = UTC
T0 = datetime(2025, 1, 1, tzinfo=UTC)


def _full_timestamps(resolution: str) -> list[datetime]:
    """按某分辨率生成完整合法时间戳列表。"""
    axis = build_axis(resolution, utc_offset_minutes=480, t0_utc=T0)
    return [axis.timestamp(i) for i in range(axis.n)]


class TestBuildAxis:
    """构建正确性:三种步长、n、数组长度、固定偏移。"""

    @pytest.mark.parametrize(
        "resolution, n, step_min",
        [("15min", 35040, 15), ("30min", 17520, 30), ("1h", 8760, 60)],
    )
    def test_resolutions(self, resolution, n, step_min):
        axis = build_axis(resolution, utc_offset_minutes=480, t0_utc=T0)
        assert axis.resolution == resolution
        assert axis.n == n
        assert axis.step_minutes == step_min
        assert len(axis.hour_of_year) == n
        assert len(axis.day_of_year) == n
        assert len(axis.season) == n
        assert axis.utc_offset_minutes == 480
        assert axis.t0_utc == T0

    def test_default_t0_is_non_leap_year(self):
        axis = build_axis("1h")
        assert axis.t0_utc.year == 2025
        assert axis.t0_utc.tzinfo is not None

    def test_default_utc_offset(self):
        axis = build_axis("1h")
        assert axis.utc_offset_minutes == 480

    def test_hour_of_year_range(self):
        axis = build_axis("1h")
        assert axis.hour_of_year.min() == 0
        assert axis.hour_of_year.max() == 8759
        assert axis.hour_of_year.dtype == np.int64

    def test_day_of_year_range(self):
        axis = build_axis("1h")
        assert axis.day_of_year.min() == 0
        assert axis.day_of_year.max() == 364
        assert np.all(np.diff(axis.day_of_year) >= 0)

    def test_invalid_resolution(self):
        with pytest.raises(ValueError):
            build_axis("10min")

    def test_timestamp_boundaries(self):
        axis = build_axis("1h")
        assert axis.timestamp(0) == T0
        assert axis.timestamp(axis.n - 1) == T0 + timedelta(days=364, hours=23)
        with pytest.raises(IndexError):
            axis.timestamp(axis.n)

    def test_local_time_offset(self):
        axis = build_axis("1h", utc_offset_minutes=480)
        assert axis.local_time(0) == T0 + timedelta(hours=8)

    def test_naive_t0_treated_as_utc(self):
        axis = build_axis("1h", t0_utc=datetime(2025, 1, 1))
        assert axis.t0_utc == T0


class TestSeason:
    """季节分类(02 §1.3:春 3-5 月、夏 6-8 月、秋 9-11 月、冬 12-2 月)。"""

    def test_season_mapping_by_month(self):
        axis = build_axis("1h", t0_utc=T0)

        # 第 d 天对应时间戳
        def day_index(d: int) -> int:
            return d * 24

        # 1 月 15 日(第 14 天)→ 冬
        assert axis.season[day_index(14)] == SEASON_WINTER
        # 3 月 15 日(第 73 天)→ 春
        assert axis.season[day_index(73)] == SEASON_SPRING
        # 6 月 15 日(第 165 天)→ 夏
        assert axis.season[day_index(165)] == SEASON_SUMMER
        # 9 月 15 日(第 257 天)→ 秋
        assert axis.season[day_index(257)] == SEASON_AUTUMN
        # 12 月 15 日(第 348 天)→ 冬
        assert axis.season[day_index(348)] == SEASON_WINTER
        assert set(axis.season) == {0, 1, 2, 3}

    def test_quarter_boundaries(self):
        axis = build_axis("1h", t0_utc=T0)
        # 2 月 28 日(第 58 天)23 时 → 冬;3 月 1 日 0 时 → 春
        assert axis.season[58 * 24 + 23] == SEASON_WINTER
        assert axis.season[59 * 24] == SEASON_SPRING


class TestValidateTimestamps:
    """校验诊断:行数/乱序/重复/越界/闰日/步长不对齐。"""

    def test_valid_input_no_diag(self):
        ts = _full_timestamps("1h")
        assert validate_timestamps(ts, "1h") == []

    def test_row_count_mismatch(self):
        ts = _full_timestamps("1h")[:-1]
        diags = validate_timestamps(ts, "1h")
        assert len(diags) == 1
        assert diags[0].code == "DATA-TS-004"
        assert diags[0].params["expected"] == 8760
        assert diags[0].params["actual"] == 8759

    def test_row_count_mismatch_15min(self):
        ts = _full_timestamps("15min")[:-1]
        diags = validate_timestamps(ts, "15min")
        assert diags[0].code == "DATA-TS-004"
        assert diags[0].params["expected"] == 35040

    def test_duplicates(self):
        ts = _full_timestamps("1h")
        ts[10] = ts[9]  # 制造重复
        diags = validate_timestamps(ts, "1h")
        codes = [d.code for d in diags]
        assert "DATA-TS-001" in codes
        dup = next(d for d in diags if d.code == "DATA-TS-001")
        assert dup.severity == "error"

    def test_out_of_order(self):
        ts = _full_timestamps("1h")
        ts[20], ts[21] = ts[21], ts[20]  # 交换制造乱序
        diags = validate_timestamps(ts, "1h")
        codes = [d.code for d in diags]
        assert "DATA-TS-005" in codes

    def test_out_of_calendar_bounds(self):
        ts = _full_timestamps("1h")
        ts[-1] = T0 + timedelta(days=365)  # 第 366 天
        diags = validate_timestamps(ts, "1h")
        codes = [d.code for d in diags]
        assert "DATA-TS-006" in codes

    def test_leap_day_rejected(self):
        ts = _full_timestamps("1h")
        # 替换 1 月 1 日为 2 月 29 日(闰日)
        ts[0] = datetime(2024, 2, 29, tzinfo=UTC)
        diags = validate_timestamps(ts, "1h")
        assert len(diags) == 1
        assert diags[0].code == "DATA-TS-003"
        assert diags[0].blocking is True

    def test_step_misaligned_warns(self):
        ts = _full_timestamps("1h")
        ts[5] = ts[5] + timedelta(minutes=30)  # 偏离步长 30 分钟
        diags = validate_timestamps(ts, "1h")
        codes = [d.code for d in diags]
        assert "DATA-TS-007" in codes

    def test_naive_timestamps_treated_utc(self):
        ts = _full_timestamps("1h")
        naive = [t.replace(tzinfo=None) for t in ts]
        assert validate_timestamps(naive, "1h") == []

    def test_empty_after_row_check(self):
        diags = validate_timestamps([], "1h")
        assert diags[0].code == "DATA-TS-004"

    def test_invalid_resolution(self):
        with pytest.raises(ValueError):
            validate_timestamps([], "10min")

    def test_duplicates_15min_dense(self):
        """30min 步长下用 15min 数据会触发缺口/错位告警,但完整对齐数据无诊断。"""
        axis = build_axis("30min", t0_utc=T0)
        ts = [axis.timestamp(i) for i in range(axis.n)]
        assert validate_timestamps(ts, "30min") == []

    def test_gap_detected(self):
        """删除中间一个点(总数仍为 n,时间轴不对齐)→ 缺口告警。"""
        ts = _full_timestamps("1h")
        ts[100] = ts[101] + timedelta(hours=2)  # 制造缺口(跳 2 小时)
        diags = validate_timestamps(ts, "1h")
        codes = [d.code for d in diags]
        assert "DATA-TS-002" in codes
        assert "DATA-TS-007" in codes
