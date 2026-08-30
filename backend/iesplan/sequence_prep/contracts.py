"""序列预备公开契约(0.6.5 事项 3): 物理量语义、重采样规则与产物类型。

宪法 7.5 与 [device-data-csv.md](../../../manual/developer-guide/zh-CN/formats/device-data-csv.md)
「step 与序列预备规则」: 原始输入文件可以声明不同于项目基线的采样间隔, 但
必须在序列预备阶段按公开物理量语义完成重采样/聚合, 生成全周期、等间隔、
从 0 开始连续 ``step`` 的计算序列及变换回执。

列语义固定为三类(瞬时/强度量、区间累计量、状态/离散量), 每类在目标步长
变长/变短时的方法唯一固定, 由 ``RESAMPLE_RULES`` 公开声明; 语义缺失或未知
时预备必须结构化阻断(``DATA-PREP-001``), 不允许回退到"全部均值"或"全部
线性插值"。``data_predict`` 唯一固定算法 ``ies.predict.ridge@1.0.0``, 显式
训练输入/训练目标/预测输入, 不进入用户配置或算法选择器。

本模块只依赖标准库与 ``core.diagnostics``, 不访问数据库/文件系统/网络。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from iesplan.core.diagnostics import Diagnostic

# ---------------------------------------------------------------------------
# 物理量语义(公共物理量登记表: 三类固定语义; 语义不明确必须阻断)
# ---------------------------------------------------------------------------

#: 瞬时量或强度量(如功率、温度、压力)
SEMANTICS_INSTANTANEOUS: Final[str] = "instantaneous"
#: 区间累计量(如区间能量、质量、费用)
SEMANTICS_CUMULATIVE: Final[str] = "cumulative"
#: 状态量或离散量(如开关、档位、运行状态)
SEMANTICS_STATE: Final[str] = "state"

#: 全部合法列语义(小写 snake_case, 宪法 7.1)
QUANTITY_SEMANTICS: Final[tuple[str, ...]] = (
    SEMANTICS_INSTANTANEOUS,
    SEMANTICS_CUMULATIVE,
    SEMANTICS_STATE,
)

#: 目标步长相对源步长方向
_DIRECTION_COARSER: Final[str] = "coarser"  # 目标步长变长(降采样)
_DIRECTION_FINER: Final[str] = "finer"      # 目标步长变短(升采样)

#: 重采样方法标识(写入变换回执, 稳定契约)
METHOD_TIME_WEIGHTED_MEAN: Final[str] = "time_weighted_mean"
METHOD_LINEAR_INTERPOLATION: Final[str] = "linear_interpolation"
METHOD_SUM: Final[str] = "sum"
METHOD_PRO_RATA: Final[str] = "pro_rata_split"
METHOD_INTERVAL_LAST: Final[str] = "interval_last"
METHOD_FORWARD_HOLD: Final[str] = "forward_hold"
METHOD_NONE: Final[str] = "none"  # 源分辨率 == 目标分辨率

#: 固定分类与方法表(唯一权威; 语义 → 方向 → 方法)
RESAMPLE_RULES: Final[Mapping[str, Mapping[str, str]]] = {
    SEMANTICS_INSTANTANEOUS: {
        _DIRECTION_COARSER: METHOD_TIME_WEIGHTED_MEAN,
        _DIRECTION_FINER: METHOD_LINEAR_INTERPOLATION,
    },
    SEMANTICS_CUMULATIVE: {
        _DIRECTION_COARSER: METHOD_SUM,
        _DIRECTION_FINER: METHOD_PRO_RATA,
    },
    SEMANTICS_STATE: {
        _DIRECTION_COARSER: METHOD_INTERVAL_LAST,
        _DIRECTION_FINER: METHOD_FORWARD_HOLD,
    },
}

#: 其他变换方法标识(展开/预测, 写入回执)
METHOD_CONSTANT_EXPAND: Final[str] = "constant_expand"
METHOD_PERIOD_EXPAND: Final[str] = "period_expand"
METHOD_RIDGE_PREDICT: Final[str] = "ridge_predict"

#: 预备回执 schema 标识与版本(规范化摘要前缀, 宪法 7.7)
RECEIPT_SCHEMA: Final[str] = "ies.sequence-prep.receipt"
RECEIPT_SCHEMA_VERSION: Final[str] = "1.0.0"
#: 预备域实现/规范化算法标识(写入回执与摘要)
PREP_CANON_ALGORITHM_ID: Final[str] = "ies.sequence_prep.prepare"
PREP_CANON_ALGORITHM_VERSION: Final[str] = "1.0.0"


# ---------------------------------------------------------------------------
# 预备输入契约
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DataRepeatSpec:
    """``data_repeat`` 预备规格: 每列显式声明物理量语义。

    ``semantics``: 文件数值列(interface ID) → 三类语义之一; 缺失或未知列在
    预备时返回 ``DATA-PREP-001`` 阻断诊断。
    """

    semantics: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PredictFile:
    """``data_predict`` 显式输入文件(训练输入/训练目标/预测输入)。"""

    data: bytes
    sha256: str  # 输入字节 SHA-256(与预备时重新计算的摘要核对, 摘要链闭合)


@dataclass(frozen=True, slots=True)
class PredictSpec:
    """``data_predict`` 预备规格: 显式输入引用、特征列与特征语义。

    - ``data_ref``: 绑定的 ``data_predict`` 接口数据引用(预备输出按该引用
      锁定列/单位/有效区间, 目标列必须与该引用下的接口完全一致);
    - ``training_input_ref`` / ``training_target_ref`` / ``prediction_input_ref``:
      三个显式输入文件引用(应用层按引用解析对象, 域服务只消费字节);
    - ``feature_columns``: 训练输入/预测输入的特征列(契约声明顺序, 预测
      输入不得重新排列特征顺序);
    - ``feature_semantics``: 特征列 → 三类语义之一(预测输入重采样用;
      缺失或未知列返回 ``DATA-PREP-001`` 阻断)。
    """

    data_ref: str
    training_input_ref: str
    training_target_ref: str
    prediction_input_ref: str
    feature_columns: tuple[str, ...] = ()
    feature_semantics: Mapping[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 预备产物契约
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreparedSequence:
    """一次成功预备的不可变产物: 计算用文件字节 + 变换回执 + (可选)训练产物。

    全部内容寻址: ``canonical_sha256`` 为 ``ies.device-data`` 2.0.0 规范表格
    摘要(经 devices 规范化器重新校验闭合); ``receipt_sha256`` 为回执规范化
    摘要; ``training_artifact_bytes/sha256`` 仅 ``data_predict`` 存在。
    域服务只在全部校验通过后构造本对象(失败返回诊断, 不产出部分产物)。
    """

    canonical_bytes: bytes
    canonical_sha256: str
    receipt: Mapping[str, Any]
    receipt_sha256: str
    training_artifact_bytes: bytes | None = None
    training_artifact_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class PrepOutcome:
    """预备结果: 要么带不可变产物, 要么带结构化阻断诊断(不抛异常)。"""

    ok: bool
    diagnostics: list[Diagnostic] = field(default_factory=list)
    result: PreparedSequence | None = None


__all__ = [
    "SEMANTICS_INSTANTANEOUS",
    "SEMANTICS_CUMULATIVE",
    "SEMANTICS_STATE",
    "QUANTITY_SEMANTICS",
    "RESAMPLE_RULES",
    "METHOD_TIME_WEIGHTED_MEAN",
    "METHOD_LINEAR_INTERPOLATION",
    "METHOD_SUM",
    "METHOD_PRO_RATA",
    "METHOD_INTERVAL_LAST",
    "METHOD_FORWARD_HOLD",
    "METHOD_NONE",
    "METHOD_CONSTANT_EXPAND",
    "METHOD_PERIOD_EXPAND",
    "METHOD_RIDGE_PREDICT",
    "RECEIPT_SCHEMA",
    "RECEIPT_SCHEMA_VERSION",
    "PREP_CANON_ALGORITHM_ID",
    "PREP_CANON_ALGORITHM_VERSION",
    "DataRepeatSpec",
    "PredictFile",
    "PredictSpec",
    "PreparedSequence",
    "PrepOutcome",
]
