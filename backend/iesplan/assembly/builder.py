"""ies.assembly 2.0.0 GUI 项目导出构造器。

将项目内容(设备/端口/连接/数据集绑定/项目基线)映射为 ies.assembly 2.0.0
纯技术实例形态(definition/asset_origin/properties/predefined_interfaces),
与手写 YAML 进入同一校验入口(validator.validate_project_export)。

连接只映射显式 from/to 端点引用；损耗由用户在项目图中显式实例化的
普通设备方程或约束表达，本构造器不按设备类型分支、不合成设备、
不猜测经济绑定。

无静默默认:project_baseline.resolution/leap_year/scenario_mode、
设备 kind 均须显式声明,缺字段/非法值一律返回
ASM-CONV-001 阻断诊断,不回退、不猜测、不吞错。装配不包含 calculation
(计算配置在计算包生成时固定)。设备数据绑定(CSV 序列来源)由独立
声明入口提供(S2c),本构造器只处理标量参数,不做数据集列映射。

依赖: devices 公开门面(get_device)、core 公共契约;不依赖 services 与 ORM。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from iesplan.assembly.diags import (
    ASM_CONV_UNMAPPABLE,
)
from iesplan.assembly.diags import make_asm_diag as make_diag
from iesplan.core.diagnostics import Diagnostic
from iesplan.core.errors import NotFoundError

#: 全项目统一计算分辨率(均能确定性切分一天:96/48/24 步)
_BASELINE_RESOLUTIONS: tuple[str, ...] = ("15min", "30min", "1h")

#: 导出合成路径(数据集版本 → 包内相对 CSV；字节由调用方显式注入)
_EXPORT_DATA_PATH = "data/ds{vid}.data.csv"


def dataset_export_path(vid: int) -> str:
    """数据集版本 → 导出合成路径(调用方按此路径注入对象字节)。"""
    return _EXPORT_DATA_PATH.format(vid=int(vid))


@dataclass(slots=True)
class BuildDocResult:
    doc: dict | None
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.doc is not None and not any(d.blocking for d in self.diagnostics)


def build_assembly_doc_from_content(
    content: Mapping,
    *,
    finance: Mapping[str, Any] | None = None,
    finance_binding: Mapping[str, Any] | None = None,
    tariff_bindings: Mapping[str, Any] | None = None,
    planning: Mapping[str, Any] | None = None,
) -> BuildDocResult:
    """项目内容 → ies.assembly 2.0.0 文档(无静默默认)。

    输入结构(2.0 内容字段):
      content = {"model": {"devices": [...], "ports": [...], "connections": [...]},
                 "project_baseline": {...}, "name": ...}
      或 content 直接含 devices/ports/connections(扁平图)。

    字段映射:
    - assembly.id/name: 内容 name(必填, 缺失阻断; id 为其 slug);
    - project_baseline: 内容 project_baseline 显式声明
      (resolution/leap_year/scenario_mode 必填, 缺失/非法阻断;
      不保存时区与起止时间);
    - devices: 每个实例 → {definition: {id: 裸稳定 ID},
      asset_origin: 内容 kind(existing/new, 缺失/非法阻断),
      properties: {name: {value, unit(以注册 descriptor 单位为准)}},
      predefined_interfaces: {iface: 来源声明}};
      设备类型只消费明确 2.0 输入: definition.id 优先, 否则 device_type
      必须本身就是裸稳定 ID(无 params.type_detail 回退);标量参数若命中
      predefined 接口名则为 constant 绑定，命中 property 名则为属性覆盖，
      其余原样透传由校验器判未声明；数据序列来源由独立声明入口提供，
      本构造器不做数据集列映射；
    - connections: 每个图连接必须有 id 且端口可解析, 否则阻断;
      只映射显式 from/to 端点引用，不解释、不合成、不补绑定。
    - finance/finance_binding/tariff_bindings/planning: 调用方显式提供,
      原样透传进文档(缺席则结构阶段按节缺失阻断)。

    返回 (doc, diagnostics)。任一阻断错误 → doc=None(不进入校验器)。
    """
    diags: list[Diagnostic] = []
    model_part = content.get("model") if isinstance(content.get("model"), Mapping) else content
    devices_raw = (model_part.get("devices") or []) if isinstance(model_part, Mapping) else []
    ports_raw = (model_part.get("ports") or []) if isinstance(model_part, Mapping) else []
    conns_raw = (model_part.get("connections") or []) if isinstance(model_part, Mapping) else []

    graph_id = content.get("graph_id")
    name_raw = content.get("name")
    if isinstance(name_raw, str) and name_raw:
        name = name_raw
    elif graph_id:
        name = f"graph_{graph_id}"
    else:
        diags.append(
            make_diag(
                ASM_CONV_UNMAPPABLE,
                severity="error",
                blocking=True,
                params={"reason": "assembly_name_missing"},
                location={"object_type": "assembly", "field": "name"},
            )
        )
        return BuildDocResult(doc=None, diagnostics=diags)
    slug = _slugify_id(name)
    if not slug:
        diags.append(
            make_diag(
                ASM_CONV_UNMAPPABLE,
                severity="error",
                blocking=True,
                params={"reason": "assembly_id_empty", "value": name},
                location={"object_type": "assembly", "field": "name"},
            )
        )
        return BuildDocResult(doc=None, diagnostics=diags)

    # 1) 项目计算基线(resolution/leap_year/scenario_mode 必须显式声明,
    #    无静默默认;不保存时区与起止时间)
    baseline_raw = content.get("project_baseline")
    if not isinstance(baseline_raw, Mapping):
        diags.append(
            make_diag(
                ASM_CONV_UNMAPPABLE,
                severity="error",
                blocking=True,
                params={"reason": "project_baseline_missing"},
                location={"object_type": "assembly", "field": "project_baseline"},
            )
        )
        return BuildDocResult(doc=None, diagnostics=diags)
    resolution_raw = baseline_raw.get("resolution")
    if not isinstance(resolution_raw, str) or not resolution_raw:
        diags.append(
            make_diag(
                ASM_CONV_UNMAPPABLE,
                severity="error",
                blocking=True,
                params={"reason": "project_baseline_resolution_missing", "value": resolution_raw},
                location={"object_type": "assembly", "field": "project_baseline.resolution"},
            )
        )
        return BuildDocResult(doc=None, diagnostics=diags)
    resolution = resolution_raw
    if resolution not in _BASELINE_RESOLUTIONS:
        diags.append(
            make_diag(
                ASM_CONV_UNMAPPABLE,
                severity="error",
                blocking=True,
                params={"reason": "project_baseline_resolution_unknown", "value": resolution},
                location={"object_type": "assembly", "field": "project_baseline.resolution"},
            )
        )
        return BuildDocResult(doc=None, diagnostics=diags)
    leap_year_raw = baseline_raw.get("leap_year")
    if not isinstance(leap_year_raw, bool):
        diags.append(
            make_diag(
                ASM_CONV_UNMAPPABLE,
                severity="error",
                blocking=True,
                params={"reason": "project_baseline_leap_year_invalid", "value": leap_year_raw},
                location={"object_type": "assembly", "field": "project_baseline.leap_year"},
            )
        )
        return BuildDocResult(doc=None, diagnostics=diags)
    leap_year = leap_year_raw
    scenario_raw = baseline_raw.get("scenario_mode")
    if scenario_raw != "single":
        diags.append(
            make_diag(
                ASM_CONV_UNMAPPABLE,
                severity="error",
                blocking=True,
                params={"reason": "project_baseline_scenario_mode_invalid", "value": scenario_raw},
                location={"object_type": "assembly", "field": "project_baseline.scenario_mode"},
            )
        )
        return BuildDocResult(doc=None, diagnostics=diags)
    scenario_mode = scenario_raw

    # 2) 设备实例(2.0 纯技术形态)
    refs = _device_refs(devices_raw)
    device_ref_of = {d.get("id"): ref for d, ref in zip(devices_raw, refs)}
    port_index = {p.get("id"): p for p in ports_raw}
    devices_out: dict[str, dict] = {}
    seen_refs: set[str] = set()
    for d, dev_id in sorted(zip(devices_raw, refs), key=lambda t: t[1]):
        if dev_id in seen_refs:
            diags.append(
                _conv_fail(
                    dev_id,
                    "id",
                    "duplicate_device_ref",
                    {"name": d.get("name")},
                )
            )
            continue
        seen_refs.add(dev_id)
        type_id = _resolve_type_id(d)
        kind_raw = d.get("kind")
        if kind_raw not in ("existing", "new"):
            diags.append(
                make_diag(
                    ASM_CONV_UNMAPPABLE,
                    severity="error",
                    blocking=True,
                    params={"reason": "device_kind_missing", "device": dev_id, "value": kind_raw},
                    location={"object_type": "device", "object_id": dev_id, "field": "kind"},
                )
            )
            continue
        instance, inst_diags = _build_instance(d, type_id, dev_id, kind_raw)
        diags.extend(inst_diags)
        if inst_diags and any(x.blocking for x in inst_diags):
            continue
        devices_out[dev_id] = instance

    # 4) 连接(只映射显式 from/to 端点引用);
    #    边 id 统一加 e 前缀,满足新格式局部 ID(lower_snake)约束;
    #    无 id / 端口不可解析一律阻断(无静默丢弃)
    connections_out: dict[str, dict] = {}
    for c in sorted(conns_raw, key=lambda x: str(x.get("id") or "")):
        raw_id = str(c.get("id") or "")
        if not raw_id:
            diags.append(
                make_diag(
                    ASM_CONV_UNMAPPABLE,
                    severity="error",
                    blocking=True,
                    params={"reason": "connection_id_missing"},
                    location={"object_type": "connection"},
                )
            )
            continue
        edge_id = f"e{raw_id}"
        from_p = port_index.get(c.get("from_port_id"))
        to_p = port_index.get(c.get("to_port_id"))
        if from_p is None or to_p is None:
            diags.append(
                make_diag(
                    ASM_CONV_UNMAPPABLE,
                    severity="error",
                    blocking=True,
                    params={"reason": "connection_port_unresolved", "connection": raw_id},
                    location={"object_type": "connection", "object_id": raw_id},
                )
            )
            continue
        from_ref = f"{device_ref_of.get(from_p.get('device_id'), '?')}.{from_p.get('name')}"
        to_ref = f"{device_ref_of.get(to_p.get('device_id'), '?')}.{to_p.get('name')}"
        connections_out[edge_id] = {"from": from_ref, "to": to_ref}

    # 5) outputs(派生为空列表;后续 GUI 入口可补充)
    outputs_out = {"series": [], "metrics": []}

    # 6) extensions(空映射)
    extensions_out: dict = {}

    # 7) finance_binding 由调用方显式提供，原样透传，不增补、不猜测
    merged_binding: dict | None = None
    if finance_binding is not None:
        merged_binding = dict(finance_binding)

    # 8) 组装(装配不包含 calculation；resources 注册表已删除，来源直挂设备；
    # finance/finance_binding/tariff_bindings/planning 由调用方显式提供，
    # 不猜测；缺席则结构阶段阻断)
    doc = {
        "schema": "ies.assembly",
        "schema_version": "2.0.0",
        "assembly": {"id": slug, "name": name},
        "project_baseline": {
            "resolution": resolution,
            "leap_year": leap_year,
            "scenario_mode": scenario_mode,
        },
        "devices": devices_out,
        "connections": connections_out,
    }
    if finance is not None:
        doc["finance"] = dict(finance)
    if merged_binding is not None:
        doc["finance_binding"] = merged_binding
    if tariff_bindings is not None:
        doc["tariff_bindings"] = dict(tariff_bindings)
    if planning is not None:
        doc["planning"] = dict(planning)
    doc.update(
        {
            "outputs": outputs_out,
            "extensions": extensions_out,
        }
    )

    if any(d.blocking for d in diags):
        return BuildDocResult(doc=None, diagnostics=diags)
    return BuildDocResult(doc=doc, diagnostics=diags)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _resolve_type_id(device: dict) -> str:
    """2.0 设备类型: definition.id 优先, 否则 device_type 本身必须为裸稳定 ID。

    无 params.type_detail 回退；未知值原样返回，由校验器判未注册。
    """
    definition = device.get("definition")
    if isinstance(definition, Mapping):
        raw = definition.get("id")
        if isinstance(raw, str) and raw:
            return raw
    return str(device.get("device_type", ""))


def _device_refs(devices: list) -> list[str]:
    """实例引用(与内容一一对应，确定性、唯一、合法本地 id)。

    数字 id → ``d{id}``；冻结前名键设备(id 缺席)用名称 slug；名称无
    可用字符时按排序位置回退 ``dx{n}``。名称是 upsert 唯一键，slug 对
    同一内容稳定；回退仅影响无法 slug 化的名称。重复引用由调用方阻断，
    不静默丢弃、不合并。
    """
    refs: list[str] = []
    for position, device in enumerate(devices):
        raw = device.get("id") if isinstance(device, dict) else None
        if raw is not None:
            refs.append(f"d{raw}")
            continue
        name = device.get("name") if isinstance(device, dict) else None
        slug = _slugify_id(name) if isinstance(name, str) else ""
        refs.append(slug if slug else f"dx{position}")
    return refs


def _conv_fail(dev_id: str, field: str, reason: str, extra: dict | None = None) -> Diagnostic:
    params: dict = {"reason": reason, "device": dev_id}
    if extra:
        params.update(extra)
    return make_diag(
        ASM_CONV_UNMAPPABLE,
        severity="error",
        blocking=True,
        params=params,
        location={"object_type": "device", "object_id": dev_id, "field": field},
    )


def _descriptor_of(type_id: str):
    from iesplan.devices import get_device

    try:
        return get_device(type_id)
    except NotFoundError:
        return None


def _descriptor_unit(type_id: str, prop: str) -> str:
    descriptor = _descriptor_of(type_id)
    if descriptor is None:
        return "1"
    spec = descriptor.properties.get(prop)
    return spec.unit if spec is not None and spec.unit else "1"


def _build_instance(
    device: dict,
    type_id: str,
    dev_id: str,
    asset_origin: str,
) -> tuple[dict | None, list[Diagnostic]]:
    """项目设备 → 2.0 实例形态(属性覆盖 + 预定义来源声明)。

    命中 predefined 接口名的标量参数落为 constant；命中 property 名的
    标量落为属性覆盖(单位取 descriptor)；其余标量透传由校验器判未声明。
    数据序列来源由独立声明入口提供(S2c)，此处不做数据集列映射。
    未注册设备仅发射 definition，由校验器阻断。
    """
    diags: list[Diagnostic] = []
    descriptor = _descriptor_of(type_id)
    predefined_ids = (
        {name for name, iface in descriptor.interfaces.items() if iface.type == "predefined"}
        if descriptor is not None
        else set()
    )
    property_ids = set(descriptor.properties) if descriptor is not None else set()

    properties_out: dict[str, dict] = {}
    predefined_out: dict[str, dict] = {}
    params = device.get("params") or {}
    for key, value in params.items():
        if key.startswith("_"):
            continue
        if isinstance(value, dict):
            continue
        if key in predefined_ids and isinstance(value, (int, float)) and not isinstance(value, bool):
            predefined_out[key] = {"mode": "constant", "value": value}
        elif key in property_ids:
            properties_out[key] = {"value": value, "unit": _descriptor_unit(type_id, key)}
        else:
            properties_out[key] = {"value": value, "unit": "1"}

    if any(d.blocking for d in diags):
        return None, diags
    return (
        {
            "definition": {"id": type_id},
            "asset_origin": asset_origin,
            "properties": properties_out,
            "predefined_interfaces": predefined_out,
        },
        diags,
    )


def _slugify_id(text: str) -> str:
    """装配 id:仅保留 lower_snake/短横线字符;空字符串表示名称无可用字符。"""
    import re

    cleaned = re.sub(r"[^a-z0-9_-]+", "_", text.strip().lower()).strip("-_")
    if not re.match(r"^[a-z][a-z0-9_-]*$", cleaned):
        return ""
    return cleaned


__all__ = [
    "BuildDocResult",
    "build_assembly_doc_from_content",
]
