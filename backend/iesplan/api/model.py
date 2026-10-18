"""设备类型注册表 API(公开目录 + 项目作用域选择器)。

路由清单:
- GET /api/registry/device-types            设备类型+参数 schema + 真实接口(公开, 供画布)
- GET /api/projects/{project_id}/device-types  公开目录 ∪ 本项目已保存模型(项目内认证)

项目建模写入走项目草稿语义命令(PUT /api/projects/{id}/draft), 不再经本模块。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from iesplan.api.auth import CurrentUser
from iesplan.api.deps import get_request_db
from iesplan.application import models as model_cases

#: 设备类型注册表(公开, 前端画布取设备面板与参数表单 schema)
registry_router = APIRouter(prefix="/api", tags=["registry"])

#: 项目作用域选择器(项目内认证; 公开目录 + 本项目已保存模型)
project_selector_router = APIRouter(prefix="/api/projects/{project_id}", tags=["registry"])


# ---------------------------------------------------------------------------
# 设备类型注册表(公开)
# ---------------------------------------------------------------------------


@registry_router.get("/registry/device-types", summary="设备类型注册表(公开)")
def device_types_public() -> dict[str, Any]:
    """公开设备类型清单 + 参数 schema + 真实端口(RR-P1-04: 供前端画布渲染)。

    数据路径归 application 用例(application.models 门面)；
    端口/方向/载能来自 YAML 设备目录(公开 descriptor)，路由只做转交。
    """
    return model_cases.list_device_types()


# ---------------------------------------------------------------------------
# 项目作用域选择器
# ---------------------------------------------------------------------------


@project_selector_router.get(
    "/device-types", summary="项目设备类型(公开目录 + 本项目已保存模型)"
)
def device_types_for_project(
    project_id: int,
    db: Annotated[Session, Depends(get_request_db)],
    user: CurrentUser,
) -> dict[str, Any]:
    """项目选择器数据：目录设备不消失，本项目已保存模型以真实端口/参数出现。"""
    return model_cases.list_project_device_types(db, user, project_id)
