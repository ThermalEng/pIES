"""数据集域 SQL repository 实现（datasets/dataset_versions/dataset_files）。

- 本模块是 `DatasetRepository` 协议的实现，可经 `iesplan.dataset` 门面调用；
- 只访问 `iesplan.models.dataset` 的表；绝不 commit/rollback（调用方事务拥有）；
- 唯一冲突转 `DatasetConflictError`；冲突后调用方须回滚会话
  （与旧 services 契约一致；实测 SA 2.0 下 flush 失败后会话不可继续）；
- 文件正文读写走 storage（对象 id），本模块只维护引用行；
- 时间以 `datetime.isoformat()` 原样映射为记录字符串。
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from iesplan.dataset.contracts import (
    DatasetConflictError,
    DatasetFileRecord,
    DatasetNotFoundError,
    DatasetRecord,
    DatasetVersionRecord,
)
from iesplan.models.dataset import Dataset, DatasetFile, DatasetVersion


def _iso(value: datetime | None) -> str | None:
    """ORM 时间 → 记录字符串（原样 isoformat，不增减时区后缀）。"""
    return value.isoformat() if value is not None else None


def _now() -> datetime:
    return datetime.now(UTC)


def _row_to_dataset(row: Dataset) -> DatasetRecord:
    return DatasetRecord(
        id=row.id,
        name=row.name,
        project_id=row.project_id,
        description=row.description,
        status=row.status,
        default_license=row.default_license,
        created_by=row.created_by,
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


def _row_to_version(row: DatasetVersion) -> DatasetVersionRecord:
    return DatasetVersionRecord(
        id=row.id,
        dataset_id=row.dataset_id,
        version_no=row.version_no,
        timeline=row.timeline,
        fixed_utc_offset_minutes=row.fixed_utc_offset_minutes,
        fields=row.fields,
        units=row.units,
        resolution=row.resolution,
        quality_report=row.quality_report,
        provenance=row.provenance,
        license=row.license,
        created_by=row.created_by,
        created_reason=row.created_reason,
        created_at=_iso(row.created_at),
    )


def _row_to_file(row: DatasetFile) -> DatasetFileRecord:
    return DatasetFileRecord(
        id=row.id,
        dataset_version_id=row.dataset_version_id,
        object_id=row.object_id,
        file_kind=row.file_kind,
        format=row.format,
        row_count=row.row_count,
        size_bytes=row.size_bytes,
    )


def get_dataset(db: Session, dataset_id: int) -> DatasetRecord | None:
    """按 id 取数据集；不存在返回 None。"""
    row = db.get(Dataset, dataset_id)
    return _row_to_dataset(row) if row is not None else None


def list_datasets(db: Session, project_id: int) -> list[DatasetRecord]:
    """列出项目数据集（含 project_id 为空的共享数据集），按 id 升序。"""
    rows = db.execute(
        select(Dataset)
        .where((Dataset.project_id == project_id) | (Dataset.project_id.is_(None)))
        .order_by(Dataset.id)
    ).scalars().all()
    return [_row_to_dataset(row) for row in rows]


def create_dataset(
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
    row = Dataset(
        project_id=project_id,
        name=name,
        description=description,
        status="draft",
        default_license=default_license,
        created_by=created_by,
    )
    if source_category is not None:
        row.source_category = source_category
    if default_provenance is not None:
        row.default_provenance = dict(default_provenance)
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise DatasetConflictError(
            "数据集名称重复", params={"name": name, "project_id": project_id}
        ) from exc
    return _row_to_dataset(row)


def set_dataset_status(db: Session, dataset_id: int, status: str) -> DatasetRecord:
    """切换数据集状态；数据集缺失抛 DatasetNotFoundError。"""
    row = db.get(Dataset, dataset_id)
    if row is None:
        raise DatasetNotFoundError("数据集不存在", params={"dataset_id": dataset_id})
    row.status = status
    row.updated_at = _now()
    db.flush()
    return _row_to_dataset(row)


def get_version(db: Session, version_id: int) -> DatasetVersionRecord | None:
    """按 id 取版本；不存在返回 None。"""
    row = db.get(DatasetVersion, version_id)
    return _row_to_version(row) if row is not None else None


def list_versions_by_ids(db: Session, version_ids: Collection[int]) -> list[DatasetVersionRecord]:
    """按 id 批量取版本；空输入返回空清单（绑定校验用）。"""
    if not version_ids:
        return []
    rows = (
        db.execute(select(DatasetVersion).where(DatasetVersion.id.in_(version_ids))).scalars().all()
    )
    return [_row_to_version(row) for row in rows]


def list_dataset_ids(db: Session, project_id: int) -> list[int]:
    """项目自有数据集 id（严格归属，不含共享数据集；绑定归属校验用）。"""
    return list(
        db.execute(
            select(Dataset.id).where(Dataset.project_id == project_id).order_by(Dataset.id)
        ).scalars()
    )


def list_versions(db: Session, dataset_id: int) -> list[DatasetVersionRecord]:
    """列出版本（version_no 倒序）。"""
    rows = db.execute(
        select(DatasetVersion)
        .where(DatasetVersion.dataset_id == dataset_id)
        .order_by(DatasetVersion.version_no.desc())
    ).scalars().all()
    return [_row_to_version(row) for row in rows]


def get_latest_version(db: Session, dataset_id: int) -> DatasetVersionRecord | None:
    """取最新版本；无版本返回 None。"""
    row = db.execute(
        select(DatasetVersion)
        .where(DatasetVersion.dataset_id == dataset_id)
        .order_by(DatasetVersion.version_no.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _row_to_version(row) if row is not None else None


def get_version_by_no(
    db: Session, dataset_id: int, version_no: int
) -> DatasetVersionRecord | None:
    """按 (dataset_id, version_no) 取版本；不存在返回 None。"""
    row = db.execute(
        select(DatasetVersion).where(
            DatasetVersion.dataset_id == dataset_id,
            DatasetVersion.version_no == version_no,
        )
    ).scalar_one_or_none()
    return _row_to_version(row) if row is not None else None


def create_version(
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
    """追加不可变版本（含 version_no 分配）；并发冲突抛 DatasetConflictError。"""
    max_no = db.execute(
        select(func.max(DatasetVersion.version_no)).where(DatasetVersion.dataset_id == dataset_id)
    ).scalar()
    row = DatasetVersion(
        dataset_id=dataset_id,
        version_no=(max_no or 0) + 1,
        timeline=timeline,
        resolution=resolution,
        fixed_utc_offset_minutes=fixed_utc_offset_minutes,
        fields=fields,
        units=units,
        quality_report=quality_report,
        provenance=provenance,
        license=license,
        created_by=created_by,
        created_reason=created_reason,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise DatasetConflictError(
            "数据集版本创建冲突", params={"dataset_id": dataset_id}
        ) from exc
    return _row_to_version(row)


def add_file(
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
    row = DatasetFile(
        dataset_version_id=dataset_version_id,
        object_id=object_id,
        file_kind=file_kind,
        format=format,
        row_count=row_count,
        size_bytes=size_bytes,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise DatasetConflictError(
            "数据集文件已登记", params={"dataset_version_id": dataset_version_id}
        ) from exc
    return _row_to_file(row)


def list_files(db: Session, version_id: int) -> list[DatasetFileRecord]:
    """列出版本文件引用行。"""
    rows = db.execute(
        select(DatasetFile).where(DatasetFile.dataset_version_id == version_id)
    ).scalars().all()
    return [_row_to_file(row) for row in rows]
