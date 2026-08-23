"""计算配置 API(U06): /api/projects/{id}/config 与 /api/registry/algorithms。

- GET    /api/projects/{id}/config          当前配置 + 参数元数据(单位/范围/帮助键)
- PUT    /api/projects/{id}/config          保存 {config, expected_revision}; 校验不通过返回 422 + 标准错误信封
- POST   /api/projects/{id}/config/validate 只校验不保存
- GET    /api/projects/{id}/config/default  重新生成默认配置
- GET    /api/registry/algorithms           算法列表 + 能力

注意: 本模块导出 config_router / registry_router 两个路由, 由集成阶段在
main.py 通过 include_router 挂载(get_db 依赖见 iesplan/db.py)。

认证与权限: 配置域全部端点要求窗口会话认证(iesplan.api.auth.CurrentUser,
未认证 401); 读/校验接口要求项目 view, 保存要求项目 edit(403);
算法注册表(/api/registry/algorithms)公开。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from iesplan.api.auth import CurrentUser
from iesplan.core.errors import error_envelope
from iesplan.db import get_db
from iesplan.services import config as config_service
from iesplan.services import project as project_service

#: FastAPI 依赖注入的数据库会话
DbSession = Annotated[Session, Depends(get_db)]

#: 配置域路由: 挂载前缀 /api/projects/{project_id}/config
config_router = APIRouter(prefix="/api/projects/{project_id}/config", tags=["config"])

#: 注册表路由: 挂载前缀 /api/registry
registry_router = APIRouter(prefix="/api/registry", tags=["registry"])


class ConfigSaveRequest(BaseModel):
    """保存请求: 计算配置 + 期望草稿修订(乐观锁)。"""

    config: dict = Field(description="计算配置(结构见 services/config.py)")
    expected_revision: int = Field(ge=1, description="期望的草稿修订号")


class ConfigValidateRequest(BaseModel):
    """校验请求: 只校验不保存。"""

    config: dict = Field(description="计算配置")


def _diagnostics(diags: list) -> list[dict]:
    """诊断对象列表序列化为 dict 列表(04 §5.4 JSON 结构)。"""
    return [d.to_dict() for d in diags]


def _has_errors(diags: list) -> bool:
    """是否存在阻断保存的诊断(error/blocking 任一即阻断)。"""
    return any(d.severity in ("error", "blocking") or d.blocking for d in diags)


@config_router.get("", summary="读取当前计算配置")
def get_config_endpoint(project_id: int, db: DbSession, user: CurrentUser) -> dict:
    """当前配置 + 参数元数据; 未保存过返回生成的默认配置(version=None)。"""
    project_service.ensure_access(db, user, project_id, "view")
    return config_service.get_config(project_id, db)


@config_router.put("", summary="保存计算配置")
def save_config_endpoint(
    project_id: int,
    body: ConfigSaveRequest,
    db: DbSession,
    user: CurrentUser,
) -> JSONResponse:
    """保存配置(与草稿修订绑定); 校验不通过返回 422 + 标准错误信封, 不落库。"""
    project_service.ensure_access(db, user, project_id, "edit")
    graph = config_service.load_work_graph(db, project_id)
    diags = config_service.validate_config(body.config, graph)
    if _has_errors(diags):
        return JSONResponse(
            status_code=422,
            content=error_envelope(
                code="CONFIG-VAL-001",
                message_key="ies.error.data_validation_failed",
                params={"diagnostics": _diagnostics(diags), "count": len(diags)},
            ),
        )
    row = config_service.save_config(db, project_id, body.config, body.expected_revision)
    return JSONResponse(
        status_code=200,
        content={
            "config": config_service._row_to_config(row),
            "meta": config_service.parameter_metadata(graph),
            "version": row.version,
            "status": row.status,
            "diagnostics": [],
        },
    )


@config_router.post("/validate", summary="校验计算配置(不保存)")
def validate_config_endpoint(
    project_id: int,
    body: ConfigValidateRequest,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    """只校验不保存; 始终返回 200 + diagnostics(前端实时校验用)。"""
    project_service.ensure_access(db, user, project_id, "view")
    graph = config_service.load_work_graph(db, project_id)
    diags = config_service.validate_config(body.config, graph)
    return {"diagnostics": _diagnostics(diags), "count": len(diags)}


@config_router.get("/default", summary="重新生成默认计算配置")
def default_config_endpoint(project_id: int, db: DbSession, user: CurrentUser) -> dict:
    """基于系统模型设备清单重新生成默认配置(不保存)。"""
    project_service.ensure_access(db, user, project_id, "view")
    graph = config_service.load_work_graph(db, project_id)
    return {
        "config": config_service.get_default_config(project_id, db),
        "meta": config_service.parameter_metadata(graph),
    }


@registry_router.get("/algorithms", summary="算法注册表列表")
def algorithms_endpoint() -> dict:
    """算法列表 + 能力清单 + 参数规格(供算法选择与能力检查)。"""
    return {"algorithms": config_service.list_algorithms_meta()}
