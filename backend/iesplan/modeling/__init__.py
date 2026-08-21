"""建模模块(2 层)门面(03 §5)。

职责:消费设备初始化模块的 DeviceSpec(+ 标准 csv 数据),产出标准化后台调用命令
ModuleCommand(标准化函数名/输入/输出字段规格含单位/状态标志),注册进全局命令表;
计算模块按命令分发(``call_command``),不再直接 import 设备函数。

分层:仅依赖 0 层(core)与包内模块;依赖 devices 包的部分以 ``devspec.py``
(按文档接口实现的最小可用版)自足,待 ``iesplan.devices`` 落地后切换导入点。
"""

from __future__ import annotations

from iesplan.modeling.build import build_command
from iesplan.modeling.command import (
    DeviceRunResult,
    ModuleCommand,
    call_command,
    clear_commands,
    get_command,
    get_command_or_raise,
    get_entry_function,
    list_commands,
    make_command_id,
    parse_command_id,
    register_command,
    replace_all_commands,
    resolve_function_ref,
    snapshot,
)
from iesplan.modeling.datadriven import (
    build_periodic_entry,
    build_prediction_entry,
    periodic_repeat,
    prediction_model,
)
from iesplan.modeling.devspec import (
    DeviceSpec,
    PortSpec,
    SeriesSpec,
    StateSpec,
    validate_spec,
)
from iesplan.modeling.enums import (
    COMMAND_ID_PREFIX,
    FIDELITY_VALUES,
    FUNCTION_REF_PREFIX,
    MODEL_METHODS,
    MODEL_METHOD_DATA_PREDICT,
    MODEL_METHOD_DATA_REPEAT,
    MODEL_METHOD_MECHANISM,
)
from iesplan.modeling.errors import (
    ModelingConfigError,
    ModelingError,
    ModelingNotImplementedError,
)
from iesplan.modeling.functions import (
    MECHANISM_FUNCTIONS,
    MechanismSpec,
    ParamBinding,
    as_device_entry,
    boiler_output,
    chiller_output,
    gas_volume_m3,
    heat_pump_cop,
    heat_transfer_q,
    mechanism_spec_for,
    power_balance,
    pv_output,
    simulate_battery,
)

__all__ = [
    # 命令注册表与调用契约
    "ModuleCommand",
    "DeviceRunResult",
    "register_command",
    "replace_all_commands",
    "get_command",
    "get_command_or_raise",
    "list_commands",
    "clear_commands",
    "snapshot",
    "resolve_function_ref",
    "get_entry_function",
    "make_command_id",
    "parse_command_id",
    "call_command",
    # 命令生成
    "build_command",
    # 数据方法
    "periodic_repeat",
    "build_periodic_entry",
    "prediction_model",
    "build_prediction_entry",
    # 设备规格(设备初始化模块的最小可用版)
    "DeviceSpec",
    "PortSpec",
    "SeriesSpec",
    "StateSpec",
    "validate_spec",
    # 枚举
    "MODEL_METHODS",
    "MODEL_METHOD_MECHANISM",
    "MODEL_METHOD_DATA_REPEAT",
    "MODEL_METHOD_DATA_PREDICT",
    "FIDELITY_VALUES",
    "FUNCTION_REF_PREFIX",
    "COMMAND_ID_PREFIX",
    # 机理函数与映射表
    "MECHANISM_FUNCTIONS",
    "MechanismSpec",
    "ParamBinding",
    "as_device_entry",
    "mechanism_spec_for",
    "pv_output",
    "heat_pump_cop",
    "boiler_output",
    "chiller_output",
    "gas_volume_m3",
    "simulate_battery",
    "heat_transfer_q",
    "power_balance",
    # 错误
    "ModelingError",
    "ModelingConfigError",
    "ModelingNotImplementedError",
]
