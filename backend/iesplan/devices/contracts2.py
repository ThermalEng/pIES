"""`ies.device-model` 2.2.0 公开契约：深度不可变值对象与唯一规范化。

对应格式标准 [device-model-yaml.md](../../../manual/developer-guide/zh-CN/formats/device-model-yaml.md)
与包内 `schema/device-model-1.0.0.contract.md`。

顶层只允许 `schema/schema_version/device/properties/interfaces/equations`；
模板额外允许顶层 `inputs`（见 ``DeviceModelTemplate``）。设备不声明独立语义版本，
内容由稳定 ID、发布 revision 与校验回执固定。文本文件只校验字头。

所有 interface 都不承载项目来源（source/mode/value/data_ref/target_type 均禁止）；
项目来源由装配实例绑定。本模块是纯数据类型与纯函数：不访问数据库、文件系统与业务模块。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

#: 统一文件契约标识与版本
SCHEMA_ID = "ies.device-model"
SCHEMA_VERSION = "1.0.0"

#: 模板 inputs 叶子类型（标量 + 预定义数据 + 结构容器）
INPUT_SCALAR_TYPES: tuple[str, ...] = ("number", "boolean", "string")
INPUT_DATA_TYPES: tuple[str, ...] = ("data_repeat", "data_full", "data_predict")
INPUT_TYPES: tuple[str, ...] = INPUT_SCALAR_TYPES + INPUT_DATA_TYPES + ("object", "array")

#: 接口类型（五类；缺省规范化为 blind）
INTERFACE_TYPES: tuple[str, ...] = ("in", "out", "bidirectional", "predefined", "blind")

#: 允许被连接的接口类型（in/out/bidirectional 之外均为盲或预定义）
CONNECTABLE_TYPES: tuple[str, ...] = ("in", "out", "bidirectional")

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
class InterfaceSpec:
    """``interfaces`` 单项：序列接口。

    值域界可为数值或 ``{property_ref}``（实例额定能力引用，实例级解析）。
    设备模型不承载项目来源；来源由装配实例绑定。
    """

    id: str
    type: str  # in/out/bidirectional/predefined/blind
    carrier: str
    unit: str
    valid_range: tuple[float | None, float | None]
    minimum_property_ref: str | None = None
    maximum_property_ref: str | None = None

    @property
    def minimum(self) -> float | None:
        return self.valid_range[0]

    @property
    def maximum(self) -> float | None:
        return self.valid_range[1]


@dataclass(frozen=True, slots=True)
class EquationVariable:
    """``equations.variables`` 内部序列变量（值域界同接口支持 property 引用）。"""

    id: str
    unit: str
    valid_range: tuple[float | None, float | None] | None = None
    initial_property_ref: str | None = None
    initial_expression: str | None = None
    minimum_property_ref: str | None = None
    maximum_property_ref: str | None = None


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
    """一份 ``ies.device-model`` 2.2.0 设备的深度不可变文档。

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
    """``inputs`` 叶子声明（number/boolean/string/data_repeat/data_full/data_predict/object/array）。"""

    path: str  # 点分路径，如 "peak_power_kw"、"profile.rows"
    type: str
    unit: str | None = None
    valid_range: tuple[float | None, float | None] | None = None
    children: tuple["TemplateInputSpec", ...] = ()  # object/array 子声明


@dataclass(frozen=True, slots=True)
class TemplateInputs:
    """模板顶层 ``inputs`` 的扁平声明视图（供表单生成与实例化校验）。"""

    raw: Mapping[str, Any]  # 原始 inputs 树
    leaves: tuple[TemplateInputSpec, ...]  # 深度优先叶子清单


@dataclass(frozen=True, slots=True)
class CanonicalModel:
    """规范化结果：规范文本与回执。"""

    canonical_text: str
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
                **({"initial": {"expression": v.initial_expression}}
                   if v.initial_expression is not None else {}),
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


def _valid_range_of(raw: object) -> tuple[float | None, float | None] | None:
    """``{"minimum", "maximum"}`` → 值域元组；缺席返回 None。"""
    if not isinstance(raw, Mapping):
        return None
    return (raw.get("minimum"), raw.get("maximum"))


def from_dict(raw: Mapping[str, Any]) -> DeviceModelDocument:
    """规范 ``to_dict`` 形态 → 深度不可变文档（纯结构还原，不重校验）。

    供已通过入口校验并保存的模型对象在消费链读取时还原为领域文档；
    不再重复 schema/方程/值域校验（入口唯一校验，宪法 §2.6）。
    """
    device_raw = raw.get("device")
    device = (
        DeviceInfo(id=str(device_raw["id"]), names=_freeze(device_raw.get("names") or {}))
        if isinstance(device_raw, Mapping)
        else None
    )
    properties = MappingProxyType(
        {
            str(pid): PropertySpec(
                id=str(pid),
                value=item["value"],
                unit=str(item["unit"]),
                valid_range=_valid_range_of(item.get("valid_range")),
            )
            for pid, item in (raw.get("properties") or {}).items()
        }
    )
    interfaces: dict[str, InterfaceSpec] = {}
    for iid, item in (raw.get("interfaces") or {}).items():
        interfaces[str(iid)] = InterfaceSpec(
            id=str(iid),
            type=str(item["type"]),
            carrier=str(item["carrier"]),
            unit=str(item["unit"]),
            valid_range=_valid_range_of(item.get("valid_range")) or (None, None),
        )
    equations_raw = raw.get("equations") or {}
    variables = MappingProxyType(
        {
            str(vid): EquationVariable(
                id=str(vid),
                unit=str(item["unit"]),
                valid_range=_valid_range_of(item.get("valid_range")),
                initial_property_ref=(item.get("initial") or {}).get("property_ref"),
                initial_expression=(item.get("initial") or {}).get("expression"),
            )
            for vid, item in (equations_raw.get("variables") or {}).items()
        }
    )
    relations = tuple(
        EquationRelation(id=str(item["id"]), expression=str(item["expression"]))
        for item in (equations_raw.get("relations") or [])
    )
    inputs = _freeze(raw["inputs"]) if raw.get("inputs") is not None else None
    return DeviceModelDocument(
        schema_version=str(raw.get("schema_version", SCHEMA_VERSION)),
        device=device,
        properties=properties,
        interfaces=MappingProxyType(interfaces),
        equations=Equations(variables=variables, relations=relations),
        inputs=inputs,
    )


def canonical_receipt(document: DeviceModelDocument) -> dict[str, Any]:
    """校验回执：schema、规范化器版本与结构统计。文本文件只校验字头。"""
    return {
        "schema": SCHEMA_ID,
        "schema_version": document.schema_version,
        "canonicalizer": "ies.device-model.canonical@1.0.0",
        "device_id": document.device.id if document.device is not None else None,
        "property_count": len(document.properties),
        "interface_count": len(document.interfaces),
        "relation_count": len(document.equations.relations),
        "is_template": document.inputs is not None,
    }
