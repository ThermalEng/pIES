"""模型域公开契约（系统图/设备/端口/连接，归属 model）。

- 图/设备/端口/连接一经写入的不可变语义由调用方保证；版本图不可修改；
- 只含不可变值对象与领域错误；不导入 ORM、Session、services 或 application。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from iesplan.core.errors import ConflictError, NotFoundError


class ModelNotFoundError(NotFoundError):
    """图/设备/端口/连接不存在（沿用基类诊断码，不新增码）。"""


class ModelConflictError(ConflictError):
    """名称重复/并发建图冲突（沿用基类诊断码，不新增码）。"""


@dataclass(frozen=True, slots=True)
class GraphRecord:
    """系统图（system_graphs 表公开视图）。

    工作图挂 draft_id；版本图挂 project_version_id（二者互斥）。
    """

    id: int
    project_id: int
    name: str
    draft_id: int | None = None
    project_version_id: int | None = None
    created_by: int = 0
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class DeviceRecord:
    """设备（devices 表公开视图）。"""

    id: int
    graph_id: int
    device_type: str
    kind: str
    name: str
    params: dict[str, Any]
    model_fidelity: str = "medium"
    status: str = "active"
    description: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class PortRecord:
    """端口（ports 表公开视图）。"""

    id: int
    device_id: int
    port_type: str
    direction: str
    name: str
    capacity: float | None = None
    params: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ConnectionRecord:
    """连接（connections 表公开视图）。"""

    id: int
    graph_id: int
    from_port_id: int
    to_port_id: int
    conn_type: str
    loss_rate: float = 0.0
    capacity: float | None = None
    params: dict[str, Any] | None = None
