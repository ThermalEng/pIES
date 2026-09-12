"""数据集域 repository 协议（datasets/dataset_versions/dataset_files）。

实现规则（切片 4 落实）：
- 只做查询、写入、flush；绝不 commit/rollback；
- 文件正文读写走 storage（对象 id），本协议只维护引用行；
- 数据内容校验（CSV/YAML 字头与领域约束）是用户输入边界，归领域服务，
  不在本协议。
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy.orm import Session

from iesplan.dataset.contracts import DatasetFileRecord, DatasetRecord, DatasetVersionRecord


class DatasetRepository(Protocol):
    """数据集聚合 repository 协议（无状态方法组，db 由调用方事务拥有）。"""

    def get_dataset(self, db: Session, dataset_id: int) -> DatasetRecord | None: ...

    def list_datasets(self, db: Session, project_id: int) -> list[DatasetRecord]:
        """列出项目数据集（含 project_id 为空的共享数据集）。"""
        ...

    def create_dataset(
        self,
        db: Session,
        *,
        name: str,
        created_by: int,
        project_id: int | None = None,
        description: str | None = None,
        default_license: str | None = None,
        source_category: str | None = None,
        default_provenance: dict[str, Any] | None = None,
    ) -> DatasetRecord:
        """创建数据集；同范围重名抛 DatasetConflictError。"""
        ...

    def set_dataset_status(self, db: Session, dataset_id: int, status: str) -> DatasetRecord: ...

    def get_version(self, db: Session, version_id: int) -> DatasetVersionRecord | None: ...

    def list_versions(self, db: Session, dataset_id: int) -> list[DatasetVersionRecord]:
        """列出版本（version_no 倒序）。"""
        ...

    def get_latest_version(self, db: Session, dataset_id: int) -> DatasetVersionRecord | None: ...

    def get_version_by_no(
        self, db: Session, dataset_id: int, version_no: int
    ) -> DatasetVersionRecord | None: ...

    def create_version(
        self,
        db: Session,
        *,
        dataset_id: int,
        timeline: str,
        fixed_utc_offset_minutes: int,
        fields: dict[str, Any],
        units: dict[str, Any],
        created_by: int,
        resolution: str | None = None,
        quality_report: dict[str, Any] | None = None,
        provenance: dict[str, Any] | None = None,
        license: str | None = None,
        created_reason: str | None = None,
    ) -> DatasetVersionRecord:
        """追加不可变版本（含 version_no 分配）。"""
        ...

    def add_file(
        self,
        db: Session,
        *,
        dataset_version_id: int,
        object_id: int,
        file_kind: str,
        format: str,
        row_count: int = 0,
        size_bytes: int = 0,
    ) -> DatasetFileRecord:
        """登记版本文件引用；重复对象抛 DatasetConflictError。"""
        ...

    def list_files(self, db: Session, version_id: int) -> list[DatasetFileRecord]: ...
