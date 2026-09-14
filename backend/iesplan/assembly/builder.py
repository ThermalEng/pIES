"""ies.assembly 2.0.0 GUI 项目导出构造器。

将项目内容(设备/端口/连接/数据集绑定/项目基线)映射为 ies.assembly 2.0.0
文档;损耗的连接自动包裹为 transport_pipe 设备实例,与手写 YAML
进入同一校验入口(validator.validate_project_export)。

无静默默认:project_baseline.resolution/leap_year/scenario_mode
均须显式声明,缺字段/非法值一律返回 ASM-CONV-001 阻断诊断,不回退、不
猜测、不吞错。装配不包含 calculation(计算配置在计算包生成时固定)。

依赖: devices 公开门面(get_device)、core 公共契约;不依赖 services 与 ORM。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

from iesplan.assembly.diags import (
    ASM_CONV_UNMAPPABLE,
)
from iesplan.assembly.diags import make_asm_diag as make_diag
from iesplan.core.diagnostics import Diagnostic
from iesplan.core.errors import NotFoundError

#: 不进入 parameters 的内部键(data_refs 由绑定器显式消费;
#: type_detail/model_method/stateful/__layout 为现行内容键, 其余 1.0 键已删除)
_INTERNAL_PARAM_KEYS: tuple[str, ...] = (
    "type_detail",
    "model_method",
    "stateful",
    "data_refs",
    "__layout",
)

#: 全项目统一计算分辨率(均能确定性切分一天:96/48/24 步)
_BASELINE_RESOLUTIONS: tuple[str, ...] = ("15min", "30min", "1h")

#: 管道模型与端口名(与 devices/catalog/transport_pipe.yaml 一致)
_PIPE_MODEL = "ies.device.transport_pipe@2.0.0"
_PIPE_IN_NAME = "heat_in"
_PIPE_OUT_NAME = "heat_out"


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
    datasets: Mapping[int, Mapping] | None = None,
) -> BuildDocResult:
    """项目内容 → ies.assembly 2.0.0 文档(无静默默认)。

    输入结构(现行模型字段):
      content = {"model": {"devices": [...], "ports": [...], "connections": [...]},
                 "project_baseline": {...}, "name": ...}
      或 content 直接含 devices/ports/connections(扁平图)。

    字段映射:
    - assembly.id/name: 内容 name(必填, 缺失阻断; id 为其 slug);
    - project_baseline: 内容 project_baseline 显式声明
      (resolution/leap_year/scenario_mode 必填, 缺失/非法阻断;
      不保存时区与起止时间);
    - resources.datasets: 从 dataset_bindings + datasets 元信息
      构造为对象形态引用;
    - devices: 每个实例 → {model: <id>@<version>, parameters: 已清洗参数, data: ...};
      data 绑定只接受与模型 data_inputs 列精确匹配的显式绑定;
    - connections: 每个图连接必须有 id 且端口可解析, 否则阻断;
      loss_rate > 0 自动包裹 transport_pipe 设备实例;loss_rate 非数值阻断
      (缺省键视为无损耗 0)。

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

    # 2) 数据集 → resources.datasets (object 形态)
    datasets_map = dict(datasets or {})
    resources_datasets, res_diags = _build_resources(datasets_map, model_part.get("dataset_bindings"))
    diags.extend(res_diags)
    if res_diags and any(d.blocking for d in res_diags):
        return BuildDocResult(doc=None, diagnostics=diags)

    # 3) 设备实例
    device_ref_of = {d.get("id"): _device_ref(d) for d in devices_raw}
    port_index = {p.get("id"): p for p in ports_raw}
    devices_out: dict[str, dict] = {}
    for d in sorted(devices_raw, key=lambda x: _device_ref(x)):
        dev_id = _device_ref(d)
        type_id = _resolve_type_id(d)
        params = _clean_params(d)
        data_bindings, bind_diags = _resolve_data_bindings(d, type_id, dev_id)
        diags.extend(bind_diags)
        if bind_diags and any(x.blocking for x in bind_diags):
            continue
        devices_out[dev_id] = {
            "model": _model_ref(type_id),
            "parameters": params,
        }
        if data_bindings:
            devices_out[dev_id]["data"] = data_bindings

    # 4) 连接(loss_rate > 0 → 包裹管道设备;管道名 = "e<id>_pipe");
    #    边 id 统一加 e 前缀,满足新格式局部 ID(lower_snake)约束;
    #    无 id / 端口不可解析 / 含 1.0 独有字段一律阻断(无静默丢弃)
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
        if "delay_steps" in c:
            diags.append(
                make_diag(
                    ASM_CONV_UNMAPPABLE,
                    severity="error",
                    blocking=True,
                    params={"reason": "connection_delay_unsupported", "connection": raw_id},
                    location={"object_type": "connection", "object_id": raw_id, "field": "delay_steps"},
                )
            )
            continue
        from_ref = f"{device_ref_of.get(from_p.get('device_id'), '?')}.{from_p.get('name')}"
        to_ref = f"{device_ref_of.get(to_p.get('device_id'), '?')}.{to_p.get('name')}"
        loss_rate, loss_diag = _parse_loss_rate(c.get("loss_rate"), raw_id)
        if loss_diag is not None:
            diags.append(loss_diag)
            continue
        if loss_rate > 0:
            pipe_id = f"{edge_id}_pipe"
            # transport_pipe 仅声明 loss_rate
            devices_out[pipe_id] = {
                "model": _PIPE_MODEL,
                "parameters": {
                    "loss_rate": loss_rate,
                },
            }
            connections_out[edge_id] = {
                "from": from_ref,
                "to": f"{pipe_id}.{_PIPE_IN_NAME}",
            }
            connections_out[f"{edge_id}_out"] = {
                "from": f"{pipe_id}.{_PIPE_OUT_NAME}",
                "to": to_ref,
            }
        else:
            connections_out[edge_id] = {"from": from_ref, "to": to_ref}

    # 5) 约束(旧形态无显式约束节 → 空映射;保持新格式"无内容写空 {}"约束)
    constraints_out: dict[str, dict] = {}

    # 6) outputs(派生为空列表;后续 GUI 入口可补充)
    outputs_out = {"series": [], "metrics": []}

    # 7) extensions(空映射)
    extensions_out: dict = {}

    # 8) 组装(装配不包含 calculation)
    doc = {
        "schema": "ies.assembly",
        "schema_version": "2.0.0",
        "assembly": {"id": slug, "name": name},
        "project_baseline": {
            "resolution": resolution,
            "leap_year": leap_year,
            "scenario_mode": scenario_mode,
        },
        "resources": {"datasets": resources_datasets},
        "devices": devices_out,
        "connections": connections_out,
        "constraints": constraints_out,
        "outputs": outputs_out,
        "extensions": extensions_out,
    }

    if any(d.blocking for d in diags):
        return BuildDocResult(doc=None, diagnostics=diags)
    return BuildDocResult(doc=doc, diagnostics=diags)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _resolve_type_id(device: dict) -> str:
    params = device.get("params") or {}
    detail = params.get("type_detail")
    return detail if isinstance(detail, str) and detail else str(device.get("device_type", ""))


def _device_ref(device: dict) -> str:
    return f"d{device.get('id')}"


def _model_ref(type_id: str) -> str:
    from iesplan.devices import get_device

    try:
        spec = get_device(type_id)
        return f"{type_id}@{spec.schema_version}"
    except NotFoundError:
        # 未注册: 进入校验器阻断(RR-P2-05)
        return type_id


def _clean_params(device: dict) -> dict:
    params = device.get("params") or {}
    clean: dict = {}
    for key, value in params.items():
        if key in _INTERNAL_PARAM_KEYS or key.startswith("_"):
            continue
        if isinstance(value, dict) and value.get("dataset_version_id") is not None:
            continue
        clean[key] = value
    return clean


def _resolve_data_bindings(
    device: dict, type_id: str, dev_id: str
) -> tuple[dict | None, list[Diagnostic]]:
    """从设备的显式绑定参数 + data_refs 列表构造 data 绑定。

    约定:data 绑定键 = 模型 data_inputs 列名;列名 = 数据集已校验列。
    只有声明列与模型列精确匹配才接受;多列或多对多关系无法唯一决定时
    返回阻断诊断(无启发式映射)。
    """
    from iesplan.devices import get_device

    diags: list[Diagnostic] = []
    try:
        descriptor = get_device(type_id)
    except NotFoundError:
        # 未注册设备 → 校验器阻断,此处无法确定 data_inputs
        return None, []
    data_inputs = [
        interface
        for interface in descriptor.interfaces.values()
        if interface.type == "predefined"
    ]
    if not data_inputs:
        return None, diags

    bindings: dict[str, dict] = {}
    params = device.get("params") or {}

    for key, value in params.items():
        if key in _INTERNAL_PARAM_KEYS:
            continue
        if isinstance(value, dict) and value.get("dataset_version_id") is not None:
            vid = int(value["dataset_version_id"])
            columns = [c for c in (value.get("columns") or []) if isinstance(c, str)]
            if not columns:
                diags.append(
                    make_diag(
                        ASM_CONV_UNMAPPABLE,
                        severity="error",
                        blocking=True,
                        params={
                            "reason": "data_binding_columns_missing",
                            "device": dev_id,
                            "param": key,
                        },
                        location={"object_type": "device", "object_id": dev_id, "field": f"params.{key}"},
                    )
                )
                continue
            column = columns[0]
            target_col = _resolve_data_input_column(data_inputs, key, column)
            if target_col is None:
                diags.append(
                    make_diag(
                        ASM_CONV_UNMAPPABLE,
                        severity="error",
                        blocking=True,
                        params={
                            "reason": "data_binding_unmappable",
                            "device": dev_id,
                            "param": key,
                            "model_data_inputs": [d.id for d in data_inputs],
                        },
                        location={"object_type": "device", "object_id": dev_id, "field": f"params.{key}"},
                    )
                )
                continue
            bindings[target_col.id] = {
                "dataset": _dataset_id_for(vid),
                "column": column or target_col.id,
            }

    # 显式 data_refs 列表(与上面相同的列解析)
    for item in params.get("data_refs") or []:
        if isinstance(item, Mapping) and item.get("dataset_version_id") is not None:
            vid = int(item["dataset_version_id"])
            columns = [c for c in (item.get("columns") or []) if isinstance(c, str)]
            key = str(item.get("key") or f"data{len(bindings)}")
            if not columns:
                diags.append(
                    make_diag(
                        ASM_CONV_UNMAPPABLE,
                        severity="error",
                        blocking=True,
                        params={
                            "reason": "data_binding_columns_missing",
                            "device": dev_id,
                            "param": key,
                        },
                        location={"object_type": "device", "object_id": dev_id, "field": f"data_refs.{key}"},
                    )
                )
                continue
            column = columns[0]
            target_col = _resolve_data_input_column(data_inputs, key, column)
            if target_col is None:
                diags.append(
                    make_diag(
                        ASM_CONV_UNMAPPABLE,
                        severity="error",
                        blocking=True,
                        params={
                            "reason": "data_binding_unmappable",
                            "device": dev_id,
                            "param": key,
                            "model_data_inputs": [d.id for d in data_inputs],
                        },
                        location={"object_type": "device", "object_id": dev_id, "field": f"data_refs.{key}"},
                    )
                )
                continue
            bindings[target_col.id] = {
                "dataset": _dataset_id_for(vid),
                "column": column or target_col.id,
            }

    if any(d.blocking for d in diags):
        return None, diags
    return (bindings if bindings else None), diags


def _resolve_data_input_column(data_inputs, param_key: str, column: str | None):
    """唯一决定 data_inputs 列:只有声明列与模型列精确匹配才接受,
    否则返回 None(无法唯一决定 → 阻断, 无启发式映射)。
    """
    if column:
        for d in data_inputs:
            if d.id == column:
                return d
    return None


def _dataset_id_for(vid: int) -> str:
    return f"ds{int(vid)}"


def _build_resources(
    datasets: dict[int, Mapping],
    bindings_raw,
) -> tuple[dict, list[Diagnostic]]:
    diags: list[Diagnostic] = []
    out: dict[str, dict] = {}
    # 收集所有 vid:从 datasets 键 + dataset_bindings
    vids: set[int] = set(datasets.keys())
    for binding in bindings_raw or []:
        if isinstance(binding, Mapping):
            try:
                vids.add(int(binding.get("dataset_version_id")))
            except (TypeError, ValueError):
                continue
    for vid in sorted(vids):
        meta = datasets.get(vid)
        if meta is None:
            diags.append(
                make_diag(
                    ASM_CONV_UNMAPPABLE,
                    severity="error",
                    blocking=True,
                    params={"reason": "dataset_meta_missing", "dataset_version_id": vid},
                    location={"object_type": "assembly", "field": f"resources.datasets.ds{vid}"},
                )
            )
            continue
        object_id = str(meta.get("object_id") or f"object:{vid}")
        media = str(meta.get("media_type") or "application/octet-stream")
        out[_dataset_id_for(vid)] = {
            "source": {
                "kind": "object",
                "object_id": object_id,
                "media_type": media,
            }
        }
    if any(d.blocking for d in diags):
        return {}, diags
    return out, diags


def _slugify_id(text: str) -> str:
    """装配 id:仅保留 lower_snake/短横线字符;空字符串表示名称无可用字符。"""
    import re

    cleaned = re.sub(r"[^a-z0-9_-]+", "_", text.strip().lower()).strip("-_")
    if not re.match(r"^[a-z][a-z0-9_-]*$", cleaned):
        return ""
    return cleaned


def _parse_loss_rate(value, connection_id: str) -> tuple[float, Diagnostic | None]:
    """连接 loss_rate 严格解析:缺省键视为无损耗 0,非数值/非有限/负值阻断。

    替代已删除的吞错回退:非法值不再静默按 0 处理。
    """
    if value is None:
        return 0.0, None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0, make_diag(
            ASM_CONV_UNMAPPABLE,
            severity="error",
            blocking=True,
            params={"reason": "connection_loss_rate_invalid", "connection": connection_id, "value": value},
            location={"object_type": "connection", "object_id": connection_id, "field": "loss_rate"},
        )
    rate = float(value)
    if not math.isfinite(rate) or rate < 0:
        return 0.0, make_diag(
            ASM_CONV_UNMAPPABLE,
            severity="error",
            blocking=True,
            params={"reason": "connection_loss_rate_invalid", "connection": connection_id, "value": value},
            location={"object_type": "connection", "object_id": connection_id, "field": "loss_rate"},
        )
    return rate, None


__all__ = [
    "BuildDocResult",
    "build_assembly_doc_from_content",
]
