"""模型面向 API 的完整用例(application/models/cases)。

每个函数对应 `api/model.py` 的一个 HTTP 业务动作，接收已认证主体(`user`)、
业务参数与事务会话(`db`)，在内部完成授权、业务步骤与事务，返回与 HTTP
无关的普通字典；路由只做 DTO、一次调用与错误/响应映射。

吸收的原路由顺序(行为、权限、事务与错误语义与原路由逐一一致，不新增
校验/hash/防御分支)：

- 读图/校验：view 授权 → 只读查询/诊断(无事务提交)；
- 创建设备：edit 授权 → 创建(提交) → 二次查询端口 → 序列化；
- 更新设备/连接：edit 授权 → 更新(提交) → 序列化；
- 删除设备/连接：edit 授权 → 删除(提交)；
- 创建连接：edit 授权 → 连接(提交) → 序列化。

公开设备类型注册表无授权、无事务，由 `selector` 直供，路由直接转交，
不在本模块重复包装。

依赖方向：api → application.models.cases → {application service,
application.projects.authorization} → 领域公开门面。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from iesplan.application.models import service as model_service
from iesplan.application.projects.authorization import ensure_access
from iesplan.identity.contracts import UserRecord


def get_model_graph(db: Session, user: UserRecord, project_id: int) -> dict:
    """读取项目工作图(原路由顺序：view 授权 → 只读取图；无提交)。"""
    ensure_access(db, user, project_id, "view")
    return model_service.get_graph(db, project_id)


def create_device(
    db: Session,
    user: UserRecord,
    project_id: int,
    *,
    device_type: str,
    name: str,
    params: dict[str, Any] | None = None,
    is_existing: bool = False,
    model_precision: str = "medium",
    position: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """创建设备(原路由顺序：edit 授权 → 创建并提交 → 二次查端口 → 序列化)。"""
    ensure_access(db, user, project_id, "edit")
    device = model_service.create_device(
        db,
        project_id,
        device_type,
        name,
        params=params,
        is_existing=is_existing,
        model_precision=model_precision,
        position=position,
        created_by=user.id,
    )
    ports = model_service.get_device_ports(db, device.id)
    return {
        "device": model_service.serialize_device(device),
        "ports": [model_service.serialize_port(p) for p in ports],
    }


def update_device(
    db: Session,
    user: UserRecord,
    project_id: int,
    device_id: int,
    *,
    name: str | None = None,
    params: dict[str, Any] | None = None,
    position: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """更新设备(原路由顺序：edit 授权 → 更新并提交 → 序列化)。"""
    ensure_access(db, user, project_id, "edit")
    device = model_service.update_device(
        db,
        project_id,
        device_id,
        name=name,
        params=params,
        position=position,
    )
    return {"device": model_service.serialize_device(device)}


def delete_device(db: Session, user: UserRecord, project_id: int, device_id: int) -> dict[str, Any]:
    """删除设备(原路由顺序：edit 授权 → 级联删除并提交)。"""
    ensure_access(db, user, project_id, "edit")
    model_service.delete_device(db, project_id, device_id)
    return {"ok": True, "deleted": device_id}


def create_connection(
    db: Session,
    user: UserRecord,
    project_id: int,
    from_port_id: int,
    to_port_id: int,
    attrs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """创建连接(原路由顺序：edit 授权 → 连接并提交 → 序列化)。"""
    ensure_access(db, user, project_id, "edit")
    conn = model_service.connect(db, project_id, from_port_id, to_port_id, attrs=attrs)
    return {"connection": model_service.serialize_connection(conn)}


def update_connection(
    db: Session,
    user: UserRecord,
    project_id: int,
    conn_id: int,
    attrs: dict[str, Any],
) -> dict[str, Any]:
    """更新连接(原路由顺序：edit 授权 → 更新并提交 → 序列化)。"""
    ensure_access(db, user, project_id, "edit")
    conn = model_service.update_connection(db, project_id, conn_id, attrs)
    return {"connection": model_service.serialize_connection(conn)}


def delete_connection(
    db: Session, user: UserRecord, project_id: int, conn_id: int
) -> dict[str, Any]:
    """断开连接(原路由顺序：edit 授权 → 删除并提交)。"""
    ensure_access(db, user, project_id, "edit")
    model_service.disconnect(db, project_id, conn_id)
    return {"ok": True, "deleted": conn_id}


def validate_model(db: Session, user: UserRecord, project_id: int) -> dict[str, Any]:
    """模型校验(原路由顺序：view 授权 → 只读诊断 → 序列化；无提交)。"""
    ensure_access(db, user, project_id, "view")
    diags = model_service.validate_project_model(db, project_id)
    return {"diagnostics": [d.to_dict() for d in diags]}


__all__ = [
    "create_connection",
    "create_device",
    "delete_connection",
    "delete_device",
    "get_model_graph",
    "update_connection",
    "update_device",
    "validate_model",
]
