"""`ies.assembly` 2.0.0 接口网络纯协议校验(0.8.0 切片)。

消费 2.0 设备描述符(``devices.contracts2.DeviceModelDocument``)与装配 2.0
接口网络文档(devices/connections/predefined_interfaces),校验五类 interface
连接规则(宪法 §4.2/§4.4 + formats/assembly-yaml.md):

- ``in/out/bidirectional`` 可连接: 起点必须 out/bidirectional,终点必须
  in/bidirectional;``predefined`` 与 ``blind`` 从不出现在 connections;
- 两端 carrier 相同、单位量纲兼容、有效区间无冲突;禁止自环与重复边;
- ``predefined`` 只允许 constant/data_repeat/data_predict 来源;
  ``blind`` 不连接、不接收预定义数据;
- 每个设备实例用 ``definition.id + content_sha256`` 固定精确内容,与提供的
  descriptor 内容锁一致。

成功输出 ValidatedAssemblyArtifact 风格不可变三件套(纯协议版本):
规范文本、SHA-256 与校验回执;失败返回结构化诊断,不产生任何可执行产物。

**迁移边界**: 本模块是装配 2.0 切片的纯协议实现,不导入、不消费旧 1.0 的
AssemblySpec/ModelCommand/DeviceSpec 与设备注册表;旧 1.0 代码保持原样,由
后续整体迁移切片删除。资源解析、时间轴、规划经济与计算兼容校验属于后续切片,
本模块只做接口网络(阶段 3 子集)的纯协议校验。

依赖边界: 只消费 core(diagnostics/units)与 devices 公开 descriptor 纯类型。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from iesplan.assembly.diags import (
    ASM_BIND_INVALID,
    ASM_EDGE_BAD_SINK,
    ASM_EDGE_BAD_SOURCE,
    ASM_EDGE_CARRIER,
    ASM_EDGE_DUPLICATE,
    ASM_EDGE_RANGE_CONFLICT,
    ASM_EDGE_SELF_LOOP,
    ASM_EDGE_UNIT_DIM,
    ASM_LOCK_MISMATCH,
    ASM_REF_MODEL_UNREG,
    ASM_REF_PORT_UNDEF,
    ASM_SYN_FIELD,
    ASM_SYN_SCHEMA,
    ASM_SYN_TYPE,
    ASM_SYN_VERSION,
)
from iesplan.assembly.diags import (
    make_asm_diag as make_diag,
)
from iesplan.core.diagnostics import SEVERITY_ERROR, Diagnostic
from iesplan.core.units import UnitError, dims_of
from iesplan.devices.contracts2 import (
    SOURCE_MODES,
    DeviceModelDocument,
    content_sha256,
)

SCHEMA2_ID = "ies.assembly"
SCHEMA2_VERSION = "2.0.0"

VALIDATOR2_ID = "ies.assembly.validator2"
VALIDATOR2_VERSION = "2.0.0"

CANON2_ALGORITHM_ID = "ies.assembly.canonical2"
CANON2_ALGORITHM_VERSION = "2.0.0"

#: 实例/连接/接口 ID 模式(与 devices 2.0 一致)
_ID_PATTERN = re.compile(r"^[a-z0-9]+([._-][a-z0-9]+)*$")
#: content_sha256: 小写 64 位十六进制
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

#: 预定义数据来源模式(装配校验同样只放行这三类)
PREDEFINED_SOURCE_MODES: tuple[str, ...] = SOURCE_MODES
#: 允许连接的目标/源接口类型
SOURCE_TYPES: tuple[str, ...] = ("out", "bidirectional")
SINK_TYPES: tuple[str, ...] = ("in", "bidirectional")

_CANONICAL_KWARGS: dict[str, Any] = {
    "ensure_ascii": False,
    "sort_keys": True,
    "separators": (",", ":"),
    "allow_nan": False,
}

_ASSET_ORIGINS: tuple[str, ...] = ("existing", "new")


def _freeze_value(value: object) -> object:
    """递归冻结 JSON 值(深度不可变,供回执容器)。"""
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze_value(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_value(v) for v in value)
    return value


def _thaw_value(value: object) -> object:
    """只读容器 → 普通 dict/list(JSON 可序列化)。"""
    if isinstance(value, Mapping):
        return {str(k): _thaw_value(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw_value(v) for v in value]
    return value


def _stable_diagnostic_dict(diag: Diagnostic) -> dict:
    """回执仅保留确定性诊断语义,不写入 occurred_at/trace 等运行上下文。"""
    data = diag.to_dict()
    return {
        key: data[key]
        for key in (
            "code", "severity", "blocking", "message_key", "params",
            "location", "fix_hint_key", "ref_ids", "suppressed",
        )
    }


# ---------------------------------------------------------------------------
# 校验回执与成功产物(三件套纯协议版本)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NetworkReceipt:
    """接口网络校验回执(确定性;不含签发时间等运行上下文)。

    与 1.0 ``ValidationReceipt`` 同构: 校验器/规范化算法/schema 版本、
    设备内容锁(instance → content_sha256)、网络摘要与零阻断诊断。
    """

    network_sha256: str = ""
    schema: str = SCHEMA2_ID
    schema_version: str = SCHEMA2_VERSION
    validator_id: str = VALIDATOR2_ID
    validator_version: str = VALIDATOR2_VERSION
    canonical_algorithm_id: str = CANON2_ALGORITHM_ID
    canonical_algorithm_version: str = CANON2_ALGORITHM_VERSION
    device_locks: Mapping[str, str] = field(default_factory=dict)
    diagnostics: tuple[Diagnostic, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.device_locks, Mapping):
            raise TypeError("device_locks 须为 Mapping")
        diagnostics = tuple(self.diagnostics)
        if not all(isinstance(diag, Diagnostic) for diag in diagnostics):
            raise TypeError("diagnostics 须仅包含 Diagnostic")
        object.__setattr__(self, "device_locks", _freeze_value(self.device_locks))
        object.__setattr__(self, "diagnostics", diagnostics)

    def to_dict(self) -> dict:
        """确定性 JSON 兼容字典(字段固定顺序)。"""
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "validator": {"id": self.validator_id, "version": self.validator_version},
            "canonical_algorithm": {
                "id": self.canonical_algorithm_id,
                "version": self.canonical_algorithm_version,
            },
            "network_sha256": self.network_sha256,
            "device_locks": _thaw_value(self.device_locks),
            "diagnostics": [_stable_diagnostic_dict(diag) for diag in self.diagnostics],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> NetworkReceipt:
        """从持久化 JSON 严格恢复;畸形字段直接拒绝,不做兼容回退。"""
        if not isinstance(payload, Mapping):
            raise TypeError("receipt 须为 Mapping")
        expected_keys = {
            "schema", "schema_version", "validator", "canonical_algorithm",
            "network_sha256", "device_locks", "diagnostics",
        }
        if set(payload) != expected_keys:
            raise ValueError("receipt 字段集合与当前契约不一致")
        validator = payload.get("validator")
        canonical = payload.get("canonical_algorithm")
        if not isinstance(validator, Mapping) or not isinstance(canonical, Mapping):
            raise TypeError("receipt.validator/canonical_algorithm 须为 Mapping")
        if set(validator) != {"id", "version"} or set(canonical) != {"id", "version"}:
            raise ValueError("receipt validator/canonical_algorithm 字段集合不一致")
        locks = payload.get("device_locks", {})
        raw_diags = payload.get("diagnostics", [])
        if not isinstance(locks, Mapping) or not isinstance(raw_diags, (list, tuple)):
            raise TypeError("receipt device_locks/diagnostics 类型非法")
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in locks.items()):
            raise TypeError("receipt device_locks 值须为字符串")
        diagnostics = tuple(
            _restore_diagnostic(raw) for raw in raw_diags
        )
        string_fields = {
            "network_sha256": payload["network_sha256"],
            "schema": payload["schema"],
            "schema_version": payload["schema_version"],
            "validator.id": validator["id"],
            "validator.version": validator["version"],
            "canonical_algorithm.id": canonical["id"],
            "canonical_algorithm.version": canonical["version"],
        }
        for name, value in string_fields.items():
            if not isinstance(value, str) or not value:
                raise TypeError(f"receipt.{name} 须为非空字符串")
        return cls(
            network_sha256=payload["network_sha256"],
            schema=payload["schema"],
            schema_version=payload["schema_version"],
            validator_id=validator["id"],
            validator_version=validator["version"],
            canonical_algorithm_id=canonical["id"],
            canonical_algorithm_version=canonical["version"],
            device_locks=locks,
            diagnostics=diagnostics,
        )


def _restore_diagnostic(raw: object) -> Diagnostic:
    """严格恢复持久化诊断(与 1.0 回执同构的字段集合)。"""
    if not isinstance(raw, Mapping):
        raise TypeError("receipt diagnostic 须为 Mapping")
    expected_keys = {
        "code", "severity", "blocking", "message_key", "params", "location",
        "fix_hint_key", "ref_ids", "suppressed",
    }
    if set(raw) != expected_keys:
        raise ValueError("receipt diagnostic 字段集合不一致")
    params = raw["params"]
    location = raw["location"]
    ref_ids = raw["ref_ids"]
    if not isinstance(params, Mapping):
        raise TypeError("receipt diagnostic.params 须为 Mapping")
    if location is not None and not isinstance(location, Mapping):
        raise TypeError("receipt diagnostic.location 须为 Mapping 或 null")
    if not isinstance(ref_ids, (list, tuple)):
        raise TypeError("receipt diagnostic.ref_ids 须为数组")
    for key in ("code", "severity", "message_key", "fix_hint_key"):
        if not isinstance(raw[key], str) or not raw[key]:
            raise TypeError(f"receipt diagnostic.{key} 须为非空字符串")
    return Diagnostic(
        code=raw["code"],
        severity=raw["severity"],
        blocking=raw["blocking"],
        message_key=raw["message_key"],
        params=params,
        location=location,
        fix_hint_key=raw["fix_hint_key"],
        ref_ids=tuple(ref_ids),
        suppressed=raw["suppressed"],
    )


@dataclass(frozen=True, slots=True)
class ValidatedInterfaceNetwork:
    """唯一、可签名的接口网络成功产物(不可变三件套,纯协议版本)。

    - canonical_text: 规范网络文本(确定性 JSON 形态);
    - network_sha256: 规范字节 SHA-256;
    - receipt: 校验回执(含相同摘要与设备内容锁)。

    ``verify()`` 重新计算摘要并核对三件套一致;任何不一致必须拒绝使用并
    重新装配,禁止带病继续计算。
    """

    canonical_text: str
    network_sha256: str
    receipt: NetworkReceipt

    def verify(self) -> bool:
        return (
            hashlib.sha256(self.canonical_text.encode("utf-8")).hexdigest() == self.network_sha256
            and self.receipt.network_sha256 == self.network_sha256
            and self.receipt.schema == SCHEMA2_ID
            and self.receipt.schema_version == SCHEMA2_VERSION
            and self.receipt.validator_id == VALIDATOR2_ID
            and self.receipt.validator_version == VALIDATOR2_VERSION
            and self.receipt.canonical_algorithm_id == CANON2_ALGORITHM_ID
            and self.receipt.canonical_algorithm_version == CANON2_ALGORITHM_VERSION
            and not any(diag.blocking for diag in self.receipt.diagnostics)
        )

    def verify_or_raise(self) -> ValidatedInterfaceNetwork:
        """一致性校验失败抛 ValueError(阻断计算);成功返回自身。"""
        if not self.verify():
            raise ValueError(
                "接口网络产物三件套不一致: canonical_text/network_sha256/receipt 必须同时验证"
            )
        return self

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA2_ID,
            "schema_version": SCHEMA2_VERSION,
            "canonical_text": self.canonical_text,
            "network_sha256": self.network_sha256,
            "receipt": self.receipt.to_dict(),
        }


@dataclass(slots=True)
class InterfaceNetworkResult:
    """校验结果: 要么有成功产物(三件套),要么有结构化诊断列表。"""

    diagnostics: list[Diagnostic] = field(default_factory=list)
    artifact: ValidatedInterfaceNetwork | None = None

    @property
    def ok(self) -> bool:
        """校验通过且无阻断诊断(``artifact`` 必须非空)。"""
        return self.artifact is not None and not any(d.blocking for d in self.diagnostics)


# ---------------------------------------------------------------------------
# 诊断辅助
# ---------------------------------------------------------------------------


def _diag(code: str, detail: str, *, location: Mapping[str, object] | None = None,
          blocking: bool = True, severity: str = SEVERITY_ERROR) -> Diagnostic:
    """构造 ASM 域接口网络诊断(码已登记;聚合独立问题)。"""
    return make_diag(
        code,
        severity=severity,
        blocking=blocking,
        params={"detail": detail},
        location=location,
    )


def _node_loc(kind: str, field: str, value: object | None = None) -> dict[str, object]:
    loc: dict[str, object] = {"object_type": "interface-network", "field": field}
    if value is not None:
        loc["value"] = value
    return loc


def _split_endpoint(ref: str) -> tuple[str, str] | None:
    """``<instance>.<interface>`` → (instance, interface);非法返回 None。"""
    if "." not in ref:
        return None
    inst, _, iface = ref.partition(".")
    if not inst or not iface:
        return None
    return inst, iface


def _ranges_intersect(
    a_min: float | None, a_max: float | None, b_min: float | None, b_max: float | None
) -> bool:
    """两个闭合区间是否有交集(空边界视为 ±∞)。"""
    lo = max(a_min if a_min is not None else -math.inf, b_min if b_min is not None else -math.inf)
    hi = min(a_max if a_max is not None else math.inf, b_max if b_max is not None else math.inf)
    return lo <= hi


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def validate_interface_network2(
    doc: Mapping[str, Any],
    documents: Mapping[str, DeviceModelDocument],
    *,
    source_name: str = "<interface-network>",
) -> InterfaceNetworkResult:
    """装配 2.0 接口网络纯协议校验入口。

    ``doc``: 安全解析后的装配 2.0 文档子集(devices + connections +
    predefined_interfaces;顶层 schema/schema_version 可选,缺省按 2.0.0)。
    ``documents``: 实例 ID → 已校验的 2.0 设备描述符(内容锁来源)。

    成功返回三件套产物;任何 error 都不产生可执行产物。
    """
    diags: list[Diagnostic] = []
    _check_structure(doc, diags, source_name=source_name)
    instances = doc.get("devices") if isinstance(doc.get("devices"), Mapping) else {}
    connections = doc.get("connections") if isinstance(doc.get("connections"), Mapping) else {}

    _check_device_locks(instances, documents, diags)
    _check_connections(instances, connections, documents, diags)
    _check_predefined_bindings(instances, documents, diags)

    if any(d.blocking for d in diags):
        return InterfaceNetworkResult(diagnostics=diags, artifact=None)

    canonical_text = _canonical_text(instances, connections)
    digest = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    locks = {
        inst_id: content_sha256(documents[inst_id])
        for inst_id in sorted(instances)
        if documents.get(inst_id) is not None
    }
    receipt = NetworkReceipt(
        network_sha256=digest,
        device_locks=locks,
        diagnostics=tuple(d for d in diags if not d.blocking),
    )
    artifact = ValidatedInterfaceNetwork(
        canonical_text=canonical_text, network_sha256=digest, receipt=receipt
    )
    if not artifact.verify():
        diags.append(_diag(
            "ASM-ART-001",
            "三件套内部一致性失败(self_verify_failed)",
            location=_node_loc("artifact", "artifact"),
        ))
        return InterfaceNetworkResult(diagnostics=diags, artifact=None)
    return InterfaceNetworkResult(diagnostics=diags, artifact=artifact)


# ---------------------------------------------------------------------------
# 阶段 1:结构
# ---------------------------------------------------------------------------


def _check_structure(doc: Mapping[str, Any], diags: list[Diagnostic], *, source_name: str) -> None:
    """顶层 schema/版本/节类型/ID/引用形状(聚合同阶段独立问题)。"""
    schema = doc.get("schema")
    if schema not in ("ies.assembly", None):
        diags.append(_diag(
            ASM_SYN_SCHEMA,
            f"schema 标识无法识别: {schema!r}(期望 ies.assembly)",
            location=_node_loc("structure", "schema", schema),
        ))
    version = doc.get("schema_version", SCHEMA2_VERSION)
    if version != SCHEMA2_VERSION:
        diags.append(_diag(
            ASM_SYN_VERSION,
            f"schema_version 不受支持: {version!r}(期望 2.0.0)",
            location=_node_loc("structure", "schema_version", version),
        ))
    devices = doc.get("devices")
    connections = doc.get("connections")
    if not isinstance(devices, Mapping):
        diags.append(_diag(ASM_SYN_TYPE, "devices 必须是 mapping",
                           location=_node_loc("structure", "devices")))
    if not isinstance(connections, Mapping):
        diags.append(_diag(ASM_SYN_TYPE, "connections 必须是 mapping",
                           location=_node_loc("structure", "connections")))
    if not isinstance(devices, Mapping) or not isinstance(connections, Mapping):
        return

    for inst_id, inst_raw in devices.items():
        fld = f"devices.{inst_id}"
        if not isinstance(inst_id, str) or not _ID_PATTERN.fullmatch(inst_id):
            diags.append(_diag(ASM_SYN_TYPE, f"设备实例 ID 非法: {inst_id!r}",
                               location=_node_loc("structure", fld, inst_id)))
            continue
        if not isinstance(inst_raw, Mapping):
            diags.append(_diag(ASM_SYN_TYPE, f"{fld} 必须是 mapping",
                               location=_node_loc("structure", fld)))
            continue
        definition = inst_raw.get("definition")
        if not isinstance(definition, Mapping):
            diags.append(_diag(ASM_SYN_FIELD, f"{fld}.definition 缺失(必须固定精确设备内容)",
                               location=_node_loc("structure", f"{fld}.definition")))
        else:
            def_id = definition.get("id")
            def_sha = definition.get("content_sha256")
            if not isinstance(def_id, str) or not _ID_PATTERN.fullmatch(def_id):
                diags.append(_diag(ASM_SYN_TYPE, f"{fld}.definition.id 非法: {def_id!r}",
                                   location=_node_loc("structure", f"{fld}.definition.id", def_id)))
            if not isinstance(def_sha, str) or not _SHA256_PATTERN.fullmatch(def_sha):
                diags.append(_diag(
                    ASM_SYN_TYPE,
                    f"{fld}.definition.content_sha256 必须是小写 64 位十六进制",
                    location=_node_loc("structure", f"{fld}.definition.content_sha256", def_sha),
                ))
        origin = inst_raw.get("asset_origin", "existing")
        if origin not in _ASSET_ORIGINS:
            diags.append(_diag(ASM_SYN_TYPE,
                               f"{fld}.asset_origin 必须是 existing/new 之一: {origin!r}",
                               location=_node_loc("structure", f"{fld}.asset_origin", origin)))
        bindings_raw = inst_raw.get("predefined_interfaces", {})
        if not isinstance(bindings_raw, Mapping):
            diags.append(_diag(ASM_SYN_TYPE, f"{fld}.predefined_interfaces 必须是 mapping",
                               location=_node_loc("structure", f"{fld}.predefined_interfaces")))
        else:
            for iface_id, binding in bindings_raw.items():
                ifld = f"{fld}.predefined_interfaces.{iface_id}"
                if not isinstance(iface_id, str) or not _ID_PATTERN.fullmatch(iface_id):
                    diags.append(_diag(ASM_SYN_TYPE, f"{fld}.predefined_interfaces 键非法: {iface_id!r}",
                                       location=_node_loc("structure", ifld)))
                    continue
                if not isinstance(binding, Mapping):
                    diags.append(_diag(ASM_SYN_TYPE,
                                       f"{fld}.predefined_interfaces.{iface_id} 必须是 mapping",
                                       location=_node_loc("structure", ifld)))
                    continue
                data_ref = binding.get("data_ref")
                if not isinstance(data_ref, str) or not data_ref.strip():
                    diags.append(_diag(ASM_SYN_FIELD,
                                       f"{fld}.predefined_interfaces.{iface_id}.data_ref 缺失",
                                       location=_node_loc("structure", f"{ifld}.data_ref")))

    for conn_id, conn_raw in connections.items():
        fld = f"connections.{conn_id}"
        if not isinstance(conn_id, str) or not _ID_PATTERN.fullmatch(conn_id):
            diags.append(_diag(ASM_SYN_TYPE, f"connection ID 非法: {conn_id!r}",
                               location=_node_loc("structure", fld, conn_id)))
            continue
        if not isinstance(conn_raw, Mapping):
            diags.append(_diag(ASM_SYN_TYPE, f"{fld} 必须是 mapping",
                               location=_node_loc("structure", fld)))
            continue
        for side in ("from", "to"):
            ref = conn_raw.get(side)
            if not isinstance(ref, str) or _split_endpoint(ref) is None:
                diags.append(_diag(ASM_SYN_TYPE,
                                   f"{fld}.{side} 必须形如 '<instance>.<interface>': {ref!r}",
                                   location=_node_loc("structure", f"{fld}.{side}", ref)))


# ---------------------------------------------------------------------------
# 阶段 2:设备内容锁
# ---------------------------------------------------------------------------


def _check_device_locks(
    instances: Mapping[str, Any],
    documents: Mapping[str, DeviceModelDocument],
    diags: list[Diagnostic],
) -> None:
    """实例 definition 与提供的 descriptor 内容锁一致(精确设备内容固定)。"""
    for inst_id, inst_raw in instances.items():
        if not isinstance(inst_raw, Mapping):
            continue
        definition = inst_raw.get("definition")
        if not isinstance(definition, Mapping):
            continue
        def_id = definition.get("id")
        def_sha = definition.get("content_sha256")
        doc = documents.get(inst_id)
        if doc is None:
            diags.append(_diag(
                ASM_REF_MODEL_UNREG,
                f"实例 {inst_id!r} 缺少对应 2.0 descriptor(documents[{inst_id!r}])",
                location=_node_loc("lock", f"devices.{inst_id}.definition"),
            ))
            continue
        actual_device_id = doc.device.id if doc.device is not None else None
        if def_id != actual_device_id:
            diags.append(_diag(
                ASM_LOCK_MISMATCH,
                f"实例 {inst_id!r} definition.id {def_id!r} 与 descriptor 设备 ID "
                f"{actual_device_id!r} 不一致",
                location=_node_loc("lock", f"devices.{inst_id}.definition.id", def_id),
            ))
        actual_sha = content_sha256(doc)
        if def_sha is not None and actual_sha != def_sha:
            diags.append(_diag(
                ASM_LOCK_MISMATCH,
                f"实例 {inst_id!r} 内容锁不一致: definition.content_sha256 {def_sha!r} "
                f"!= descriptor 实际 {actual_sha!r}",
                location=_node_loc("lock", f"devices.{inst_id}.definition.content_sha256", def_sha),
            ))


# ---------------------------------------------------------------------------
# 阶段 3:连接(五类接口规则 / carrier / 量纲 / 值域)
# ---------------------------------------------------------------------------


def _check_connections(
    instances: Mapping[str, Any],
    connections: Mapping[str, Any],
    documents: Mapping[str, DeviceModelDocument],
    diags: list[Diagnostic],
) -> None:
    """逐边校验: 端点存在、类型规则、自环/重复、carrier、量纲、值域交集。"""
    seen_pairs: set[tuple[str, str]] = set()
    for conn_id, conn_raw in connections.items():
        fld = f"connections.{conn_id}"
        if not isinstance(conn_raw, Mapping):
            continue
        source_ref = conn_raw.get("from")
        target_ref = conn_raw.get("to")
        if not isinstance(source_ref, str) or not isinstance(target_ref, str):
            continue
        src = _split_endpoint(source_ref)
        tgt = _split_endpoint(target_ref)
        if src is None or tgt is None:
            continue  # 结构阶段已诊断
        src_inst, src_iface = src
        tgt_inst, tgt_iface = tgt

        src_doc = documents.get(src_inst)
        tgt_doc = documents.get(tgt_inst)
        if src_doc is None or tgt_doc is None:
            diags.append(_diag(
                ASM_REF_PORT_UNDEF,
                f"连接 {conn_id!r} 端点实例缺少 descriptor: {source_ref!r} / {target_ref!r}",
                location=_node_loc("edge", fld),
            ))
            continue
        s_iface = src_doc.interfaces.get(src_iface)
        t_iface = tgt_doc.interfaces.get(tgt_iface)
        if s_iface is None or t_iface is None:
            diags.append(_diag(
                ASM_REF_PORT_UNDEF,
                f"连接 {conn_id!r} 引用未定义的 interface: "
                f"{source_ref if s_iface is None else target_ref!r}",
                location=_node_loc("edge", fld),
            ))
            continue
        if s_iface.type not in SOURCE_TYPES:
            diags.append(_diag(
                ASM_EDGE_BAD_SOURCE,
                f"连接 {conn_id!r} 起点 {source_ref!r} 类型 {s_iface.type!r} 不允许作为源"
                f"(predefined/blind 从不出现在 connections)",
                location=_node_loc("edge", f"{fld}.from", source_ref),
            ))
        if t_iface.type not in SINK_TYPES:
            diags.append(_diag(
                ASM_EDGE_BAD_SINK,
                f"连接 {conn_id!r} 终点 {target_ref!r} 类型 {t_iface.type!r} 不允许作为汇"
                f"(predefined/blind 从不出现在 connections)",
                location=_node_loc("edge", f"{fld}.to", target_ref),
            ))
        if src_inst == tgt_inst and src_iface == tgt_iface:
            diags.append(_diag(
                ASM_EDGE_SELF_LOOP,
                f"连接 {conn_id!r} 构成自环: {source_ref!r}",
                location=_node_loc("edge", fld),
            ))
        pair = (source_ref, target_ref)
        if pair in seen_pairs:
            diags.append(_diag(
                ASM_EDGE_DUPLICATE,
                f"重复连接 {source_ref!r} → {target_ref!r}(同两端同方向)",
                location=_node_loc("edge", fld),
            ))
        seen_pairs.add(pair)
        if s_iface.carrier != t_iface.carrier:
            diags.append(_diag(
                ASM_EDGE_CARRIER,
                f"连接 {conn_id!r} 两端载体不一致: {s_iface.carrier!r} != {t_iface.carrier!r}",
                location=_node_loc("edge", fld),
            ))
        try:
            dims_ok = dims_of(s_iface.unit) == dims_of(t_iface.unit)
        except UnitError:
            dims_ok = False
        if not dims_ok:
            diags.append(_diag(
                ASM_EDGE_UNIT_DIM,
                f"连接 {conn_id!r} 两端单位量纲不可换算: {s_iface.unit!r} vs {t_iface.unit!r}",
                location=_node_loc("edge", fld),
            ))
        if not _ranges_intersect(
            s_iface.minimum, s_iface.maximum, t_iface.minimum, t_iface.maximum
        ):
            diags.append(_diag(
                ASM_EDGE_RANGE_CONFLICT,
                f"连接 {conn_id!r} 两端有效区间无交集: {source_ref!r} {s_iface.valid_range} "
                f"vs {target_ref!r} {t_iface.valid_range}",
                location=_node_loc("edge", fld),
            ))


# ---------------------------------------------------------------------------
# 阶段 4:预定义绑定(predefined 来源模式 / blind 禁止)
# ---------------------------------------------------------------------------


def _check_predefined_bindings(
    instances: Mapping[str, Any],
    documents: Mapping[str, DeviceModelDocument],
    diags: list[Diagnostic],
) -> None:
    """预定义接口绑定规则(宪法 §4.2/§7.8):

    - 绑定目标必须是 ``type: predefined`` 的接口(``blind`` 不接收预定义数据);
    - ``constant`` 来源直接来自设备内容,不允许实例绑定;
    - ``data_repeat/data_predict`` 必须为每个实例提供绑定(data_ref);
    - 任何非 predefined 接口(含 blind)禁止出现在 predefined_interfaces。
    """
    for inst_id, inst_raw in instances.items():
        if not isinstance(inst_raw, Mapping):
            continue
        doc = documents.get(inst_id)
        bindings_raw = inst_raw.get("predefined_interfaces", {})
        if not isinstance(bindings_raw, Mapping):
            continue
        if doc is None:
            continue  # 内容锁阶段已诊断
        bound: set[str] = set()
        for iface_id, binding in bindings_raw.items():
            if not isinstance(iface_id, str) or not isinstance(binding, Mapping):
                continue
            bound.add(iface_id)
            iface = doc.interfaces.get(iface_id)
            fld = f"devices.{inst_id}.predefined_interfaces.{iface_id}"
            if iface is None:
                diags.append(_diag(
                    ASM_REF_PORT_UNDEF,
                    f"实例 {inst_id!r} 绑定未定义的 interface: {iface_id!r}",
                    location=_node_loc("bind", fld),
                ))
                continue
            if iface.type != "predefined":
                diags.append(_diag(
                    ASM_BIND_INVALID,
                    f"实例 {inst_id!r} 绑定目标 {iface_id!r} 类型 {iface.type!r} 不允许"
                    f"(只有 type: predefined 可绑定预定义数据)",
                    location=_node_loc("bind", fld),
                ))
                continue
            mode = iface.source.mode if iface.source is not None else None
            if mode == "constant":
                diags.append(_diag(
                    ASM_BIND_INVALID,
                    f"实例 {inst_id!r} 的 constant 预定义接口 {iface_id!r} 不需要也不允许实例绑定"
                    f"(constant 直接来自设备内容)",
                    location=_node_loc("bind", fld),
                ))
                continue
            if mode not in PREDEFINED_SOURCE_MODES:
                diags.append(_diag(
                    ASM_BIND_INVALID,
                    f"实例 {inst_id!r} 的预定义接口 {iface_id!r} 来源模式非法: {mode!r}"
                    f"(只允许 {PREDEFINED_SOURCE_MODES})",
                    location=_node_loc("bind", fld),
                ))
                continue
            data_ref = binding.get("data_ref")
            if not isinstance(data_ref, str) or not data_ref.strip():
                diags.append(_diag(
                    ASM_BIND_INVALID,
                    f"实例 {inst_id!r} 的预定义接口 {iface_id!r}({mode})缺少 data_ref 绑定",
                    location=_node_loc("bind", f"{fld}.data_ref"),
                ))
        # 反向: data_repeat/data_predict 接口必须被绑定
        for iface_id, iface in doc.interfaces.items():
            mode = iface.source.mode if iface.source is not None else None
            if iface.type == "predefined" and mode in ("data_repeat", "data_predict"):
                if iface_id not in bound:
                    diags.append(_diag(
                        ASM_BIND_INVALID,
                        f"实例 {inst_id!r} 的预定义接口 {iface_id!r}({mode})缺少实例绑定",
                        location=_node_loc("bind", f"devices.{inst_id}.predefined_interfaces.{iface_id}"),
                    ))


# ---------------------------------------------------------------------------
# 规范文本
# ---------------------------------------------------------------------------


def _canonical_text(instances: Mapping[str, Any], connections: Mapping[str, Any]) -> str:
    """规范化网络文本: 稳定键序、紧凑 JSON、UTF-8(确定性)。"""
    devices_out: dict[str, Any] = {}
    for inst_id, inst_raw in sorted(instances.items()):
        if not isinstance(inst_raw, Mapping):
            continue
        bindings = inst_raw.get("predefined_interfaces")
        definition = inst_raw.get("definition")
        if not isinstance(definition, Mapping):
            definition = {}
        device_entry: dict[str, Any] = {
            "definition": {
                "id": definition.get("id", ""),
                "content_sha256": definition.get("content_sha256", ""),
            },
            "asset_origin": inst_raw.get("asset_origin", "existing"),
        }
        if isinstance(bindings, Mapping) and bindings:
            device_entry["predefined_interfaces"] = {
                iface_id: {"data_ref": binding.get("data_ref", "")}
                for iface_id, binding in sorted(bindings.items())
                if isinstance(binding, Mapping)
            }
        devices_out[inst_id] = device_entry
    connections_out: dict[str, Any] = {}
    for conn_id, conn_raw in sorted(connections.items()):
        if not isinstance(conn_raw, Mapping):
            continue
        connections_out[conn_id] = {
            "from": conn_raw.get("from", ""),
            "to": conn_raw.get("to", ""),
        }
    payload = {
        "schema": SCHEMA2_ID,
        "schema_version": SCHEMA2_VERSION,
        "devices": devices_out,
        "connections": connections_out,
    }
    return json.dumps(payload, **_CANONICAL_KWARGS)


__all__ = [
    "SCHEMA2_ID",
    "SCHEMA2_VERSION",
    "VALIDATOR2_ID",
    "VALIDATOR2_VERSION",
    "CANON2_ALGORITHM_ID",
    "CANON2_ALGORITHM_VERSION",
    "PREDEFINED_SOURCE_MODES",
    "NetworkReceipt",
    "ValidatedInterfaceNetwork",
    "InterfaceNetworkResult",
    "validate_interface_network2",
]
