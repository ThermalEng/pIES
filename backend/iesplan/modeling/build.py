"""建模命令生成(03 §5.2 build_command):设备规格 → 标准化后台调用命令。

按 spec.model_method 分发:
- mechanism    → 映射表查找基础函数(MECHANISM_FUNCTIONS,白名单前缀校验),包装为
  device_entry 统一契约函数,function_ref = spec.model_function;
- data_repeat  → 包装 build_periodic_entry(周期重复 + 容量缩放闭包);
- data_predict → 包装 build_prediction_entry(prediction_model stub 接口)。

命令的输入/输出字段规格(字段名 + 单位)由设备 yaml 声明派生:
- inputs  = parameters(参数规格,含 unit/min/max)+ time_series.inputs(标准列);
- outputs = out 方向端口(单位取 capacity_ref 参数单位,缺省取机理映射默认单位)
            与 time_series.outputs 列,按声明顺序去重;
- state_fields = states(有状态设备的状态输入/输出字段规格,02 §2.5)。

约束(02 §3 / 03 §14.7):mechanism 的 model_function 必须在
iesplan.modeling.functions.* 白名单内且映射表存在;data_repeat 必须携带 profile;
data_predict 必须声明 model_file;校验失败抛 ModelingConfigError(拒载,不静默降级)。
"""

from __future__ import annotations

from iesplan.core.registry import ParameterSpec

from iesplan.modeling.command import (
    ModuleCommand,
    make_command_id,
    register_command,
)
from iesplan.modeling.datadriven import build_periodic_entry, build_prediction_entry, periodic_output_key
from iesplan.modeling.devspec import DeviceSpec, validate_spec
from iesplan.modeling.enums import (
    FUNCTION_REF_PREFIX,
    MODEL_METHOD_DATA_PREDICT,
    MODEL_METHOD_DATA_REPEAT,
    MODEL_METHOD_MECHANISM,
)
from iesplan.modeling.errors import ModelingConfigError
from iesplan.modeling.functions import as_device_entry, mechanism_spec_for


def _build_inputs(spec: DeviceSpec) -> tuple[ParameterSpec, ...]:
    """输入字段规格 = 参数规格 + time_series.inputs 标准列(字段名+单位+min/max)。"""
    fields = [spec.parameters[key] for key in spec.parameters]
    for series in spec.time_series.get("inputs", ()):
        fields.append(ParameterSpec(name=series.key, unit=series.unit))
    return tuple(fields)


def _build_outputs(spec: DeviceSpec, fallback_name: str, fallback_unit: str) -> tuple[ParameterSpec, ...]:
    """输出字段规格 = out 端口 + time_series.outputs 列,按声明顺序去重。

    端口单位取 capacity_ref 参数单位(容量单位=参数单位,02 §2.2);无端口时
    取机理映射默认输出名/单位。
    """
    fields: list[ParameterSpec] = []
    seen: set[str] = set()
    for port in spec.ports:
        if port.direction not in ("out", "bidirectional"):
            continue
        unit = fallback_unit
        if port.capacity_ref and port.capacity_ref in spec.parameters:
            unit = spec.parameters[port.capacity_ref].unit
        if port.name not in seen:
            seen.add(port.name)
            fields.append(ParameterSpec(name=port.name, unit=unit))
    for series in spec.time_series.get("outputs", ()):
        if series.key not in seen:
            seen.add(series.key)
            fields.append(ParameterSpec(name=series.key, unit=series.unit))
    if not fields:
        fields.append(ParameterSpec(name=fallback_name or "output", unit=fallback_unit or "-"))
    return tuple(fields)


def _build_state_fields(spec: DeviceSpec) -> tuple[ParameterSpec, ...]:
    """状态字段规格:名称+单位,min/max 取自 bounds 引用的参数(02 §2.5)。"""
    fields: list[ParameterSpec] = []
    for state in spec.states:
        min_val = max_val = None
        if state.bounds:
            if state.bounds.get("min_ref") in spec.parameters:
                min_val = spec.parameters[state.bounds["min_ref"]].min
            if state.bounds.get("max_ref") in spec.parameters:
                max_val = spec.parameters[state.bounds["max_ref"]].max
        fields.append(ParameterSpec(name=state.key, unit=state.unit, min=min_val, max=max_val))
    return tuple(fields)


def build_command(spec: DeviceSpec, profile: dict | None = None) -> ModuleCommand:
    """按 spec.model_method 生成并注册标准化命令,返回 ModuleCommand(03 §5.2)。

    profile: data_repeat 设备的周期曲线数据(标准 csv 列 → 数组,必填)。
    """
    errors = validate_spec(spec)
    if errors:
        raise ModelingConfigError("设备规格校验失败: " + "; ".join(errors))

    command_id = make_command_id(spec.type_id, spec.model_method, spec.version)
    entry = None
    function_ref = ""

    if spec.model_method == MODEL_METHOD_MECHANISM:
        if not spec.model_function.startswith(FUNCTION_REF_PREFIX):
            raise ModelingConfigError(
                f"机理函数引用必须在 {FUNCTION_REF_PREFIX}* 白名单内(03 §14.7): {spec.model_function!r}"
            )
        ms = mechanism_spec_for(spec.model_function)
        if ms is None:
            raise ModelingConfigError(f"机理映射表缺少函数: {spec.model_function!r}")
        if ms.state_key is not None and not spec.stateful:
            raise ModelingConfigError(f"函数 {ms.name} 为有状态模型,但设备声明为 stateless")
        if ms.state_key is None and spec.stateful:
            raise ModelingConfigError(f"函数 {ms.name} 为无状态模型,但设备声明为 stateful")
        function_ref = spec.model_function
        entry = as_device_entry(
            ms.fn,
            series_keys=ms.series_keys,
            param_bindings=ms.param_bindings,
            output_name=ms.output_name,
            state_key=ms.state_key,
            state_arg=ms.state_arg,
            takes_dt=ms.takes_dt,
        )
        outputs = _build_outputs(spec, ms.output_name, ms.output_unit)
        data_file = None

    elif spec.model_method == MODEL_METHOD_DATA_REPEAT:
        if profile is None:
            raise ModelingConfigError(
                f"data_repeat 设备 {spec.type_id} 缺少标准 csv 数据(profile 必填)"
            )
        function_ref = "iesplan.modeling.datadriven.periodic_repeat"
        entry = build_periodic_entry(spec, profile)
        out_key, out_unit = periodic_output_key(spec, profile)
        outputs = _build_outputs(spec, out_key, out_unit)
        data_file = spec.data_file

    else:  # MODEL_METHOD_DATA_PREDICT
        function_ref = "iesplan.modeling.datadriven.prediction_model"
        entry = build_prediction_entry(spec)
        outputs = _build_outputs(spec, "", "")
        data_file = spec.model_file

    cmd = ModuleCommand(
        command_id=command_id,
        function_ref=function_ref,
        version=spec.version,
        stateful=spec.stateful,
        inputs=_build_inputs(spec),
        outputs=outputs,
        data_file=data_file,
        state_fields=_build_state_fields(spec) if spec.stateful else (),
    )
    register_command(cmd, fn=entry)
    return cmd
