"""财务三件套与规划配置端点用例(application/configuration/revision_cases)。

``api/config_revisions.py`` 每个 HTTP 业务动作只转交本模块的一个完整用例。
用例接收已认证主体、业务参数和事务会话，在内部完成授权、
业务步骤与事务，返回与 HTTP 无关的应用结果；API 只做 DTO、
一次调用和错误/响应映射。

职责（与重构前路由内联顺序一致）：
- 项目侧读：鉴权(view)→读取；
- 项目侧写：鉴权(edit)→追加 revision/重合并→读回；
- 注册表读/写：已认证直接读写（无项目能力检查，与原路由一致）。

事务：写用例经 ``revisions`` 事务型函数拥有提交/回滚；读用例不提交。
本模块不新增校验/哈希/防御分支。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from iesplan.application.configuration import revisions
from iesplan.application.projects import ensure_access
from iesplan.core.errors import NotFoundError
from iesplan.identity.contracts import UserRecord

__all__ = [
    "delete_finance_overrides_case",
    "get_effective_finance_case",
    "get_finance_overrides_case",
    "get_finance_profile_case",
    "get_planning_config_case",
    "get_project_profile_case",
    "list_finance_profiles_case",
    "register_finance_profile_case",
    "save_finance_overrides_case",
    "save_planning_config_case",
    "set_project_profile_case",
]


def get_project_profile_case(
    db: Session, user: UserRecord, project_id: int
) -> dict[str, Any]:
    """读取项目当前引用的注册 Profile（未引用 → 404）。"""
    ensure_access(db, user, project_id, "view")
    profile, row = revisions.get_project_profile(db, project_id)
    return {"finance_profile": profile.to_dict(), "row": row}


def set_project_profile_case(
    db: Session, user: UserRecord, project_id: int, profile_ref: dict[str, Any]
) -> dict[str, Any]:
    """项目引用已登记 Profile：引用 {id}，原子生成空覆盖 Effective。"""
    ensure_access(db, user, project_id, "edit")
    profile_id = str(profile_ref.get("id", ""))
    if not profile_id:
        raise revisions.InvalidRequestError(
            "profile_ref 必须包含 {id}",
            params={"detail": "profile_ref 必须包含 {id}"},
        )
    overrides_rev, eff_row, effective = revisions.set_project_finance_profile(
        db, project_id, profile_id, user.id
    )
    _, profile = revisions.get_finance_profile_by_ref(db, profile_id)
    return {
        "finance_profile": profile.to_dict(),
        "overrides_revision": overrides_rev,
        "effective_finance_config": effective.to_dict(),
        "revision": eff_row.revision,
    }


def get_finance_overrides_case(
    db: Session, user: UserRecord, project_id: int
) -> dict[str, Any]:
    """读取项目当前 FinanceOverrides（无覆盖 → 404，不静默返回空文档）。"""
    ensure_access(db, user, project_id, "view")
    overrides, revision = revisions.get_finance_overrides(db, project_id)
    if overrides is None or revision is None:
        raise NotFoundError(
            "项目尚未保存 FinanceOverrides",
            params={"project_id": project_id},
            location={"object_type": "finance_overrides", "object_id": project_id},
        )
    return {"finance_overrides": overrides.to_dict(), "revision": revision}


def save_finance_overrides_case(
    db: Session,
    user: UserRecord,
    project_id: int,
    finance_overrides: dict[str, Any],
    expected_revision: int | None,
) -> dict[str, Any]:
    """保存覆盖：追加 Overrides revision → 重新合并生成新 Effective（失败原子）。"""
    ensure_access(db, user, project_id, "edit")
    overrides_rev, eff_row, effective = revisions.save_finance_overrides(
        db, project_id, finance_overrides, expected_revision, user.id
    )
    overrides, _ = revisions.get_finance_overrides(db, project_id)
    assert overrides is not None
    return {
        **{"finance_overrides": overrides.to_dict(), "revision": overrides_rev},
        **{
            "effective_finance_config": effective.to_dict(),
            "revision": eff_row.revision,
        },
    }


def delete_finance_overrides_case(
    db: Session,
    user: UserRecord,
    project_id: int,
    expected_revision: int | None,
) -> dict[str, Any]:
    """清空覆盖：追加显式空 Overrides + 新 Effective，失效旧 Planning（409 乐观锁）。"""
    ensure_access(db, user, project_id, "edit")
    overrides_rev, eff_row, effective = revisions.delete_finance_overrides(
        db, project_id, expected_revision, user.id
    )
    overrides, _ = revisions.get_finance_overrides(db, project_id)
    assert overrides is not None
    return {
        **{"finance_overrides": overrides.to_dict(), "revision": overrides_rev},
        **{
            "effective_finance_config": effective.to_dict(),
            "revision": eff_row.revision,
        },
    }


def get_effective_finance_case(
    db: Session, user: UserRecord, project_id: int
) -> dict[str, Any]:
    """读取项目当前 EffectiveFinanceConfig（未生成 → 404）。"""
    ensure_access(db, user, project_id, "view")
    effective, revision, _ = revisions.get_effective_finance_config(db, project_id)
    return {"effective_finance_config": effective.to_dict(), "revision": revision}


def get_planning_config_case(
    db: Session, user: UserRecord, project_id: int
) -> dict[str, Any]:
    """读取项目当前生效规划配置（未保存 → 404）。"""
    ensure_access(db, user, project_id, "view")
    config, revision, _ = revisions.get_planning_config(db, project_id)
    return {"planning_config": config.to_dict(), "revision": revision}


def save_planning_config_case(
    db: Session,
    user: UserRecord,
    project_id: int,
    planning_config: dict[str, Any],
    expected_revision: int | None,
) -> dict[str, Any]:
    """保存规划配置：乐观锁 409。"""
    ensure_access(db, user, project_id, "edit")
    _, revision = revisions.save_planning_config(
        db, project_id, planning_config, expected_revision, user.id
    )
    config, _, _ = revisions.get_planning_config(db, project_id)
    return {"planning_config": config.to_dict(), "revision": revision}


def list_finance_profiles_case(
    db: Session, user: UserRecord
) -> dict[str, Any]:
    """列出已登记地区 Profile（按 profile_id 去重取最新登记）。

    user 为已认证主体证明（全局注册表无项目能力检查，与原路由一致）。
    """
    _ = user
    items = revisions.list_finance_profiles(db)
    return {"items": items, "count": len(items)}


def register_finance_profile_case(
    db: Session, user: UserRecord, finance_profile: dict[str, Any]
) -> dict[str, Any]:
    """登记地区 Profile。"""
    row, profile = revisions.register_finance_profile(
        db, finance_profile, user.id
    )
    return {
        "finance_profile": profile.to_dict(),
        "row": revisions.profile_row_dict(row),
    }


def get_finance_profile_case(
    db: Session, user: UserRecord, profile_id: str
) -> dict[str, Any]:
    """读取已登记 Profile（按 profile_id 最新登记；不存在 → 404）。

    user 为已认证主体证明（全局注册表无项目能力检查，与原路由一致）。
    """
    _ = user
    row, profile = revisions.get_finance_profile_by_ref(db, profile_id)
    return {
        "finance_profile": profile.to_dict(),
        "row": revisions.profile_row_dict(row),
    }
