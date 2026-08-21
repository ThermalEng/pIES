"""设备规格数据模型(设备初始化模块的最小可用版,按文档接口实现)。

背景:建模模块(2 层)的输入是设备初始化模块(1 层,``iesplan/devices/``)产出的
``DeviceSpec``。该包当前尚未落地,按实施规则"依赖模块未实现时用文档定义的接口
自行实现最小可用版本",本文件按 **03 §4.2(DeviceSpec 字段)** 与 **02 §6.1
(PortSpec/SeriesSpec/StateSpec 字段)** 的定案接口实现最小版,供建模模块独立导入;
``iesplan.devices.spec`` 落地后,仅需把 `build.py`/测试中的导入指向新包(字段签名不变)。

字段命名遵循 05 §7.1 裁决:``model_method``(mechanism|data_repeat|data_predict)、
``stateful: bool``;02 的 modeling_method/statefulness 命名废止。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from iesplan.core.contracts.parameters import ParameterSpec

from iesplan.modeling.enums import FIDELITY_VALUES, MODEL_METHODS


@dataclass(frozen=True, slots=True)
class SeriesSpec:
    """标准 csv 时间序列列声明(02 §2.4)。"""

    key: str
    unit: str
    resolution: str = "1h"  # "15min" | "30min" | "1h"
    required: bool = True
    period: str | None = None  # "day" | "week" | "year"(仅 data_repeat 设备的 inputs 带)


@dataclass(frozen=True, slots=True)
class PortSpec:
    """端口定义(02 §2.2)。"""

    name: str
    port_type: str = "electric"  # electric/thermal/cooling/fuel/water/data
    direction: str = "out"  # in/out/bidirectional
    energy_carrier: str = "electric"
    capacity_ref: str | None = None  # 容量取自的参数名(单位=参数单位)


@dataclass(frozen=True, slots=True)
class StateSpec:
    """状态定义(02 §2.5,仅 stateful 设备)。"""

    key: str
    unit: str = "-"
    initial_ref: str | None = None  # 初始值取自的参数名
    bounds: dict[str, str] | None = None  # {"min_ref": 参数名, "max_ref": 参数名}


@dataclass(frozen=True, slots=True)
class DeviceSpec:
    """设备规格(03 §4.2 接口 + 02 yaml 章节字段;建模模块的唯一输入)。"""

    type_id: str  # 'ies.device.pv'
    version: str  # '1.4.0'
    name_zh: str
    name_en: str
    energy_carriers: list[str]  # ['electric','heat','cool','gas','solar']
    is_load: bool
    capabilities: list[str]
    extends: str = "ies.device.base"
    parameters: dict[str, ParameterSpec] = field(default_factory=dict)  # 参数名 → 规格
    help_topic: str = ""
    # ---- 建模标志(05 §7.1 裁决命名)----
    model_method: str = "mechanism"  # mechanism | data_repeat | data_predict
    stateful: bool = False  # 有/无状态模型标志
    fidelity: str = "medium"  # low | medium | high(与 model_method 正交)
    model_function: str = ""  # 机理:命令函数引用(白名单 iesplan.modeling.functions.*)
    model_file: str | None = None  # data_predict:预测模型文件路径
    data_file: str | None = None  # data_repeat:标准 csv 数据文件
    price_refs: dict[str, str] = field(default_factory=dict)  # 参数名 → prices.yaml 键
    profile_ref: str | None = None  # 附带标准时间序列的对象存储引用
    # ---- 图与 schema 元数据(02 yaml 章节;装配/检查模块消费)----
    ports: tuple[PortSpec, ...] = ()
    time_series: dict[str, tuple[SeriesSpec, ...]] = field(default_factory=dict)  # {"inputs","outputs"}
    states: tuple[StateSpec, ...] = ()


def validate_spec(spec: DeviceSpec) -> list[str]:
    """结构校验(02 §3 约束,错误即拒载):返回错误文案列表,空列表表示通过。

    校验项:
    - model_method ∈ (mechanism, data_repeat, data_predict);fidelity ∈ (low, medium, high);
    - stateful 设备必须声明 states;非 stateful 设备不得声明 states(02 §3);
    - mechanism 必须声明 model_function;data_repeat 必须声明 data_file;
    - data_predict 必须声明 model_file(02 §3);
    - 参数 unit 非空('-' 表示无量纲,与注册表约定一致)。
    """
    errors: list[str] = []
    if spec.model_method not in MODEL_METHODS:
        errors.append(f"model_method 非法: {spec.model_method!r}(应为 {'/'.join(MODEL_METHODS)})")
    if spec.fidelity not in FIDELITY_VALUES:
        errors.append(f"fidelity 非法: {spec.fidelity!r}(应为 {'/'.join(FIDELITY_VALUES)})")
    if spec.stateful and not spec.states:
        errors.append("stateful 设备必须声明 states(02 §3)")
    if not spec.stateful and spec.states:
        errors.append("states 仅允许出现在 stateful 设备(02 §3)")
    if spec.model_method == "mechanism" and not spec.model_function:
        errors.append("mechanism 设备必须声明 model_function")
    if spec.model_method == "data_repeat" and not spec.data_file:
        errors.append("data_repeat 设备必须声明 data_file(标准 csv)")
    if spec.model_method == "data_predict" and not spec.model_file:
        errors.append("data_predict 设备必须声明 model_file")
    for name, p in spec.parameters.items():
        if not p.unit:
            errors.append(f"参数 {name} 缺少 unit")
    return errors
