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

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from iesplan.configuration.contracts import (
    CalcConfigRecord,
    ConfigurationConflictError,
    ConfigurationNotFoundError,
    EffectiveRevisionRecord,
    FinanceProfileRecord,
    OverridesRevisionRecord,
    PlanningRevisionRecord,
)
from iesplan.models.calc import CalcConfig
from iesplan.models.config_revision import (
    EffectiveFinanceRevision,
    FinanceOverridesRevision,
    FinanceProfile,
    PlanningConfigRevision,
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
