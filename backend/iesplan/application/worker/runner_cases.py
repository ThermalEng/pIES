"""Worker 输入装配用例(application/worker.runner_cases): 快照输入读取转调。

本模块收拢 ``iesplan.worker.runner`` 原先对领域服务与 ORM 行的直接
访问, 只做转调与行级读取搬运, 不新增校验/hash/回退:

- 项目版本内容读取 → ``application.projects.content_objects.load_content_object``;
- 数据集对象字节读取 → ``storage.get_object``;
- 数据集 CSV 解析 → ``application.datasets.parse_csv``;
- 快照/任务行读 → ``lease_cases`` 共享读(同包复用);
- 项目版本内容指针 → tasks 域门面; 数据集版本/数据文件指针 →
  dataset 域门面; 样本计数 → tasks 域门面。

本模块不导入 ``models.*``。

依赖方向: worker → application → (storage/领域门面)。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from iesplan import dataset as dataset_domain
from iesplan import tasks as tasks_domain
from iesplan.application.datasets import parse_csv as _parse_dataset_csv
from iesplan.application.projects.content_objects import load_content_object
from iesplan.application.worker.lease_cases import get_snapshot_record, get_task_record
from iesplan.dataset import DatasetVersionRecord
from iesplan.storage import get_object

__all__ = [
    "count_completed_samples",
    "get_dataset_data_object",
    "get_dataset_version_record",
    "get_project_content_id",
    "get_snapshot_record",
    "get_task_record",
    "load_dataset_blob",
    "load_version_content",
    "parse_dataset_csv",
]


def load_version_content(db: Session, content_object_id: int) -> dict:
    """按对象 id 读取项目版本内容对象(转调 application/projects 内容对象用例)。"""
    return load_content_object(db, content_object_id)


def load_dataset_blob(db: Session, object_id: int) -> bytes:
    """按对象 id 读取数据集文件字节(转调 storage 公开门面)。"""
    return get_object(db, object_id)


def parse_dataset_csv(raw: bytes, resolution: str) -> tuple[list[dict], list]:
    """解析数据集 CSV(转调 datasets 用例, 返回 (rows, diagnostics) 原样)。"""
    return _parse_dataset_csv(raw, resolution)


def get_project_content_id(db: Session, version_id: int) -> int | None:
    """按主键取项目版本的内容对象 id; 版本缺失返回 None。"""
    return tasks_domain.get_project_version_content_id(db, version_id)


def get_dataset_version_record(db: Session, version_id: int) -> DatasetVersionRecord | None:
    """按主键取数据集版本公开视图; 不存在返回 None。"""
    return dataset_domain.get_version(db, version_id)


def get_dataset_data_object(db: Session, version_id: int) -> int | None:
    """取版本首个 data 文件的对象 id(id 升序首个; 无 data 文件返回 None)。"""
    files = dataset_domain.list_files(db, version_id)
    candidates = sorted(
        (f for f in files if f.file_kind == "data"), key=lambda f: f.id
    )
    return candidates[0].object_id if candidates else None


def count_completed_samples(db: Session, parent_task_id: int) -> int:
    """已完成样本数(父任务部分完成判定用; 无返回 0)。"""
    return tasks_domain.count_completed_samples(db, parent_task_id)
