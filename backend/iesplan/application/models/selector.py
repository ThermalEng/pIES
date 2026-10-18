"""模型选择器用例(application/models/selector.py)。

模型选择器页面数据路径：设备类型清单 + 参数 schema + 真实端口
（供前端画布渲染设备面板与参数表单）。

公共选择器只消费公开目录；项目选择器在公开目录之上叠加本项目已保存
模型（保存入口已校验的不可变文档），供画布选择与装配消费。数据只经
devices 域公开门面与项目设备描述用例获取，本模块只做传输序列化，不维护
独立的设备类型静态表。API 路由只做转交。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from iesplan.application.models.project_devices import load_project_device_documents
from iesplan.application.projects import ensure_access, get_current_draft_content
from iesplan.devices import DeviceModelDocument, list_devices
from iesplan.devices.contracts2 import PropertySpec
from iesplan.identity.contracts import UserRecord


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


def list_project_device_types(
    db: Session, user: UserRecord, project_id: int
) -> dict[str, Any]:
    """项目作用域选择器：公开目录 ∪ 当前草稿精确引用的本项目模型（同 ID 以项目模型为准）。

    只读当前草稿内容声明的 ``project_models`` 引用与已冻结对象；恢复历史
    版本后引用集随之变化，其他项目模型不可见。
    """
    ensure_access(db, user, project_id, "view")
    content = get_current_draft_content(db, project_id)
    items: dict[str, dict[str, Any]] = {
        item["type_id"]: item for item in list_device_types()["items"]
    }
    for type_id, document in sorted(
        load_project_device_documents(db, content).items()
    ):
        items[type_id] = _device_type_schema(document)
    return {"items": list(items.values())}


__all__ = ["list_device_types", "list_project_device_types"]
