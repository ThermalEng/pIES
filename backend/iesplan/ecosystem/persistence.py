"""本地模型与算法生态 ORM 与 Repository（归属 ecosystem 域）。

仅本模块持有 ecosystem_settings / local_artifacts / member_artifact_installs ORM 声明。
所有写入只做 flush，绝不在此 commit/rollback（事务边界归 application 层）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from iesplan.db import JSONB, Base, bigint_pk
from iesplan.ecosystem.contracts import (
    APPROVAL_STATUSES,
    ARTIFACT_KINDS,
    INSTALL_STATUSES,
    MARKET_MODES,
    SOURCE_ORIGINS,
    EcosystemConflictError,
    EcosystemSettingsRecord,
    LocalArtifactRecord,
    MemberArtifactInstallRecord,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class EcosystemSetting(Base):
    """实例级正交生态设置表（固定单例行 id=1）。"""

    __tablename__ = "ecosystem_settings"
    __table_args__ = (
        CheckConstraint(f"market_mode IN {MARKET_MODES}", name="ck_ecosystem_market_mode"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_mode: Mapped[str] = mapped_column(Text, nullable=False, default="local")
    local_upload_requires_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    pip_index_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    market_index_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    submission_gateway_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )


class LocalArtifact(Base):
    """实例级共享本地模型/插件目录表（按 (kind, stable_id, version) 去重）。"""

    __tablename__ = "local_artifacts"
    __table_args__ = (
        UniqueConstraint("kind", "stable_id", "version", name="uq_local_artifacts_identity"),
        CheckConstraint(f"kind IN {ARTIFACT_KINDS}", name="ck_local_artifacts_kind"),
        CheckConstraint(
            f"source_origin IN {SOURCE_ORIGINS}", name="ck_local_artifacts_source_origin"
        ),
        CheckConstraint(
            f"approval_status IN {APPROVAL_STATUSES}", name="ck_local_artifacts_approval_status"
        ),
        CheckConstraint(
            f"install_status IN {INSTALL_STATUSES}", name="ck_local_artifacts_install_status"
        ),
        Index("ix_local_artifacts_lookup", "kind", "approval_status", "install_status"),
    )

    id: Mapped[int] = bigint_pk()
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    stable_id: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    source_origin: Mapped[str] = mapped_column(Text, nullable=False)
    approval_status: Mapped[str] = mapped_column(Text, nullable=False)
    install_status: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    approved_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    object_id: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    image_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnostics: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )


class MemberArtifactInstall(Base):
    """成员级安装与启用版本选择表（支持同稳定 ID 多精确版本共存）。"""

    __tablename__ = "member_artifact_installs"
    __table_args__ = (
        UniqueConstraint("user_id", "artifact_id", name="uq_member_artifact_install"),
        CheckConstraint(f"kind IN {ARTIFACT_KINDS}", name="ck_member_artifact_kind"),
        Index("ix_member_artifact_installs_lookup", "user_id", "kind", "enabled"),
    )

    id: Mapped[int] = bigint_pk()
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    artifact_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("local_artifacts.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    stable_id: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )


def _row_to_settings(row: EcosystemSetting) -> EcosystemSettingsRecord:
    return EcosystemSettingsRecord(
        id=int(row.id),
        market_mode=row.market_mode,
        local_upload_requires_approval=bool(row.local_upload_requires_approval),
        pip_index_url=row.pip_index_url,
        market_index_url=row.market_index_url,
        submission_gateway_url=row.submission_gateway_url,
        updated_by=row.updated_by,
        updated_at=_iso(row.updated_at),
    )


def _row_to_artifact(row: LocalArtifact) -> LocalArtifactRecord:
    return LocalArtifactRecord(
        id=int(row.id),
        kind=row.kind,
        stable_id=row.stable_id,
        version=row.version,
        source_origin=row.source_origin,
        approval_status=row.approval_status,
        install_status=row.install_status,
        uploaded_by=int(row.uploaded_by),
        approved_by=int(row.approved_by) if row.approved_by is not None else None,
        approved_at=_iso(row.approved_at),
        object_id=row.object_id,
        metadata_json=dict(row.metadata_json or {}),
        image_ref=row.image_ref,
        diagnostics=row.diagnostics,
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


def _row_to_install(row: MemberArtifactInstall) -> MemberArtifactInstallRecord:
    return MemberArtifactInstallRecord(
        id=int(row.id),
        user_id=int(row.user_id),
        artifact_id=int(row.artifact_id),
        kind=row.kind,
        stable_id=row.stable_id,
        version=row.version,
        enabled=bool(row.enabled),
        installed_at=_iso(row.installed_at),
        updated_at=_iso(row.updated_at),
    )


def get_settings(db: Session) -> EcosystemSettingsRecord:
    """读取实例级生态设置；未显式配置时返回安全默认视图（local + 需审批）。"""
    row = db.get(EcosystemSetting, 1)
    if row is None:
        return EcosystemSettingsRecord(
            id=1,
            market_mode="local",
            local_upload_requires_approval=True,
        )
    return _row_to_settings(row)


def upsert_settings(
    db: Session,
    *,
    market_mode: str,
    local_upload_requires_approval: bool,
    pip_index_url: str | None,
    market_index_url: str | None,
    submission_gateway_url: str | None,
    updated_by: int,
) -> EcosystemSettingsRecord:
    """更新单例生态设置（只 flush，不提交）。"""
    row = db.get(EcosystemSetting, 1)
    if row is None:
        row = EcosystemSetting(
            id=1,
            market_mode=market_mode,
            local_upload_requires_approval=bool(local_upload_requires_approval),
            pip_index_url=pip_index_url,
            market_index_url=market_index_url,
            submission_gateway_url=submission_gateway_url,
            updated_by=updated_by,
            updated_at=_now(),
        )
        db.add(row)
    else:
        row.market_mode = market_mode
        row.local_upload_requires_approval = bool(local_upload_requires_approval)
        row.pip_index_url = pip_index_url
        row.market_index_url = market_index_url
        row.submission_gateway_url = submission_gateway_url
        row.updated_by = updated_by
        row.updated_at = _now()
    db.flush()
    return _row_to_settings(row)


def get_artifact(
    db: Session, artifact_id: int, *, for_update: bool = False
) -> LocalArtifactRecord | None:
    """按主键读取本地制品记录；支持 ``FOR UPDATE`` 行锁。"""
    stmt = select(LocalArtifact).where(LocalArtifact.id == artifact_id)
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    row = db.execute(stmt).scalar_one_or_none()
    return _row_to_artifact(row) if row is not None else None


def get_artifact_by_identity(
    db: Session,
    kind: str,
    stable_id: str,
    version: str,
    *,
    for_update: bool = False,
) -> LocalArtifactRecord | None:
    """按 (kind, stable_id, version) 精确读取本地制品。"""
    stmt = select(LocalArtifact).where(
        LocalArtifact.kind == kind,
        LocalArtifact.stable_id == stable_id,
        LocalArtifact.version == version,
    )
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    row = db.execute(stmt).scalar_one_or_none()
    return _row_to_artifact(row) if row is not None else None


def list_artifacts(
    db: Session,
    *,
    kind: str | None = None,
    stable_id: str | None = None,
    approval_status: str | None = None,
    install_status: str | None = None,
    has_image_ref: bool | None = None,
) -> list[LocalArtifactRecord]:
    """查询本地制品目录（按 stable_id、version、id 排序）。"""
    stmt = select(LocalArtifact)
    if kind is not None:
        stmt = stmt.where(LocalArtifact.kind == kind)
    if stable_id is not None:
        stmt = stmt.where(LocalArtifact.stable_id == stable_id)
    if approval_status is not None:
        stmt = stmt.where(LocalArtifact.approval_status == approval_status)
    if install_status is not None:
        stmt = stmt.where(LocalArtifact.install_status == install_status)
    if has_image_ref is True:
        stmt = stmt.where(LocalArtifact.image_ref.is_not(None))
    stmt = stmt.order_by(LocalArtifact.stable_id, LocalArtifact.id)
    rows = db.execute(stmt).scalars().all()
    return [_row_to_artifact(row) for row in rows]


def create_artifact(
    db: Session,
    *,
    kind: str,
    stable_id: str,
    version: str,
    source_origin: str,
    approval_status: str,
    install_status: str,
    uploaded_by: int,
    object_id: str,
    metadata_json: dict[str, Any],
    approved_by: int | None = None,
    approved_at: datetime | None = None,
    image_ref: str | None = None,
    diagnostics: Any | None = None,
) -> LocalArtifactRecord:
    """创建实例级共享制品行；身份冲突时抛 ``EcosystemConflictError``（409）。"""
    row = LocalArtifact(
        kind=kind,
        stable_id=stable_id,
        version=version,
        source_origin=source_origin,
        approval_status=approval_status,
        install_status=install_status,
        uploaded_by=uploaded_by,
        approved_by=approved_by,
        approved_at=approved_at,
        object_id=object_id,
        metadata_json=dict(metadata_json),
        image_ref=image_ref,
        diagnostics=diagnostics,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise EcosystemConflictError(
            "同标识与精确版本的本地制品已存在",
            params={"kind": kind, "stable_id": stable_id, "version": version},
        ) from exc
    return _row_to_artifact(row)


_UNSET = object()


def update_artifact(
    db: Session,
    artifact_id: int,
    *,
    approval_status: Any = _UNSET,
    install_status: Any = _UNSET,
    approved_by: Any = _UNSET,
    approved_at: Any = _UNSET,
    image_ref: Any = _UNSET,
    diagnostics: Any = _UNSET,
    metadata_json: Any = _UNSET,
) -> LocalArtifactRecord | None:
    """更新制品状态/镜像引用/诊断（只 flush）。"""
    row = db.get(LocalArtifact, artifact_id)
    if row is None:
        return None
    if approval_status is not _UNSET:
        row.approval_status = approval_status
    if install_status is not _UNSET:
        row.install_status = install_status
    if approved_by is not _UNSET:
        row.approved_by = approved_by
    if approved_at is not _UNSET:
        row.approved_at = approved_at
    if image_ref is not _UNSET:
        row.image_ref = image_ref
    if diagnostics is not _UNSET:
        row.diagnostics = diagnostics
    if metadata_json is not _UNSET:
        row.metadata_json = dict(metadata_json)
    row.updated_at = _now()
    db.flush()
    return _row_to_artifact(row)


def lock_artifacts_for_submission(
    db: Session,
    *,
    identities: list[tuple[str, str, str]],
) -> list[LocalArtifactRecord]:
    """按精确 (kind, stable_id, version) 清单对本地制品行加 ``FOR UPDATE`` 行锁。"""
    if not identities:
        return []
    ordered = sorted(set(identities))
    cond = sa.or_(
        *[
            sa.and_(
                LocalArtifact.kind == kind,
                LocalArtifact.stable_id == stable_id,
                LocalArtifact.version == version,
            )
            for kind, stable_id, version in ordered
        ]
    )
    stmt = (
        select(LocalArtifact)
        .where(cond)
        .order_by(LocalArtifact.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    rows = db.execute(stmt).scalars().all()
    return [_row_to_artifact(row) for row in rows]


def get_member_install(
    db: Session, user_id: int, artifact_id: int
) -> MemberArtifactInstallRecord | None:
    stmt = select(MemberArtifactInstall).where(
        MemberArtifactInstall.user_id == user_id,
        MemberArtifactInstall.artifact_id == artifact_id,
    )
    row = db.execute(stmt).scalar_one_or_none()
    return _row_to_install(row) if row is not None else None


def list_member_installs(
    db: Session,
    user_id: int,
    *,
    kind: str | None = None,
    enabled_only: bool = False,
) -> list[MemberArtifactInstallRecord]:
    stmt = select(MemberArtifactInstall).where(MemberArtifactInstall.user_id == user_id)
    if kind is not None:
        stmt = stmt.where(MemberArtifactInstall.kind == kind)
    if enabled_only:
        stmt = stmt.where(MemberArtifactInstall.enabled.is_(True))
    stmt = stmt.order_by(MemberArtifactInstall.stable_id, MemberArtifactInstall.id)
    rows = db.execute(stmt).scalars().all()
    return [_row_to_install(row) for row in rows]


def upsert_member_install(
    db: Session,
    *,
    user_id: int,
    artifact: LocalArtifactRecord,
    enabled: bool = True,
) -> MemberArtifactInstallRecord:
    """为成员安装并启用（或更新启用态）指定精确制品版本。"""
    stmt = select(MemberArtifactInstall).where(
        MemberArtifactInstall.user_id == user_id,
        MemberArtifactInstall.artifact_id == artifact.id,
    )
    row = db.execute(stmt).scalar_one_or_none()
    if row is None:
        row = MemberArtifactInstall(
            user_id=user_id,
            artifact_id=artifact.id,
            kind=artifact.kind,
            stable_id=artifact.stable_id,
            version=artifact.version,
            enabled=bool(enabled),
        )
        db.add(row)
    else:
        row.enabled = bool(enabled)
        row.updated_at = _now()
    db.flush()
    return _row_to_install(row)


def set_member_install_enabled(
    db: Session, user_id: int, artifact_id: int, *, enabled: bool
) -> MemberArtifactInstallRecord | None:
    stmt = select(MemberArtifactInstall).where(
        MemberArtifactInstall.user_id == user_id,
        MemberArtifactInstall.artifact_id == artifact_id,
    )
    row = db.execute(stmt).scalar_one_or_none()
    if row is None:
        return None
    row.enabled = bool(enabled)
    row.updated_at = _now()
    db.flush()
    return _row_to_install(row)


def delete_member_install(db: Session, user_id: int, artifact_id: int) -> bool:
    stmt = select(MemberArtifactInstall).where(
        MemberArtifactInstall.user_id == user_id,
        MemberArtifactInstall.artifact_id == artifact_id,
    )
    row = db.execute(stmt).scalar_one_or_none()
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True


def list_enabled_artifacts_for_user(
    db: Session,
    user_id: int,
    *,
    kind: str | None = None,
    kinds: tuple[str, ...] | None = None,
) -> list[LocalArtifactRecord]:
    """查询某成员已安装且启用、且实例侧已审批就绪（approved + ready）的制品。"""
    stmt = (
        select(LocalArtifact)
        .join(MemberArtifactInstall, MemberArtifactInstall.artifact_id == LocalArtifact.id)
        .where(
            MemberArtifactInstall.user_id == user_id,
            MemberArtifactInstall.enabled.is_(True),
            LocalArtifact.approval_status == "approved",
            LocalArtifact.install_status == "ready",
        )
    )
    if kind is not None:
        stmt = stmt.where(LocalArtifact.kind == kind)
    if kinds is not None:
        stmt = stmt.where(LocalArtifact.kind.in_(kinds))
    stmt = stmt.order_by(LocalArtifact.stable_id, LocalArtifact.id)
    rows = db.execute(stmt).scalars().all()
    return [_row_to_artifact(row) for row in rows]


IMMUTABLE_TABLES: tuple[str, ...] = ()


def install_triggers() -> tuple[str, ...]:
    """本域无不可变表触发器（制品生命周期依赖状态推进与卸载互斥）。"""
    return ()
