"""模型域 repository 协议（system_graphs/devices/ports/connections）。

实现规则：
- 查询、写入、flush 由 repository 完成；绝不 commit/rollback；
- 并发建图/重名插入的唯一冲突转为 ModelConflictError，由调用方决定重试；
- 端口重同步（按名对齐）与设备级联删除在同事务内完成。
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, Protocol

from sqlalchemy.orm import Session

from iesplan.model.contracts import (
    ConnectionRecord,
    DeviceRecord,
    GraphRecord,
    PortRecord,
)


class ModelRepository(Protocol):
    """模型聚合 repository 协议（无状态方法组，db 由调用方事务拥有）。"""

    def find_working_graph(self, db: Session, project_id: int) -> GraphRecord | None:
        """项目工作图（挂草稿，按 id 升序取最早一张）；无返回 None。"""
        ...

    def get_graph(self, db: Session, graph_id: int) -> GraphRecord | None:
        """按主键取图；不存在返回 None。"""
        ...

    def create_graph(
        self,
        db: Session,
        *,
        project_id: int,
        draft_id: int,
        name: str,
        created_by: int,
    ) -> GraphRecord:
        """创建工作图；唯一冲突抛 ModelConflictError。"""
        ...

    def get_device(self, db: Session, device_id: int) -> DeviceRecord | None:
        """按主键取设备；不存在返回 None。"""
        ...

    def find_device_by_name(
        self, db: Session, graph_id: int, name: str, exclude_id: int | None = None
    ) -> DeviceRecord | None:
        """图内按名取设备（重名校验用）；无返回 None。"""
        ...

    def list_devices(self, db: Session, graph_id: int) -> list[DeviceRecord]:
        """图内设备（id 升序）。"""
        ...

    def create_device(
        self,
        db: Session,
        *,
        graph_id: int,
        device_type: str,
        kind: str,
        name: str,
        params: dict[str, Any],
        model_fidelity: str = "medium",
        status: str = "active",
    ) -> DeviceRecord:
        """创建设备行；重名冲突抛 ModelConflictError。"""
        ...

    def update_device(
        self,
        db: Session,
        device_id: int,
        *,
        name: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> DeviceRecord:
        """更新设备名称/参数（仅提供字段）并刷新 updated_at；缺失抛 ModelNotFoundError。"""
        ...

    def delete_device_cascade(self, db: Session, device_id: int) -> None:
        """删除设备及其端口与连接（级联）；设备缺失抛 ModelNotFoundError。"""
        ...

    def get_port(self, db: Session, port_id: int) -> PortRecord | None:
        """按主键取端口；不存在返回 None。"""
        ...

    def list_ports(self, db: Session, graph_id: int) -> list[PortRecord]:
        """图内端口（经设备归属，id 升序）。"""
        ...

    def list_ports_by_device(self, db: Session, device_id: int) -> list[PortRecord]:
        """设备端口（id 升序）。"""
        ...

    def create_ports(
        self, db: Session, device_id: int, specs: Collection[dict[str, Any]]
    ) -> list[PortRecord]:
        """批量建端口；spec 含 name/port_type/direction，params 缺省为空对象。"""
        ...

    def resync_device_ports(
        self, db: Session, device_id: int, wanted: Collection[dict[str, Any]]
    ) -> list[PortRecord]:
        """按名对齐设备端口：删除多余端口及其连接，补齐缺失端口；返回当前端口。"""
        ...

    def get_connection(self, db: Session, conn_id: int) -> ConnectionRecord | None:
        """按主键取连接；不存在返回 None。"""
        ...

    def list_connections(self, db: Session, graph_id: int) -> list[ConnectionRecord]:
        """图内连接（id 升序）。"""
        ...

    def find_connection(
        self,
        db: Session,
        *,
        graph_id: int,
        from_port_id: int,
        to_port_id: int,
        conn_type: str,
    ) -> ConnectionRecord | None:
        """按（图，两端，类型）取连接（重复校验用）；无返回 None。"""
        ...

    def create_connection(
        self,
        db: Session,
        *,
        graph_id: int,
        from_port_id: int,
        to_port_id: int,
        conn_type: str,
        capacity: float | None = None,
        loss_rate: float = 0.0,
        params: dict[str, Any] | None = None,
    ) -> ConnectionRecord:
        """创建连接行；冲突抛 ModelConflictError。"""
        ...

    def update_connection(
        self,
        db: Session,
        conn_id: int,
        *,
        capacity: float | None = None,
        loss_rate: float | None = None,
        params: dict[str, Any] | None = None,
    ) -> ConnectionRecord:
        """更新连接属性（仅提供字段）；缺失抛 ModelNotFoundError。"""
        ...

    def delete_connection(self, db: Session, conn_id: int) -> None:
        """删除连接行；缺失抛 ModelNotFoundError。"""
        ...
