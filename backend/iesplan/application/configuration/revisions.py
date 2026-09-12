"""财务三件套与规划配置 revision 用例(application/configuration)。

finance profile / overrides / planning 读写编排（旧
``iesplan.services.config_revisions`` 已删除），经 configuration/project
域公开门面 + finance 值对象 + planning 域规则 + storage 公开门面实现。

复制来源（基线 5c40b01 ``services/config_revisions.py``，0.6.5 条目 1-2）：
- Profile 登记：``register_finance_profile`` / ``get_finance_profile_by_ref``
  / ``profile_row_dict`` / ``list_finance_profiles`` / ``get_project_profile``；
- Overrides / Effective：``set_project_finance_profile`` /
  ``save_finance_overrides`` / ``save_finance_overrides_empty`` /
  ``delete_finance_overrides`` / ``get_effective_finance_config`` /
  ``get_finance_overrides``；
- 规划配置：``get_planning_config`` / ``save_planning_config``。

职责（与旧服务一致）：
- Profile 登记: 地区 FinanceProfile 注册表(按 profile_id 复用; 每次登记新行);
- Overrides 保存: 追加不可变 revision 并更新项目指针; revision 号单调递增;
- Effective 生成/保存: 保存 Overrides 后由确定性合并器从 Profile + Overrides
  重新合并并完整校验后追加; 用户不能直接 author Effective；
- Planning 保存: 规划与结果财务计算固定同一有效快照(由项目指针保证);
- 并发保护: 保存必须携带 expected_revision(当前指针值), 不匹配 → 409；
- 失败原子: 任一校验/合并/摘要不一致失败 → 不落任何行。

事务：写用例顶层函数拥有提交/回滚（``db.commit`` 收尾，失败 ``db.rollback``）；
内部实现只 ``flush``（旧服务本就只 flush，由 API 层提交；此处改由用例顶层提交）。
读用例不提交事务。

调用方向：``api → application.configuration.revisions → {configuration,
project} 域公开门面 + finance 值对象（FinanceProfile/FinanceOverrides/
EffectiveFinanceConfig/merge_effective）+ planning 域规则 +
storage 公开门面``；不导入 ORM、不导入其他域内部模块、不调用 ``services.*``。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from iesplan import configuration as configuration_domain
from iesplan import project as project_domain
from iesplan.configuration.contracts import (
    EffectiveRevisionRecord,
    FinanceProfileRecord,
    OverridesRevisionRecord,
    PlanningRevisionRecord,
)
from iesplan.core.contracts import PlanningConfig, PlanningConfigError
from iesplan.core.diagnostics import SEVERITY_ERROR
from iesplan.core.errors import AppError, ConflictError, NotFoundError
from iesplan.finance import (
    EffectiveFinanceConfig,
    FinanceOverrides,
    FinanceProfile,
    FinanceTripletError,
    merge_effective,
)
from iesplan.core.yamlmini import dump as yaml_dump
from iesplan.planning.contracts import validate_planning_domain
from iesplan.project.contracts import ProjectRecord
from iesplan.storage import add_ref, put_object


class InvalidRequestError(AppError):
    """配置校验失败(HTTP 400; code 为 PROJ-FIN/PROJ-PLAN 领域码)。"""

    code = "PROJ-FIN-001"
    http_status = 400
    severity = SEVERITY_ERROR
    message_key = "ies.diag.param.invalid"


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
    """Profile 完整 YAML 字节。

    使用 core.yamlmini.dump(block 风格, 与 load 互逆)。文本文件只校验字头。
    """
    return yaml_dump(profile.to_dict()).encode("utf-8")


def _register_finance_profile(
    db: Session,
    payload: object,
    user_id: int,
) -> tuple[FinanceProfileRecord, FinanceProfile]:
    """登记地区 FinanceProfile 到注册表（只 flush，不提交）。

    - 严格恢复(FinanceProfile.from_dict: 拒未知/缺失字段);
    - 注册表按 profile_id 唯一: 同 id 已存在即复用既有行(不改写内容);
    - 新登记时 YAML 完整字节写入对象并建立稳定 owner 引用(防 orphan 清理);
    - 任何校验失败 → 不落任何行。
    """
    try:
        profile = FinanceProfile.from_dict(payload)
    except FinanceTripletError as exc:
        raise InvalidRequestError(
            f"FinanceProfile 非法: {exc}",
            code=_triplet_error_code(exc),
            params={"detail": str(exc)},
        ) from exc
    existing = configuration_domain.get_profile(db, profile.profile_id)
    # 注册表按 profile_id 唯一(0007 起): 同 id 已存在即复用既有行, 不改写
    # 内容(文本文件只校验字头, 不做内容摘要去重); 返回既有行规范内容。
    if existing is not None:
        return existing, FinanceProfile.from_dict(existing.content)
    obj = put_object(
        db,
        _profile_yaml_bytes(profile),
        "application/yaml",
        source_category="finance_profile",
    )
    # profile_id 重复 → ConfigurationConflictError(409)直接抛给调用方;
    # 不在此处 rollback/回读复用: 事务归属按宪法由上层(application 用例)拥有。
    row = configuration_domain.register_profile(
        db,
        profile_id=profile.profile_id,
        region=profile.profile.get("region", ""),
        content=profile.to_dict(),
        object_id=obj.id,
        created_by=user_id,
    )
    # 建立稳定 owner 引用, 防止对象被当作 orphan 清理(宪法 10.3)
    add_ref(
        db,
        obj.id,
        "finance_profile",
        row.id,
        ref_entity_type="finance_profiles",
        purpose="finance_profile_yaml",
    )
    db.flush()
    return row, profile


def register_finance_profile(
    db: Session,
    payload: object,
    user_id: int,
) -> tuple[FinanceProfileRecord, FinanceProfile]:
    """事务型登记地区 FinanceProfile；application 层统一提交或回滚。"""
    try:
        result = _register_finance_profile(db, payload, user_id)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def get_finance_profile_by_ref(db: Session, profile_id: str) -> tuple[FinanceProfileRecord, FinanceProfile]:
    """按 {id} 取注册 Profile(同 id 多次登记取最新行; 不存在 → 404)。文本文件只校验字头。"""
    row = configuration_domain.get_profile(db, profile_id)
    if row is None:
        raise NotFoundError(
            "FinanceProfile 未登记",
            params={"profile_id": profile_id},
            location={"object_type": "finance_profile", "object_id": profile_id},
        )
    profile = FinanceProfile.from_dict(row.content)
    return row, profile


def profile_row_dict(row: FinanceProfileRecord) -> dict:
    """注册 Profile 行 → 公开字典(API/审计用; 不含 ORM 内部字段)。"""
    return {
        "id": row.id,
        "profile_id": row.profile_id,
        "region": row.region,
        "object_id": row.object_id,
    }


def list_finance_profiles(db: Session) -> list[dict]:
    """列出已登记地区 Profile(按 profile_id 去重取最新登记, 跨库确定性)。"""
    rows = configuration_domain.list_profiles(db)
    seen: dict[str, FinanceProfileRecord] = {}
    for row in rows:
        if row.profile_id not in seen:
            seen[row.profile_id] = row
    return [profile_row_dict(seen[k]) for k in sorted(seen)]


def get_project_profile(db: Session, project_id: int) -> tuple[FinanceProfile, dict]:
    """读项目当前引用的注册 Profile(未引用 → 404)。"""
    project = project_domain.require_project(db, project_id)
    if project.finance_profile_id is None:
        raise NotFoundError(
            "项目尚未引用 FinanceProfile",
            params={"project_id": project_id},
            location={"object_type": "finance_profile", "object_id": project_id},
        )
    row = configuration_domain.get_profile_row(db, project.finance_profile_id)
    if row is None:
        raise NotFoundError("项目引用的 FinanceProfile 不存在")
    profile = FinanceProfile.from_dict(row.content)
    return profile, profile_row_dict(row)


# ---------------------------------------------------------------------------
# Overrides / Effective(项目级)
# ---------------------------------------------------------------------------


def _set_project_profile(
    db: Session, project: ProjectRecord, profile_row: FinanceProfileRecord
) -> ProjectRecord:
    """项目引用已注册 Profile(更新当前 Profile 指针; Overrides/Effective/Planning 一并失效)。"""
    if project.finance_profile_id != profile_row.id:
        # Profile 行变更后既有 Overrides/Effective 基于旧行, 显式 revision 引用不再成立:
        # 按失败原子清空指针(不静默保留失效快照, 宪法 2.2/4.6)。
        return project_domain.update_revision_pointers(
            db,
            project.id,
            finance_profile_id=profile_row.id,
            overrides_revision=None,
            effective_finance_revision=None,
            planning_revision=None,
        )
    return project


def _current_effective(
    db: Session, project: ProjectRecord
) -> tuple[EffectiveRevisionRecord, EffectiveFinanceConfig] | None:
    """读项目当前 Effective revision(无 → None)。"""
    if project.effective_finance_revision is None:
        return None
    row = configuration_domain.get_effective_revision(db, project.id, project.effective_finance_revision)
    if row is None:
        raise AppError(
            "项目 Effective 财务快照指针损坏(指向不存在的 revision)",
            code="PROJ-FIN-003",
            params={"project_id": project.id, "revision": project.effective_finance_revision},
        )
    effective = EffectiveFinanceConfig.from_dict(row.content)
    return row, effective


def _current_overrides(
    db: Session, project: ProjectRecord
) -> tuple[OverridesRevisionRecord, FinanceOverrides] | None:
    """读项目当前 Overrides revision(无 → None)。"""
    if project.overrides_revision is None:
        return None
    row = configuration_domain.get_overrides_revision(db, project.id, project.overrides_revision)
    if row is None:
        raise AppError(
            "项目 Overrides 指针损坏(指向不存在的 revision)",
            code="PROJ-FIN-003",
            params={"project_id": project.id, "revision": project.overrides_revision},
        )
    overrides = FinanceOverrides.from_dict(
        row.content,
        profile=None,  # 结构恢复(不依赖 Profile 在场)
    )
    return row, overrides


def _store_config_receipt(
    db: Session,
    *,
    kind: str,
    project_id: int,
    revision: int,
    refs: dict,
    created_by: int,
) -> int:
    """可审计回执落对象存储, 返回对象 id(0.6.5 条目 1 退出标准)。

    回执记录本次 revision 固定的引用(稳定 ID/revision), 不存业务文本
    摘要; 调用方把返回 id 写入 revision 行 receipt_object_id 并建 owner
    引用(防 orphan 清理)。失败抛错, 由外层事务回滚(无部分发布)。
    """
    receipt = {
        "schema": "ies.config-receipt",
        "schema_version": "1",
        "kind": kind,
        "project_id": project_id,
        "revision": revision,
        "refs": dict(refs),
        "created_by": created_by,
        "created_at": datetime.now(UTC).isoformat(),
    }
    handle = put_object(
        db,
        json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        "application/json",
        source_category="config_receipt",
    )
    return handle.id


def _attach_config_receipt(
    db: Session,
    *,
    object_id: int,
    ref_type: str,
    row_id: int,
    ref_entity_type: str,
    purpose: str,
) -> None:
    """回执对象建 owner 引用(引用清单为权威, 防 orphan 清理误回收)。"""
    add_ref(
        db,
        object_id,
        ref_type,
        row_id,
        ref_entity_type=ref_entity_type,
        purpose=purpose,
    )


def _set_project_finance_profile(
    db: Session,
    project_id: int,
    profile_id: str,
    user_id: int,
) -> tuple[int, EffectiveRevisionRecord, EffectiveFinanceConfig]:
    """项目引用已登记 Profile 并原子生成空覆盖 Effective（只 flush，不提交）。

    用户不能直接 author Effective, 引用 Profile 即触发合并器生成
    (空覆盖 → Effective == Profile 内容)。
    """
    project = project_domain.require_project(db, project_id)
    profile_row, _ = get_finance_profile_by_ref(db, profile_id)
    # 失效旧 Planning 由 _set_project_profile + save_finance_overrides_empty 共同保证
    project = _set_project_profile(db, project, profile_row)
    # 传入当前指针(None 因已清空)以生成空覆盖
    overrides_rev, eff_row, effective = _save_finance_overrides_empty(
        db, project_id, project.overrides_revision, user_id
    )
    return overrides_rev, eff_row, effective


def set_project_finance_profile(
    db: Session,
    project_id: int,
    profile_id: str,
    user_id: int,
) -> tuple[int, EffectiveRevisionRecord, EffectiveFinanceConfig]:
    """事务型项目引用 Profile；application 层统一提交或回滚。"""
    try:
        result = _set_project_finance_profile(db, project_id, profile_id, user_id)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def _save_finance_overrides(
    db: Session,
    project_id: int,
    payload: object,
    expected_revision: int | None,
    user_id: int,
) -> tuple[int, EffectiveRevisionRecord, EffectiveFinanceConfig]:
    """保存 FinanceOverrides: 追加 revision → 重新合并生成 Effective（只 flush，不提交）。

    流程(失败原子, 任一失败不落任何行):
    1. 项目必须已引用注册 Profile(否则 400, 无静默默认);
    2. 严格恢复 + 对目标 Profile 结构校验(profile_ref 必须精确匹配, 覆盖
       只许既有叶子, 禁改单位/carrier/direction/tax, 禁新增 finance_type/
       price_id);
    3. 乐观锁: expected_revision 必须等于项目当前 overrides_revision
       (None=首次, 空覆盖文档语义; 不匹配 → 409);
    4. 追加 Overrides revision(空覆盖 = 显式空文档; 版本链以显式 revision
       引用, 不计算业务文本摘要);
    5. 从 Profile + 新 Overrides 确定性合并 → 追加 Effective revision →
       更新项目 overrides_revision + effective_finance_revision;
    6. 任何新 Effective 生成都会使当前 planning 指针失效(历史行保留,
       宪法 4.6: 规划必须重新引用新有效快照)。

    返回 (overrides_revision, effective_row, effective)。
    """
    project = project_domain.require_project(db, project_id)
    if project.finance_profile_id is None:
        raise InvalidRequestError(
            "项目尚未引用已注册 FinanceProfile(请先选择地区 Profile)",
            code="PROJ-FIN-002",
            params={"detail": "项目未设置 Profile"},
        )
    profile_row = configuration_domain.get_profile_row(db, project.finance_profile_id)
    if profile_row is None:
        raise AppError(
            "项目 Profile 指针损坏",
            code="PROJ-FIN-003",
            params={"project_id": project_id},
        )
    profile = FinanceProfile.from_dict(profile_row.content)
    try:
        overrides = FinanceOverrides.from_dict(payload, profile=profile)
    except FinanceTripletError as exc:
        raise InvalidRequestError(
            f"FinanceOverrides 非法: {exc}",
            code=_triplet_error_code(exc),
            params={"detail": str(exc)},
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
    # 追加 Overrides revision(序号经域预读表内 max+1; Profile 切换后指针清空
    # 不重置计数; 回执对象先于行落盘, 故序号显式传入, 唯一约束兜底并发)
    next_overrides_rev = configuration_domain.next_overrides_revision(db, project_id)
    overrides_receipt_id = _store_config_receipt(
        db,
        kind="finance_overrides",
        project_id=project_id,
        revision=next_overrides_rev,
        refs={"profile_id": overrides.profile_ref["id"]},
        created_by=user_id,
    )
    overrides_row = configuration_domain.append_overrides(
        db,
        project_id=project_id,
        revision=next_overrides_rev,
        content=overrides.to_dict(),
        profile_id=overrides.profile_ref["id"],
        created_by=user_id,
        receipt_object_id=overrides_receipt_id,
    )
    _attach_config_receipt(
        db,
        object_id=overrides_row.receipt_object_id,
        ref_type="finance_overrides_revision",
        row_id=overrides_row.id,
        ref_entity_type="finance_overrides",
        purpose="finance_overrides_receipt",
    )
    # 合并生成 Effective(from_dict 已按 Profile 完成全部结构校验, 合并失败即
    # 内部错误, 不吞异常, 事务回滚后以 500 可见)
    effective = merge_effective(profile, overrides)
    next_eff_rev = configuration_domain.next_effective_revision(db, project_id)
    eff_receipt_id = _store_config_receipt(
        db,
        kind="effective_finance",
        project_id=project_id,
        revision=next_eff_rev,
        refs={
            "profile_id": effective.profile_id,
            "overrides_revision": next_overrides_rev,
        },
        created_by=user_id,
    )
    eff_row = configuration_domain.append_effective(
        db,
        project_id=project_id,
        revision=next_eff_rev,
        content=effective.to_dict(),
        profile_id=effective.profile_id,
        created_by=user_id,
        receipt_object_id=eff_receipt_id,
    )
    _attach_config_receipt(
        db,
        object_id=eff_row.receipt_object_id,
        ref_type="effective_finance_revision",
        row_id=eff_row.id,
        ref_entity_type="effective_finance_revisions",
        purpose="effective_finance_receipt",
    )
    # 任何新 Effective 生成即失效旧 Planning(历史行保留, 指针清空)；
    # 指针移动经 project 域 repository 一次完成。
    project_domain.update_revision_pointers(
        db,
        project_id,
        finance_profile_id=project.finance_profile_id,
        overrides_revision=next_overrides_rev,
        effective_finance_revision=next_eff_rev,
        planning_revision=None,
    )
    db.flush()
    return next_overrides_rev, eff_row, effective


def save_finance_overrides(
    db: Session,
    project_id: int,
    payload: object,
    expected_revision: int | None,
    user_id: int,
) -> tuple[int, EffectiveRevisionRecord, EffectiveFinanceConfig]:
    """事务型保存 FinanceOverrides；application 层统一提交或回滚。"""
    try:
        result = _save_finance_overrides(db, project_id, payload, expected_revision, user_id)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def _save_finance_overrides_empty(
    db: Session,
    project_id: int,
    expected_revision: int | None,
    user_id: int,
) -> tuple[int, EffectiveRevisionRecord, EffectiveFinanceConfig]:
    """无覆盖保存: 显式空 Overrides 文档 → 合并结果等于 Profile（只 flush，不提交）。"""
    project = project_domain.require_project(db, project_id)
    profile_row = configuration_domain.get_profile_row(db, project.finance_profile_id)
    if profile_row is None:
        raise InvalidRequestError(
            "项目尚未引用已注册 FinanceProfile",
            code="PROJ-FIN-002",
            params={"detail": "项目未设置 Profile"},
        )
    profile = FinanceProfile.from_dict(profile_row.content)
    empty = FinanceOverrides.empty_for_profile(profile)
    return _save_finance_overrides(db, project_id, empty.to_dict(), expected_revision, user_id)


def save_finance_overrides_empty(
    db: Session,
    project_id: int,
    expected_revision: int | None,
    user_id: int,
) -> tuple[int, EffectiveRevisionRecord, EffectiveFinanceConfig]:
    """事务型无覆盖保存；application 层统一提交或回滚。"""
    try:
        result = _save_finance_overrides_empty(db, project_id, expected_revision, user_id)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def _delete_finance_overrides(
    db: Session,
    project_id: int,
    expected_revision: int | None,
    user_id: int,
) -> tuple[int, EffectiveRevisionRecord, EffectiveFinanceConfig]:
    """清空覆盖: 追加显式空 Overrides + 新 Effective, 失效旧 Planning（只 flush，不提交）。

    不是删除历史, 而是追加空文档 revision(宪法 11 不可变追加)。
    """
    return _save_finance_overrides_empty(db, project_id, expected_revision, user_id)


def delete_finance_overrides(
    db: Session,
    project_id: int,
    expected_revision: int | None,
    user_id: int,
) -> tuple[int, EffectiveRevisionRecord, EffectiveFinanceConfig]:
    """事务型清空覆盖；application 层统一提交或回滚。"""
    try:
        result = _delete_finance_overrides(db, project_id, expected_revision, user_id)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def get_effective_finance_config(
    db: Session, project_id: int
) -> tuple[EffectiveFinanceConfig, int, EffectiveRevisionRecord]:
    """读取项目当前生效 EffectiveFinanceConfig; 未生成 → 404(无静默默认)。"""
    project = project_domain.require_project(db, project_id)
    current = _current_effective(db, project)
    if current is None:
        raise NotFoundError(
            "项目尚未生成有效财务快照(EffectiveFinanceConfig)",
            params={"project_id": project_id},
            location={"object_type": "effective_finance_config", "object_id": project_id},
        )
    return current[1], current[0].revision, current[0]


def get_finance_overrides(db: Session, project_id: int) -> tuple[FinanceOverrides | None, int | None]:
    """读项目当前 Overrides(无覆盖 → (None, None); 有 → (obj, revision))。"""
    project = project_domain.require_project(db, project_id)
    current = _current_overrides(db, project)
    if current is None:
        return None, None
    return current[1], current[0].revision


# ---------------------------------------------------------------------------
# 规划配置
# ---------------------------------------------------------------------------


def get_planning_config(db: Session, project_id: int) -> tuple[PlanningConfig, int, PlanningRevisionRecord]:
    """读取项目当前生效规划配置; 未保存过 → 404。"""
    project = project_domain.require_project(db, project_id)
    if project.planning_revision is None:
        raise NotFoundError(
            "项目尚未保存规划配置",
            params={"project_id": project_id},
            location={"object_type": "planning_config", "object_id": project_id},
        )
    row = configuration_domain.get_planning_revision(db, project_id, project.planning_revision)
    if row is None:
        raise AppError(
            "项目规划配置指针损坏(指向不存在的 revision)",
            code="PROJ-PLAN-004",
            params={"project_id": project_id, "revision": project.planning_revision},
        )
    config = PlanningConfig.from_dict(row.content)
    return config, row.revision, row


def _save_planning_config(
    db: Session,
    project_id: int,
    payload: object,
    expected_revision: int | None,
    user_id: int,
) -> tuple[PlanningRevisionRecord, int]:
    """保存规划配置（只 flush，不提交）。

    - 项目未生成 EffectiveFinanceConfig → 400;
    - 乐观锁同前。
    """
    project = project_domain.require_project(db, project_id)
    try:
        config = PlanningConfig.from_dict(payload)
    except PlanningConfigError as exc:
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
            "规划配置必须先生成有效财务快照(请先设置 Profile)",
            code="PROJ-PLAN-002",
            params={"detail": "项目尚未生成 EffectiveFinanceConfig"},
        )
    if project.planning_revision != expected_revision:
        raise ConflictError(
            "规划配置已在其他会话中修改, 请基于最新 revision 重试",
            code="SYS-STORE-004",
            params={"expected_revision": expected_revision, "current": project.planning_revision},
        )
    next_revision = configuration_domain.next_planning_revision(db, project_id)
    receipt_object_id = _store_config_receipt(
        db,
        kind="planning_config",
        project_id=project_id,
        revision=next_revision,
        refs={"effective_revision": project.effective_finance_revision},
        created_by=user_id,
    )
    row = configuration_domain.append_planning(
        db,
        project_id=project_id,
        revision=next_revision,
        content=config.to_dict(),
        created_by=user_id,
        receipt_object_id=receipt_object_id,
    )
    project_domain.update_revision_pointers(
        db,
        project_id,
        finance_profile_id=project.finance_profile_id,
        overrides_revision=project.overrides_revision,
        effective_finance_revision=project.effective_finance_revision,
        planning_revision=next_revision,
    )
    db.flush()
    _attach_config_receipt(
        db,
        object_id=row.receipt_object_id,
        ref_type="planning_config_revision",
        row_id=row.id,
        ref_entity_type="planning_configs",
        purpose="planning_config_receipt",
    )
    return row, next_revision


def save_planning_config(
    db: Session,
    project_id: int,
    payload: object,
    expected_revision: int | None,
    user_id: int,
) -> tuple[PlanningRevisionRecord, int]:
    """事务型保存规划配置；application 层统一提交或回滚。"""
    try:
        result = _save_planning_config(db, project_id, payload, expected_revision, user_id)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


__all__ = [
    "InvalidRequestError",
    "delete_finance_overrides",
    "get_effective_finance_config",
    "get_finance_overrides",
    "get_finance_profile_by_ref",
    "get_planning_config",
    "get_project_profile",
    "list_finance_profiles",
    "profile_row_dict",
    "register_finance_profile",
    "save_finance_overrides",
    "save_finance_overrides_empty",
    "save_planning_config",
    "set_project_finance_profile",
]
