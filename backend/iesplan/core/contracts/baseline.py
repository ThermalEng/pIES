"""无状态纯数据类型: 项目计算基线(宪法 7.5 / 0.6.5 前置阶段事项 1)。

项目创建时一次性固定的不可变计算口径只包含时间分辨率、运行起始年和
场景模式(无闰年字段, 闰年逐年按公历推导)。``initial_step``、
``step_duration`` 与 ``step_count`` 均从这些显式事实唯一推导，不形成
第二组可提交字段；全周期点数与年度边界由分辨率、起始年与提交时确认的
评价年限推导(见 ``horizon_point_count``)。

多场景与已有项目基线变更属于 1.0.0 之后的 Roadmap 能力; 本项目只实现
``single`` 场景模式。

本模块只依赖标准库与 ``core.diagnostics``, 不导入任何业务模块
(core/contracts 边界, 宪法 4.1)。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from iesplan.core.diagnostics import Diagnostic, make_diag

#: 全项目统一计算分辨率: 每个 step 的采样间隔(必须能确定性切分一天)。
RESOLUTION_VALUES: Final[tuple[str, ...]] = ("15min", "30min", "1h")

#: 场景模式: 当前只支持 single(单场景)。
SCENARIO_MODES: Final[tuple[str, ...]] = ("single",)

#: 默认场景模式(当前唯一取值)。
DEFAULT_SCENARIO_MODE: Final[str] = "single"

#: 设备方程使用的公开时间步长变量名。
TIMELINE_STEP_DURATION_VAR: Final[str] = "step_duration"

#: 分辨率 → (普通年 365 天点数, 闰年 366 天点数)。
_POINT_COUNTS: Final[dict[str, tuple[int, int]]] = {
    "15min": (365 * 24 * 4, 366 * 24 * 4),  # 35040 / 35136
    "30min": (365 * 24 * 2, 366 * 24 * 2),  # 17520 / 17568
    "1h": (365 * 24, 366 * 24),  # 8760 / 8784
}


def point_count_for_baseline(resolution: str, leap_year: bool) -> int:
    """分辨率 + 闰年 → 单年全周期点数的权威纯推导。

    供 ``ProjectBaseline.point_count`` 与计算阶段共用；非法可信状态
    （未知分辨率）让原生 ``KeyError`` 暴露，不包装诊断。
    """
    normal, leap = _POINT_COUNTS[resolution]
    return leap if leap_year else normal


def is_leap_year(year: int) -> bool:
    """公历闰年判定(能被 4 整除, 整百年须被 400 整除)。

    运行起始年及各评价年份的闰否唯一来源；不接受调用方传入闰年标记。
    """
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def point_count_for_year(resolution: str, year: int) -> int:
    """分辨率 + 公历年份 → 该年点数(闰年按 ``is_leap_year`` 推导)。"""
    return point_count_for_baseline(resolution, is_leap_year(year))


def horizon_year_point_counts(
    resolution: str, start_year: int, evaluation_years: int
) -> tuple[int, ...]:
    """评价期逐年点数(``start_year`` 起连续 ``evaluation_years`` 年)。

    年数须为正整数；非法输入由调用方前置校验(本函数只做推导)。
    """
    return tuple(
        point_count_for_year(resolution, start_year + offset)
        for offset in range(evaluation_years)
    )


def horizon_point_count(
    resolution: str, start_year: int, evaluation_years: int
) -> int:
    """全周期总点数(逐年点数之和)。"""
    return sum(horizon_year_point_counts(resolution, start_year, evaluation_years))


def horizon_year_boundary_steps(
    resolution: str, start_year: int, evaluation_years: int
) -> tuple[int, ...]:
    """年度边界 step 下标(第 k 年起始 step；首项恒为 0)。"""
    boundaries: list[int] = [0]
    for count in horizon_year_point_counts(resolution, start_year, evaluation_years):
        boundaries.append(boundaries[-1] + count)
    return tuple(boundaries)

#: 分辨率对应的小时步长。
_RESOLUTION_HOURS: Final[dict[str, float]] = {
    "15min": 0.25,
    "30min": 0.5,
    "1h": 1.0,
}

#: 基线字段白名单(严格恢复: 未知核心字段拒绝, 宪法 7.1)。
#: ``leap_year`` 已删除(闰年由起始年推导); 旧字段出现即未知字段拒绝。
_BASELINE_FIELDS: Final[frozenset[str]] = frozenset(
    {"resolution", "start_year", "scenario_mode"}
)

#: 基线校验诊断码(登记于 core.diagnostics.NEW_DIAG_CODES)。
BASELINE_INVALID = "PROJ-BASE-001"


class ProjectBaselineError(ValueError):
    """项目计算基线校验失败(非法分辨率/场景模式/起始年/类型/未知字段)。"""


@dataclass(frozen=True, slots=True)
class ProjectBaseline:
    """项目计算基线(创建时一次性固定, 创建后不可修改)。

    属性:
        resolution: 全项目统一计算分辨率('15min' | '30min' | '1h')。
        start_year: 运行起始年(公历整数年)。
        scenario_mode: 场景模式; 当前固定为 'single'。
    """

    resolution: str
    start_year: int
    scenario_mode: str = DEFAULT_SCENARIO_MODE

    def __post_init__(self) -> None:
        if self.resolution not in RESOLUTION_VALUES:
            raise ProjectBaselineError(
                f"非法基线分辨率: {self.resolution!r}, 允许值 {sorted(RESOLUTION_VALUES)}",
            )
        if not isinstance(self.start_year, int) or isinstance(self.start_year, bool):
            raise ProjectBaselineError(
                f"start_year 必须是公历整数年, 实际 {type(self.start_year).__name__}",
            )
        if self.scenario_mode not in SCENARIO_MODES:
            raise ProjectBaselineError(
                f"非法场景模式: {self.scenario_mode!r}, 允许值 {sorted(SCENARIO_MODES)}",
            )

    @property
    def point_count(self) -> int:
        """起始年单年点数(由 resolution 与起始年公历闰否唯一推导)。"""
        return point_count_for_year(self.resolution, self.start_year)

    @property
    def initial_step(self) -> int:
        """计算序列的首个 step，固定从 0 开始。"""
        return 0

    @property
    def step_duration(self) -> float:
        """由 resolution 唯一推导的步长，单位为小时。"""
        return _RESOLUTION_HOURS[self.resolution]

    @property
    def step_count(self) -> int:
        """由 resolution 与起始年唯一推导的项目计算点数(起始年单年)。"""
        return self.point_count

    def canonical_payload(self) -> str:
        """规范化负载(稳定键序 + 紧凑 JSON)，不含摘要。"""
        import json

        return json.dumps(
            {
                "resolution": self.resolution,
                "start_year": self.start_year,
                "scenario_mode": self.scenario_mode,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def to_dict(self) -> dict:
        """公开字典形态(API/持久化/装配共用的唯一序列化)。

        仅包含用户输入口径字段; 不携带内部派生摘要(宪法 2.6)。
        """
        return {
            "resolution": self.resolution,
            "start_year": self.start_year,
            "scenario_mode": self.scenario_mode,
        }

    @classmethod
    def from_dict(cls, mapping: object) -> "ProjectBaseline":
        """严格恢复: 未知字段拒绝; resolution/start_year 缺失拒绝; 枚举之外拒绝。

        scenario_mode 缺失时取默认 'single'(文档化默认值)。
        """
        if not isinstance(mapping, Mapping):
            raise ProjectBaselineError(
                f"基线必须是字典, 实际 {type(mapping).__name__}",
            )
        unknown = set(mapping) - _BASELINE_FIELDS
        if unknown:
            raise ProjectBaselineError(
                f"基线存在未知字段: {sorted(unknown)}",
            )
        missing = {"resolution", "start_year"} - set(mapping)
        if missing:
            raise ProjectBaselineError(
                f"基线缺少必需字段: {sorted(missing)}",
            )
        year_raw = mapping["start_year"]
        if not isinstance(year_raw, int) or isinstance(year_raw, bool):
            raise ProjectBaselineError(
                f"start_year 必须是公历整数年, 实际 {type(year_raw).__name__}",
            )
        baseline = cls(
            resolution=str(mapping["resolution"]),
            start_year=year_raw,
            scenario_mode=str(mapping.get("scenario_mode", DEFAULT_SCENARIO_MODE)),
        )
        return baseline

    @classmethod
    def validate(cls, mapping: object) -> list[Diagnostic]:
        """校验字典形态基线, 返回结构化诊断(非法时非空; 不抛异常)。

        供 API/迁移/包导入复用, 与 ``from_dict`` 同源; 每条诊断携带
        可定位字段与修复提示。
        """
        if not isinstance(mapping, Mapping):
            return [
                make_diag(
                    BASELINE_INVALID,
                    params={"detail": f"基线必须是字典, 实际 {type(mapping).__name__}"},
                    location={"object_type": "project_baseline", "field": ""},
                )
            ]
        diags: list[Diagnostic] = []
        for key in sorted(set(mapping) - _BASELINE_FIELDS):
            diags.append(
                make_diag(
                    BASELINE_INVALID,
                    params={"detail": f"未知字段: {key}"},
                    location={"object_type": "project_baseline", "field": key},
                )
            )
        resolution = mapping.get("resolution")
        if resolution is None:
            diags.append(
                make_diag(
                    BASELINE_INVALID,
                    params={"detail": "缺少必需字段: resolution"},
                    location={"object_type": "project_baseline", "field": "resolution"},
                )
            )
        elif resolution not in RESOLUTION_VALUES:
            diags.append(
                make_diag(
                    BASELINE_INVALID,
                    params={
                        "detail": f"非法分辨率: {resolution!r}, "
                        f"允许值 {sorted(RESOLUTION_VALUES)}",
                    },
                    location={"object_type": "project_baseline", "field": "resolution"},
                )
            )
        start_year = mapping.get("start_year")
        if start_year is None:
            diags.append(
                make_diag(
                    BASELINE_INVALID,
                    params={"detail": "缺少必需字段: start_year"},
                    location={"object_type": "project_baseline", "field": "start_year"},
                )
            )
        elif not isinstance(start_year, int) or isinstance(start_year, bool):
            diags.append(
                make_diag(
                    BASELINE_INVALID,
                    params={"detail": "start_year 必须是公历整数年"},
                    location={"object_type": "project_baseline", "field": "start_year"},
                )
            )
        scenario_mode = mapping.get("scenario_mode", DEFAULT_SCENARIO_MODE)
        if scenario_mode not in SCENARIO_MODES:
            diags.append(
                make_diag(
                    BASELINE_INVALID,
                    params={
                        "detail": f"非法场景模式: {scenario_mode!r}, "
                        f"允许值 {sorted(SCENARIO_MODES)}",
                    },
                    location={"object_type": "project_baseline", "field": "scenario_mode"},
                )
            )
        return diags
