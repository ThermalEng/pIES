"""`ies.device-model` 2.0.0 公开契约：深度不可变值对象与唯一规范化。

对应格式标准 [device-model-yaml.md](../../../manual/developer-guide/zh-CN/formats/device-model-yaml.md)
与包内 `schema/device-model-2.0.0.contract.md`。

顶层只允许 `schema/schema_version/device/properties/interfaces/equations`；
模板额外允许顶层 `inputs`（见 ``DeviceModelTemplate``）。设备不声明独立语义版本，
内容由稳定 ID、规范字节 SHA-256、发布 revision 与校验回执固定。

本模块是纯数据类型与纯函数：不访问数据库、文件系统与业务模块。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

#: 统一文件契约标识与版本
SCHEMA_ID = "ies.device-model"
SCHEMA_VERSION = "2.0.0"

#: 模板 inputs 叶子类型（标量 + 预定义数据 + 结构容器）
INPUT_SCALAR_TYPES: tuple[str, ...] = ("number", "boolean", "string")
INPUT_DATA_TYPES: tuple[str, ...] = ("data_repeat", "data_predict")
INPUT_TYPES: tuple[str, ...] = INPUT_SCALAR_TYPES + INPUT_DATA_TYPES + ("object", "array")

#: 接口类型（五类；缺省规范化为 blind）
INTERFACE_TYPES: tuple[str, ...] = ("in", "out", "bidirectional", "predefined", "blind")
#: 预定义来源模式
SOURCE_MODES: tuple[str, ...] = ("constant", "data_repeat", "data_predict")

#: 允许被连接的接口类型（in/out/bidirectional 之外均为盲或预定义）
CONNECTABLE_TYPES: tuple[str, ...] = ("in", "out", "bidirectional")
#: 允许声明 source 的接口类型
SOURCE_TYPES: tuple[str, ...] = ("predefined",)

#: 稳定设备类型 ID（非项目实例 ID）
_ID_PATTERN = re.compile(r"^[a-z0-9]+([._-][a-z0-9]+)*$")

#: 规范序列化选项（稳定键序、紧凑、非 ASCII 保留）
_CANONICAL_KWARGS: dict[str, Any] = {
    "ensure_ascii": False,
    "sort_keys": True,
    "separators": (",", ":"),
    "allow_nan": False,
}


def is_valid_id(value: object) -> bool:
    """稳定设备类型 ID 是否符合命名规则（小写、点/下划线/连字符分段）。"""
    return isinstance(value, str) and bool(_ID_PATTERN.fullmatch(value))


def _freeze(value: Any) -> Any:
    """递归转换为深度不可变结构（tuple 代替 list，MappingProxyType 代替 dict）。"""
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """``device``：稳定身份与本地化显示名。"""

    id: str
    names: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True, slots=True)
class PropertySpec:
    """``properties`` 单项：非时变技术常量。"""

    id: str
    value: float | bool | str
    unit: str
    valid_range: tuple[float | None, float | None] | None = None

    @property
    def minimum(self) -> float | None:
        return self.valid_range[0] if self.valid_range else None

    @property
    def maximum(self) -> float | None:
        return self.valid_range[1] if self.valid_range else None


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """``source``：预定义序列来源。"""

    mode: str  # constant | data_repeat | data_predict
    value: float | bool | str | None = None
    data_ref: str | None = None


@dataclass(frozen=True, slots=True)
class InterfaceSpec:
    """``interfaces`` 单项：序列接口。"""

    id: str
    type: str  # in/out/bidirectional/predefined/blind
    carrier: str
    unit: str
    valid_range: tuple[float | None, float | None]
    source: SourceSpec | None = None

    @property
    def minimum(self) -> float | None:
        return self.valid_range[0]

    @property
    def maximum(self) -> float | None:
        return self.valid_range[1]


@dataclass(frozen=True, slots=True)
class EquationVariable:
    """``equations.variables`` 内部序列变量。"""

    id: str
    unit: str
    valid_range: tuple[float | None, float | None] | None = None
    initial_property_ref: str | None = None


@dataclass(frozen=True, slots=True)
class EquationRelation:
    """``equations.relations`` 单条关系式。"""

    id: str
    expression: str


@dataclass(frozen=True, slots=True)
class Equations:
    """``equations``：内部变量与关系式。"""

    variables: Mapping[str, EquationVariable] = field(
        default_factory=lambda: MappingProxyType({})
    )
    relations: tuple[EquationRelation, ...] = ()


@dataclass(frozen=True, slots=True)
class DeviceModelDocument:
    """一份 ``ies.device-model`` 2.0.0 设备的深度不可变文档。

    ``inputs`` 仅在模板（未实例化阶段）存在；普通模型为 ``None``。
    """

    schema_version: str = SCHEMA_VERSION
    device: DeviceInfo | None = None
    properties: Mapping[str, PropertySpec] = field(default_factory=lambda: MappingProxyType({}))
    interfaces: Mapping[str, InterfaceSpec] = field(default_factory=lambda: MappingProxyType({}))
    equations: Equations = field(default_factory=Equations)
    inputs: Mapping[str, Any] | None = None  # 模板专用；实例化后删除


@dataclass(frozen=True, slots=True)
class TemplateInputSpec:
    """``inputs`` 叶子声明（number/boolean/string/data_repeat/data_predict/object/array）。"""

    path: str  # 点分路径，如 "peak_power_kw"、"profile.rows"
    type: str
    unit: str | None = None
    valid_range: tuple[float | None, float | None] | None = None
    default: float | bool | str | None = None
    data_ref: str | None = None  # data_repeat/data_predict 绑定的数据引用
    children: tuple["TemplateInputSpec", ...] = ()  # object/array 子声明


@dataclass(frozen=True, slots=True)
class TemplateInputs:
    """模板顶层 ``inputs`` 的扁平声明视图（供表单生成与实例化校验）。"""

    raw: Mapping[str, Any]  # 原始 inputs 树
    leaves: tuple[TemplateInputSpec, ...]  # 深度优先叶子清单


@dataclass(frozen=True, slots=True)
class CanonicalModel:
    """规范化结果：规范字节、内容摘要与回执。"""

    canonical_text: str
    content_sha256: str
    receipt: Mapping[str, Any]

    @property
    def canonical_bytes(self) -> bytes:
        return self.canonical_text.encode("utf-8")


def to_dict(document: DeviceModelDocument) -> dict[str, Any]:
    """文档 → 普通 dict（供规范化/回执/序列化；非规范化顺序）。"""
    out: dict[str, Any] = {
        "schema": SCHEMA_ID,
        "schema_version": document.schema_version,
    }
    if document.device is not None:
        out["device"] = {"id": document.device.id, "names": dict(document.device.names)}
    out["properties"] = {
        pid: {
            "value": p.value,
            "unit": p.unit,
            "valid_range": (
                {"minimum": p.valid_range[0], "maximum": p.valid_range[1]}
                if p.valid_range is not None
                else None
            ),
        }
        for pid, p in document.properties.items()
    }
    out["interfaces"] = {
        iid: {
            "type": iface.type,
            "carrier": iface.carrier,
            "unit": iface.unit,
            "valid_range": {"minimum": iface.minimum, "maximum": iface.maximum},
            **(
                {
                    "source": {
                        "mode": iface.source.mode,
                        **(
                            {"value": iface.source.value}
                            if iface.source.value is not None
                            else {}
                        ),
                        **(
                            {"data_ref": iface.source.data_ref}
                            if iface.source.data_ref is not None
                            else {}
                        ),
                    }
                }
                if iface.source is not None
                else {}
            ),
        }
        for iid, iface in document.interfaces.items()
    }
    out["equations"] = {
        "variables": {
            vid: {
                "unit": v.unit,
                **({"valid_range": {"minimum": v.valid_range[0], "maximum": v.valid_range[1]}}
                   if v.valid_range is not None else {}),
                **({"initial": {"property_ref": v.initial_property_ref}}
                   if v.initial_property_ref is not None else {}),
            }
            for vid, v in document.equations.variables.items()
        },
        "relations": [
            {"id": r.id, "expression": r.expression} for r in document.equations.relations
        ],
    }
    if document.inputs is not None:
        out["inputs"] = document.inputs
    return out


def canonical_bytes(document: DeviceModelDocument) -> bytes:
    """版本化规范字节：稳定键序、紧凑 JSON、UTF-8；不允许 NaN/Infinity。"""
    return json.dumps(to_dict(document), **_CANONICAL_KWARGS).encode("utf-8")


def content_sha256(document: DeviceModelDocument) -> str:
    """对规范字节计算的小写 64 位十六进制 SHA-256（宪法 §7.2）。"""
    return hashlib.sha256(canonical_bytes(document)).hexdigest()


def canonical_receipt(document: DeviceModelDocument) -> dict[str, Any]:
    """校验回执：schema、规范化器版本、内容摘要与结构摘要。"""
    return {
        "schema": SCHEMA_ID,
        "schema_version": document.schema_version,
        "canonicalizer": "ies.device-model.canonical@2.0.0",
        "content_sha256": content_sha256(document),
        "device_id": document.device.id if document.device is not None else None,
        "property_count": len(document.properties),
        "interface_count": len(document.interfaces),
        "relation_count": len(document.equations.relations),
        "is_template": document.inputs is not None,
    }
