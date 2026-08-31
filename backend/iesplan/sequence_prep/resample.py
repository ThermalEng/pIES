"""纯重采样引擎: 按公开物理量语义把任意分辨率序列转到目标分辨率。

规则表(device-data-csv.md「step 与序列预备规则」, 唯一权威见
``contracts.RESAMPLE_RULES``):

| 列语义 | 目标步长变长 | 目标步长变短 |
| --- | --- | --- |
| 瞬时/强度量 | 时间覆盖加权平均 | 线性插值 |
| 区间累计量 | 求和并保持区间总量 | 按子区间时长比例分配, 保持总量 |
| 状态/离散量 | 取目标区间内最后一个观测值 | 前向保持 |

两个分辨率同为 {15,30,60} 分钟, 目标网格总是与源网格对齐(采样比为整数);
不能整除时返回 ``DATA-PREP-002`` 阻断诊断。序列预备只按 step 序号与分辨率
计算, 不使用时间戳/时区(core/timeaxis.py 不可修改, 本模块也不引用它)。

本模块是纯函数: 不访问数据库、文件系统与业务模块。
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from iesplan.core.diagnostics import Diagnostic, make_diag
from iesplan.core.timeaxis import RESOLUTIONS
from iesplan.sequence_prep.contracts import (
    QUANTITY_SEMANTICS,
    RESAMPLE_RULES,
    SEMANTICS_CUMULATIVE,
    SEMANTICS_INSTANTANEOUS,
    SEMANTICS_STATE,
)

#: 诊断码(登记于 core/diagnostics.py NEW_DIAG_CODES)
DATA_PREP_SEMANTICS_MISSING = "DATA-PREP-001"
DATA_PREP_GRID_MISALIGNED = "DATA-PREP-002"


def _step_minutes(resolution: str) -> int:
    """分辨率 → 每步分钟数。"""
    if resolution not in RESOLUTIONS:
        raise ValueError(f"非法分辨率: {resolution!r},允许值 {sorted(RESOLUTIONS)}")
    return RESOLUTIONS[resolution][1]


def resample_method(
    semantics: str, source_resolution: str, target_resolution: str
) -> tuple[str | None, list[Diagnostic]]:
    """解析语义 → 重采样方法(源==目标时返回 ``none``)。

    返回 (method, diagnostics): 语义未知/缺失时返回 (None, [DATA-PREP-001
    阻断诊断]); 否则 diagnostics 为空。
    """
    if semantics not in QUANTITY_SEMANTICS:
        return None, [
            make_diag(
                DATA_PREP_SEMANTICS_MISSING,
                blocking=True,
                params={"column": "", "allowed": sorted(QUANTITY_SEMANTICS), "actual": semantics},
                location={"object_type": "sequence_prep", "field": "semantics"},
            )
        ]
    if source_resolution == target_resolution:
        return "none", []
    direction = "coarser" if _step_minutes(target_resolution) > _step_minutes(source_resolution) else "finer"
    return RESAMPLE_RULES[semantics][direction], []


def resample_series(
    values: Sequence[float],
    source_resolution: str,
    target_resolution: str,
    semantics: str,
) -> tuple[list[float] | None, list[Diagnostic]]:
    """把一列序列按公开语义重采样到目标分辨率(纯确定性函数)。

    参数:
        values: 源序列(有限浮点数, 长度即源点数; 校验由调用方负责)。
        source_resolution: 源分辨率('15min' | '30min' | '1h')。
        target_resolution: 目标分辨率。
        semantics: 列物理量语义(contracts.QUANTITY_SEMANTICS 之一)。
    返回:
        (values, diagnostics): 成功时 values 为长度推导的目标序列;
        语义未知或网格无法对齐时返回 (None, [阻断诊断])。
    """
    method, diags = resample_method(semantics, source_resolution, target_resolution)
    if diags:
        return None, diags
    src = np.asarray(values, dtype=np.float64)
    if method == "none":
        return [float(v) for v in src.tolist()], []

    s_min = _step_minutes(source_resolution)
    t_min = _step_minutes(target_resolution)
    n = src.shape[0]
    if t_min > s_min:
        m = t_min // s_min
        if n % m != 0:
            return None, [
                make_diag(
                    DATA_PREP_GRID_MISALIGNED,
                    blocking=True,
                    params={
                        "detail": (
                            f"降采样点数 {n} 不能被采样比 {m} 整除"
                            f"({source_resolution}→{target_resolution})"
                        ),
                        "source_resolution": source_resolution,
                        "target_resolution": target_resolution,
                        "n": n,
                        "ratio": m,
                    },
                    location={"object_type": "sequence_prep", "field": "steps"},
                )
            ]
        blocks = src[: n - n % m].reshape(-1, m)
        if semantics == SEMANTICS_INSTANTANEOUS:
            out = blocks.mean(axis=1)  # 时间覆盖加权平均(等长区间, 即算术平均)
        elif semantics == SEMANTICS_CUMULATIVE:
            out = blocks.sum(axis=1)  # 求和并保持区间总量
        elif semantics == SEMANTICS_STATE:
            out = blocks[:, -1]  # 目标区间内最后一个观测值
        else:  # pragma: no cover - 语义已在 resample_method 校验
            raise AssertionError(f"未覆盖语义: {semantics}")
        return [float(v) for v in out.tolist()], []

    m = s_min // t_min
    n_out = n * m
    out = np.empty(n_out, dtype=np.float64)
    if semantics == SEMANTICS_INSTANTANEOUS:
        # 线性插值: 目标 j 位于源区间 i=j//m 内, 偏移 f=(j%m)/m;
        # 最后一个源区间之后按末值前向保持(线性插值的自然延伸)。
        src_ext = np.concatenate([src, src[-1:]])
        idx = np.arange(n_out) // m
        frac = (np.arange(n_out) % m) / m
        out = src_ext[idx] + (src_ext[idx + 1] - src_ext[idx]) * frac
    elif semantics == SEMANTICS_CUMULATIVE:
        # 按时长比例分配: 源区间总量按子区间等分(等长子区间, 每份 = 总量/m)
        out = np.repeat(src, m) / m
    else:  # SEMANTICS_STATE
        # 前向保持: 源区间内的每个目标子区间取源值
        out = np.repeat(src, m)
    return [float(v) for v in out.tolist()], []


__all__ = ["resample_method", "resample_series"]
