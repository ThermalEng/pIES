"""财务三件套与规划配置 revision API(0.6.5 条目 1-2;替换旧单体 FinanceConfig)。

项目侧(prefix /api/projects/{project_id}):
- GET  /finance-profile         当前引用 Profile(含注册信息)
- PUT  /finance-profile         引用/切换已登记 Profile(原子生成空覆盖 Effective)
- GET  /finance-overrides       当前 Overrides(无覆盖 → 404)
- PUT  /finance-overrides       保存 Overrides → 确定性重合并生成 Effective
- DELETE /finance-overrides     清空覆盖(回退到 Profile 裸合并, 空覆盖文档)
- GET  /effective-finance       当前 EffectiveFinanceConfig(未生成 → 404)
- GET  /planning-config         当前规划配置 + revision
- PUT  /planning-config         追加不可变 revision(引用 Effective content)

地区 Profile 登记(prefix /api/finance-profiles, 全局):
- GET  /                        已登记 Profile 列表
- POST /                        登记地区 Profile(按 profile_id 复用)
- GET  /{profile_id}            按稳定 id 取 Profile

认证与权限: 全部端点要求窗口会话认证(CurrentUser); 项目读要求 view、
写要求 edit(403)。校验失败 400/422 标准错误信封; 并发冲突 409
(SYS-STORE-004); 未保存 404(无静默默认, 宪法 2.2)。

DTO 契约(宪法 8.1): 请求/响应字段与 core/finance 契约一一对应;
finance_profile / finance_overrides / effective_finance / planning_config
为完整字典形态(由服务层严格恢复，文本文件只校验字头)。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from iesplan.api.auth import CurrentUser
from iesplan.core.errors import NotFoundError, http_error
from iesplan.db import get_db
from iesplan.services import config_revisions as config_service
from iesplan.services import project as project_service

#: FastAPI 依赖注入的数据库会话
DbSession = Annotated[Session, Depends(get_db)]

router = APIRouter(prefix="/api/projects/{project_id}", tags=["config-revisions"])
profile_router = APIRouter(prefix="/api/finance-profiles", tags=["finance-profiles"])


class ProfileSaveRequest(BaseModel):
    """引用/切换地区 FinanceProfile: 必须传 profile_ref{id}。"""

    profile_ref: dict[str, Any] = Field(..., description="引用 {id}")


class OverridesSaveRequest(BaseModel):
    """保存 FinanceOverrides 请求体(乐观锁: expected_revision=当前 overrides_revision)。"""

    finance_overrides: dict[str, Any]
    expected_revision: int | None = Field(default=None, ge=1)


class OverridesDeleteRequest(BaseModel):
    """清空 FinanceOverrides 请求体(乐观锁)。"""

    expected_revision: int | None = Field(default=None, ge=1)


class PlanningConfigSaveRequest(BaseModel):
    """保存规划配置请求体。"""

    planning_config: dict[str, Any]
    expected_revision: int | None = Field(default=None, ge=1)


class ProfileRegistrationRequest(BaseModel):
    """登记地区 Profile 请求体。"""

    finance_profile: dict[str, Any]


def _finance_response(config: Any, revision: int) -> dict:
    return {"finance_config": config.to_dict(), "revision": revision}


def _effective_response(effective: Any, revision: int) -> dict:
    return {"effective_finance_config": effective.to_dict(), "revision": revision}


def _overrides_response(overrides: Any, revision: int) -> dict:
    return {"finance_overrides": overrides.to_dict(), "revision": revision}


def _planning_response(config: Any, revision: int) -> dict:
    return {"planning_config": config.to_dict(), "revision": revision}


def _profile_row_response(row: dict) -> dict:
    return row


# ---------------------------------------------------------------------------
# 项目侧: Profile 引用 / Overrides / Effective / Planning
# ---------------------------------------------------------------------------


@router.get("/finance-profile", summary="当前引用的地区 FinanceProfile")
def get_project_profile_endpoint(
    project_id: int,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """读取项目当前引用的注册 Profile(未引用 → 404)。"""
    project_service.ensure_access(db, user, project_id, "view")
    profile, row = config_service.get_project_profile(db, project_id)
    return {"finance_profile": profile.to_dict(), "row": _profile_row_response(row)}


@router.put("/finance-profile", summary="引用/切换已登记的地区 FinanceProfile")
def set_project_profile_endpoint(
    project_id: int,
    payload: ProfileSaveRequest,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """项目引用已登记 Profile: 引用 {id}, 原子生成空覆盖 Effective。"""
    project_service.ensure_access(db, user, project_id, "edit")
    ref = payload.profile_ref
    profile_id = str(ref.get("id", ""))
    if not profile_id:
        raise http_error(
            400,
            "PROJ-FIN-001",
            "ies.diag.param.invalid",
            detail="profile_ref 必须包含 {id}",
        )
    overrides_rev, eff_row, effective = config_service.set_project_finance_profile(
        db, project_id, profile_id, user.id
    )
    # 提交事务: 指针/空覆盖/Effective/planning 失效一次性持久化
    db.commit()
    _, profile = config_service.get_finance_profile_by_ref(db, profile_id)
    return {
        "finance_profile": profile.to_dict(),
        "overrides_revision": overrides_rev,
        **dict(_effective_response(effective, eff_row.revision)),
    }


@router.get("/finance-overrides", summary="当前 FinanceOverrides")
def get_finance_overrides_endpoint(
    project_id: int,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """读取项目当前 FinanceOverrides(无覆盖 → 404, 不静默返回空文档)。"""
    project_service.ensure_access(db, user, project_id, "view")
    overrides, revision = config_service.get_finance_overrides(db, project_id)
    if overrides is None or revision is None:
        raise NotFoundError(
            "项目尚未保存 FinanceOverrides",
            params={"project_id": project_id},
            location={"object_type": "finance_overrides", "object_id": project_id},
        )
    return _overrides_response(overrides, revision)


@router.put("/finance-overrides", summary="保存 FinanceOverrides(自动重合并 Effective)")
def save_finance_overrides_endpoint(
    project_id: int,
    payload: OverridesSaveRequest,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """保存覆盖: 追加 Overrides revision → 重新合并生成新 Effective(失败原子)。"""
    project_service.ensure_access(db, user, project_id, "edit")
    overrides_rev, eff_row, effective = config_service.save_finance_overrides(
        db, project_id, payload.finance_overrides, payload.expected_revision, user.id
    )
    db.commit()
    overrides, _ = config_service.get_finance_overrides(db, project_id)
    assert overrides is not None
    return {
        **_overrides_response(overrides, overrides_rev),
        **dict(_effective_response(effective, eff_row.revision)),
    }


@router.delete("/finance-overrides", summary="清空 FinanceOverrides(追加空文档, 失效旧 Planning)")
def delete_finance_overrides_endpoint(
    project_id: int,
    payload: OverridesDeleteRequest,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """清空覆盖: 追加显式空 Overrides + 新 Effective, 失效旧 Planning(409 乐观锁)。"""
    project_service.ensure_access(db, user, project_id, "edit")
    overrides_rev, eff_row, effective = config_service.delete_finance_overrides(
        db, project_id, payload.expected_revision, user.id
    )
    db.commit()
    overrides, _ = config_service.get_finance_overrides(db, project_id)
    assert overrides is not None
    return {
        **_overrides_response(overrides, overrides_rev),
        **dict(_effective_response(effective, eff_row.revision)),
    }


@router.get("/effective-finance", summary="当前 EffectiveFinanceConfig")
def get_effective_finance_endpoint(
    project_id: int,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """读取项目当前 EffectiveFinanceConfig(未生成 → 404)。"""
    project_service.ensure_access(db, user, project_id, "view")
    effective, revision, _ = config_service.get_effective_finance_config(db, project_id)
    return _effective_response(effective, revision)


@router.get("/planning-config", summary="当前规划配置")
def get_planning_config_endpoint(
    project_id: int,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """读取项目当前生效规划配置(未保存 → 404)。"""
    project_service.ensure_access(db, user, project_id, "view")
    config, revision, _ = config_service.get_planning_config(db, project_id)
    return _planning_response(config, revision)


@router.put("/planning-config", summary="保存规划配置(新 revision)")
def save_planning_config_endpoint(
    project_id: int,
    payload: PlanningConfigSaveRequest,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """保存规划配置: 乐观锁 409。"""
    project_service.ensure_access(db, user, project_id, "edit")
    _, revision = config_service.save_planning_config(
        db, project_id, payload.planning_config, payload.expected_revision, user.id
    )
    db.commit()
    config, _, _ = config_service.get_planning_config(db, project_id)
    return _planning_response(config, revision)


# ---------------------------------------------------------------------------
# 地区 Profile 注册表(全局)
# ---------------------------------------------------------------------------


@profile_router.get("", summary="已登记 FinanceProfile 列表")
def list_finance_profiles_endpoint(
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """列出已登记地区 Profile(按 profile_id 去重取最新登记)。"""
    items = config_service.list_finance_profiles(db)
    return {"items": items, "count": len(items)}


@profile_router.post("", summary="登记地区 FinanceProfile(注册表按 id 唯一)")
def register_finance_profile_endpoint(
    payload: ProfileRegistrationRequest,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """登记地区 Profile。"""
    row, profile = config_service.register_finance_profile(
        db, payload.finance_profile, user.id
    )
    db.commit()
    return {
        "finance_profile": profile.to_dict(),
        "row": config_service.profile_row_dict(row),
    }


@profile_router.get("/{profile_id}", summary="按稳定 id 取地区 FinanceProfile")
def get_finance_profile_endpoint(
    profile_id: str,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """读取已登记 Profile(按 profile_id 最新登记; 不存在 → 404)。"""
    row, profile = config_service.get_finance_profile_by_ref(db, profile_id)
    return {
        "finance_profile": profile.to_dict(),
        "row": config_service.profile_row_dict(row),
    }
