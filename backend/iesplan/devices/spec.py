"""设备 yaml 规范:数据模型与结构校验(02 §2、§6.1;05 §7.6 合并入 spec.py)。

字段命名遵循 05 §7.1 裁决:
- 模型类型标志统一为 ``model_method``,取值 ``mechanism | data_repeat | data_predict``
  (02 的 ``modeling_method`` / ``data_periodic`` / ``data_forecast`` 命名废止);
- 状态标志统一为布尔 ``stateful``(02 的 ``statefulness`` 枚举废止);
- ``fidelity``(low/medium/high)与 model_method 正交共存(沿用 model_fidelity CHECK)。

参数 schema 复用 core/registry.py::ParameterSpec(只读参照其字段命名, 不修改)。
本模块只做"单个 yaml 文件"的结构解析与字段级校验;跨字段约束(csv 必选、states
必填等)在 loader.validate_device_dir 完成;``$price:`` 引用解析在 pricing.py。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from iesplan.core.errors import AppError
from iesplan.core.registry import ParameterSpec
from iesplan.core.timeaxis import RESOLUTIONS
from iesplan.devices import yamlmini

# ---------------------------------------------------------------------------
# 模型类型标志枚举(05 §7.1 定案)
# ---------------------------------------------------------------------------

#: 模型类型标志:机理 / 数据-周期重复 / 数据-预测
MODEL_METHODS: tuple[str, ...] = ("mechanism", "data_repeat", "data_predict")
#: 模型精度档(与 model_method 正交,沿用 model_fidelity CHECK 取值)
FIDELITY_VALUES: tuple[str, ...] = ("low", "medium", "high")
#: 端口类型(02 §2.2 表 + pv 示例的 solar 端口)
PORT_TYPES: tuple[str, ...] = ("electric", "thermal", "cooling", "fuel", "water", "data", "solar")
#: 端口方向
PORT_DIRECTIONS: tuple[str, ...] = ("in", "out", "bidirectional")
#: data_repeat 周期粒度
PERIOD_VALUES: tuple[str, ...] = ("day", "week", "year")
#: data_predict 模型文件格式
MODEL_FILE_FORMATS: tuple[str, ...] = ("onnx", "csv_lookup", "python")

#: 模型类型标志中文名(展示用)
MODEL_METHOD_LABELS: dict[str, str] = {
    "mechanism": "机理模型",
    "data_repeat": "数据-周期重复",
    "data_predict": "数据-预测",
}

_ID_PATTERN = re.compile(r"^ies\.device\.[a-z][a-z0-9_]*$")
_SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
#: mechanism 函数包白名单前缀(05 §7.7: 限 iesplan.modeling.functions.*)
FUNCTION_PACKAGE_PREFIX = "iesplan.modeling.functions."


def _err(message: str, **params: object) -> AppError:
    """结构错误:统一 SYS-CFG-001(02 §6.1:码沿用 SYS-CFG-001)。"""
    return AppError(message, code="SYS-CFG-001", message_key="ies.diag.store.config_invalid", params=params)


# ---------------------------------------------------------------------------
# 数据模型(02 §6.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeriesSpec:
    """时间序列列声明(02 §2.4)。"""

    key: str
    unit: str
    resolution: str  # "15min" | "30min" | "1h"
    required: bool = True
    period: str | None = None  # "day" | "week" | "year"(仅 data_repeat)
    convert: dict | None = None  # 非标准单位换算声明 {to, factor, offset}


@dataclass(frozen=True, slots=True)
class PortSpec:
    """端口定义(02 §2.2)。"""

    name: str
    port_type: str  # electric/thermal/cooling/fuel/water/data/solar
    direction: str  # in/out/bidirectional
    energy_carrier: str
    capacity_ref: str | None = None


@dataclass(frozen=True, slots=True)
class StateSpec:
    """状态定义(02 §2.5,stateful 设备)。"""

    key: str
    unit: str
    initial_ref: str | None = None
    bounds: dict[str, str] | None = None  # {"min_ref": ..., "max_ref": ...}


@dataclass(frozen=True, slots=True)
class DeviceYamlSpec:
    """设备 yaml 规格(02 §6.1;字段命名按 05 §7.1/§7.7)。"""

    type_id: str
    version: str
    name_zh: str
    name_en: str
    model_method: str  # mechanism | data_repeat | data_predict
    stateful: bool
    fidelity: str = "medium"
    energy_carriers: list[str] = field(default_factory=list)
    is_load: bool = False
    capabilities: list[str] = field(default_factory=list)
    extends: str = "ies.device.base"
    help_topic: str = ""
    ports: list[PortSpec] = field(default_factory=list)
    parameters: dict[str, ParameterSpec] = field(default_factory=dict)
    time_series: dict[str, list[SeriesSpec]] = field(
        default_factory=lambda: {"inputs": [], "outputs": []}
    )
    states: list[StateSpec] = field(default_factory=list)
    function: dict = field(default_factory=dict)  # {"entry","package"} 或 {"model_file": {...}}
    base_dir: str = ""  # yaml 所在目录(相对引用 model_file/csv 用)


# ---------------------------------------------------------------------------
# 解析(结构/枚举/必填字段,AppError SYS-CFG-001)
# ---------------------------------------------------------------------------


def _req_str(raw: dict, key: str, file: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _err(f"缺少必填字段: {key}", file=file, field=key)
    return value.strip()


def load_yaml(path: Path) -> DeviceYamlSpec:
    """解析单个设备 yaml;结构/枚举/必填字段非法抛 AppError(码 SYS-CFG-001)。

    参数默认值原样保留(可能为 ``$price:`` 字符串), 价格解析在加载期由
    pricing.resolve_param_default 完成。
    """
    path = Path(path)
    file = str(path)
    try:
        raw = yamlmini.load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise _err("设备 yaml 读取失败", file=file) from exc
    except yamlmini.YamlParseError as exc:
        raise _err("设备 yaml 语法错误", file=file, line=exc.line, detail=str(exc)) from exc
    if not isinstance(raw, dict):
        raise _err("设备 yaml 顶层必须为映射", file=file)

    type_id = _req_str(raw, "type_id", file)
    if not _ID_PATTERN.match(type_id):
        raise _err(f"type_id 不合法: {type_id!r}", file=file, type_id=type_id)
    version = _req_str(raw, "version", file)
    if not _SEMVER_PATTERN.match(version):
        raise _err(f"version 必须为 semver x.y.z: {version!r}", file=file, version=version)
    name_zh = _req_str(raw, "name_zh", file)
    name_en = _req_str(raw, "name_en", file)
    model_method = _req_str(raw, "model_method", file)
    if model_method not in MODEL_METHODS:
        raise _err(
            f"model_method 非法: {model_method!r}, 允许值 {MODEL_METHODS}",
            file=file,
            model_method=model_method,
        )
    stateful = raw.get("stateful")
    if not isinstance(stateful, bool):
        raise _err("stateful 必须为布尔值", file=file)
    fidelity = raw.get("fidelity", "medium")
    if fidelity not in FIDELITY_VALUES:
        raise _err(f"fidelity 非法: {fidelity!r}", file=file, fidelity=fidelity)
    carriers = raw.get("energy_carriers", [])
    if not isinstance(carriers, list) or not carriers or not all(isinstance(c, str) for c in carriers):
        raise _err("energy_carriers 必须为非空字符串列表", file=file)
    is_load = raw.get("is_load", False)
    if not isinstance(is_load, bool):
        raise _err("is_load 必须为布尔值", file=file)
    capabilities = raw.get("capabilities", [])
    if not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
        raise _err("capabilities 必须为字符串列表", file=file)
    extends = raw.get("extends", "ies.device.base")
    if not isinstance(extends, str):
        raise _err("extends 必须为字符串", file=file)
    help_topic = raw.get("help_topic", "")
    if not isinstance(help_topic, str):
        raise _err("help_topic 必须为字符串", file=file)

    function = raw.get("function") or {}
    if not isinstance(function, dict):
        raise _err("function 必须为映射", file=file)

    return DeviceYamlSpec(
        type_id=type_id,
        version=version,
        name_zh=name_zh,
        name_en=name_en,
        model_method=model_method,
        stateful=stateful,
        fidelity=fidelity,
        energy_carriers=list(carriers),
        is_load=is_load,
        capabilities=list(capabilities),
        extends=extends,
        help_topic=help_topic,
        ports=_parse_ports(raw.get("ports", []), file),
        parameters=_parse_parameters(raw.get("parameters", {}), file),
        time_series=_parse_time_series(raw.get("time_series", {}), file),
        states=_parse_states(raw.get("states", []), file),
        function=function,
        base_dir=str(path.resolve().parent),
    )


def _parse_ports(items: object, file: str) -> list[PortSpec]:
    if not isinstance(items, list):
        raise _err("ports 必须为列表", file=file)
    out: list[PortSpec] = []
    for it in items:
        if not isinstance(it, dict):
            raise _err("端口必须为映射", file=file)
        name = it.get("name")
        if not isinstance(name, str) or not name:
            raise _err("端口缺少 name", file=file)
        port_type = it.get("port_type")
        if port_type not in PORT_TYPES:
            raise _err(f"端口 {name!r} port_type 非法: {port_type!r}", file=file, port=name)
        direction = it.get("direction")
        if direction not in PORT_DIRECTIONS:
            raise _err(f"端口 {name!r} direction 非法: {direction!r}", file=file, port=name)
        carrier = it.get("energy_carrier")
        if not isinstance(carrier, str) or not carrier:
            raise _err(f"端口 {name!r} 缺少 energy_carrier", file=file, port=name)
        capacity_ref = it.get("capacity_ref")
        if capacity_ref is not None and not isinstance(capacity_ref, str):
            raise _err(f"端口 {name!r} capacity_ref 必须为字符串", file=file, port=name)
        out.append(
            PortSpec(
                name=name,
                port_type=port_type,
                direction=direction,
                energy_carrier=carrier,
                capacity_ref=capacity_ref,
            )
        )
    return out


_PARAM_KEYS = (
    "unit",
    "min",
    "max",
    "default",
    "is_optimizable",
    "stock_or_addition",
    "existing_default",
    "enum",
    "help_key",
)


def _parse_parameters(items: object, file: str) -> dict[str, ParameterSpec]:
    if not isinstance(items, dict):
        raise _err("parameters 必须为映射", file=file)
    out: dict[str, ParameterSpec] = {}
    for name, data in items.items():
        if not isinstance(name, str) or not name:
            raise _err("参数名必须为非空字符串", file=file)
        if not isinstance(data, dict):
            raise _err(f"参数 {name!r} 必须为映射", file=file, param=name)
        unknown = [k for k in data if k not in _PARAM_KEYS]
        if unknown:
            raise _err(f"参数 {name!r} 含未知键: {unknown}", file=file, param=name, keys=unknown)
        unit = data.get("unit")
        if not isinstance(unit, str) or not unit.strip():
            raise _err(f"参数 {name!r} 缺少 unit", file=file, param=name)
        mn, mx = data.get("min"), data.get("max")
        for v in (mn, mx):
            if v is not None and not isinstance(v, (int, float)):
                raise _err(f"参数 {name!r} min/max 必须为数值或 null", file=file, param=name)
        if isinstance(mn, (int, float)) and isinstance(mx, (int, float)) and mn > mx:
            raise _err(f"参数 {name!r} min({mn}) 大于 max({mx})", file=file, param=name)
        default = data.get("default")
        if not isinstance(default, (int, float, str, dict, list, bool, type(None))):
            raise _err(f"参数 {name!r} default 类型非法", file=file, param=name)
        is_optimizable = data.get("is_optimizable", False)
        if not isinstance(is_optimizable, bool):
            raise _err(f"参数 {name!r} is_optimizable 必须为布尔值", file=file, param=name)
        stock = data.get("stock_or_addition", "stock")
        if stock not in ("stock", "addition"):
            raise _err(f"参数 {name!r} stock_or_addition 非法: {stock!r}", file=file, param=name)
        enum = data.get("enum")
        if enum is not None:
            if not isinstance(enum, list):
                raise _err(f"参数 {name!r} enum 必须为列表", file=file, param=name)
            enum = tuple(enum)
        existing = data.get("existing_default")
        if existing is not None and not isinstance(existing, (int, float)):
            raise _err(f"参数 {name!r} existing_default 必须为数值", file=file, param=name)
        help_key = data.get("help_key", "")
        if not isinstance(help_key, str):
            raise _err(f"参数 {name!r} help_key 必须为字符串", file=file, param=name)
        out[name] = ParameterSpec(
            name=name,
            unit=unit,
            min=mn,
            max=mx,
            default=default,
            is_optimizable=is_optimizable,
            existing_default=existing,
            stock_or_addition=stock,
            help_key=help_key,
            enum=enum,
        )
    return out


_SERIES_KEYS = ("key", "unit", "resolution", "required", "period", "convert")


def _parse_time_series(raw: object, file: str) -> dict[str, list[SeriesSpec]]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise _err("time_series 必须为映射", file=file)
    unknown_sec = [k for k in raw if k not in ("inputs", "outputs")]
    if unknown_sec:
        raise _err(f"time_series 含未知章节: {unknown_sec}", file=file, keys=unknown_sec)
    return {
        "inputs": _parse_series_list(raw.get("inputs", []), file),
        "outputs": _parse_series_list(raw.get("outputs", []), file),
    }


def _parse_series_list(items: object, file: str) -> list[SeriesSpec]:
    if not isinstance(items, list):
        raise _err("time_series 章节必须为列表", file=file)
    out: list[SeriesSpec] = []
    for it in items:
        if not isinstance(it, dict):
            raise _err("时间序列列必须为映射", file=file)
        unknown = [k for k in it if k not in _SERIES_KEYS]
        if unknown:
            raise _err(f"时间序列列含未知键: {unknown}", file=file, keys=unknown)
        key = it.get("key")
        if not isinstance(key, str) or not key:
            raise _err("时间序列列缺少 key", file=file)
        unit = it.get("unit", "")
        if not isinstance(unit, str):
            raise _err(f"列 {key!r} unit 必须为字符串", file=file, field=key)
        resolution = it.get("resolution", "1h")
        if resolution not in RESOLUTIONS:
            raise _err(f"列 {key!r} resolution 非法: {resolution!r}", file=file, field=key)
        required = it.get("required", True)
        if not isinstance(required, bool):
            raise _err(f"列 {key!r} required 必须为布尔值", file=file, field=key)
        period = it.get("period")
        if period is not None and period not in PERIOD_VALUES:
            raise _err(f"列 {key!r} period 非法: {period!r}", file=file, field=key)
        convert = it.get("convert")
        if convert is not None and not isinstance(convert, dict):
            raise _err(f"列 {key!r} convert 必须为映射", file=file, field=key)
        out.append(
            SeriesSpec(
                key=key,
                unit=unit,
                resolution=resolution,
                required=required,
                period=period,
                convert=convert,
            )
        )
    return out


_STATE_KEYS = ("key", "unit", "initial_ref", "bounds")


def _parse_states(items: object, file: str) -> list[StateSpec]:
    if items is None:
        return []
    if not isinstance(items, list):
        raise _err("states 必须为列表", file=file)
    out: list[StateSpec] = []
    for it in items:
        if not isinstance(it, dict):
            raise _err("状态必须为映射", file=file)
        unknown = [k for k in it if k not in _STATE_KEYS]
        if unknown:
            raise _err(f"状态含未知键: {unknown}", file=file, keys=unknown)
        key = it.get("key")
        if not isinstance(key, str) or not key:
            raise _err("状态缺少 key", file=file)
        unit = it.get("unit", "")
        if not isinstance(unit, str):
            raise _err(f"状态 {key!r} unit 必须为字符串", file=file, field=key)
        initial_ref = it.get("initial_ref")
        if initial_ref is not None and not isinstance(initial_ref, str):
            raise _err(f"状态 {key!r} initial_ref 必须为字符串", file=file, field=key)
        bounds = it.get("bounds")
        if bounds is not None:
            if not isinstance(bounds, dict) or not all(k in ("min_ref", "max_ref") for k in bounds):
                raise _err(
                    f"状态 {key!r} bounds 必须为 {{min_ref, max_ref}} 映射", file=file, field=key
                )
            for v in bounds.values():
                if not isinstance(v, str):
                    raise _err(f"状态 {key!r} bounds 引用必须为参数名字符串", file=file, field=key)
        out.append(StateSpec(key=key, unit=unit, initial_ref=initial_ref, bounds=bounds))
    return out


# ---------------------------------------------------------------------------
# 派生与兼容
# ---------------------------------------------------------------------------


def with_resolved_defaults(spec: DeviceYamlSpec, resolved: dict[str, object]) -> DeviceYamlSpec:
    """以价格解析结果替换参数默认值, 返回新 spec(不修改原对象)。"""
    if not resolved:
        return spec
    parameters = dict(spec.parameters)
    for name, value in resolved.items():
        parameters[name] = replace(parameters[name], default=value)
    return replace(spec, parameters=parameters)


def spec_to_dict(spec: DeviceYamlSpec) -> dict:
    """序列化为 JSON 兼容字典(供 GET /api/devices/types 等 API 输出)。"""
    return {
        "type_id": spec.type_id,
        "version": spec.version,
        "name_zh": spec.name_zh,
        "name_en": spec.name_en,
        "model_method": spec.model_method,
        "stateful": spec.stateful,
        "fidelity": spec.fidelity,
        "energy_carriers": list(spec.energy_carriers),
        "is_load": spec.is_load,
        "capabilities": list(spec.capabilities),
        "extends": spec.extends,
        "help_topic": spec.help_topic,
        "ports": [
            {
                "name": p.name,
                "port_type": p.port_type,
                "direction": p.direction,
                "energy_carrier": p.energy_carrier,
                "capacity_ref": p.capacity_ref,
            }
            for p in spec.ports
        ],
        "parameters": {
            name: {
                "unit": p.unit,
                "min": p.min,
                "max": p.max,
                "default": p.default,
                "is_optimizable": p.is_optimizable,
                "existing_default": p.existing_default,
                "stock_or_addition": p.stock_or_addition,
                "help_key": p.help_key,
                "enum": list(p.enum) if p.enum else None,
            }
            for name, p in spec.parameters.items()
        },
        "time_series": {
            "inputs": [
                {"key": s.key, "unit": s.unit, "resolution": s.resolution,
                 "required": s.required, "period": s.period, "convert": s.convert}
                for s in spec.time_series.get("inputs", [])
            ],
            "outputs": [
                {"key": s.key, "unit": s.unit, "resolution": s.resolution,
                 "required": s.required, "period": s.period, "convert": s.convert}
                for s in spec.time_series.get("outputs", [])
            ],
        },
        "states": [
            {"key": s.key, "unit": s.unit, "initial_ref": s.initial_ref, "bounds": s.bounds}
            for s in spec.states
        ],
        "function": spec.function,
    }


def to_registry_spec(spec: DeviceYamlSpec) -> "DeviceTypeSpec":
    """转 core/registry.py::DeviceTypeSpec(兼容层, 供现有 API 复用)。

    model_method/stateful 两字段的透传属 core/registry.py 侧改造项(05 §6 兼容边界),
    本模块只读映射其既有字段。
    """
    from iesplan.core.registry import DeviceTypeSpec

    return DeviceTypeSpec(
        type_id=spec.type_id,
        version=spec.version,
        name_zh=spec.name_zh,
        name_en=spec.name_en,
        energy_carriers=list(spec.energy_carriers),
        is_load=spec.is_load,
        capabilities=list(spec.capabilities),
        extends=spec.extends,
        parameters=dict(spec.parameters),
        help_topic=spec.help_topic,
    )


def to_modeling_spec(spec: DeviceYamlSpec):
    """转 modeling.devspec.DeviceSpec(建模模块输入, 05 §2.3 阶段 ①→② 契约)。

    映射规则:
    - model_function = function.package + '.' + function.entry(机理);
    - data_file / model_file 取自 function.model_file 或 yaml 所在目录相对引用;
    - ports/time_series/states 字段直接平移(两包字段签名一致)。
    """
    from iesplan.modeling.devspec import DeviceSpec as ModelingDeviceSpec
    from iesplan.modeling.devspec import PortSpec as ModelingPortSpec
    from iesplan.modeling.devspec import SeriesSpec as ModelingSeriesSpec
    from iesplan.modeling.devspec import StateSpec as ModelingStateSpec

    fn = spec.function if isinstance(spec.function, dict) else {}
    model_file = None
    data_file = None
    if isinstance(fn.get("model_file"), dict):
        ref = fn["model_file"].get("file")
        if isinstance(ref, str) and ref:
            model_file = ref
    if isinstance(fn.get("data_file"), str) and fn["data_file"]:
        data_file = fn["data_file"]
    model_function = ""
    if spec.model_method == "mechanism":
        package = fn.get("package") or ""
        entry = fn.get("entry") or ""
        if package and entry:
            model_function = f"{package}.{entry}"

    return ModelingDeviceSpec(
        type_id=spec.type_id,
        version=spec.version,
        name_zh=spec.name_zh,
        name_en=spec.name_en,
        energy_carriers=list(spec.energy_carriers),
        is_load=spec.is_load,
        capabilities=list(spec.capabilities),
        extends=spec.extends,
        parameters=dict(spec.parameters),
        help_topic=spec.help_topic,
        model_method=spec.model_method,
        stateful=spec.stateful,
        fidelity=spec.fidelity,
        model_function=model_function,
        model_file=model_file,
        data_file=data_file,
        ports=tuple(
            ModelingPortSpec(
                name=p.name,
                port_type=p.port_type,
                direction=p.direction,
                energy_carrier=p.energy_carrier,
                capacity_ref=p.capacity_ref,
            )
            for p in spec.ports
        ),
        time_series={
            "inputs": tuple(
                ModelingSeriesSpec(
                    key=s.key, unit=s.unit, resolution=s.resolution,
                    required=s.required, period=s.period,
                )
                for s in spec.time_series.get("inputs", [])
            ),
            "outputs": tuple(
                ModelingSeriesSpec(
                    key=s.key, unit=s.unit, resolution=s.resolution,
                    required=s.required, period=s.period,
                )
                for s in spec.time_series.get("outputs", [])
            ),
        },
        states=tuple(
            ModelingStateSpec(
                key=s.key, unit=s.unit, initial_ref=s.initial_ref, bounds=s.bounds
            )
            for s in spec.states
        ),
    )
