"""无状态纯数据类型: 项目计算基线(宪法 7.5 / 0.6.5 前置阶段事项 1)。

项目创建时一次性固定的不可变计算口径, 当前只包含时间分辨率、是否考虑
闰年和场景模式; 场景模式当前固定为 ``single``。基线不保存时区、开始/结束
时间或典型日/周/年截取区间——计算序列统一使用从 ``0`` 开始的连续 ``step``,
全周期点数由 ``resolution`` 与 ``leap_year`` 唯一推导。

多场景与已有项目基线变更属于 1.0.0 之后的 Roadmap 能力; 本项目只实现
``single`` 场景模式。

本模块只依赖标准库与 ``core.diagnostics``, 不导入任何业务模块
(core/contracts 边界, 宪法 4.1)。
"""

from __future__ import annotations

import hashlib
import json
import re
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

#: 基线规范化算法 ID 与版本(写入摘要; 语义变化必须升版本)。
BASELINE_CANON_ALGORITHM_ID: Final[str] = "ies.project_baseline.canonical"
BASELINE_CANON_ALGORITHM_VERSION: Final[str] = "1.0.0"

#: 分辨率 → (普通年 365 天点数, 闰年 366 天点数)。
_POINT_COUNTS: Final[dict[str, tuple[int, int]]] = {
    "15min": (365 * 24 * 4, 366 * 24 * 4),  # 35040 / 35136
    "30min": (365 * 24 * 2, 366 * 24 * 2),  # 17520 / 17568
    "1h": (365 * 24, 366 * 24),  # 8760 / 8784
}

#: 摘要必须为 64 位小写十六进制(严格恢复, 不允许截断/伪造摘要)。
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")

#: 基线字段白名单(严格恢复: 未知核心字段拒绝, 宪法 7.1)。
#: ``sha256`` 是 ``to_dict`` 携带的派生摘要字段, 恢复时接受并校验其一致性
#: (保证 ``from_dict(to_dict(x)) == x`` 自洽, 且摘要可校验)。
_BASELINE_FIELDS: Final[frozenset[str]] = frozenset(
    {"resolution", "leap_year", "scenario_mode", "sha256"}
)

#: 基线校验诊断码(登记于 core.diagnostics.NEW_DIAG_CODES)。
BASELINE_INVALID = "PROJ-BASE-001"


class ProjectBaselineError(ValueError):
    """项目计算基线校验失败(非法分辨率/场景模式/类型/未知字段)。"""


@dataclass(frozen=True, slots=True)
class ProjectBaseline:
    """项目计算基线(创建时一次性固定, 创建后不可修改)。

    属性:
        resolution: 全项目统一计算分辨率('15min' | '30min' | '1h')。
        leap_year: 是否按 366 天(闰年)生成全周期序列(False=普通年 365 天)。
        scenario_mode: 场景模式; 当前固定为 'single'。
    """

    resolution: str
    leap_year: bool
    scenario_mode: str = DEFAULT_SCENARIO_MODE

    def __post_init__(self) -> None:
        if self.resolution not in RESOLUTION_VALUES:
            raise ProjectBaselineError(
                f"非法基线分辨率: {self.resolution!r}, 允许值 {sorted(RESOLUTION_VALUES)}",
            )
        if not isinstance(self.leap_year, bool):
            raise ProjectBaselineError(
                f"leap_year 必须是布尔值, 实际 {type(self.leap_year).__name__}",
            )
        if self.scenario_mode not in SCENARIO_MODES:
            raise ProjectBaselineError(
                f"非法场景模式: {self.scenario_mode!r}, 允许值 {sorted(SCENARIO_MODES)}",
            )

    @property
    def point_count(self) -> int:
        """普通年/闰年全周期点数(由 resolution 与 leap_year 唯一推导)。"""
        normal, leap = _POINT_COUNTS[self.resolution]
        return leap if self.leap_year else normal

    def canonical_payload(self) -> str:
        """规范化字节负载(稳定键序 + 紧凑 JSON, 摘要计算输入)。"""
        return json.dumps(
            {
                "resolution": self.resolution,
                "leap_year": self.leap_year,
                "scenario_mode": self.scenario_mode,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def digest(self) -> str:
        """确定性 SHA-256 摘要(算法 ID/版本前缀 + 规范化负载, 宪法 7.7)。"""
        return hashlib.sha256(
            (
                f"{BASELINE_CANON_ALGORITHM_ID}@{BASELINE_CANON_ALGORITHM_VERSION}\n"
                f"{self.canonical_payload()}"
            ).encode("utf-8")
        ).hexdigest()

    def to_dict(self) -> dict:
        """公开字典形态(含摘要; API/持久化/装配共用的唯一序列化)。"""
        return {
            "resolution": self.resolution,
            "leap_year": self.leap_year,
            "scenario_mode": self.scenario_mode,
            "sha256": self.digest(),
        }

    @classmethod
    def from_dict(cls, mapping: object) -> "ProjectBaseline":
        """严格恢复: 未知字段拒绝; resolution/leap_year 缺失拒绝; 枚举之外拒绝。

        scenario_mode 缺失时取默认 'single'(文档化默认值)。``sha256`` 为
        ``to_dict`` 携带的派生摘要字段: 缺失时允许(摘要由 digest() 重新计算),
        存在时必须与当前规范化算法摘要一致, 否则拒绝(防伪造/防摘要漂移)。
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
        missing = {"resolution", "leap_year"} - set(mapping)
        if missing:
            raise ProjectBaselineError(
                f"基线缺少必需字段: {sorted(missing)}",
            )
        leap_raw = mapping["leap_year"]
        if not isinstance(leap_raw, bool):
            raise ProjectBaselineError(
                f"leap_year 必须是布尔值, 实际 {type(leap_raw).__name__}",
            )
        baseline = cls(
            resolution=str(mapping["resolution"]),
            leap_year=leap_raw,
            scenario_mode=str(mapping.get("scenario_mode", DEFAULT_SCENARIO_MODE)),
        )
        declared_sha256 = mapping.get("sha256")
        if declared_sha256 is not None and str(declared_sha256) != baseline.digest():
            raise ProjectBaselineError(
                f"基线摘要与规范化算法不一致: 声明 {declared_sha256!r}, "
                f"期望 {baseline.digest()}",
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
        leap_year = mapping.get("leap_year")
        if leap_year is None:
            diags.append(
                make_diag(
                    BASELINE_INVALID,
                    params={"detail": "缺少必需字段: leap_year"},
                    location={"object_type": "project_baseline", "field": "leap_year"},
                )
            )
        elif not isinstance(leap_year, bool):
            diags.append(
                make_diag(
                    BASELINE_INVALID,
                    params={"detail": "leap_year 必须是布尔值"},
                    location={"object_type": "project_baseline", "field": "leap_year"},
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
        # 派生摘要字段: 提供时必须为 64 位小写十六进制且与规范化算法摘要一致。
        declared_sha256 = mapping.get("sha256")
        if declared_sha256 is not None:
            if (
                not isinstance(declared_sha256, str)
                or not _SHA256_RE.fullmatch(declared_sha256)
            ):
                diags.append(
                    make_diag(
                        BASELINE_INVALID,
                        params={"detail": "sha256 必须是 64 位小写十六进制字符串"},
                        location={"object_type": "project_baseline", "field": "sha256"},
                    )
                )
            elif not diags:
                try:
                    expected = cls(
                        resolution=str(mapping["resolution"]),
                        leap_year=bool(mapping["leap_year"]),
                        scenario_mode=str(scenario_mode),
                    ).digest()
                except ProjectBaselineError:
                    expected = ""
                if declared_sha256 != expected:
                    diags.append(
                        make_diag(
                            BASELINE_INVALID,
                            params={
                                "detail": f"基线摘要与规范化算法不一致: "
                                f"声明 {declared_sha256!r}, 期望 {expected}",
                            },
                            location={"object_type": "project_baseline", "field": "sha256"},
                        )
                    )
        return diags
