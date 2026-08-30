"""规划/财务配置 revision API(0.6.5 事项 3)。

- GET  /api/projects/{id}/finance-config      当前财务配置 + revision
- PUT  /api/projects/{id}/finance-config      追加不可变 revision(乐观锁)
- GET  /api/projects/{id}/planning-config     当前规划配置 + revision
- PUT  /api/projects/{id}/planning-config     追加不可变 revision(乐观锁)

认证与权限: 全部端点要求窗口会话认证(CurrentUser); 读要求项目 view,
写要求项目 edit(403)。校验失败 400/422 标准错误信封; 并发冲突 409
(SYS-STORE-004); 未保存 404(无静默默认, 宪法 2.2)。

DTO 契约(宪法 8.1): 请求/响应字段与 core 契约一一对应; finance_config /
planning_config 为完整字典形态(含派生 revision 字段, 由服务层严格恢复)。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from iesplan.api.auth import CurrentUser
from iesplan.db import get_db
from iesplan.services import config_revisions as config_service
from iesplan.services import project as project_service

#: FastAPI 依赖注入的数据库会话
DbSession = Annotated[Session, Depends(get_db)]

router = APIRouter(prefix="/api/projects/{project_id}", tags=["config-revisions"])


class FinanceConfigSaveRequest(BaseModel):
    """保存财务配置请求体(乐观锁: expected_revision=当前指针; 首次保存为 null)。"""

    finance_config: dict[str, Any]
    expected_revision: int | None = Field(default=None, ge=1)


class PlanningConfigSaveRequest(BaseModel):
    """保存规划配置请求体(finance_config 引用一致性由服务层强制)。"""

    planning_config: dict[str, Any]
    expected_revision: int | None = Field(default=None, ge=1)


def _finance_response(config: Any, revision: int) -> dict:
    return {"finance_config": config.to_dict(), "revision": revision}


def _planning_response(config: Any, revision: int) -> dict:
    return {"planning_config": config.to_dict(), "revision": revision}


@router.get("/finance-config", summary="当前公共财务配置")
def get_finance_config_endpoint(
    project_id: int,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """读取项目当前生效财务配置(未保存 → 404 标准错误信封)。"""
    project_service.ensure_access(db, user, project_id, "view")
    config, revision, _ = config_service.get_finance_config(db, project_id)
    return _finance_response(config, revision)


@router.put("/finance-config", summary="保存公共财务配置(新 revision)")
def save_finance_config_endpoint(
    project_id: int,
    payload: FinanceConfigSaveRequest,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """保存财务配置: 追加不可变 revision 并更新项目指针(乐观锁 409)。"""
    project_service.ensure_access(db, user, project_id, "edit")
    _, revision = config_service.save_finance_config(
        db, project_id, payload.finance_config, payload.expected_revision, user.id
    )
    db.commit()
    config, _, _ = config_service.get_finance_config(db, project_id)
    return _finance_response(config, revision)


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
    """保存规划配置: 强制 finance_revision 与当前财务配置一致(422), 乐观锁 409。"""
    project_service.ensure_access(db, user, project_id, "edit")
    _, revision = config_service.save_planning_config(
        db, project_id, payload.planning_config, payload.expected_revision, user.id
    )
    db.commit()
    config, _, _ = config_service.get_planning_config(db, project_id)
    return _planning_response(config, revision)
