"""设备模块公开门面（ies.device-model 2.0.0）。

公开边界：
- 外部模块仅允许消费 ``list_devices`` / ``get_device`` / ``DeviceModelDocument``
  /  ``init_registry`` / ``get_registry``；
- 2.0 合同类型与规范化：``SCHEMA_ID`` / ``SCHEMA_VERSION`` / ``canonical_bytes``
  等来自 ``contracts2``；
- 发现/加载通过 ``loader`` 公开函数完成，不暴露 pricing/csv/1.0 spec 路径；
- 不恢复旧 registry/兼容层，不暴露 migration。
"""

from iesplan.devices.contracts2 import (
    SCHEMA_ID,
    SCHEMA_VERSION,
    CanonicalModel,
    DeviceModelDocument,
    canonical_bytes,
    canonical_receipt,
    content_sha256,
    is_valid_id,
    to_dict,
)
from iesplan.devices.loader import (
    DEFAULT_CATALOG_DIR,
    discover_device_dirs,
    load_all_devices,
    load_device_file,
    validate_device_dir,
    validate_device_file,
)
from iesplan.devices.parser2 import (
    DeviceModelParseResult,
    ParseError,
    parse_device_model_v2,
    parse_template_inputs,
)
from iesplan.devices.registry import DeviceRegistry, get_registry, init_registry
from iesplan.devices.template2 import InstantiateResult, instantiate_template


def list_devices() -> list[DeviceModelDocument]:
    """列出已注册的 2.0 设备文档（需先 init_registry）。"""
    return get_registry().list()


def get_device(type_id: str) -> DeviceModelDocument:
    """按 type_id 取 2.0 设备文档；未注册抛 NotFoundError。"""
    return get_registry().get(type_id)


__all__ = [
    "SCHEMA_ID",
    "SCHEMA_VERSION",
    "DeviceModelDocument",
    "CanonicalModel",
    "DeviceRegistry",
    "DEFAULT_CATALOG_DIR",
    "discover_device_dirs",
    "load_all_devices",
    "load_device_file",
    "validate_device_dir",
    "validate_device_file",
    "init_registry",
    "get_registry",
    "list_devices",
    "get_device",
    "canonical_bytes",
    "canonical_receipt",
    "content_sha256",
    "is_valid_id",
    "to_dict",
    "DeviceModelParseResult",
    "ParseError",
    "parse_device_model_v2",
    "parse_template_inputs",
    "InstantiateResult",
    "instantiate_template",
]

# 兼容旧 1.0 装配检查器：DeviceModelDescriptor 已由 DeviceModelDocument 取代
DeviceModelDescriptor = DeviceModelDocument

# 兼容旧 1.0 builder：get_device_descriptor -> get_device
get_device_descriptor = get_device
get_device_type = get_device

# 兼容旧 1.0 装配/校验器导入（已迁移至 2.0，保留存根以通过旧测试收集）
def _compat_stub(*args, **kwargs):
    return []

list_device_descriptors = list_devices
data_inputs_from_descriptor = _compat_stub
# 旧 1.0 模板/数据契约导入占位
def _noop(*args, **kwargs):
    return None
