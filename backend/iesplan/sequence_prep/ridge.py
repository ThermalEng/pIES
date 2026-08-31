"""``ies.predict.ridge@1.0.0``: data_predict 唯一固定默认预测算法。

契约(device-data-csv.md「step 与序列预备规则」与 AI 需求确认决策):

- 带截距的岭回归; 数值特征按**训练集**均值和**总体标准差**(ddof=0)确定性
  标准化, 零方差特征标准化为 ``0``;
- 正则系数固定 ``alpha=1.0`` 且**不惩罚截距**(特征标准化后训练集均值为零,
  截距即训练目标均值, 与 scikit-learn Ridge(fit_intercept=True) 同解);
- 每个目标列独立训练; 随机种子字段固定 ``42``(算法不使用随机抽样);
- 训练产物保存各特征均值/标准差、截距和回归系数, 学习参数不写入设备模型;
- 相同规范输入在固定依赖版本下必须产生逐字节相同的训练产物与预测序列。

求解使用闭式解 ``(ZᵀZ + αI)⁻¹ Zᵀỹ``(numpy 确定性线性代数), 不引入额外
依赖; 矩阵求解对相同输入在相同依赖环境下逐字节确定。本模块是纯函数,
不访问数据库/文件系统/网络。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Final

import numpy as np

#: 算法稳定 ID 与精确版本(写入训练产物与预备回执)
ALGORITHM_ID: Final[str] = "ies.predict.ridge"
ALGORITHM_VERSION: Final[str] = "1.0.0"
#: 正则系数(固定, 不进入用户配置)
ALPHA: Final[float] = 1.0
#: 随机种子字段(固定; 算法不使用随机抽样)
SEED: Final[int] = 42
#: 训练产物 schema 标识与版本
ARTIFACT_SCHEMA: Final[str] = "ies.predict.artifact"
ARTIFACT_SCHEMA_VERSION: Final[str] = "1.0.0"
#: 标准化规则标识(写入产物, 稳定契约)
STANDARDIZATION_RULE: Final[str] = "train_mean_population_std_zero_variance_zero"

#: 规范化 JSON 选项(稳定键序、紧凑、非 ASCII 保留、禁非有限值)
_CANONICAL_KWARGS: dict[str, Any] = {
    "ensure_ascii": False,
    "sort_keys": True,
    "separators": (",", ":"),
    "allow_nan": False,
}


@dataclass(frozen=True, slots=True)
class RidgeModel:
    """已训练的不可变岭回归模型(训练产物数据面)。"""

    feature_order: tuple[str, ...]
    target_order: tuple[str, ...]
    means: dict[str, float]       # 特征训练集均值
    stds: dict[str, float]        # 特征训练集总体标准差(ddof=0)
    zero_variance: dict[str, bool]
    coefficients: dict[str, dict[str, float]]  # 目标 → 特征 → 系数
    intercepts: dict[str, float]  # 目标 → 截距(训练目标均值)
    n_train_rows: int


def train_ridge(features: np.ndarray, targets: np.ndarray) -> RidgeModel:
    """确定性训练带截距岭回归(特征已在调用方按契约声明顺序排列)。

    参数:
        features: (n, p) float64 训练特征矩阵。
        targets: (n, k) float64 训练目标矩阵(每列独立训练)。
    返回:
        RidgeModel(含训练集统计量与系数)。
    异常:
        ValueError: 行数不一致、空数据或非有限值(由调用方转为阻断诊断)。
    """
    if features.ndim != 2 or targets.ndim != 2:
        raise ValueError(f"训练输入必须为二维矩阵, 实际 {features.ndim}D / {targets.ndim}D")
    if features.shape[0] == 0 or targets.shape[0] == 0:
        raise ValueError("训练输入与训练目标不能为空")
    if features.shape[0] != targets.shape[0]:
        raise ValueError(
            f"训练输入行数 {features.shape[0]} 与训练目标行数 {targets.shape[0]} 不一致"
        )
    if not (np.isfinite(features).all() and np.isfinite(targets).all()):
        raise ValueError("训练输入或训练目标含非有限值")
    n, p = features.shape
    k = targets.shape[1]

    means = np.mean(features, axis=0)
    stds = np.std(features, axis=0, ddof=0)
    zero_var = stds == 0.0
    # 零方差特征标准化为 0; 其余按训练集均值/总体标准差标准化
    z = np.where(zero_var, 0.0, (features - means) / np.where(zero_var, 1.0, stds))
    # 不惩罚截距: 特征零均值 ⇒ 截距 = 训练目标均值, 系数在中心化目标上求解
    y_mean = np.mean(targets, axis=0)
    y_centered = targets - y_mean
    gram = z.T @ z + ALPHA * np.eye(p)
    weights = np.linalg.solve(gram, z.T @ y_centered)  # (p, k) 确定性闭式解

    return RidgeModel(
        feature_order=tuple(str(i) for i in range(p)),
        target_order=tuple(str(i) for i in range(k)),
        means={str(i): float(v) for i, v in enumerate(means)},
        stds={str(i): float(v) for i, v in enumerate(stds)},
        zero_variance={str(i): bool(v) for i, v in enumerate(zero_var)},
        coefficients={
            str(j): {str(i): float(weights[i, j]) for i in range(p)} for j in range(k)
        },
        intercepts={str(j): float(y_mean[j]) for j in range(k)},
        n_train_rows=n,
    )


def predict_ridge(model: RidgeModel, features: np.ndarray) -> np.ndarray:
    """用训练好的模型对预测特征输出各目标预测值。

    预测特征按**训练集**均值/总体标准差标准化(零方差特征为 0)。
    返回 (m, k) float64; 特征列数须与训练一致(调用方保证列序)。
    """
    if features.ndim != 2 or features.shape[1] != len(model.feature_order):
        raise ValueError(
            f"预测输入列数 {features.shape[1]} 与训练特征数 {len(model.feature_order)} 不一致"
        )
    if not np.isfinite(features).all():
        raise ValueError("预测输入含非有限值")
    p = features.shape[1]
    means = np.asarray([model.means[str(i)] for i in range(p)], dtype=np.float64)
    stds = np.asarray([model.stds[str(i)] for i in range(p)], dtype=np.float64)
    zero_var = np.asarray([model.zero_variance[str(i)] for i in range(p)], dtype=bool)
    z = np.where(zero_var, 0.0, (features - means) / np.where(zero_var, 1.0, stds))
    k = len(model.target_order)
    weights = np.asarray(
        [[model.coefficients[str(j)][str(i)] for i in range(p)] for j in range(k)],
        dtype=np.float64,
    ).T  # (p, k)
    intercepts = np.asarray([model.intercepts[str(j)] for j in range(k)], dtype=np.float64)
    return z @ weights + intercepts


def artifact_payload(model: RidgeModel) -> str:
    """训练产物规范化负载(稳定键序紧凑 JSON; 摘要计算输入)。"""
    return json.dumps(
        {
            "schema": ARTIFACT_SCHEMA,
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "algorithm_id": ALGORITHM_ID,
            "algorithm_version": ALGORITHM_VERSION,
            "alpha": ALPHA,
            "seed": SEED,
            "standardization": STANDARDIZATION_RULE,
            "n_train_rows": model.n_train_rows,
            "feature_order": list(model.feature_order),
            "target_order": list(model.target_order),
            "features": {
                name: {
                    "mean": model.means[name],
                    "std": model.stds[name],
                    "zero_variance": model.zero_variance[name],
                }
                for name in model.feature_order
            },
            "coefficients": {
                target: dict(model.coefficients[target]) for target in model.target_order
            },
            "intercepts": {target: model.intercepts[target] for target in model.target_order},
        },
        **_CANONICAL_KWARGS,
    )


def artifact_bytes(model: RidgeModel) -> bytes:
    """训练产物规范字节(UTF-8; 内容寻址对象保存形态)。"""
    return artifact_payload(model).encode()


def artifact_sha256(model: RidgeModel) -> str:
    """训练产物确定性 SHA-256(算法 ID/版本前缀 + 规范负载, 宪法 7.7)。"""
    return hashlib.sha256(
        (f"{ALGORITHM_ID}@{ALGORITHM_VERSION}\n{artifact_payload(model)}").encode()
    ).hexdigest()


__all__ = [
    "ALGORITHM_ID",
    "ALGORITHM_VERSION",
    "ALPHA",
    "SEED",
    "ARTIFACT_SCHEMA",
    "ARTIFACT_SCHEMA_VERSION",
    "STANDARDIZATION_RULE",
    "RidgeModel",
    "train_ridge",
    "predict_ridge",
    "artifact_bytes",
    "artifact_sha256",
]
