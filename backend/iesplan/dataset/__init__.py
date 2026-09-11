"""数据集域公开门面（datasets/dataset_versions/dataset_files 表，归属 dataset）。

外部只允许经本门面消费 contract 与 repository 协议；不得导入本域
repository 实现（切片 4 落实）、`iesplan.models` 或 services。
"""

from __future__ import annotations

from iesplan.dataset.contracts import (
    DatasetConflictError,
    DatasetFileRecord,
    DatasetNotFoundError,
    DatasetRecord,
    DatasetVersionRecord,
)
from iesplan.dataset.repository import DatasetRepository

__all__ = [
    "DatasetConflictError",
    "DatasetFileRecord",
    "DatasetNotFoundError",
    "DatasetRecord",
    "DatasetRepository",
    "DatasetVersionRecord",
]
