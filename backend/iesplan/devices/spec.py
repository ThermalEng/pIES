"""设备 yaml 规范：数据模型与公开契约（roadmap 0.5.0 迁移后）。

本模块是 ``ies.device-model`` 1.0.0 新格式的**解析与值对象层**：
- ``load_yaml`` 解析新格式（schema/device/parameters/ports/data_inputs/states/
  model_commands/extensions）；
- ``DeviceModelDescriptor`` 是公开深度不可变值对象（list→tuple, dict→
  MappingProxyType）；**不暴露 function/package/module/宿主机路径**；
- ``to_model_descriptor`` 把解析后的 YAML 规格转为公开描述。

消费方（services/assembly/engines/modeling）只读公开 descriptor 字段：
type_id/version/name_zh/name_en/model_method/stateful/fidelity/energy_carriers/
is_load/capabilities/extends/help_topic/parameters/ports/time_series/states/
model_commands。``function`` 与 ``standard_csv_path`` 已从公开面移除——
建模命令解析只存在于组合根与 modeling provider 内部。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType

from iesplan.core.contracts.parameters import ParameterSpec
from iesplan.core.errors import AppError
from iesplan.core.timeaxis import RESOLUTIONS
from iesplan.devices import yamlmini
from iesplan.devices.parser import parse_device_model_yaml

# ---------------------------------------------------------------------------
# 枚举（新契约）
# ---------------------------------------------------------------------------

MODEL_METHODS: tuple[str, ...] = ("mechanism", "data_repeat", "data_predict")
FIDELITY_VALUES: tuple[str, ...] = ("low", "medium", "high")
PORT_TYPES: tuple[str, ...] = ("electric", "thermal", "cooling", "fuel", "water", "data", "solar")
PORT_DIRECTIONS: tuple[str, ...] = ("in", "out", "bidirectional")
PERIOD_VALUES: tuple[str, ...] = ("day", "week", "year")

MODEL_METHOD_LABELS: dict[str, str] = {
    "mechanism": "机理模型",
    "data_repeat": "数据-周期重复",
    "data_predict": "数据-预测",
}

_SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")

#: 载能 → 端口类型（公开 descriptor 端口携带 port_type 供前端/装配消费）
CARRIER_PORT_TYPE: dict[str, str] = {
    "electric": "electric",
    "heat": "thermal",
    "cool": "cooling",
    "gas": "fuel",
    "solar": "solar",
    "water": "water",
    "data": "data",
}


def _err(message: str, **params: object) -> AppError:
    return AppError(message, code="SYS-CFG-001", message_key="ies.diag.store.config_invalid", params=params)


# ---------------------------------------------------------------------------
# 数据模型（深度不可变）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeriesSpec:
    """时间序列列声明（映射自新格式 data_inputs；装配/建模消费）。"""

    key: str
    unit: str
    resolution: str = "1h"
    required: bool = True
    period: str | None = None


@dataclass(frozen=True, slots=True)
class PortSpec:
    """端口定义（映射自新格式 ports）。"""

    name: str
    port_type: str
    direction: str
    energy_carrier: str
    capacity_ref: str | None = None


@dataclass(frozen=True, slots=True)
class StateSpec:
    """状态定义（映射自新格式 states）。"""

    key: str
    unit: str
    initial_ref: str | None = None
    bounds: MappingProxyType | None = None  # {"min_ref":..., "max_ref":...}


@dataclass(frozen=True, slots=True)
class DeviceYamlSpec:
    """设备 yaml 规格（内部解析结果；字段命名按新契约）。

    只承载解析后的不可变数据；``model_commands`` 映射
    capability → ``<command-id>@<exact-version>``。``source_path`` 是
    **内部**路径字段（devices 模块用于标准 csv 推导），不进公开 descriptor。
    """

    type_id: str
    version: str
    name_zh: str
    name_en: str
    model_method: str
    stateful: bool
    fidelity: str = "medium"
    energy_carriers: tuple[str, ...] = ()
    is_load: bool = False
    capabilities: tuple[str, ...] = ()
    extends: str = "ies.device.base"
    help_topic: str = ""
    ports: tuple[PortSpec, ...] = ()
    parameters: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    time_series: MappingProxyType = field(
        default_factory=lambda: MappingProxyType({"inputs": (), "outputs": ()})
    )
    states: tuple[StateSpec, ...] = ()
    model_commands: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    extensions: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    source_path: str = ""  # 内部：源 yaml 完整路径（标准 csv 推导用，不进公开 descriptor）


@dataclass(frozen=True, slots=True)
class DeviceModelDescriptor:
    """已验证设备描述的公开契约（深度不可变；devices → 外部模块唯一传递物）。

    建模命令由 ``model_commands``（capability → 稳定命令 ID@版本）声明；
    **不包含** function/package/module/宿主机路径。
    """

    type_id: str
    version: str
    name_zh: str
    name_en: str
    model_method: str
    stateful: bool
    fidelity: str
    energy_carriers: tuple[str, ...]
    is_load: bool
    capabilities: tuple[str, ...]
    extends: str
    help_topic: str
    parameters: MappingProxyType  # name → ParameterSpec
    ports: tuple[PortSpec, ...]
    time_series: MappingProxyType  # {"inputs": tuple, "outputs": tuple}
    states: tuple[StateSpec, ...]
    model_commands: MappingProxyType  # capability → "id@version"


def to_model_descriptor(spec: DeviceYamlSpec) -> DeviceModelDescriptor:
    """DeviceYamlSpec → 公开建模描述（深度不可变，不含路径/函数入口）。"""
    return DeviceModelDescriptor(
        type_id=spec.type_id,
        version=spec.version,
        name_zh=spec.name_zh,
        name_en=spec.name_en,
        model_method=spec.model_method,
        stateful=spec.stateful,
        fidelity=spec.fidelity,
        energy_carriers=tuple(spec.energy_carriers),
        is_load=spec.is_load,
        capabilities=tuple(spec.capabilities),
        extends=spec.extends,
        help_topic=spec.help_topic,
        parameters=MappingProxyType(dict(spec.parameters)),
        ports=tuple(spec.ports),
        time_series=MappingProxyType(
            {"inputs": tuple(spec.time_series.get("inputs", ())), "outputs": tuple(spec.time_series.get("outputs", ()))}
        ),
        states=tuple(spec.states),
        model_commands=MappingProxyType(dict(spec.model_commands)),
    )


def model_command_refs(desc: DeviceModelDescriptor) -> list[str]:
    """公开辅助：设备声明的全部命令引用（id@version 列表）。"""
    return list(desc.model_commands.values())


# ---------------------------------------------------------------------------
# 解析（新格式 → DeviceYamlSpec）
# ---------------------------------------------------------------------------


def _parse_parameters(parsed, param_help: MappingProxyType | None = None) -> MappingProxyType:
    """新格式 parameters → {name: ParameterSpec}（价格引用原样保留）。

    ``param_help`` 取自 extensions.ies.meta.param_help（help_key 元数据）。
    """
    param_help = param_help or MappingProxyType({})
    out: dict[str, ParameterSpec] = {}
    for name, p in parsed.parameters.items():
        default = p.default
        enum = p.enum
        out[name] = ParameterSpec(
            name=name,
            unit=p.unit,
            min=p.minimum,
            max=p.maximum,
            default=default,
            is_optimizable=p.optimizable,
            existing_default=default if isinstance(default, (int, float)) and not isinstance(default, bool) else (0.0 if p.stock_or_addition == "addition" else default),
            stock_or_addition=p.stock_or_addition,
            help_key=str(param_help.get(name, "")),
            enum=tuple(enum) if enum is not None else None,
        )
    return MappingProxyType(out)


def _parse_ports(parsed) -> tuple[PortSpec, ...]:
    out: list[PortSpec] = []
    for name, p in parsed.ports.items():
        out.append(
            PortSpec(
                name=name,
                port_type=CARRIER_PORT_TYPE.get(p.carrier, p.carrier),
                direction=p.direction,
                energy_carrier=p.carrier,
                capacity_ref=p.capacity_parameter,
            )
        )
    return tuple(out)


def _parse_time_series(parsed) -> MappingProxyType:
    """新格式 data_inputs → time_series（inputs 节；outputs 为空）。

    周期粒度自 extensions.periods 恢复（data_repeat 设备）。
    """
    periods = parsed.extensions.get("periods") if isinstance(parsed.extensions.get("periods"), dict) else {}
    inputs: list[SeriesSpec] = []
    for column, d in parsed.data_inputs.items():
        inputs.append(
            SeriesSpec(
                key=column,
                unit=d.unit,
                resolution="1h",
                required=d.required,
                period=periods.get(column),
            )
        )
    return MappingProxyType({"inputs": tuple(inputs), "outputs": ()})


def _parse_states(parsed) -> tuple[StateSpec, ...]:
    out: list[StateSpec] = []
    for name, s in parsed.states.items():
        bounds = None
        if s.minimum_ref or s.maximum_ref:
            b: dict[str, str] = {}
            if s.minimum_ref:
                b["min_ref"] = s.minimum_ref
            if s.maximum_ref:
                b["max_ref"] = s.maximum_ref
            bounds = MappingProxyType(b)
        out.append(StateSpec(key=name, unit=s.unit, initial_ref=s.initial_ref, bounds=bounds))
    return tuple(out)


def _meta(parsed, key: str, default: object = "") -> object:
    """extensions.ies.meta 元数据读取（help_topic/is_load/extends 等）。"""
    meta = parsed.extensions.get("ies.meta")
    if not isinstance(meta, dict):
        return default
    return meta.get(key, default)


def load_yaml(path: Path) -> DeviceYamlSpec:
    """解析单个新格式设备 yaml；结构/未知字段非法抛 AppError（码 SYS-CFG-001）。"""
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

    result = parse_device_model_yaml(raw, file=file)
    if not result.ok:
        first = result.diagnostics[0] if result.diagnostics else None
        if first is not None:
            raise _err(
                f"设备 yaml 校验失败: {first.params.get('detail', '')}",
                file=file,
                field=first.location.get("field") if first.location else None,
                diagnostics=[d.to_dict() for d in result.diagnostics],
            )
        raise _err("设备 yaml 校验失败", file=file)

    parsed = result.document
    info = parsed.device
    names = info.names if info is not None else {}
    meta = parsed.extensions.get("ies.meta")
    param_help = MappingProxyType(meta.get("param_help", {})) if isinstance(meta, dict) and isinstance(meta.get("param_help"), dict) else MappingProxyType({})
    return DeviceYamlSpec(
        type_id=info.id,
        version=info.version,
        name_zh=names.get("zh-CN", ""),
        name_en=names.get("en-US", ""),
        model_method=info.model_method,
        stateful=info.stateful,
        fidelity=info.fidelity,
        energy_carriers=tuple(info.energy_carriers),
        is_load=bool(_meta(parsed, "is_load", False)),
        capabilities=tuple(info.capabilities),
        extends=str(_meta(parsed, "extends", "ies.device.base")),
        help_topic=str(_meta(parsed, "help_topic", "")),
        ports=_parse_ports(parsed),
        parameters=_parse_parameters(parsed, param_help),
        time_series=_parse_time_series(parsed),
        states=_parse_states(parsed),
        model_commands=MappingProxyType(dict(parsed.model_commands)),
        extensions=parsed.extensions,
        source_path=str(path.resolve()),
    )


def _parse_parameters_legacy(items: object, file: str) -> dict[str, ParameterSpec]:  # pragma: no cover
    """旧格式参数解析（迁移期保留；新格式不再使用）。"""
    return {}


# ---------------------------------------------------------------------------
# 兼容与派生
# ---------------------------------------------------------------------------


def with_resolved_defaults(spec: DeviceYamlSpec, resolved: dict[str, object]) -> DeviceYamlSpec:
    """以价格解析结果替换参数默认值, 返回新 spec(不修改原对象)。"""
    if not resolved:
        return spec
    parameters = dict(spec.parameters)
    for name, value in resolved.items():
        parameters[name] = replace(parameters[name], default=value)
    return replace(spec, parameters=MappingProxyType(parameters))


def spec_to_dict(spec: DeviceYamlSpec) -> dict:
    """序列化为 JSON 兼容字典（供 GET /api/devices/types 等 API 输出）。

    不再输出 function/宿主机路径；model_commands 为公开命令引用。
    """
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
                 "required": s.required, "period": s.period}
                for s in spec.time_series.get("inputs", ())
            ],
            "outputs": [
                {"key": s.key, "unit": s.unit, "resolution": s.resolution,
                 "required": s.required, "period": s.period}
                for s in spec.time_series.get("outputs", ())
            ],
        },
        "states": [
            {"key": s.key, "unit": s.unit, "initial_ref": s.initial_ref, "bounds": dict(s.bounds) if s.bounds else None}
            for s in spec.states
        ],
        "model_commands": dict(spec.model_commands),
    }


def to_modeling_spec(spec: DeviceYamlSpec):
    """转 modeling.devspec.DeviceSpec（建模模块输入；命令解析在 provider 内部）。

    机理设备：model_function 由稳定命令 ID 在 modeling provider 内解析；
    本函数不再拼接 function.package/entry（文件不暴露模块路径）。
    """
    from iesplan.modeling.devspec import DeviceSpec as ModelingDeviceSpec
    from iesplan.modeling.devspec import PortSpec as ModelingPortSpec
    from iesplan.modeling.devspec import SeriesSpec as ModelingSeriesSpec
    from iesplan.modeling.devspec import StateSpec as ModelingStateSpec

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
        model_function="",  # 由 modeling provider 经命令 ID 解析
        model_file=None,
        data_file=None,
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
                for s in spec.time_series.get("inputs", ())
            ),
            "outputs": tuple(
                ModelingSeriesSpec(
                    key=s.key, unit=s.unit, resolution=s.resolution,
                    required=s.required, period=s.period,
                )
                for s in spec.time_series.get("outputs", ())
            ),
        },
        states=tuple(
            ModelingStateSpec(
                key=s.key, unit=s.unit, initial_ref=s.initial_ref, bounds=s.bounds
            )
            for s in spec.states
        ),
    )
