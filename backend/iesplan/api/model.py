"""设备类型注册表 API(公开, prefix /api/registry)。

路由清单:
- GET /api/registry/device-types  设备类型+参数 schema + 真实接口(公开, 供画布)

项目建模写入走项目草稿语义命令(PUT /api/projects/{id}/draft), 不再经本模块。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from iesplan.application import models as model_cases

#: 设备类型注册表(公开, 前端画布取设备面板与参数表单 schema)
registry_router = APIRouter(prefix="/api", tags=["registry"])


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
