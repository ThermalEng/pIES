"""配置域 repository SQL 实现（finance 系列 revision + CalcConfig，归属 configuration）。

实现规则：
- revision 类表只 INSERT（不可变）；序号由调用方经 next_* 预读后显式传入，
  唯一约束兜底并发（回执对象需先于行落盘，序号必须在行插入前确定）；
- CalcConfig 创建时 version 自动取同项目同名 max+1；
- 只 flush，不 commit/rollback；领域错误见 contracts。
"""

from __future__ import annotations

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

from iesplan.configuration.contracts import (
    CalcConfigRecord,
    ConfigurationConflictError,
    EffectiveRevisionRecord,
    FinanceProfileRecord,
    OverridesRevisionRecord,
    PlanningRevisionRecord,
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


def _row_to_profile(row: FinanceProfile) -> FinanceProfileRecord:
    return FinanceProfileRecord(
        id=row.id,
        profile_id=row.profile_id,
        region=row.region,
        content=row.content,
        object_id=row.object_id,
        created_by=row.created_by,
    )


def _row_to_overrides(row: FinanceOverridesRevision) -> OverridesRevisionRecord:
    return OverridesRevisionRecord(
        id=row.id,
        project_id=row.project_id,
        revision=row.revision,
        content=row.content,
        profile_id=row.profile_id,
        receipt_object_id=row.receipt_object_id,
        created_by=row.created_by,
    )


def _row_to_effective(row: EffectiveFinanceRevision) -> EffectiveRevisionRecord:
    return EffectiveRevisionRecord(
        id=row.id,
        project_id=row.project_id,
        revision=row.revision,
        content=row.content,
        profile_id=row.profile_id,
        receipt_object_id=row.receipt_object_id,
        content_object_id=row.content_object_id,
        created_by=row.created_by,
    )


def _row_to_planning(row: PlanningConfigRevision) -> PlanningRevisionRecord:
    return PlanningRevisionRecord(
        id=row.id,
        project_id=row.project_id,
        revision=row.revision,
        content=row.content,
        receipt_object_id=row.receipt_object_id,
        created_by=row.created_by,
    )


# ---------------------------------------------------------------------------
# Profile 注册表
# ---------------------------------------------------------------------------


def get_profile(db: Session, profile_id: str) -> FinanceProfileRecord | None:
    """按 profile_id 取最新登记行；不存在返回 None。"""
    row = db.execute(
        select(FinanceProfile)
        .where(FinanceProfile.profile_id == profile_id)
        .order_by(FinanceProfile.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _row_to_profile(row) if row is not None else None


def get_profile_row(db: Session, row_id: int) -> FinanceProfileRecord | None:
    """按注册行主键取 Profile；不存在返回 None。"""
    row = db.get(FinanceProfile, row_id)
    return _row_to_profile(row) if row is not None else None


def list_profiles(db: Session) -> list[FinanceProfileRecord]:
    """列出全部登记行，按 (profile_id, id 倒序)；去重取最新由调用方完成。"""
    rows = (
        db.execute(select(FinanceProfile).order_by(FinanceProfile.profile_id, FinanceProfile.id.desc()))
        .scalars()
        .all()
    )
    return [_row_to_profile(row) for row in rows]


def register_profile(
    db: Session,
    *,
    profile_id: str,
    region: str,
    content: dict[str, Any],
    object_id: int,
    created_by: int,
) -> FinanceProfileRecord:
    """登记地区 Profile 新行；profile_id 重复抛 ConfigurationConflictError。"""
    row = FinanceProfile(
        profile_id=profile_id,
        region=region,
        content=content,
        object_id=object_id,
        created_by=created_by,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise ConfigurationConflictError("地区 Profile 重复登记", params={"profile_id": profile_id}) from exc
    return _row_to_profile(row)


# ---------------------------------------------------------------------------
# Overrides / Effective / Planning revision
# ---------------------------------------------------------------------------


def get_overrides_revision(db: Session, project_id: int, revision: int) -> OverridesRevisionRecord | None:
    """按 (project_id, revision) 取 Overrides 行；不存在返回 None。"""
    row = db.execute(
        select(FinanceOverridesRevision).where(
            FinanceOverridesRevision.project_id == project_id,
            FinanceOverridesRevision.revision == revision,
        )
    ).scalar_one_or_none()
    return _row_to_overrides(row) if row is not None else None


def get_current_overrides(db: Session, project_id: int) -> OverridesRevisionRecord | None:
    """取项目表内最新 Overrides 行；无行返回 None。"""
    row = db.execute(
        select(FinanceOverridesRevision)
        .where(FinanceOverridesRevision.project_id == project_id)
        .order_by(FinanceOverridesRevision.revision.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _row_to_overrides(row) if row is not None else None


def next_overrides_revision(db: Session, project_id: int) -> int:
    """表内 max(revision)+1（指针清空不重置计数）。"""
    current = db.execute(
        select(func.max(FinanceOverridesRevision.revision)).where(
            FinanceOverridesRevision.project_id == project_id
        )
    ).scalar()
    return int(current or 0) + 1


def append_overrides(
    db: Session,
    *,
    project_id: int,
    revision: int,
    content: dict[str, Any],
    profile_id: str,
    created_by: int,
    receipt_object_id: int | None = None,
) -> OverridesRevisionRecord:
    """追加 Overrides revision 行；(project_id, revision) 冲突抛 ConfigurationConflictError。"""
    row = FinanceOverridesRevision(
        project_id=project_id,
        revision=revision,
        content=content,
        profile_id=profile_id,
        created_by=created_by,
        receipt_object_id=receipt_object_id,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise ConfigurationConflictError(
            "财务覆盖 revision 冲突", params={"project_id": project_id, "revision": revision}
        ) from exc
    return _row_to_overrides(row)


def get_effective_revision(db: Session, project_id: int, revision: int) -> EffectiveRevisionRecord | None:
    """按 (project_id, revision) 取 Effective 行；不存在返回 None。"""
    row = db.execute(
        select(EffectiveFinanceRevision).where(
            EffectiveFinanceRevision.project_id == project_id,
            EffectiveFinanceRevision.revision == revision,
        )
    ).scalar_one_or_none()
    return _row_to_effective(row) if row is not None else None


def get_current_effective(db: Session, project_id: int) -> EffectiveRevisionRecord | None:
    """取项目表内最新 Effective 行；无行返回 None。"""
    row = db.execute(
        select(EffectiveFinanceRevision)
        .where(EffectiveFinanceRevision.project_id == project_id)
        .order_by(EffectiveFinanceRevision.revision.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _row_to_effective(row) if row is not None else None


def next_effective_revision(db: Session, project_id: int) -> int:
    """表内 max(revision)+1（指针清空不重置计数）。"""
    current = db.execute(
        select(func.max(EffectiveFinanceRevision.revision)).where(
            EffectiveFinanceRevision.project_id == project_id
        )
    ).scalar()
    return int(current or 0) + 1


def append_effective(
    db: Session,
    *,
    project_id: int,
    revision: int,
    content: dict[str, Any],
    profile_id: str,
    created_by: int,
    content_object_id: int,
    receipt_object_id: int | None = None,
) -> EffectiveRevisionRecord:
    """追加 Effective revision 行；(project_id, revision) 冲突抛 ConfigurationConflictError。"""
    row = EffectiveFinanceRevision(
        project_id=project_id,
        revision=revision,
        content=content,
        profile_id=profile_id,
        created_by=created_by,
        receipt_object_id=receipt_object_id,
        content_object_id=content_object_id,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise ConfigurationConflictError(
            "有效快照 revision 冲突", params={"project_id": project_id, "revision": revision}
        ) from exc
    return _row_to_effective(row)


def get_planning_revision(db: Session, project_id: int, revision: int) -> PlanningRevisionRecord | None:
    """按 (project_id, revision) 取规划配置行；不存在返回 None。"""
    row = db.execute(
        select(PlanningConfigRevision).where(
            PlanningConfigRevision.project_id == project_id,
            PlanningConfigRevision.revision == revision,
        )
    ).scalar_one_or_none()
    return _row_to_planning(row) if row is not None else None


def get_current_planning(db: Session, project_id: int) -> PlanningRevisionRecord | None:
    """取项目表内最新规划配置行；无行返回 None。"""
    row = db.execute(
        select(PlanningConfigRevision)
        .where(PlanningConfigRevision.project_id == project_id)
        .order_by(PlanningConfigRevision.revision.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _row_to_planning(row) if row is not None else None


def next_planning_revision(db: Session, project_id: int) -> int:
    """表内 max(revision)+1（指针清空不重置计数）。"""
    current = db.execute(
        select(func.max(PlanningConfigRevision.revision)).where(
            PlanningConfigRevision.project_id == project_id
        )
    ).scalar()
    return int(current or 0) + 1


def append_planning(
    db: Session,
    *,
    project_id: int,
    revision: int,
    content: dict[str, Any],
    created_by: int,
    receipt_object_id: int | None = None,
) -> PlanningRevisionRecord:
    """追加规划配置 revision 行；(project_id, revision) 冲突抛 ConfigurationConflictError。"""
    row = PlanningConfigRevision(
        project_id=project_id,
        revision=revision,
        content=content,
        created_by=created_by,
        receipt_object_id=receipt_object_id,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise ConfigurationConflictError(
            "规划配置 revision 冲突", params={"project_id": project_id, "revision": revision}
        ) from exc
    return _row_to_planning(row)


# ---------------------------------------------------------------------------
# CalcConfig（计算配置不可变 revision, 0.12-A1 起收敛旧自由形态）
# ---------------------------------------------------------------------------


def _row_to_calculation_config(row: CalcConfig) -> CalcConfigRecord:
    return CalcConfigRecord(
        id=row.id,
        project_id=row.project_id,
        version=row.version,
        content=row.content,
        created_by=row.created_by,
    )


def get_latest_calculation_config(db: Session, project_id: int) -> CalcConfigRecord | None:
    """取项目最新计算配置 revision；未保存过返回 None。"""
    row = (
        db.execute(
            select(CalcConfig)
            .where(CalcConfig.project_id == project_id)
            .order_by(CalcConfig.version.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    return _row_to_calculation_config(row) if row is not None else None


def create_calculation_config(
    db: Session,
    *,
    project_id: int,
    content: dict[str, Any],
    created_by: int,
) -> CalcConfigRecord:
    """追加计算配置 revision（version 取表内 max+1；并发冲突抛错）。

    content 须为应用输入入口已校验的 CalculationConfig 全文；本层不重复校验。
    """
    max_version = db.execute(
        select(func.max(CalcConfig.version)).where(CalcConfig.project_id == project_id)
    ).scalar()
    row = CalcConfig(
        project_id=project_id,
        version=int(max_version or 0) + 1,
        content=content,
        created_by=created_by,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise ConfigurationConflictError(
            "计算配置创建冲突", params={"project_id": project_id}
        ) from exc
    return _row_to_calculation_config(row)


# ---------------------------------------------------------------------------
# ORM 表定义: Wave2A 由 iesplan.models.calc(CalcConfig) 迁入, 表真相归本域所有。
# ---------------------------------------------------------------------------

class CalcConfig(Base):
    """计算配置不可变 revision(仅 INSERT, 0.12-A1 起收敛旧自由形态)。

    每行 content 即已发布 12 字段 CalculationConfig 全文(to_dict 形态,
    应用输入入口唯一校验)；version 按项目单调递增，读取以最新 revision
    为准。旧形态列(params/variables/objectives/constraints/min_irr/
    algorithm/solver/tolerances/random_seed/status)已退役(0.12-A1)。
    """

    __tablename__ = "calc_configs"

    id: Mapped[int] = bigint_pk()
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_calc_configs_revision"),
        UniqueConstraint("project_id", "version", name="uq_calc_configs_revision"),
        Index("idx_calc_configs_project", "project_id", sa.text("version DESC")),
    )


# ---------------------------------------------------------------------------
# ORM 表定义: Wave2A 由 iesplan.models.config_revision 迁入, 表真相归本域所有。
# ---------------------------------------------------------------------------

class FinanceProfile(Base):
    """地区 FinanceProfile 注册表(已注册、可复用的地区财务基准)。

    每次登记写入新行; content 存内容 JSON(FinanceProfile.to_dict 形态);
    object_id 指向 YAML 对象。文本文件只校验字头，不做内容摘要。
    """

    __tablename__ = "finance_profiles"

    id: Mapped[int] = bigint_pk()
    profile_id: Mapped[str] = mapped_column(Text, nullable=False)
    region: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"), nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        UniqueConstraint("profile_id", name="uq_finance_profiles_id"),
        Index("idx_finance_profiles_id", "profile_id"),
    )

class FinanceOverridesRevision(Base):
    """项目 FinanceOverrides 不可变 revision(仅 INSERT, 追加式)。"""

    __tablename__ = "finance_overrides"

    id: Mapped[int] = bigint_pk()
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    profile_id: Mapped[str] = mapped_column(Text, nullable=False)
    #: 可审计回执对象引用(objects.id; 0011 起新行必备, 存量开发行可空)
    receipt_object_id: Mapped[int | None] = mapped_column(ForeignKey("objects.id"))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_finance_overrides_revision"),
        UniqueConstraint("project_id", "revision", name="uq_finance_overrides_revision"),
        Index("idx_finance_overrides_project", "project_id", sa.text("revision DESC")),
    )

class EffectiveFinanceRevision(Base):
    """项目 EffectiveFinanceConfig 不可变 revision(仅 INSERT, 合并器产物)。

    装配/规划/财务计算只消费当前 Effective revision 指向的快照。
    """

    __tablename__ = "effective_finance_revisions"

    id: Mapped[int] = bigint_pk()
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    profile_id: Mapped[str] = mapped_column(Text, nullable=False)
    #: 可审计回执对象引用(objects.id; 0011 起新行必备, 存量开发行可空)
    receipt_object_id: Mapped[int | None] = mapped_column(ForeignKey("objects.id"))
    #: 快照内容对象引用(objects.id; 有效快照 YAML 内容对象, 装配精确引用
    #: 的真实对象身份来源; receipt 只存校验回执, 不冒充内容对象。
    #: 新鲜建表即非空: 签发流程先创建内容对象再创建 revision，不保留
    #: 签发前旧行，不加旧库迁移)
    content_object_id: Mapped[int] = mapped_column(ForeignKey("objects.id"), nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_effective_finance_revisions_revision"),
        UniqueConstraint(
            "project_id", "revision", name="uq_effective_finance_revisions_revision"
        ),
        Index(
            "idx_effective_finance_revisions_project",
            "project_id", sa.text("revision DESC"),
        ),
    )

class PlanningConfigRevision(Base):
    """规划配置不可变 revision(仅 INSERT, 追加式)。

    规划与结果财务计算必须消费同一有效快照(宪法 4.6)。
    """

    __tablename__ = "planning_configs"

    id: Mapped[int] = bigint_pk()
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    #: 可审计回执对象引用(objects.id; 0011 起新行必备, 存量开发行可空)
    receipt_object_id: Mapped[int | None] = mapped_column(ForeignKey("objects.id"))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_planning_configs_revision"),
        UniqueConstraint("project_id", "revision", name="uq_planning_configs_revision"),
        Index("idx_planning_configs_project", "project_id", sa.text("revision DESC")),
    )


#: 本域拥有的不可变表(仅 INSERT, 禁止 UPDATE/DELETE)
IMMUTABLE_TABLES: tuple[str, ...] = (
    "finance_profiles",
    "finance_overrides",
    "effective_finance_revisions",
    "planning_configs",
    "calc_configs",
)


def install_triggers() -> tuple[str, ...]:
    """公开钩子: 返回本域触发器部署语句(按执行序, 含幂等 DROP, 供组合根编排收集)。"""
    statements = [drop_trigger_function_sql(f"tg_{table}_immutable") for table in IMMUTABLE_TABLES]
    statements.extend(immutable_trigger_sql(table) for table in IMMUTABLE_TABLES)
    statements.extend(immutable_revoke_sql(table) for table in IMMUTABLE_TABLES)
    statements.append(drop_trigger_function_sql("tg_calc_configs_frozen"))
    return tuple(statements)
