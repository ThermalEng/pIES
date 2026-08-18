"""系统模型 API(U04 模型写入单元, prefix /api/projects/{project_id}/model)。

路由清单:
- GET    /api/projects/{project_id}/model           系统图(设备+端口+连接+布局)
- POST   /api/projects/{project_id}/model/devices   创建设备(自动生成端口)
- PUT    /api/projects/{project_id}/model/devices/{device_id}   更新参数/位置/名称
- DELETE /api/projects/{project_id}/model/devices/{device_id}   删除设备(级联端口与连接)
- POST   /api/projects/{project_id}/model/connections          创建连接(源→汇)
- DELETE /api/projects/{project_id}/model/connections/{conn_id} 断开连接
- GET    /api/projects/{project_id}/model/validate  拓扑+参数诊断
- GET    /api/registry/device-types                  设备类型+参数 schema(公开, 供画布)

认证说明: 与 iesplan.api.projects 一致, 本阶段以 X-User-Id 请求头模拟认证主体
(集成时以 iesplan.api.auth 的窗口会话凭证校验依赖替换)。

路由挂载(集成阶段在 main.py 中执行):
    app.include_router(registry_router)
    app.include_router(model_router)
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from iesplan.core.diagnostics import SEVERITY_BLOCKING
from iesplan.core.errors import AppError
from iesplan.core.registry import DeviceTypeSpec, ParameterSpec, list_device_types
from iesplan.db import get_db
from iesplan.models.identity import User
from iesplan.services import model as svc

#: 设备类型注册表(公开, 前端画布取设备面板与参数表单 schema)
registry_router = APIRouter(prefix="/api", tags=["registry"])

#: 项目系统图操作(工作图)
model_router = APIRouter(prefix="/api/projects/{project_id}/model", tags=["model"])

DbSession = Annotated[Session, Depends(get_db)]


def _http_error(status: int, code: str, message_key: str, params: dict[str, Any]) -> AppError:
    """构造带指定 HTTP 状态码的应用错误(状态码在错误实例上设置)。"""
    err = AppError("", code=code, severity=SEVERITY_BLOCKING, message_key=message_key, params=params)
    err.http_status = status
    return err


def get_current_user(request: Request, db: DbSession) -> User:
    """当前认证主体(阶段实现: 从 X-User-Id 请求头读取; 正式会话认证由 U01 提供)。

    集成时以 iesplan.api.auth 的窗口会话凭证校验依赖替换本函数。
    """
    raw = request.headers.get("X-User-Id")
    if not raw:
        raise _http_error(401, "AUTH-REQ-001", "ies.diag.perm.denied", {"reason": "missing_identity"})
    try:
        user_id = int(raw)
    except ValueError as exc:
        raise _http_error(401, "AUTH-REQ-001", "ies.diag.perm.denied", {"reason": "bad_identity"}) from exc
    user = db.get(User, user_id)
    if user is None:
        raise _http_error(401, "AUTH-REQ-001", "ies.diag.perm.denied", {"reason": "unknown_user"})
    return user


#: 当前用户依赖(须在 get_current_user 定义之后声明)
CurrentUser = Annotated[User, Depends(get_current_user)]


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------


class Position(BaseModel):
    """画布坐标(布局信息, 不入图内容哈希)。"""

    x: float
    y: float


class DeviceCreate(BaseModel):
    """创建设备请求体(device_type 为注册表类型 id, 如 ies.device.heat_pump)。"""

    device_type: str
    name: str = Field(min_length=1, max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)
    is_existing: bool = False
    model_precision: str = "medium"
    position: Position | None = None


class DeviceUpdate(BaseModel):
    """更新设备请求体(仅更新提供的字段)。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    params: dict[str, Any] | None = None
    position: Position | None = None


class ConnectionCreate(BaseModel):
    """创建连接请求体(from 源端口 → to 汇端口)。"""

    from_port_id: int
    to_port_id: int
    attrs: dict[str, Any] = Field(default_factory=dict)


class ConnectionUpdate(BaseModel):
    """更新连接请求体(容量/损耗率/扩展参数)。"""

    attrs: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 设备类型注册表(公开)
# ---------------------------------------------------------------------------


def _parameter_schema(p: ParameterSpec) -> dict[str, Any]:
    """参数规格 → 公开 schema(前端表单渲染用)。"""
    return {
        "name": p.name,
        "unit": p.unit,
        "min": p.min,
        "max": p.max,
        "default": p.default,
        "is_optimizable": p.is_optimizable,
        "existing_default": p.existing_default,
        "stock_or_addition": p.stock_or_addition,
        "help_key": p.help_key,
        "enum": list(p.enum) if p.enum else None,
    }


def _device_type_schema(spec: DeviceTypeSpec) -> dict[str, Any]:
    """设备类型注册项 → 公开 schema。"""
    return {
        "type_id": spec.type_id,
        "version": spec.version,
        "name_zh": spec.name_zh,
        "name_en": spec.name_en,
        "energy_carriers": list(spec.energy_carriers),
        "is_load": spec.is_load,
        "capabilities": list(spec.capabilities),
        "help_topic": spec.help_topic,
        "parameters": {name: _parameter_schema(p) for name, p in spec.parameters.items()},
    }


@registry_router.get("/registry/device-types", summary="设备类型注册表(公开)")
def device_types_public() -> dict[str, Any]:
    """公开设备类型清单 + 参数 schema(供前端画布渲染设备面板)。"""
    return {"items": [_device_type_schema(spec) for spec in list_device_types()]}


# ---------------------------------------------------------------------------
# 系统图
# ---------------------------------------------------------------------------


@model_router.get("", summary="获取项目系统图(设备+端口+连接+布局)")
def get_model_graph(project_id: int, db: DbSession, user: CurrentUser) -> dict:
    """读取项目工作图: 拓扑(设备/端口/连接)与画布布局对象。"""
    return svc.get_graph(db, project_id)


# ---------------------------------------------------------------------------
# 设备
# ---------------------------------------------------------------------------


@model_router.post("/devices", summary="创建设备", status_code=201)
def create_device(
    project_id: int,
    body: DeviceCreate,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """创建设备: 校验注册表类型与参数, 按载体生成端口, 返回设备与端口。"""
    device = svc.create_device(
        db,
        project_id,
        body.device_type,
        body.name,
        params=body.params,
        is_existing=body.is_existing,
        model_precision=body.model_precision,
        position=body.position.model_dump() if body.position else None,
        created_by=user.id,
    )
    ports = svc.get_device_ports(db, device.id)
    return {"device": svc.serialize_device(device), "ports": [svc.serialize_port(p) for p in ports]}


@model_router.put("/devices/{device_id}", summary="更新设备(参数/位置/名称)")
def update_device(
    project_id: int,
    device_id: int,
    body: DeviceUpdate,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """更新设备名称/参数/位置(仅更新提供的字段; 参数重新按注册表校验)。"""
    device = svc.update_device(
        db,
        project_id,
        device_id,
        name=body.name,
        params=body.params,
        position=body.position.model_dump() if body.position else None,
    )
    return {"device": svc.serialize_device(device)}


@model_router.delete("/devices/{device_id}", summary="删除设备(级联端口与连接)")
def delete_device(
    project_id: int,
    device_id: int,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """删除设备及其端口与关联连接。"""
    svc.delete_device(db, project_id, device_id)
    return {"ok": True, "deleted": device_id}


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------


@model_router.post("/connections", summary="创建连接", status_code=201)
def create_connection(
    project_id: int,
    body: ConnectionCreate,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """创建连接: 校验能源类型一致/方向兼容/同项目/无重复, 失败返回带定位的诊断。"""
    conn = svc.connect(db, project_id, body.from_port_id, body.to_port_id, attrs=body.attrs)
    return {"connection": svc.serialize_connection(conn)}


@model_router.put("/connections/{conn_id}", summary="更新连接(容量/损耗率/扩展参数)")
def update_connection(
    project_id: int,
    conn_id: int,
    body: ConnectionUpdate,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """更新连接属性(capacity/loss_rate/params)。"""
    conn = svc.update_connection(db, project_id, conn_id, body.attrs)
    return {"connection": svc.serialize_connection(conn)}


@model_router.delete("/connections/{conn_id}", summary="断开连接")
def delete_connection(
    project_id: int,
    conn_id: int,
    db: DbSession,
    user: CurrentUser,
) -> dict[str, Any]:
    """删除连接。"""
    svc.disconnect(db, project_id, conn_id)
    return {"ok": True, "deleted": conn_id}


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


@model_router.get("/validate", summary="模型校验(拓扑+参数诊断)")
def validate_model(project_id: int, db: DbSession, user: CurrentUser) -> dict[str, Any]:
    """返回拓扑与参数诊断列表(错误/警告, 含对象定位, 04 §5.4 结构)。"""
    diags = svc.validate_project_model(db, project_id)
    return {"diagnostics": [d.to_dict() for d in diags]}
