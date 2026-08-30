"""纯周期展开: ``constant`` 常量与 ``data_repeat`` 模板展开到全周期连续 step。

规则(device-data-csv.md「step 与序列预备规则」, 由项目基线唯一决定):

- ``constant``: 直接取设备 interface 声明的值, 展开为与基线 ``point_count``
  等长的常量序列;
- ``data_repeat``: 输入是周期模板(``period: day|week|year``, 已按基线分辨率
  完成重采样), 按模板周期确定性平铺到基线全周期:
  - ``day``: 模板重复 ``全周期天数`` 次;
  - ``week``: 重复 52 个完整周 + 模板前 ``余下天数`` 天(365 = 52×7+1,
    闰年 366 = 52×7+2);
  - ``year``: 模板即一整年(365 天); 闰年基线追加模板第一天。

展开是确定性纯函数: 相同模板、周期与基线产生逐字节相同结果。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from iesplan.core.contracts import ProjectBaseline
from iesplan.core.diagnostics import Diagnostic, make_diag
from iesplan.core.timeaxis import RESOLUTIONS

#: 诊断码(登记于 core/diagnostics.py NEW_DIAG_CODES)
DATA_PREP_PERIOD_ROW_COUNT = "DATA-PREP-003"
DATA_PREP_EXPANSION_MISMATCH = "DATA-PREP-004"

#: data_repeat 周期枚举(与 ies.device-data 契约一致)
PERIOD_DAY = "day"
PERIOD_WEEK = "week"
PERIOD_YEAR = "year"
PERIOD_VALUES: tuple[str, ...] = (PERIOD_DAY, PERIOD_WEEK, PERIOD_YEAR)

#: 模板长度推导: 周期 → 目标分辨率下每个周期包含的 step 数(天数为 365 的周期)
def steps_per_day(resolution: str) -> int:
    """基线分辨率下每天包含的 step 数。"""
    if resolution not in RESOLUTIONS:
        raise ValueError(f"非法分辨率: {resolution!r},允许值 {sorted(RESOLUTIONS)}")
    return 1440 // RESOLUTIONS[resolution][1]


def expected_template_rows(resolution: str, period: str) -> int | None:
    """周期模板在指定分辨率下应有的行数(day/week/year; 非法参数返回 None)。"""
    if resolution not in RESOLUTIONS or period not in PERIOD_VALUES:
        return None
    per_day = steps_per_day(resolution)
    return {"day": per_day, "week": per_day * 7, "year": per_day * 365}[period]


def expand_template(
    column_values: Mapping[str, Sequence[float]],
    period: str,
    baseline: ProjectBaseline,
) -> tuple[dict[str, list[float]] | None, list[Diagnostic]]:
    """把周期模板确定性展开到项目基线全周期(模板已在基线分辨率下)。

    参数:
        column_values: 列名 → 模板序列(长度须等于该周期在基线分辨率下的行数)。
        period: 'day' | 'week' | 'year'。
        baseline: 项目计算基线(决定全周期点数)。
    返回:
        (columns, diagnostics): 成功时每列长度等于 ``baseline.point_count``;
        模板行数不符返回 ``DATA-PREP-003``, 展开结果与基线不符返回
        ``DATA-PREP-004``(均为阻断诊断)。
    """
    if period not in PERIOD_VALUES:
        return None, [
            make_diag(
                DATA_PREP_PERIOD_ROW_COUNT,
                blocking=True,
                params={"period": period, "expected": 0, "actual": 0, "resolution": baseline.resolution},
                location={"object_type": "sequence_prep", "field": "period"},
            )
        ]
    spd = steps_per_day(baseline.resolution)
    expected = expected_template_rows(baseline.resolution, period)
    assert expected is not None
    bad = [name for name, values in column_values.items() if len(values) != expected]
    if bad:
        return None, [
            make_diag(
                DATA_PREP_PERIOD_ROW_COUNT,
                blocking=True,
                params={
                    "period": period,
                    "resolution": baseline.resolution,
                    "expected": expected,
                    "actual": len(column_values[bad[0]]),
                    "columns": bad,
                },
                location={"object_type": "sequence_prep", "field": "steps"},
            )
        ]
    if not column_values:
        return None, [
            make_diag(
                DATA_PREP_PERIOD_ROW_COUNT,
                blocking=True,
                params={
                    "period": period,
                    "resolution": baseline.resolution,
                    "expected": expected,
                    "actual": 0,
                    "columns": [],
                },
                location={"object_type": "sequence_prep", "field": "columns"},
            )
        ]

    point_count = baseline.point_count
    full_days = point_count // spd
    out: dict[str, list[float]] = {}
    for name, values in column_values.items():
        template = [float(v) for v in values]
        if period == PERIOD_DAY:
            expanded = template * full_days
        elif period == PERIOD_WEEK:
            weeks, rest_days = divmod(full_days, 7)
            expanded = template * weeks + template[: rest_days * spd]
        else:  # PERIOD_YEAR: 模板为 365 天; 闰年基线追加模板第一天
            expanded = list(template)
            if point_count > len(expanded):
                expanded += template[:spd]
        out[name] = expanded

    bad = [name for name, values in out.items() if len(values) != point_count]
    if bad:
        return None, [
            make_diag(
                DATA_PREP_EXPANSION_MISMATCH,
                blocking=True,
                params={
                    "actual": len(out[bad[0]]),
                    "expected": point_count,
                    "columns": bad,
                },
                location={"object_type": "sequence_prep", "field": "steps"},
            )
        ]
    return out, []


__all__ = ["steps_per_day", "expected_template_rows", "expand_template"]
