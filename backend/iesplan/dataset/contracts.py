"""数据集域公开契约(datasets/dataset_versions/dataset_files 表，归属 dataset)。

- 文件正文在对象存储，本域只持有对象 id 引用（寻址键为对象 id）；
- 只含不可变值对象与领域错误；不导入 ORM、Session、services 或 application。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from iesplan.core.errors import ConflictError, NotFoundError


class DatasetNotFoundError(NotFoundError):
    """数据集/版本/文件不存在（沿用基类诊断码，不新增码）。"""


class DatasetConflictError(ConflictError):
    """数据集唯一冲突或状态冲突（沿用基类诊断码，不新增码）。"""


@dataclass(frozen=True, slots=True)
class DatasetRecord:
    """数据集元数据（datasets 表公开视图）。"""

    id: int
    name: str
    project_id: int | None = None
    description: str | None = None
    status: str = "draft"
    default_license: str | None = None
    created_by: int = 0


@dataclass(frozen=True, slots=True)
class DatasetVersionRecord:
    """数据集版本（dataset_versions 表公开视图，不可变）。"""

    id: int
    dataset_id: int
    version_no: int
    timeline: str
    fixed_utc_offset_minutes: int
    fields: dict[str, Any]
    units: dict[str, Any]
    resolution: str | None = None
    quality_report: dict[str, Any] | None = None
    provenance: dict[str, Any] | None = None
    license: str | None = None
    created_by: int = 0
    created_reason: str | None = None


@dataclass(frozen=True, slots=True)
class DatasetFileRecord:
    """数据集版本文件（dataset_files 表公开视图，指向对象 id，不可变）。"""

    id: int
    dataset_version_id: int
    object_id: int
    file_kind: str
    format: str
    row_count: int = 0
    size_bytes: int = 0
