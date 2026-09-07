"""财务三件套与规划配置 revision 服务(0.6.5 条目 1-2;替换旧单体 FinanceConfig)。

职责:
- Profile 登记: 地区 FinanceProfile 注册表(内容寻址, 复用; 每次登记新行);
- Overrides 保存: 追加不可变 revision(finance_overrides, 仅 INSERT)并更新
  项目指针; revision 号单调递增; 精确引用 Profile {id, content_sha256};
- Effective 生成/保存: 保存 Overrides 后由确定性合并器
  (merge_effective)从 Profile + Overrides 重新合并并完整校验, 追加不可变
  revision(effective_finance_revisions), 更新项目当前有效快照指针; 用户不
  能直接 author Effective —— 它只能由合并器生成(finance-yaml.md);
- Planning 保存: 强制其 finance_content_sha256 与项目**当前**
  EffectiveFinanceConfig.content_sha256 一致(宪法 4.6: 规划与结果财务计算
  固定同一有效快照内容摘要);
- 并发保护: 保存必须携带 expected_revision(当前指针值), 不匹配 → 409
  (宪法 8.4 乐观锁, 禁止最后写入静默覆盖);
- 失败原子: 任一校验/合并/摘要不一致失败 → 不落任何行(校验在 flush 前),
  Overrides 保存失败不更新指针也不产生 Effective(无静默默认/部分发布)。

本层不主动 commit, 事务边界由 API 层控制。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from iesplan.core.contracts import PlanningConfig
from iesplan.core.diagnostics import SEVERITY_ERROR
from iesplan.core.errors import AppError, ConflictError, NotFoundError
from iesplan.finance import (
    EffectiveFinanceConfig,
    FinanceOverrides,
    FinanceProfile,
    FinanceTripletError,
    merge_effective,
)
from iesplan.models.config_revision import (
    EffectiveFinanceRevision,
    FinanceOverridesRevision,
    FinanceProfile as FinanceProfileRow,
    PlanningConfigRevision,
)
from iesplan.models.project import Project
from iesplan.planning.contracts import validate_planning_domain
from iesplan.storage import put_object


class InvalidRequestError(AppError):
    """配置校验失败(HTTP 400; code 为 PROJ-FIN/PROJ-PLAN 领域码)。"""

    code = "PROJ-FIN-001"
    http_status = 400
    severity = SEVERITY_ERROR
    message_key = "ies.diag.param.invalid"


def _get_project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None or project.status == "deleted":
        raise NotFoundError(
            "项目不存在",
            params={"project_id": project_id},
            location={"object_type": "project", "object_id": project_id},
        )
    return project


def _diag_params(diags: Sequence) -> dict:
    """领域诊断 → 错误信封 params(诊断明细, 供前端按 message_key 渲染)。"""
    return {
        "count": len(diags),
        "diagnostics": [
            {
                "code": d.code,
                "detail": d.params.get("detail") or "",
                "field": (d.location or {}).get("field") or "",
            }
            for d in diags
        ],
    }


def _triplet_error_code(exc: FinanceTripletError) -> str:
    """契约校验失败 → 400(PROJ-FIN-001 财务三件套结构非法)。"""
    return "PROJ-FIN-001"


# ---------------------------------------------------------------------------
# Profile 注册表
# ---------------------------------------------------------------------------


def _profile_yaml_bytes(profile: FinanceProfile) -> bytes:
    """Profile 完整 YAML 字节(含自身 content_sha256 字段, 对象字节摘要来源)。

    使用 core.yamlmini.dump(block 风格, 与 load 互逆): 包内/对象文件可被
    安全子集解析, 对象字节 SHA-256 与 content_sha256 分别验证
    (finance-yaml.md「规范化与摘要」)。
    """
    from iesplan.core.yamlmini import dump as yaml_dump

    return yaml_dump(profile.to_dict()).encode("utf-8")


def register_finance_profile(
    db: Session,
    payload: object,
    user_id: int,
) -> tuple[FinanceProfileRow, FinanceProfile]:
    """登记地区 FinanceProfile 到注册表(内容寻址, 幂等去重)。

    - 严格恢复(FinanceProfile.from_dict: 拒未知/缺失字段, content_sha256
      声明必须等于规范摘要);
    - 内容寻址: 相同 profile_id + content_sha256 已登记 → 返回既有行
      (不重复落 YAML 对象);
    - YAML 完整字节写入内容寻址对象(对象字节 SHA-256 与 content_sha256
      两类摘要分别保留, finance-yaml.md「规范化与摘要」);
    - 任何校验失败 → 不落任何行。
    """
    try:
        profile = FinanceProfile.from_dict(payload)
    except FinanceTripletError as exc:
        raise InvalidRequestError(
            f"FinanceProfile 非法: {exc}",
            code=_triplet_error_code(exc), params={"detail": str(exc)},
        ) from exc
    existing = db.execute(
        select(FinanceProfileRow).where(
            FinanceProfileRow.profile_id == profile.profile_id,
            FinanceProfileRow.content_sha256 == profile.content_sha256,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, profile
    obj = put_object(
        db, _profile_yaml_bytes(profile), "application/yaml",
        source_category="finance_profile",
    )
    row = FinanceProfileRow(
        profile_id=profile.profile_id,
        region=profile.profile.get("region", ""),
        content=profile.canonical_dict(),
        content_sha256=profile.content_sha256,
        object_id=obj.id,
        created_by=user_id,
    )
    db.add(row)
    db.flush()
    return row, profile


def get_finance_profile(
    db: Session, profile_id: str
) -> tuple[FinanceProfileRow, FinanceProfile]:
    """读取注册 Profile(按稳定 profile_id 取最新登记; 不存在 → 404)。"""
    row = db.execute(
        select(FinanceProfileRow)
        .where(FinanceProfileRow.profile_id == profile_id)
        .order_by(FinanceProfileRow.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(
            "FinanceProfile 未登记",
            params={"profile_id": profile_id},
            location={"object_type": "finance_profile", "object_id": profile_id},
        )
    profile = FinanceProfile.from_dict({**row.content, "content_sha256": row.content_sha256})
    return row, profile


def profile_row_dict(row: FinanceProfileRow) -> dict:
    """注册 Profile 行 → 公开字典(API/审计用; 不含 ORM 内部字段)。"""
    return {
        "id": row.id,
        "profile_id": row.profile_id,
        "region": row.region,
        "content_sha256": row.content_sha256,
        "object_id": row.object_id,
    }


def list_finance_profiles(db: Session) -> list[dict]:
    """列出已登记地区 Profile(按 profile_id 去重取最新登记)。"""
    rows = db.execute(
        select(FinanceProfileRow)
        .distinct(FinanceProfileRow.profile_id)
        .order_by(FinanceProfileRow.profile_id, FinanceProfileRow.id.desc())
    ).scalars().all()
    return [profile_row_dict(row) for row in rows]


def get_project_profile(
    db: Session, project_id: int
) -> tuple[FinanceProfile, dict]:
    """读项目当前引用的注册 Profile(未引用 → 404)。"""
    project = _get_project(db, project_id)
    if project.finance_profile_id is None:
        raise NotFoundError(
            "项目尚未引用 FinanceProfile",
            params={"project_id": project_id},
            location={"object_type": "finance_profile", "object_id": project_id},
        )
    row = db.get(FinanceProfileRow, project.finance_profile_id)
    if row is None:
        raise NotFoundError("项目引用的 FinanceProfile 不存在")
    profile = FinanceProfile.from_dict({**row.content, "content_sha256": row.content_sha256})
    return profile, profile_row_dict(row)


# ---------------------------------------------------------------------------
# Overrides / Effective(项目级)
# ---------------------------------------------------------------------------


def _set_project_profile(
    db: Session, project: Project, profile_row: FinanceProfileRow
) -> None:
    """项目引用已注册 Profile(更新当前 Profile 指针; Overrides/Effective 一并失效)。"""
    if project.finance_profile_id != profile_row.id:
        project.finance_profile_id = profile_row.id
        # Profile 变更后既有 Overrides/Effective 基于旧 Profile, 摘要链不再成立:
        # 按失败原子清空指针(不静默保留失效快照, 宪法 2.2/4.6)。
        project.overrides_revision = None
        project.effective_finance_revision = None


def _current_effective(
    db: Session, project: Project
) -> tuple[EffectiveFinanceRevision, EffectiveFinanceConfig] | None:
    """读项目当前 Effective revision(无 → None)。"""
    if project.effective_finance_revision is None:
        return None
    row = db.execute(
        select(EffectiveFinanceRevision).where(
            EffectiveFinanceRevision.project_id == project.id,
            EffectiveFinanceRevision.revision == project.effective_finance_revision,
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError(
            "项目 Effective 财务快照指针损坏(指向不存在的 revision)",
            code="PROJ-FIN-003",
            params={"project_id": project.id, "revision": project.effective_finance_revision},
        )
    effective = EffectiveFinanceConfig.from_dict(
        {**row.content, "content_sha256": row.content_sha256}
    )
    if effective.content_sha256 != row.content_sha256:
        raise AppError(
            "Effective 内容摘要与持久化摘要不一致(数据损坏)",
            code="PROJ-FIN-003",
            params={"project_id": project.id, "revision": row.revision},
        )
    return row, effective


def _current_overrides(
    db: Session, project: Project
) -> tuple[FinanceOverridesRevision, FinanceOverrides] | None:
    """读项目当前 Overrides revision(无 → None)。"""
    if project.overrides_revision is None:
        return None
    row = db.execute(
        select(FinanceOverridesRevision).where(
            FinanceOverridesRevision.project_id == project.id,
            FinanceOverridesRevision.revision == project.overrides_revision,
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError(
            "项目 Overrides 指针损坏(指向不存在的 revision)",
            code="PROJ-FIN-003",
            params={"project_id": project.id, "revision": project.overrides_revision},
        )
    overrides = FinanceOverrides.from_dict(
        {**row.content, "content_sha256": row.content_sha256},
        profile=None,  # 结构恢复(不依赖 Profile 在场); 血缘在合并阶段复核
    )
    return row, overrides


def _next_revision(
    db: Session, model: type, project_id: int, project_field: str
) -> int:
    """下一个 revision 序号(表内 max+1)。

    revision 序号由 append-only 表内已有行决定(Profile 切换会清空项目指针,
    指针不复位计数; 避免清空后从 1 重新计数撞唯一约束)。并发下唯一约束
    (project_id, revision)兜底。
    """
    current = db.execute(
        select(func.max(getattr(model, "revision"))).where(
            getattr(model, "project_id") == project_id
        )
    ).scalar()
    return int(current or 0) + 1


def set_project_finance_profile(
    db: Session,
    project_id: int,
    profile_id: str,
    user_id: int,
) -> tuple[int, EffectiveFinanceRevision, EffectiveFinanceConfig]:
    """项目引用已登记 Profile 并原子生成空覆盖 Effective。

    供项目配置 API 与项目包导入确认共用: 用户不能直接 author Effective,
    引用 Profile 即触发合并器生成(空覆盖 → Effective == Profile 内容,
    overrides_sha256 = 空覆盖文档摘要)。
    """
    project = _get_project(db, project_id)
    profile_row, profile = get_finance_profile(db, profile_id)
    _set_project_profile(db, project, profile_row)
    overrides_rev, eff_row, effective = save_finance_overrides_empty(
        db, project_id, None, user_id
    )
    return overrides_rev, eff_row, effective


def save_finance_overrides(
    db: Session,
    project_id: int,
    payload: object,
    expected_revision: int | None,
    user_id: int,
) -> tuple[int, EffectiveFinanceRevision, EffectiveFinanceConfig]:
    """保存 FinanceOverrides: 追加 revision → 重新合并生成 Effective。

    流程(失败原子, 任一失败不落任何行):
    1. 项目必须已引用注册 Profile(否则 422, 无静默默认);
    2. 严格恢复 + 对目标 Profile 结构校验(profile_ref 必须精确匹配, 覆盖
       只许既有叶子, 禁改单位/carrier/direction/tax, 禁新增 finance_type/
       price_id);
    3. 乐观锁: expected_revision 必须等于项目当前 overrides_revision
       (None=首次, 空覆盖文档语义; 不匹配 → 409);
    4. 追加 Overrides revision(空覆盖 = 显式空文档, overrides_sha256 = 空
       覆盖摘要);
    5. 从 Profile + 新 Overrides 确定性合并 → 追加 Effective revision →
       更新项目 overrides_revision + effective_finance_revision。

    返回 (overrides_revision, effective_row, effective)。
    """
    project = _get_project(db, project_id)
    if project.finance_profile_id is None:
        raise InvalidRequestError(
            "项目尚未引用已注册 FinanceProfile(请先选择地区 Profile)",
            code="PROJ-FIN-002",
            params={"detail": "项目未设置 Profile"},
        )
    profile_row = db.get(FinanceProfileRow, project.finance_profile_id)
    if profile_row is None:
        raise AppError(
            "项目 Profile 指针损坏", code="PROJ-FIN-003",
            params={"project_id": project_id},
        )
    profile = FinanceProfile.from_dict(
        {**profile_row.content, "content_sha256": profile_row.content_sha256}
    )
    try:
        overrides = FinanceOverrides.from_dict(payload, profile=profile)
    except FinanceTripletError as exc:
        raise InvalidRequestError(
            f"FinanceOverrides 非法: {exc}",
            code=_triplet_error_code(exc), params={"detail": str(exc)},
        ) from exc
    if project.overrides_revision != expected_revision:
        raise ConflictError(
            "财务覆盖已在其他会话中修改, 请基于最新 revision 重试",
            code="SYS-STORE-004",
            params={
                "expected_revision": expected_revision,
                "current": project.overrides_revision,
            },
        )
    # 追加 Overrides revision(表内 max+1; Profile 切换后指针清空不重置计数)
    next_overrides_rev = _next_revision(db, FinanceOverridesRevision, project_id, "overrides_revision")
    overrides_row = FinanceOverridesRevision(
        project_id=project_id,
        revision=next_overrides_rev,
        content=overrides.canonical_dict(),
        content_sha256=overrides.content_sha256,
        profile_id=overrides.profile_ref["id"],
        profile_sha256=overrides.profile_ref["content_sha256"],
        created_by=user_id,
    )
    db.add(overrides_row)
    db.flush()
    project.overrides_revision = next_overrides_rev
    # 合并生成 Effective(任一失败抛 FinanceTripletError → 调用方回滚)
    effective = merge_effective(profile, overrides)
    next_eff_rev = _next_revision(
        db, EffectiveFinanceRevision, project_id, "effective_finance_revision"
    )
    eff_row = EffectiveFinanceRevision(
        project_id=project_id,
        revision=next_eff_rev,
        content=effective.canonical_dict(),
        content_sha256=effective.content_sha256,
        profile_id=effective.profile_id,
        profile_sha256=effective.profile_sha256,
        overrides_sha256=effective.overrides_sha256,
        created_by=user_id,
    )
    db.add(eff_row)
    project.effective_finance_revision = next_eff_rev
    db.flush()
    return next_overrides_rev, eff_row, effective


def save_finance_overrides_empty(
    db: Session,
    project_id: int,
    expected_revision: int | None,
    user_id: int,
) -> tuple[int, EffectiveFinanceRevision, EffectiveFinanceConfig]:
    """无覆盖保存: 显式空 Overrides 文档 → 合并结果等于 Profile。"""
    project = _get_project(db, project_id)
    profile_row = db.get(FinanceProfileRow, project.finance_profile_id)
    if profile_row is None:
        raise InvalidRequestError(
            "项目尚未引用已注册 FinanceProfile",
            code="PROJ-FIN-002", params={"detail": "项目未设置 Profile"},
        )
    profile = FinanceProfile.from_dict(
        {**profile_row.content, "content_sha256": profile_row.content_sha256}
    )
    empty = FinanceOverrides.empty_for_profile(profile)
    return save_finance_overrides(
        db, project_id, empty.to_dict(), expected_revision, user_id
    )


def get_effective_finance_config(
    db: Session, project_id: int
) -> tuple[EffectiveFinanceConfig, int, EffectiveFinanceRevision]:
    """读取项目当前生效 EffectiveFinanceConfig; 未生成 → 404(无静默默认)。"""
    project = _get_project(db, project_id)
    current = _current_effective(db, project)
    if current is None:
        raise NotFoundError(
            "项目尚未生成有效财务快照(EffectiveFinanceConfig)",
            params={"project_id": project_id},
            location={"object_type": "effective_finance_config", "object_id": project_id},
        )
    return current[1], current[0].revision, current[0]


def get_finance_overrides(
    db: Session, project_id: int
) -> tuple[FinanceOverrides | None, int | None]:
    """读项目当前 Overrides(无覆盖 → (None, None); 有 → (obj, revision))。"""
    project = _get_project(db, project_id)
    current = _current_overrides(db, project)
    if current is None:
        return None, None
    return current[1], current[0].revision


# ---------------------------------------------------------------------------
# 规划配置
# ---------------------------------------------------------------------------


def get_planning_config(
    db: Session, project_id: int
) -> tuple[PlanningConfig, int, PlanningConfigRevision]:
    """读取项目当前生效规划配置; 未保存过 → 404。"""
    project = _get_project(db, project_id)
    if project.planning_revision is None:
        raise NotFoundError(
            "项目尚未保存规划配置",
            params={"project_id": project_id},
            location={"object_type": "planning_config", "object_id": project_id},
        )
    row = db.execute(
        select(PlanningConfigRevision)
        .where(
            PlanningConfigRevision.project_id == project_id,
            PlanningConfigRevision.revision == project.planning_revision,
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError(
            "项目规划配置指针损坏(指向不存在的 revision)",
            code="PROJ-PLAN-004",
            params={"project_id": project_id, "revision": project.planning_revision},
        )
    config = PlanningConfig.from_dict(row.content)
    if config.revision != row.content_sha256:
        raise AppError(
            "规划配置内容摘要与持久化摘要不一致(数据损坏)",
            code="PROJ-PLAN-004",
            params={"project_id": project_id, "revision": row.revision},
        )
    return config, row.revision, row


def save_planning_config(
    db: Session,
    project_id: int,
    payload: object,
    expected_revision: int | None,
    user_id: int,
) -> tuple[PlanningConfigRevision, int]:
    """保存规划配置: 强制 finance_content_sha256 与当前 Effective 一致。

    - 项目未生成 EffectiveFinanceConfig → 400(规划必须引用已生成的有效
      财务快照内容摘要);
    - payload.finance_content_sha256 ≠ 当前 Effective.content_sha256 → 400
      (PROJ-PLAN-002), 不落任何行;
    - 乐观锁同前。
    """
    project = _get_project(db, project_id)
    try:
        config = PlanningConfig.from_dict(payload)
    except Exception as exc:  # PlanningConfigError
        raise InvalidRequestError(
            f"规划配置非法: {exc}", code="PROJ-PLAN-001", params={"detail": str(exc)}
        ) from exc
    diags = validate_planning_domain(config)
    if diags:
        raise InvalidRequestError(
            "规划配置领域校验失败",
            code="PROJ-PLAN-003",
            params=_diag_params(diags),
        )
    if project.effective_finance_revision is None:
        raise InvalidRequestError(
            "规划配置必须引用已生成的有效财务快照(请先设置 Profile 并保存财务覆盖)",
            code="PROJ-PLAN-002",
            params={"detail": "项目尚未生成 EffectiveFinanceConfig"},
        )
    current = _current_effective(db, project)
    if current is None:
        raise InvalidRequestError(
            "规划配置必须引用已生成的有效财务快照",
            code="PROJ-PLAN-002",
            params={"detail": "项目有效财务快照指针损坏"},
        )
    _, effective = current
    if config.finance_content_sha256 != effective.content_sha256:
        raise InvalidRequestError(
            "规划配置引用的 EffectiveFinanceConfig content_sha256 与当前不一致",
            code="PROJ-PLAN-002",
            params=_diag_params(
                [
                    _revision_mismatch_diag(
                        config.finance_content_sha256, effective.content_sha256
                    )
                ]
            ),
        )
    if project.planning_revision != expected_revision:
        raise ConflictError(
            "规划配置已在其他会话中修改, 请基于最新 revision 重试",
            code="SYS-STORE-004",
            params={"expected_revision": expected_revision, "current": project.planning_revision},
        )
    next_revision = _next_revision(db, PlanningConfigRevision, project_id, "planning_revision")
    row = PlanningConfigRevision(
        project_id=project_id,
        revision=next_revision,
        content=config.to_dict(),
        content_sha256=config.revision,
        finance_content_sha256=config.finance_content_sha256,
        created_by=user_id,
    )
    db.add(row)
    project.planning_revision = next_revision
    db.flush()
    return row, next_revision


def _revision_mismatch_diag(declared: str, current: str):
    """规划引用与当前 Effective 内容摘要不一致诊断(供 400 params)。"""
    from iesplan.core.diagnostics import make_diag

    return make_diag(
        "PROJ-PLAN-002",
        params={
            "detail": (
                f"规划配置引用 finance_content_sha256={declared}, "
                f"当前有效财务快照实际 content_sha256={current}"
            )
        },
        location={"object_type": "planning_config", "field": "finance_content_sha256"},
    )
