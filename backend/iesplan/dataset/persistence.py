"""数据集域 SQL repository 实现（datasets/dataset_versions/dataset_files）。

- 本模块是 `DatasetRepository` 协议的实现，可经 `iesplan.dataset` 门面调用；
- 表真相收归本模块（Wave2A 由 iesplan.models.dataset 迁入）；绝不 commit/rollback（调用方事务拥有）；
- 唯一冲突转 `DatasetConflictError`；冲突后调用方须回滚会话
  （与旧 services 契约一致；实测 SA 2.0 下 flush 失败后会话不可继续）；
- 文件正文读写走 storage（对象 id），本模块只维护引用行；
- 时间以 `datetime.isoformat()` 原样映射为记录字符串。
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from iesplan.dataset.contracts import (
    DatasetConflictError,
    DatasetFileRecord,
    DatasetNotFoundError,
    DatasetRecord,
    DatasetVersionRecord,
)
from iesplan.db import (
    Base,
    JSONB,
    bigint_pk,
    drop_trigger_function_sql,
    immutable_revoke_sql,
    immutable_trigger_sql,
)


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


# ---------------------------------------------------------------------------
# ORM 表定义: Wave2A 由 iesplan.models.dataset 迁入, 表真相归本域所有。
# ---------------------------------------------------------------------------

class Dataset(Base):
    """数据集元数据(01 §5.1)。"""

    __tablename__ = "datasets"

    id: Mapped[int] = bigint_pk()
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    default_license: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','published','deprecated')", name="ck_datasets_status"
        ),
        # 表达式唯一索引: 共享数据集(project_id NULL)按 (name) 全局去重
        Index(
            "uq_datasets_name",
            sa.func.coalesce(sa.literal_column("project_id"), 0),
            "name",
            unique=True,
        ),
        Index("idx_datasets_project", "project_id"),
    )


class DatasetVersion(Base):
    """数据集版本(不可变, 追加式, 01 §5.2)。"""

    __tablename__ = "dataset_versions"

    id: Mapped[int] = bigint_pk()
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    timeline: Mapped[str] = mapped_column(Text, nullable=False)
    resolution: Mapped[str | None] = mapped_column(Text)
    fixed_utc_offset_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    fields: Mapped[dict] = mapped_column(JSONB, nullable=False)
    units: Mapped[dict] = mapped_column(JSONB, nullable=False)
    quality_report: Mapped[dict | None] = mapped_column(JSONB)
    provenance: Mapped[dict | None] = mapped_column(JSONB)
    license: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    created_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "timeline IN ('hourly','quarter_hourly','daily','monthly','yearly','custom')",
            name="ck_dataset_versions_timeline",
        ),
        CheckConstraint(
            "fixed_utc_offset_minutes BETWEEN -720 AND 840", name="ck_dataset_versions_utc_offset"
        ),
        UniqueConstraint("dataset_id", "version_no", name="uq_dataset_versions_version"),
        Index("idx_dataset_versions_dataset", "dataset_id", sa.text("version_no DESC")),
    )


class DatasetFile(Base):
    """数据集版本文件(指向对象存储对象, 按对象 id 寻址, 不可变, 01 §5.3)。"""

    __tablename__ = "dataset_files"

    id: Mapped[int] = bigint_pk()
    dataset_version_id: Mapped[int] = mapped_column(ForeignKey("dataset_versions.id"), nullable=False)
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"), nullable=False)
    file_kind: Mapped[str] = mapped_column(Text, nullable=False)
    format: Mapped[str] = mapped_column(Text, nullable=False)
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=sa.text("0"))
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=sa.text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "file_kind IN ('data','header','manifest','metadata')", name="ck_dataset_files_kind"
        ),
        CheckConstraint("format IN ('parquet','csv','json')", name="ck_dataset_files_format"),
        CheckConstraint("row_count >= 0", name="ck_dataset_files_row_count"),
        CheckConstraint("size_bytes >= 0", name="ck_dataset_files_size"),
        UniqueConstraint("dataset_version_id", "object_id", name="uq_dataset_files_object"),
        Index("idx_dataset_files_object", "object_id"),
    )


#: 本域拥有的不可变表(仅 INSERT, 禁止 UPDATE/DELETE)
IMMUTABLE_TABLES: tuple[str, ...] = (
    "dataset_versions",
    "dataset_files",
)


def install_triggers() -> tuple[str, ...]:
    """公开钩子: 返回本域触发器部署语句(按执行序, 含幂等 DROP, 供组合根编排收集)。"""
    statements = [drop_trigger_function_sql(f"tg_{table}_immutable") for table in IMMUTABLE_TABLES]
    statements.extend(immutable_trigger_sql(table) for table in IMMUTABLE_TABLES)
    statements.extend(immutable_revoke_sql(table) for table in IMMUTABLE_TABLES)
    return tuple(statements)
