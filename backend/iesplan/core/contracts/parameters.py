"""无状态纯数据类型: 参数规格(04 §3; 宪法 4.1: core 只含无业务状态的公共基础)。

``ParameterSpec`` 从 ``core.registry`` 迁入(宪法 4.1/RR-P2-02: 纯类型归属
core/contracts, 不携带注册状态)。设备 YAML 参数、算法参数、建模命令字段规格
共用同一规格类型。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    """参数规格(04 §3:name/unit/min/max/default/存量/优化变量/帮助键)。

    属性:
        name: 参数名(注册表内唯一)。
        unit: 注册单位(如 kW/kWh/CNY/kWh;- 表示无量纲)。
        min / max: 取值范围(None 表示不限制;枚举类参数用 enum 表达)。
        default: 默认值(数值/字符串/字典;引用类参数为 None)。
        is_optimizable: 是否可作为优化变量(04 §3 的 is_optimization_variable)。
        existing_default: 存量设备默认值(存量即"容量固定只优化运行"的场景,
            见 02 §4.8);新增类容量参数存量默认 0,存量类参数取 default。
        stock_or_addition: 'stock'(存量,容量固定) | 'addition'(新增,容量可优化)。
        help_key: 帮助主题键(help.param.*)。
        enum: 可选枚举取值列表(字符串/数值)。
    """

    name: str
    unit: str
    min: float | None = None
    max: float | None = None
    default: object = None
    is_optimizable: bool = False
    existing_default: float | None = None
    stock_or_addition: str = "stock"
    help_key: str = ""
    enum: tuple | None = None
