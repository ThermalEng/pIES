"""设备初始化模块(05 第 2 节 ①段;02 设备初始化定案;05 §7.6 文件结构裁决)。

输入为数据文件而非代码(插件式, 新增设备不改代码、可热加载):
- catalog/<id>.yaml: 每设备一个(参数/端口/状态/时间序列声明, 含 model_method/stateful 标志);
- catalog/<id>.csv: 标准时间序列(data_repeat 必选; mechanism 可选样例);
- catalog/prices.yaml: 价格/成本/税收单一事实源($price: 引用, 键缺失拒绝注册)。

产出 DeviceYamlSpec($price: 已解析的参数默认值), 供建模(②段)与计算模块消费。
字段命名遵循 05 §7.1 裁决: model_method(mechanism|data_repeat|data_predict) + stateful(bool)。
"""

from iesplan.devices.loader import (
    DEFAULT_CATALOG_DIR,
    discover_device_dirs,
    load_all_devices,
    load_device_type,
    validate_device_dir,
)
from iesplan.devices.pricing import (
    PRICE_REF_PREFIX,
    PriceBook,
    algorithm_defaults,
    finance_defaults,
    get,
    load_price_book,
    resolve_param_default,
)
from iesplan.devices.registry import DeviceRegistry, get_registry, init_registry
from iesplan.devices.spec import (
    FIDELITY_VALUES,
    MODEL_FILE_FORMATS,
    MODEL_METHOD_LABELS,
    MODEL_METHODS,
    PERIOD_VALUES,
    PORT_DIRECTIONS,
    PORT_TYPES,
    DeviceYamlSpec,
    PortSpec,
    SeriesSpec,
    StateSpec,
    load_yaml,
    spec_to_dict,
    to_registry_spec,
    with_resolved_defaults,
)

__all__ = [
    "DEFAULT_CATALOG_DIR",
    "PRICE_REF_PREFIX",
    "MODEL_METHODS",
    "MODEL_METHOD_LABELS",
    "FIDELITY_VALUES",
    "PORT_TYPES",
    "PORT_DIRECTIONS",
    "PERIOD_VALUES",
    "MODEL_FILE_FORMATS",
    "PriceBook",
    "load_price_book",
    "get",
    "resolve_param_default",
    "finance_defaults",
    "algorithm_defaults",
    "DeviceYamlSpec",
    "PortSpec",
    "SeriesSpec",
    "StateSpec",
    "load_yaml",
    "spec_to_dict",
    "with_resolved_defaults",
    "to_registry_spec",
    "discover_device_dirs",
    "validate_device_dir",
    "load_device_type",
    "load_all_devices",
    "DeviceRegistry",
    "get_registry",
    "init_registry",
]
