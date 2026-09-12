"""数据集域公开门面（datasets/dataset_versions/dataset_files 表，归属 dataset）。

外部只允许经本门面消费 contract、repository 协议与 repository 实现函数；
不得导入 `iesplan.models`、services 或其他域的内部模块。
"""

from __future__ import annotations

from iesplan.dataset import persistence
from iesplan.dataset.contracts import (
    DatasetConflictError,
    DatasetFileRecord,
    DatasetNotFoundError,
    DatasetRecord,
    DatasetVersionRecord,
)
from iesplan.dataset.repository import DatasetRepository

add_file = persistence.add_file
create_dataset = persistence.create_dataset
create_version = persistence.create_version
get_dataset = persistence.get_dataset
get_latest_version = persistence.get_latest_version
get_version = persistence.get_version
get_version_by_no = persistence.get_version_by_no
list_datasets = persistence.list_datasets
list_files = persistence.list_files
list_versions = persistence.list_versions
set_dataset_status = persistence.set_dataset_status

__all__ = [
    "DatasetConflictError",
    "DatasetFileRecord",
    "DatasetNotFoundError",
    "DatasetRecord",
    "DatasetRepository",
    "DatasetVersionRecord",
    "add_file",
    "create_dataset",
    "create_version",
    "get_dataset",
    "get_latest_version",
    "get_version",
    "get_version_by_no",
    "list_datasets",
    "list_files",
    "list_versions",
    "set_dataset_status",
]
