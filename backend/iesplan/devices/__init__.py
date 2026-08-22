"""设备初始化模块 facade（roadmap 0.5.0 迁移后）。

公开边界:
- 外部模块仅允许消费 ``list_device_descriptors`` / ``get_device_descriptor`` /
  ``DeviceModelDescriptor`` / ``ParameterSpec``（由 core/contracts 转发）/
  ``get_profile_columns``（modeling 按 type_id 取标准 csv 数据）；
- 注册表生命周期入口 ``init_registry``（组合根使用）；
- 内部模块（loader/pricing/registry/profile/parser/contracts）不下划线符号、
  路径推导、价格解析实现不再由 facade 转出。

设备目录数据契约（ies.device-model 1.0.0）:
- catalog/<id>.yaml: 每设备一个（schema/device/parameters/ports/data_inputs/
  states/model_commands/extensions）;
- catalog/<id>.csv: 标准时间序列（data_repeat 必选）;
- catalog/prices.yaml: 价格/成本/税收单一事实源（$price: 引用, 键缺失拒绝注册）。

公开 descriptor 与设备文件**不暴露函数/包/模块/宿主机路径**；建模命令
（model_commands）的 ID→实现解析只存在于组合根与 modeling provider 内部。
"""

from iesplan.core.contracts import ParameterSpec
from iesplan.core.errors import AppError
from iesplan.devices.loader import DEFAULT_CATALOG_DIR
from iesplan.devices.profile import load_profile_columns
from iesplan.devices.registry import init_registry
from iesplan.devices.spec import DeviceModelDescriptor


def get_profile_columns(type_id: str) -> dict[str, "object"]:
    """按 type_id 取标准 csv 数据（data_repeat 设备典型曲线；modeling 消费）。

    路径解析在 devices 模块内部完成；调用方不感知 csv 路径规则。
    未注册或文件缺失抛 AppError（不静默降级为空数据）。
    """
    from iesplan.devices.registry import get_registry
    from iesplan.devices.spec import to_model_descriptor

    registry = get_registry()
    spec = registry.get(type_id)
    desc = to_model_descriptor(spec)
    if desc.model_method != "data_repeat":
        raise AppError(
            f"设备 {type_id} 不是 data_repeat 设备，无标准 csv 曲线",
            code="SYS-CFG-001",
            message_key="ies.diag.store.config_invalid",
            params={"device_id": type_id, "model_method": desc.model_method},
        )
    return load_profile_columns(registry.csv_path_for(type_id), desc)


def list_device_descriptors() -> list[DeviceModelDescriptor]:
    """公开设备目录（devices → 外部模块唯一入口）。

    返回已校验、价格已解析的设备建模描述; 外部模块不得导入 devices 内部
    目录扫描/价格解析/CSV 路径实现。注册表未初始化抛 AppError(SYS-CFG-001)。
    """
    from iesplan.devices.registry import get_registry
    from iesplan.devices.spec import to_model_descriptor

    registry = get_registry()
    return [to_model_descriptor(s) for s in registry.list()]


def get_device_descriptor(type_id: str) -> DeviceModelDescriptor:
    """按类型取公开设备描述(未注册抛 NotFoundError CONN-TYPE-002)。"""
    from iesplan.devices.registry import get_registry
    from iesplan.devices.spec import to_model_descriptor

    registry = get_registry()
    return to_model_descriptor(registry.get(type_id))


__all__ = [
    "ParameterSpec",
    "DEFAULT_CATALOG_DIR",
    "DeviceModelDescriptor",
    "init_registry",
    "list_device_descriptors",
    "get_device_descriptor",
    "get_profile_columns",
    "load_profile_columns",
]
