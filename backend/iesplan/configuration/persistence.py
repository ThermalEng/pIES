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
    Numeric,
    SmallInteger,
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
    ConfigurationNotFoundError,
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


def _row_to_calc_config(row: CalcConfig) -> CalcConfigRecord:
    return CalcConfigRecord(
        id=row.id,
        project_id=row.project_id,
        name=row.name,
        params=row.params,
        variables=row.variables,
        objectives=row.objectives,
        constraints=row.constraints,
        tolerances=row.tolerances,
        description=row.description,
        min_irr=float(row.min_irr) if row.min_irr is not None else None,
        algorithm=row.algorithm,
        solver=row.solver,
        random_seed=row.random_seed,
        status=row.status,
        version=row.version,
        updated_by=row.updated_by,
        updated_at=_iso(row.updated_at),
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
# CalcConfig（用户可编辑计算配置）
# ---------------------------------------------------------------------------


def get_calc_config(db: Session, config_id: int) -> CalcConfigRecord | None:
    """按主键取计算配置；不存在返回 None。"""
    row = db.get(CalcConfig, config_id)
    return _row_to_calc_config(row) if row is not None else None


def list_calc_configs(db: Session, project_id: int) -> list[CalcConfigRecord]:
    """列出项目计算配置行（按 id 升序；同名最新由调用方按 version 取）。"""
    rows = (
        db.execute(select(CalcConfig).where(CalcConfig.project_id == project_id).order_by(CalcConfig.id))
        .scalars()
        .all()
    )
    return [_row_to_calc_config(row) for row in rows]


def create_calc_config(
    db: Session,
    *,
    project_id: int,
    name: str,
    params: dict[str, Any],
    variables: list[Any],
    objectives: list[Any],
    constraints: list[Any],
    tolerances: dict[str, Any],
    updated_by: int,
    description: str | None = None,
    algorithm: str | None = None,
    solver: str | None = None,
    random_seed: int | None = None,
    min_irr: float | None = None,
) -> CalcConfigRecord:
    """创建计算配置行（同项目同名 version 自动取表内 max+1；并发冲突抛错）。"""
    max_version = db.execute(
        select(func.max(CalcConfig.version)).where(
            CalcConfig.project_id == project_id,
            CalcConfig.name == name,
        )
    ).scalar()
    row = CalcConfig(
        project_id=project_id,
        name=name,
        version=int(max_version or 0) + 1,
        status="draft",
        params=params,
        variables=variables,
        objectives=objectives,
        constraints=constraints,
        tolerances=tolerances,
        description=description,
        min_irr=min_irr,
        algorithm=algorithm,
        solver=solver,
        random_seed=random_seed,
        updated_by=updated_by,
        updated_at=_now(),
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        raise ConfigurationConflictError(
            "计算配置创建冲突", params={"project_id": project_id, "name": name}
        ) from exc
    return _row_to_calc_config(row)


#: update_calc_config 允许的写入列（白名单外键名直接拒绝，避免拼写污染行）。
_UPDATABLE_CALC_CONFIG_FIELDS = frozenset(
    {
        "params",
        "variables",
        "objectives",
        "constraints",
        "tolerances",
        "description",
        "min_irr",
        "algorithm",
        "solver",
        "random_seed",
    }
)


def update_calc_config(
    db: Session, config_id: int, *, values: dict[str, Any], updated_by: int
) -> CalcConfigRecord:
    """更新草稿态配置行；缺失抛 ConfigurationNotFoundError，非草稿态或未知列抛错。"""
    row = db.get(CalcConfig, config_id)
    if row is None:
        raise ConfigurationNotFoundError("计算配置不存在", params={"config_id": config_id})
    if row.status != "draft":
        raise ConfigurationConflictError(
            "冻结配置不可修改", params={"config_id": config_id, "status": row.status}
        )
    unknown = set(values) - _UPDATABLE_CALC_CONFIG_FIELDS
    if unknown:
        raise ConfigurationConflictError(
            "未知更新列", params={"config_id": config_id, "fields": sorted(unknown)}
        )
    for key, value in values.items():
        setattr(row, key, value)
    row.updated_by = updated_by
    row.updated_at = _now()
    db.flush()
    return _row_to_calc_config(row)


def freeze_calc_config(db: Session, config_id: int) -> CalcConfigRecord:
    """冻结配置行（draft → frozen）；缺失抛 ConfigurationNotFoundError。"""
    row = db.get(CalcConfig, config_id)
    if row is None:
        raise ConfigurationNotFoundError("计算配置不存在", params={"config_id": config_id})
    row.status = "frozen"
    row.updated_at = _now()
    db.flush()
    return _row_to_calc_config(row)


# ---------------------------------------------------------------------------
# ORM 表定义: Wave2A 由 iesplan.models.calc(CalcConfig) 迁入, 表真相归本域所有。
# ---------------------------------------------------------------------------

class CalcConfig(Base):
    """计算配置(参数/变量/目标/约束/算法/容差/种子, 01 §6.1)。"""

    __tablename__ = "calc_configs"

    id: Mapped[int] = bigint_pk()
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'"))
    variables: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'[]'"))
    objectives: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'[]'"))
    constraints: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'[]'"))
    min_irr: Mapped[float | None] = mapped_column(Numeric(6, 4))
    algorithm: Mapped[str | None] = mapped_column(Text)
    solver: Mapped[str | None] = mapped_column(Text)
    tolerances: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'"))
    random_seed: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa.text("1"))
    updated_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "min_irr IS NULL OR min_irr BETWEEN 0 AND 1", name="ck_calc_configs_min_irr"
        ),
        CheckConstraint(
            "algorithm IS NULL OR algorithm IN ('milp','lp','heuristic','ga','exhaustive','custom')",
            name="ck_calc_configs_algorithm",
        ),
        CheckConstraint("status IN ('draft','frozen')", name="ck_calc_configs_status"),
        Index(
            "uq_calc_configs_name_version", "project_id", "name", "version", unique=True
        ),
        Index("idx_calc_configs_project", "project_id", "name"),
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
)

#: calc_configs: status='frozen' 的行禁止 UPDATE(01 §6.1);DELETE 由应用层约束
CALC_CONFIGS_FROZEN_TRIGGER_SQL: str = """\
-- calc_configs: 冻结的计算配置不可修改
CREATE FUNCTION tg_calc_configs_frozen() RETURNS trigger AS $$
BEGIN
  IF OLD.status = 'frozen' THEN
    RAISE EXCEPTION '冻结的计算配置不可修改';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER tg_calc_configs_no_update BEFORE UPDATE ON calc_configs
  FOR EACH ROW EXECUTE FUNCTION tg_calc_configs_frozen();
"""


def install_triggers() -> tuple[str, ...]:
    """公开钩子: 返回本域触发器部署语句(按执行序, 含幂等 DROP, 供组合根编排收集)。"""
    statements = [drop_trigger_function_sql(f"tg_{table}_immutable") for table in IMMUTABLE_TABLES]
    statements.extend(immutable_trigger_sql(table) for table in IMMUTABLE_TABLES)
    statements.extend(immutable_revoke_sql(table) for table in IMMUTABLE_TABLES)
    statements.append(drop_trigger_function_sql("tg_calc_configs_frozen"))
    statements.append(CALC_CONFIGS_FROZEN_TRIGGER_SQL)
    return tuple(statements)
