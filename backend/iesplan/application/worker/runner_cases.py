"""Worker 输入装配用例(application/worker.runner_cases): 快照输入读取转调。

本模块收拢 ``iesplan.worker.runner`` 原先对领域服务的直接调用, 只做
转调, 不新增校验/hash/回退:

- 项目版本内容读取 → ``services.project.load_content_object``;
- 数据集对象字节读取 → ``storage.get_object``;
- 数据集 CSV 解析 → ``services.dataset.parse_csv``。

快照行/版本行/文件行的 ORM 读取仍在 ``worker.runner``(门禁 8 白名单
基线), 本模块不导入 ``models.*``。

依赖方向: worker → application → (services/storage)。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from iesplan.services import dataset as dataset_service
from iesplan.services import project as project_service
from iesplan.storage import get_object


def load_version_content(db: Session, content_object_id: int) -> dict:
    """按对象 id 读取项目版本内容对象(转调 project 服务)。"""
    return project_service.load_content_object(db, content_object_id)


def load_dataset_blob(db: Session, object_id: int) -> bytes:
    """按对象 id 读取数据集文件字节(转调 storage 公开门面)。"""
    return get_object(db, object_id)


def parse_dataset_csv(raw: bytes, resolution: str) -> tuple[list[dict], list]:
    """解析数据集 CSV(转调 dataset 服务, 返回 (rows, diagnostics) 原样)。"""
    return dataset_service.parse_csv(raw, resolution)
