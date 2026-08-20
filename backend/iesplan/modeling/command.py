"""标准函数注册表与统一调用契约(03 §5.2 + 02 §6.5)。

建模模块产出物 = **标准化后台调用命令**(ModuleCommand):标准化函数名(command_id)、
函数引用(function_ref,env path 可解析)、输入/输出字段规格(字段名+单位+min/max)、
状态标志与状态字段。计算模块的唯一调用途径是 ``call_command``(按命令 id 分发),
不再直接 import 设备函数(05 §2.1 阶段 ②→④)。

统一调用契约(02 §6.5,所有设备函数遵循):
    device_entry(params, series, state, dt_s, prices) -> DeviceRunResult
- params:  注册表单位(业务单位 kW/kWh/kWp 等,已含价格解析后的默认值);
- series:  标准列 → 内部单位序列(W/J/K,由装配层在快照装配期换算);
- state:   有状态设备的当前状态快照 dict;stateless 传 None;
- dt_s:    时间步长(秒);
- prices:  运行期价格(来自 PriceBook + 项目覆盖)。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from iesplan.core.errors import NotFoundError
from iesplan.core.registry import ParameterSpec

from iesplan.modeling.enums import COMMAND_ID_PREFIX, MODEL_METHODS


@dataclass(frozen=True, slots=True)
class ModuleCommand:
    """标准化后台调用命令(建模模块成果,计算模块唯一调用途径,03 §5.2)。"""

    command_id: str  # 'ies.command.model.ies.device.pv.mechanism.1.4.0'
    function_ref: str  # env path 可解析:'iesplan.modeling.functions.pv_output'
    version: str
    stateful: bool = False  # 有/无状态模型标志
    inputs: tuple[ParameterSpec, ...] = ()  # 输入字段规格(字段名+单位+min/max)
    outputs: tuple[ParameterSpec, ...] = ()  # 输出字段规格(字段名+单位)
    data_file: str | None = None  # 数据方法:绑定的标准 csv/模型文件引用
    state_fields: tuple[ParameterSpec, ...] = ()  # 有状态命令的状态输入/输出字段规格(02 §2.5)


@dataclass(frozen=True, slots=True)
class DeviceRunResult:
    """设备函数统一运行结果(02 §6.5)。"""

    outputs: dict[str, np.ndarray]  # 端口输出(内部单位:W 功率/J 能量/无量纲),键=端口名
    state_new: dict[str, float] | None = None  # 有状态设备的下一状态;stateless 为 None
    cost: dict[str, np.ndarray] = field(default_factory=dict)  # 运行成本序列(CNY,可选)
    emissions: dict[str, np.ndarray] = field(default_factory=dict)  # 排放序列(kgCO2,可选)


# ---------------------------------------------------------------------------
# 全局命令注册表(进程内单例;启动时由 main.py 装载 catalog 后自动注册)
# ---------------------------------------------------------------------------
_COMMANDS: dict[str, ModuleCommand] = {}
#: 数据方法生成的闭包缓存(闭包无法经 env path 解析,注册命令时附带;命令 id → Callable)
_GENERATED_FUNCS: dict[str, Callable] = {}


def make_command_id(device_type: str, model_method: str, version: str) -> str:
    """标准化命令 id:'ies.command.model.{device_type}.{method}.{version}'(03 §5.2)。"""
    return f"{COMMAND_ID_PREFIX}{device_type}.{model_method}.{version}"


def parse_command_id(command_id: str) -> tuple[str, str, str]:
    """拆解命令 id → (device_type, model_method, version);格式非法抛 ValueError。

    命令 id 结构:'ies.command.model.{type_id}.{method}.{version}'。
    type_id 自身可含点(如 'ies.device.heat_pump'),method 为固定枚举
    (mechanism/data_repeat/data_predict),version 为 semver(含点)。
    以 method 枚举为锚点定位拆分(版本段不再用 rsplit 切碎)。
    """
    prefix_len = len(COMMAND_ID_PREFIX)
    if not command_id.startswith(COMMAND_ID_PREFIX):
        raise ValueError(f"命令 id 缺少前缀 {COMMAND_ID_PREFIX!r}: {command_id!r}")
    tail = command_id[prefix_len:]
    method_pos = -1
    for method in MODEL_METHODS:
        pos = tail.rfind(f".{method}.")
        if pos > method_pos:
            method_pos = pos
    if method_pos < 0:
        raise ValueError(f"命令 id 缺少方法段(mechanism/data_repeat/data_predict): {command_id!r}")
    device_type = tail[:method_pos]
    rest = tail[method_pos + 1 :]
    method, _, version = rest.partition(".")
    if not device_type or not version or "." not in version:
        raise ValueError(f"命令 id 段不完整: {command_id!r}")
    return device_type, method, version


def register_command(cmd: ModuleCommand, fn: Callable | None = None) -> None:
    """注册命令(建模成果入库;启动时装载 catalog 后自动注册)。

    fn: 数据方法的生成闭包(可选);传入后 ``get_entry_function`` 优先返回闭包,
    否则运行时经 ``resolve_function_ref`` 解析 function_ref。重复注册同 id 视为覆盖。
    """
    if not cmd.command_id:
        raise ValueError("command_id 不能为空")
    if fn is not None and not callable(fn):
        raise ValueError(f"命令 {cmd.command_id} 的 fn 不可调用")
    _COMMANDS[cmd.command_id] = cmd
    if fn is not None:
        _GENERATED_FUNCS[cmd.command_id] = fn
    else:
        _GENERATED_FUNCS.pop(cmd.command_id, None)


def get_command(command_id: str) -> ModuleCommand | None:
    """按 id 取命令;未注册返回 None。"""
    return _COMMANDS.get(command_id)


def get_command_or_raise(command_id: str) -> ModuleCommand:
    """按 id 取命令;未注册抛 NotFoundError(02 §6.5:未知命令抛 NotFoundError)。"""
    cmd = _COMMANDS.get(command_id)
    if cmd is None:
        raise NotFoundError(f"建模命令未注册: {command_id}", code="CONN-TYPE-002")
    return cmd


def list_commands() -> list[ModuleCommand]:
    """已注册命令列表(按注册顺序,确定性)。"""
    return list(_COMMANDS.values())


def clear_commands() -> None:
    """清空注册表(测试隔离/热重载用;生产启动装载时幂等)。"""
    _COMMANDS.clear()
    _GENERATED_FUNCS.clear()


def snapshot() -> list[str]:
    """注册表快照:["ies.command.model.ies.device.pv.mechanism.1.4.0", ...](确定性)。"""
    return list(_COMMANDS.keys())


def resolve_function_ref(function_ref: str) -> Callable:
    """沿 env path 解析函数对象(importlib;03 §5.2,等价现有 executors._run_engine 解析)。"""
    module_path, _, attr = function_ref.rpartition(".")
    if not module_path or not attr:
        raise NotFoundError(f"函数引用格式非法: {function_ref!r}", code="CONN-TYPE-002")
    try:
        module = importlib.import_module(module_path)
        fn = getattr(module, attr)
    except (ImportError, AttributeError) as exc:
        raise NotFoundError(f"函数引用无法解析: {function_ref!r}", code="CONN-TYPE-002") from exc
    if not callable(fn):
        raise NotFoundError(f"函数引用指向不可调用对象: {function_ref!r}", code="CONN-TYPE-002")
    return fn


def get_entry_function(command_id: str) -> Callable:
    """取命令的执行函数:优先返回注册时附带的生成闭包,否则解析 function_ref。"""
    get_command_or_raise(command_id)
    fn = _GENERATED_FUNCS.get(command_id)
    if fn is not None:
        return fn
    return resolve_function_ref(_COMMANDS[command_id].function_ref)


# ---------------------------------------------------------------------------
# 统一调用契约分发入口
# ---------------------------------------------------------------------------


def _normalize_result(result: object, fallback_name: str | None = None) -> DeviceRunResult:
    """把各类函数返回值归一为 DeviceRunResult(兼容机理裸函数与契约函数)。"""
    if isinstance(result, DeviceRunResult):
        return result
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], dict):
        outputs, state_new = result
        return DeviceRunResult(outputs=outputs, state_new=state_new)
    if isinstance(result, dict):
        return DeviceRunResult(outputs=result)
    if isinstance(result, np.ndarray) or (isinstance(result, (list, tuple)) and not result):
        if fallback_name is None:
            raise ValueError("数组返回但未提供输出字段名,无法构造 DeviceRunResult")
        return DeviceRunResult(outputs={fallback_name: np.asarray(result, dtype=np.float64)})
    raise ValueError(f"无法识别的函数返回类型: {type(result)!r}")


def call_command(command_id: str, ctx: dict) -> DeviceRunResult:
    """标准化调用命令分发入口(02 §6.5):按 command_id 取命令并以统一契约调用。

    ctx 键:params(业务单位 dict)/series(标准列 → 内部单位 ndarray)/state(dict|None)/
    dt_s(float 秒)/prices(dict,可选);缺省键按契约默认值补齐。
    有状态命令会把 state 原样传入函数,结果中的 state_new 供下一时间步回写。
    """
    cmd = get_command_or_raise(command_id)
    fn = get_entry_function(command_id)
    params = ctx.get("params") or {}
    series = ctx.get("series") or {}
    state = ctx.get("state")
    dt_s = ctx.get("dt_s", 3600.0)
    prices = ctx.get("prices") or {}
    if cmd.stateful and state is None:
        state = {}
    result = fn(params, series, state, dt_s, prices)
    return _normalize_result(result, fallback_name=cmd.outputs[0].name if cmd.outputs else None)
