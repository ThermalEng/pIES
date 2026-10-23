"""ies.assembly 3.0.0 GUI 项目导出构造器。

将项目内容(设备/端口/连接/数据集绑定/项目基线)映射为 ies.assembly 3.0.0
纯技术实例形态(definition/asset_origin/properties/predefined_interfaces),
与手写 YAML 进入同一校验入口(validator.validate_project_export)。

连接只映射显式 from/to 端点引用；损耗由用户在项目图中显式实例化的
普通设备方程或约束表达，本构造器不按设备类型分支、不合成设备、
不猜测经济绑定。

无静默默认:project_baseline.resolution/start_year/scenario_mode、
设备 kind 均须显式声明,缺字段/非法值一律返回
ASM-CONV-001 阻断诊断,不回退、不猜测、不吞错。装配不包含 calculation
(计算配置在计算包生成时固定)。设备数据绑定(CSV 序列来源)由设备
声明 params 中预定义接口的字典形态提供({mode, dataset_version_id,
按模式可选 target_type},路径按版本合成),本构造器不做数据集列映射。

依赖: devices 公开门面(get_device)、core 公共契约;不依赖 services 与 ORM。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from iesplan.assembly.contracts import SCHEMA_VERSION
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
    demand_bindings: Mapping[str, Any] | None = None,
    planning: Mapping[str, Any] | None = None,
    device_documents: Mapping[str, Any] | None = None,
) -> BuildDocResult:
    """项目内容 → ies.assembly 3.0.0 文档(无静默默认)。

    输入结构(2.0 内容字段):
      content = {"model": {"devices": [...], "connections": [...]},
                 "project_baseline": {...}, "name": ...}
      设备只收名键条目 {name, device_type, kind, params};连接为名键条目
      {name, from_device, from_port, to_device, to_port}, 端口名即真实
      interface ID。无 model 节、无 devices 表即阻断，不读扁平内容。

    字段映射:
    - assembly.id/name: 内容 name(必填, 缺失阻断; id 为其 slug);
    - project_baseline: 内容 project_baseline 显式声明
      (resolution/start_year/scenario_mode 必填, 缺失/非法阻断;
      不保存时区与起止时间);
    - devices: 每个实例 → {definition: {id: 裸稳定 ID},
      asset_origin: 内容 kind(existing/new, 缺失/非法阻断),
      properties: {name: {value, unit(以注册 descriptor 单位为准)}},
      predefined_interfaces: {iface: 来源声明}};
      设备类型只消费 device_type 本身(必须就是裸稳定 ID, 无 definition.id
      与 params.type_detail 回退);标量参数若命中 predefined 接口名则为
      constant 绑定，命中 property 名则为属性覆盖，其余原样透传由校验器
      判未声明(下划线键与 dict 值不静默丢弃, 同样透传待判);数据序列来源
      由独立声明入口提供，本构造器不做数据集列映射;
    - connections: 2.0 名键形态, 每个连接必须有 name 且 from_device/
      from_port/to_device/to_port 显式可解析(端口名即真实 interface ID),
      否则阻断; 只映射显式端点引用，不解释、不合成、不补绑定。
    - finance/finance_binding/tariff_bindings/planning: 调用方显式提供,
      原样透传进文档(缺席则结构阶段按节缺失阻断)。
    - device_documents: 调用方显式提供的项目设备文档(已保存模型),
      与公开目录合并消费且同 ID 以显式文档为准; 本构造器不访问 DB/storage。

    返回 (doc, diagnostics)。任一阻断错误 → doc=None(不进入校验器)。
    """
    diags: list[Diagnostic] = []
    model_part = content.get("model")
    devices_raw = (model_part.get("devices") or []) if isinstance(model_part, Mapping) else []
    conns_raw = (model_part.get("connections") or []) if isinstance(model_part, Mapping) else []

    # 装配名必须由调用方显式提供（旧 graph_id 回退已删除，无静默命名）。
    name_raw = content.get("name")
    if isinstance(name_raw, str) and name_raw:
        name = name_raw
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

    # 1) 项目计算基线(resolution/start_year/scenario_mode 必须显式声明,
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
    start_year_raw = baseline_raw.get("start_year")
    if not isinstance(start_year_raw, int) or isinstance(start_year_raw, bool):
        diags.append(
            make_diag(
                ASM_CONV_UNMAPPABLE,
                severity="error",
                blocking=True,
                params={"reason": "project_baseline_start_year_invalid", "value": start_year_raw},
                location={"object_type": "assembly", "field": "project_baseline.start_year"},
            )
        )
        return BuildDocResult(doc=None, diagnostics=diags)
    start_year = start_year_raw
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
    name_ref_of: dict[str, str] = {}
    name_type_of: dict[str, str] = {}
    for d, ref in zip(devices_raw, refs):
        if not isinstance(d, Mapping):
            continue
        name = d.get("name")
        if isinstance(name, str) and name and name not in name_ref_of:
            name_ref_of[name] = ref
            name_type_of[name] = _resolve_type_id(d)
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
        instance, inst_diags = _build_instance(
            d, type_id, dev_id, kind_raw, device_documents
        )
        diags.extend(inst_diags)
        if inst_diags and any(x.blocking for x in inst_diags):
            continue
        devices_out[dev_id] = instance

    # 4) 连接(2.0 名键形态: from_device/from_port/to_device/to_port 显式端点,
    #    端口名即设备描述的真实 interface ID);
    #    边 id 由连接 name 确定性派生(slug),满足新格式局部 ID(lower_snake)约束;
    #    端点缺失 / 设备或接口不可解析 / 边 id 重复一律阻断(无静默丢弃)
    connections_out: dict[str, dict] = {}
    for c in sorted(conns_raw, key=lambda x: str(x.get("name") or "")):
        raw_name = c.get("name") if isinstance(c, Mapping) else None
        edge_id = _slugify_id(raw_name) if isinstance(raw_name, str) else ""
        if not edge_id:
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
        ends = (
            c.get("from_device"),
            c.get("from_port"),
            c.get("to_device"),
            c.get("to_port"),
        )
        if not all(isinstance(e, str) and e for e in ends):
            diags.append(
                make_diag(
                    ASM_CONV_UNMAPPABLE,
                    severity="error",
                    blocking=True,
                    params={"reason": "connection_endpoint_missing", "connection": raw_name},
                    location={"object_type": "connection", "object_id": edge_id},
                )
            )
            continue
        from_dev, from_port, to_dev, to_port = ends
        from_ref_base = name_ref_of.get(from_dev)
        to_ref_base = name_ref_of.get(to_dev)
        if from_ref_base is None or to_ref_base is None:
            diags.append(
                make_diag(
                    ASM_CONV_UNMAPPABLE,
                    severity="error",
                    blocking=True,
                    params={
                        "reason": "connection_device_unresolved",
                        "connection": raw_name,
                        "from_device": from_dev,
                        "to_device": to_dev,
                    },
                    location={"object_type": "connection", "object_id": edge_id},
                )
            )
            continue
        bad: tuple[str, str, str] | None = None
        for side, dev, port in (("from", from_dev, from_port), ("to", to_dev, to_port)):
            type_id = name_type_of[dev]
            descriptor = _descriptor_of(type_id, device_documents) if type_id else None
            if descriptor is not None and descriptor.interfaces.get(port) is None:
                bad = (side, dev, port)
                break
        if bad is not None:
            side, dev, port = bad
            diags.append(
                make_diag(
                    ASM_CONV_UNMAPPABLE,
                    severity="error",
                    blocking=True,
                    params={
                        "reason": "connection_port_unresolved",
                        "connection": raw_name,
                        "side": side,
                        "device": dev,
                        "port": port,
                    },
                    location={"object_type": "connection", "object_id": edge_id},
                )
            )
            continue
        if edge_id in connections_out:
            diags.append(
                make_diag(
                    ASM_CONV_UNMAPPABLE,
                    severity="error",
                    blocking=True,
                    params={"reason": "duplicate_connection", "connection": raw_name},
                    location={"object_type": "connection", "object_id": edge_id},
                )
            )
            continue
        connections_out[edge_id] = {
            "from": f"{from_ref_base}.{from_port}",
            "to": f"{to_ref_base}.{to_port}",
        }

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
        "schema_version": SCHEMA_VERSION,
        "assembly": {"id": slug, "name": name},
        "project_baseline": {
            "resolution": resolution,
            "start_year": start_year,
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
    if demand_bindings is not None:
        doc["demand_bindings"] = dict(demand_bindings)
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
    """2.0 设备类型即 device_type 本身(必须为裸稳定 ID)。

    无 definition.id 与 params.type_detail 回退；未知值原样返回，
    由校验器判未注册。
    """
    return str(device.get("device_type", ""))


def _device_refs(devices: list) -> list[str]:
    """实例引用(与内容一一对应，确定性、唯一、合法本地 id)。

    名键设备用名称 slug；名称无可用字符时按排序位置回退 ``dx{n}``。
    名称是 upsert 唯一键，slug 对同一内容稳定；回退仅影响无法 slug
    化的名称。不读数字 id。重复引用由调用方阻断，不静默丢弃、不合并。
    """
    refs: list[str] = []
    for position, device in enumerate(devices):
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


def _descriptor_of(type_id: str, device_documents: Mapping[str, Any] | None = None):
    if device_documents is not None:
        explicit = device_documents.get(type_id)
        if explicit is not None:
            return explicit
    from iesplan.devices import get_device

    try:
        return get_device(type_id)
    except NotFoundError:
        return None


def _descriptor_unit(
    type_id: str, prop: str, device_documents: Mapping[str, Any] | None = None
) -> str:
    descriptor = _descriptor_of(type_id, device_documents)
    if descriptor is None:
        return "1"
    spec = descriptor.properties.get(prop)
    return spec.unit if spec is not None and spec.unit else "1"


#: 数据序列来源声明允许的形态(键集合按来源模式精确匹配)。
_SOURCE_DECL_KEYS: dict[str, frozenset] = {
    "data_repeat": frozenset({"mode", "dataset_version_id"}),
    "data_full": frozenset({"mode", "dataset_version_id"}),
    "data_predict": frozenset({"mode", "dataset_version_id", "target_type"}),
}


def parse_predefined_source_declaration(value: Mapping) -> tuple[dict | None, str | None]:
    """来源声明纯解析: 映射 → (绑定声明, None), 形态不符 → (None, reason)。

    接受 ``{"mode": "data_repeat", "dataset_version_id": int}``、
    ``{"mode": "data_full", "dataset_version_id": int}`` 或
    ``{"mode": "data_predict", "dataset_version_id": int, "target_type": str}``；
    路径由构造器按版本合成(``dataset_export_path``)，声明方不拼路径。
    reason ∈ unknown_source_mode / source_declaration_keys /
    source_dataset_version / source_target_type; 纯函数不抛异常、不改写输入。
    构造器转换与预检准入共用本解析(单一权威), 不重复实现形态规则。
    """
    mode = value.get("mode")
    if mode not in ("data_repeat", "data_full", "data_predict"):
        return None, "unknown_source_mode"
    if set(value) != _SOURCE_DECL_KEYS[mode]:
        return None, "source_declaration_keys"
    vid = value.get("dataset_version_id")
    if not isinstance(vid, int) or isinstance(vid, bool):
        return None, "source_dataset_version"
    declared: dict = {"mode": mode, "path": dataset_export_path(vid)}
    if mode == "data_predict":
        target_type = value.get("target_type")
        if not isinstance(target_type, str) or not target_type:
            return None, "source_target_type"
        declared["target_type"] = target_type
    return declared, None


def _predefined_source_declaration(
    dev_id: str, iface_id: str, value: Mapping, diags: list
) -> dict | None:
    """预定义接口数据序列来源声明(params 字典形态 → 绑定声明)。

    解析委托公开纯函数 ``parse_predefined_source_declaration``(预检准入
    同集合); 形态不符一律转换阻断(ASM-CONV-001)，不猜测、不补默认。
    标量 constant 形态不受影响。
    """
    declared, reason = parse_predefined_source_declaration(value)
    if reason is None:
        return declared
    field = f"predefined_interfaces.{iface_id}"
    diags.append(_conv_fail(dev_id, field, reason, {"mode": value.get("mode")}))
    return None


def _build_instance(
    device: dict,
    type_id: str,
    dev_id: str,
    asset_origin: str,
    device_documents: Mapping[str, Any] | None = None,
) -> tuple[dict | None, list[Diagnostic]]:
    """项目设备 → 2.0 实例形态(属性覆盖 + 预定义来源声明)。

    命中 predefined 接口名的标量参数落为 constant；命中 property 名的
    标量落为属性覆盖(单位取 descriptor)；其余标量透传由校验器判未声明。
    数据序列来源由独立声明入口提供(S2c)，此处不做数据集列映射。
    未注册设备仅发射 definition，由校验器阻断。显式项目设备文档优先于目录。
    """
    diags: list[Diagnostic] = []
    descriptor = _descriptor_of(type_id, device_documents)
    predefined_ids = (
        {name for name, iface in descriptor.interfaces.items() if iface.type == "predefined"}
        if descriptor is not None
        else set()
    )
    property_ids = set(descriptor.properties) if descriptor is not None else set()

    properties_out: dict[str, dict] = {}
    predefined_out: dict[str, dict] = {}
    params = device.get("params") or {}
    # 下划线键与 dict 值不静默丢弃: 原样透传，由校验器判未声明。
    for key, value in params.items():
        if key in predefined_ids and isinstance(value, (int, float)) and not isinstance(value, bool):
            predefined_out[key] = {"mode": "constant", "value": value}
        elif key in predefined_ids and isinstance(value, Mapping):
            declared = _predefined_source_declaration(dev_id, key, value, diags)
            if declared is not None:
                predefined_out[key] = declared
        elif key in property_ids:
            properties_out[key] = {
                "value": value,
                "unit": _descriptor_unit(type_id, key, device_documents),
            }
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
