"""模型域公开门面（系统图/设备/端口/连接，归属 model）。

外部只允许经本门面消费 contract、repository 协议与 repository 实现函数；
不得导入 `iesplan.models`、services 或其他域的内部模块。
"""

from __future__ import annotations

from iesplan.model import persistence
from iesplan.model.contracts import (
    ConnectionRecord,
    DeviceRecord,
    GraphRecord,
    ModelConflictError,
    ModelNotFoundError,
    PortRecord,
)
from iesplan.model.repository import ModelRepository

create_connection = persistence.create_connection
create_device = persistence.create_device
create_graph = persistence.create_graph
create_ports = persistence.create_ports
delete_connection = persistence.delete_connection
delete_device_cascade = persistence.delete_device_cascade
find_connection = persistence.find_connection
find_device_by_name = persistence.find_device_by_name
find_working_graph = persistence.find_working_graph
get_connection = persistence.get_connection
get_device = persistence.get_device
get_graph = persistence.get_graph
get_port = persistence.get_port
list_connections = persistence.list_connections
list_devices = persistence.list_devices
list_ports = persistence.list_ports
list_ports_by_device = persistence.list_ports_by_device
resync_device_ports = persistence.resync_device_ports
update_connection = persistence.update_connection
update_device = persistence.update_device

__all__ = [
    "ConnectionRecord",
    "DeviceRecord",
    "GraphRecord",
    "ModelConflictError",
    "ModelNotFoundError",
    "ModelRepository",
    "PortRecord",
    "create_connection",
    "create_device",
    "create_graph",
    "create_ports",
    "delete_connection",
    "delete_device_cascade",
    "find_connection",
    "find_device_by_name",
    "find_working_graph",
    "get_connection",
    "get_device",
    "get_graph",
    "get_port",
    "list_connections",
    "list_devices",
    "list_ports",
    "list_ports_by_device",
    "resync_device_ports",
    "update_connection",
    "update_device",
]
