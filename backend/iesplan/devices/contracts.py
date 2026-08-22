"""`ies.device-model` 1.0.0 公开契约：深度不可变值对象与唯一规范化。

契约语义来源：``manual/developer-guide/zh-CN/formats/device-model-yaml.md``。

本模块是新契约的权威类型层，只持有**无业务状态**的不可变数据：
- ``DeviceModelDocument``：一份设备模型 YAML 校验后的完整不可变文档；
- ``DeviceInfo`` / ``DeviceParameter`` / ``DevicePort`` / ``DeviceDataInput`` /
  ``DeviceState`` / ``ModelCommandRef``：各节的值对象；
- ``canonicalize_device_model``：确定性规范化（稳定键排序 + 移除注释/别名/
  非语义空白）→ 规范 JSON + SHA-256 摘要。

设计约束（宪法 §7.7 / file-formats.md）：
- 所有 list → ``tuple``，所有 dict → ``MappingProxyType``，dataclass ``frozen``；
- 摘要基于版本化、规范化后的字节计算；
- 规范化算法唯一并由公开纯函数实现；
- 本模块禁止导入 devices 的 loader/registry/parser 等实现细节。
"""

from __future__ import annotations

import hashlib
import json
from types import MappingProxyType

from dataclasses import dataclass, field

#: 契约标识
SCHEMA_ID = "ies.device-model"
#: 契约版本（文件格式版本，非设备版本）
SCHEMA_VERSION = "1.0.0"

#: 设备模型 JSON Schema 文件路径（包内相对路径，供校验与文档引用）
DEVICE_MODEL_SCHEMA_PATH = "schema/device-model-1.0.0.schema.json"

#: 建模方式枚举
MODEL_METHODS: tuple[str, ...] = ("mechanism", "data_repeat", "data_predict")
#: 精度档枚举
FIDELITY_VALUES: tuple[str, ...] = ("low", "medium", "high")
#: 端口方向枚举
PORT_DIRECTIONS: tuple[str, ...] = ("in", "out", "bidirectional")
#: 参数取值类型枚举
VALUE_TYPES: tuple[str, ...] = ("number", "string", "boolean")
#: 存量/新增枚举
STOCK_OR_ADDITION_VALUES: tuple[str, ...] = ("stock", "addition", "not_applicable")


def _freeze(value: object) -> object:
    """递归转换为深度不可变形态（tuple / MappingProxyType / 标量）。"""
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """设备身份与建模标志（``device`` 节）。"""

    id: str
    version: str
    names: MappingProxyType  # {"zh-CN": str, "en-US": str}
    model_method: str  # mechanism | data_repeat | data_predict
    stateful: bool
    fidelity: str  # low | medium | high
    energy_carriers: tuple[str, ...]
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DeviceParameter:
    """业务参数声明（``parameters`` 节）。"""

    name: str
    value_type: str  # number | string | boolean
    quantity: str
    unit: str
    required: bool
    default: object = None
    minimum: float | None = None
    maximum: float | None = None
    enum: tuple | None = None
    optimizable: bool = False
    stock_or_addition: str = "stock"


@dataclass(frozen=True, slots=True)
class DevicePort:
    """可连接端口声明（``ports`` 节）。"""

    name: str
    carrier: str
    direction: str  # in | out | bidirectional
    quantity: str
    unit: str
    capacity_parameter: str | None = None


@dataclass(frozen=True, slots=True)
class DeviceDataInput:
    """CSV 可绑定数据列声明（``data_inputs`` 节）。"""

    column_id: str
    value_type: str
    quantity: str
    unit: str
    required: bool
    minimum: float | None = None
    maximum: float | None = None


@dataclass(frozen=True, slots=True)
class DeviceState:
    """状态声明（``states`` 节；有状态设备）。"""

    name: str
    unit: str
    initial_ref: str | None = None
    minimum_ref: str | None = None
    maximum_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ModelCommandRef:
    """建模命令引用（``model_commands`` 节）。

    ``command_id`` 为稳定命令 ID，``version`` 为精确语义版本；
    ``<command-id>@<exact-version>`` 是文件中的唯一书写形态。
    """

    command_id: str
    version: str

    @property
    def ref(self) -> str:
        """id@version 形态（规范书写）。"""
        return f"{self.command_id}@{self.version}"


@dataclass(frozen=True, slots=True)
class DeviceModelDocument:
    """一份 ``ies.device-model`` 1.0.0 设备模型的深度不可变文档。"""

    schema_id: str = SCHEMA_ID
    schema_version: str = SCHEMA_VERSION
    device: DeviceInfo | None = None
    parameters: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    ports: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    data_inputs: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    states: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    model_commands: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    extensions: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))

    def command_for(self, capability: str) -> ModelCommandRef | None:
        """按能力取命令引用（未声明返回 None）。"""
        ref = self.model_commands.get(capability)
        if ref is None:
            return None
        command_id, _, version = ref.partition("@")
        return ModelCommandRef(command_id=command_id, version=version)


# ---------------------------------------------------------------------------
# 规范化与摘要（宪法 §7.7：唯一纯函数、稳定键排序、规范字节哈希）
# ---------------------------------------------------------------------------

#: 顶层键规范顺序（书写友好顺序；规范化输出按此排序）
_TOP_LEVEL_ORDER = (
    "schema",
    "schema_version",
    "device",
    "parameters",
    "ports",
    "data_inputs",
    "states",
    "model_commands",
    "extensions",
)

_DEVICE_ORDER = (
    "id",
    "version",
    "names",
    "model_method",
    "stateful",
    "fidelity",
    "energy_carriers",
    "capabilities",
)

_PARAM_ORDER = (
    "name",
    "value_type",
    "quantity",
    "unit",
    "required",
    "default",
    "minimum",
    "maximum",
    "enum",
    "optimizable",
    "stock_or_addition",
)

_PORT_ORDER = (
    "name",
    "carrier",
    "direction",
    "quantity",
    "unit",
    "capacity_parameter",
)

_DATA_INPUT_ORDER = (
    "column_id",
    "value_type",
    "quantity",
    "unit",
    "required",
    "minimum",
    "maximum",
)

_STATE_ORDER = ("name", "unit", "initial_ref", "minimum_ref", "maximum_ref")


def _ordered(value: object, order: tuple[str, ...]) -> object:
    """按给定键序重排 dict，其余嵌套 dict 按键名排序（稳定确定性）。"""
    if isinstance(value, MappingProxyType):
        value = dict(value)
    if isinstance(value, dict):
        ordered = {k: _ordered(value[k], ()) for k in order if k in value}
        for k in sorted(k for k in value if k not in order):
            ordered[k] = _ordered(value[k], ())
        return ordered
    if isinstance(value, list):
        return [_ordered(v, ()) for v in value]
    if isinstance(value, tuple):
        return [_ordered(v, ()) for v in value]
    return value


def _document_to_plain(document: DeviceModelDocument) -> dict:
    """把不可变文档转为可序列化 plain dict（规范化输入形态）。"""
    device = None
    if document.device is not None:
        device = {
            "id": document.device.id,
            "version": document.device.version,
            "names": dict(document.device.names),
            "model_method": document.device.model_method,
            "stateful": document.device.stateful,
            "fidelity": document.device.fidelity,
            "energy_carriers": list(document.device.energy_carriers),
            "capabilities": list(document.device.capabilities),
        }
    parameters = {
        name: {
            "name": name,
            "value_type": p.value_type,
            "quantity": p.quantity,
            "unit": p.unit,
            "required": p.required,
            "default": p.default,
            "minimum": p.minimum,
            "maximum": p.maximum,
            "enum": list(p.enum) if p.enum is not None else None,
            "optimizable": p.optimizable,
            "stock_or_addition": p.stock_or_addition,
        }
        for name, p in document.parameters.items()
    }
    ports = {
        name: {
            "name": name,
            "carrier": p.carrier,
            "direction": p.direction,
            "quantity": p.quantity,
            "unit": p.unit,
            "capacity_parameter": p.capacity_parameter,
        }
        for name, p in document.ports.items()
    }
    data_inputs = {
        column: {
            "column_id": column,
            "value_type": d.value_type,
            "quantity": d.quantity,
            "unit": d.unit,
            "required": d.required,
            "minimum": d.minimum,
            "maximum": d.maximum,
        }
        for column, d in document.data_inputs.items()
    }
    states = {
        name: {
            "name": name,
            "unit": s.unit,
            "initial_ref": s.initial_ref,
            "minimum_ref": s.minimum_ref,
            "maximum_ref": s.maximum_ref,
        }
        for name, s in document.states.items()
    }
    model_commands = {
        capability: ref if isinstance(ref, str) else ref.ref
        for capability, ref in document.model_commands.items()
    }
    return {
        "schema": document.schema_id,
        "schema_version": document.schema_version,
        "device": device,
        "parameters": parameters,
        "ports": ports,
        "data_inputs": data_inputs,
        "states": states,
        "model_commands": model_commands,
        "extensions": _plain_extensions(document.extensions),
    }


def _plain_extensions(extensions: object) -> dict:
    """extensions 递归转 plain（保留任意嵌套，按键排序）。"""
    if isinstance(extensions, MappingProxyType):
        extensions = dict(extensions)
    if not isinstance(extensions, dict):
        return dict(extensions or {})
    return {k: _plain(v) for k, v in extensions.items()}


def _plain(value: object) -> object:
    if isinstance(value, MappingProxyType):
        value = dict(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def canonicalize_device_model(document: DeviceModelDocument) -> tuple[str, str]:
    """唯一规范化：返回 (规范 JSON 文本, SHA-256 摘要)。

    规范形态（file-formats.md "人工编写与规范化"）：
    - 按格式规定排序（顶层/设备/参数/端口/数据列/状态固定键序，其余按键名排序）；
    - 移除注释、别名和非语义空白（注释/别名在解析期已丢失，此处保证字节级稳定）；
    - 规范字节基于版本化后的数据计算。
    同一语义输入必须产生同一规范文本与摘要。
    """
    plain = _ordered(_document_to_plain(document), _TOP_LEVEL_ORDER)
    canonical = json.dumps(
        plain,
        ensure_ascii=False,
        sort_keys=False,  # 已由 _ordered 保证确定性
        separators=(",", ":"),
        allow_nan=False,
    )
    canonical_text = canonical + "\n"
    digest = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    return canonical_text, digest


def canonicalize_raw_mapping(raw: dict) -> tuple[str, str]:
    """对已校验的原始 YAML 映射直接做唯一规范化（纯函数）。

    用于契约测试 / 迁移回执：同一语义输入得到同一摘要。
    """
    plain = _ordered(dict(raw), _TOP_LEVEL_ORDER)
    canonical = json.dumps(
        plain,
        ensure_ascii=False,
        sort_keys=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    canonical_text = canonical + "\n"
    digest = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    return canonical_text, digest


__all__ = [
    "SCHEMA_ID",
    "SCHEMA_VERSION",
    "DEVICE_MODEL_SCHEMA_PATH",
    "MODEL_METHODS",
    "FIDELITY_VALUES",
    "PORT_DIRECTIONS",
    "VALUE_TYPES",
    "STOCK_OR_ADDITION_VALUES",
    "DeviceInfo",
    "DeviceParameter",
    "DevicePort",
    "DeviceDataInput",
    "DeviceState",
    "ModelCommandRef",
    "DeviceModelDocument",
    "canonicalize_device_model",
    "canonicalize_raw_mapping",
    "_freeze",
]
