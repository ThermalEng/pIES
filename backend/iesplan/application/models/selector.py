"""模型选择器用例(application/models/selector.py)。

模型选择器页面数据路径：公开设备类型清单 + 参数 schema + 真实端口
（供前端画布渲染设备面板与参数表单）。

数据只经 devices 域公开门面（list_devices）获取；端口/方向/载能来自
YAML 设备目录公开 descriptor，本模块只做传输序列化，不维护独立的
设备类型静态表。API 路由（api/model.py device_types_public）只做转交，
不再直调领域行为。
"""

from __future__ import annotations

from typing import Any

from iesplan.devices import DeviceModelDocument, list_devices
from iesplan.devices.contracts2 import PropertySpec


def _parameter_schema(p: PropertySpec) -> dict[str, Any]:
    """参数规格 → 公开 schema(前端表单渲染用)。"""
    return {
        "name": p.id,
        "unit": p.unit,
        "min": p.minimum,
        "max": p.maximum,
        "default": p.value,
    }


def _device_type_schema(spec: DeviceModelDocument) -> dict[str, Any]:
    """设备类型注册项 → 公开 schema(RR-P1-04: 含 YAML 真实端口/能力/模型元数据)。"""
    return {
        "type_id": spec.device.id,
        "schema_version": spec.schema_version,
        "names": dict(spec.device.names),
        "ports": [
            {
                "name": name,
                "direction": p.type,
                "energy_carrier": p.carrier,
                "unit": p.unit,
            }
            for name, p in spec.interfaces.items()
        ],
        "parameters": {name: _parameter_schema(p) for name, p in spec.properties.items()},
    }


def list_device_types() -> dict[str, Any]:
    """模型选择器数据：公开设备类型清单 + 参数 schema + 真实端口。

    端口/方向/载能来自 YAML 设备目录(公开 descriptor)；始终返回
    200 + {"items": [...]}(前端画布渲染用)。
    """
    return {"items": [_device_type_schema(desc) for desc in list_devices()]}


__all__ = ["list_device_types"]
