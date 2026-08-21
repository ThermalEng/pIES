"""设备初始化模块 facade(05 第 2 节 ①段;02 设备初始化定案;05 §7.6 文件结构裁决)。

公开边界(RR-P2-04):
- 外部模块仅允许消费 ``list_device_descriptors`` / ``get_device_descriptor`` /
  ``DeviceModelDescriptor`` / ``ParameterSpec``(由 core/contracts 转发)；
- 注册表生命周期入口 ``init_registry``(组合根使用)；
- 数据访问 ``load_profile_columns``(modeling 模块需要按 type_id 取样列)；
- 内部模块(loader/pricing/registry/parser/profile)不下划线符号、路径推导、价格解析
  实现不再由 facade 转出；如需访问必须经 ``iesplan.devices.{module}`` 显式模块路径。

设备目录数据契约:
- catalog/<id>.yaml: 每设备一个(参数/端口/状态/时间序列声明, 含 model_method/stateful);
- catalog/<id>.csv: 标准时间序列(data_repeat 必选; mechanism 可选样例);
- catalog/prices.yaml: 价格/成本/税收单一事实源($price: 引用, 键缺失拒绝注册)。
"""

from iesplan.core.contracts import ParameterSpec
from iesplan.devices.loader import DEFAULT_CATALOG_DIR
from iesplan.devices.spec import DeviceModelDescriptor
from iesplan.devices.registry import init_registry

# 公开数据访问(单一窄 API, 由 modeling 模块按 type_id 查询列定义使用)
load_profile_columns = __import__(
    "iesplan.devices.profile", fromlist=["load_profile_columns"]
).load_profile_columns


def list_device_descriptors() -> list[DeviceModelDescriptor]:
    """公开设备目录(devices → 外部模块唯一入口, BE-REG-01)。

    返回已校验、价格已解析的设备建模描述; 外部模块不得导入 devices 内部
    目录扫描/价格解析/CSV 路径实现。注册表未初始化抛 AppError(SYS-CFG-001)。
    """
    from iesplan.devices.spec import to_model_descriptor
    from iesplan.devices.registry import get_registry

    registry = get_registry()
    return [to_model_descriptor(s) for s in registry.list()]


def get_device_descriptor(type_id: str) -> DeviceModelDescriptor:
    """按类型取公开设备描述(未注册抛 NotFoundError CONN-TYPE-002)。"""
    from iesplan.devices.spec import to_model_descriptor
    from iesplan.devices.registry import get_registry

    registry = get_registry()
    return to_model_descriptor(registry.get(type_id))


__all__ = [
    "ParameterSpec",
    "DEFAULT_CATALOG_DIR",
    "DeviceModelDescriptor",
    "init_registry",
    "list_device_descriptors",
    "get_device_descriptor",
    "load_profile_columns",
]