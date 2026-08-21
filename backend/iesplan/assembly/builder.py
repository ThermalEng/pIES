"""项目图(DB 序列化结构)→ AssemblySpec → 规范装配文本。

确定性要求:同一图内容 + 同一 calc_config/数据集元信息,输出文本逐字节一致
(键序固定、浮点格式固定、list 按 id 排序),从而装配文本可纳入内容哈希。

迁移约定(04 §1.2/§5.4):Connection.loss_rate > 0 的连接生成时自动包裹为管道设备
(生成 <edge_id>_pipe 管道 + 两条瞬时边:原 from_port→管道 in、管道 out→原 to_port),
保证"边=严格相等"语义成立;loss_rate == 0 的连接直接映射为瞬时边。
"""

from __future__ import annotations

import math

from iesplan.core.errors import NotFoundError
from typing import Any

from iesplan.assembly.checker import PORT_TYPE_TO_CARRIER
from iesplan.assembly.schema import (
    CARRIER_DEFAULT_QUANTITY_UNIT,
    FORMAT_VERSION,
    MODEL_METHOD_MECHANISM,
    NATURE_INSTANT,
    AssemblyDevice,
    AssemblyEdge,
    AssemblyPipeline,
    AssemblyPort,
    AssemblySpec,
    CalcRequirements,
    DataRef,
    TimeAxisRef,
)
from iesplan.devices import get_device_descriptor as get_device_type

#: 内部保留参数键(不进入装配文本 params 章节)
_INTERNAL_PARAM_KEYS: tuple[str, ...] = (
    "type_detail",
    "model_method",
    "stateful",
    "ref_id",
    "data_refs",
    "__layout",
    "_assembly_source",
)


def _resolve_type_id(device: dict) -> str:
    """设备行 → 完整注册表类型 id(params.type_detail 优先,回退 device_type)。"""
    params = device.get("params") or {}
    detail = params.get("type_detail")
    return detail if isinstance(detail, str) and detail else str(device.get("device_type", ""))


def _device_ref(device: dict) -> str:
    """设备行 → 装配文本内 id(优先 params.ref_id 稳定字符串,否则 d<id>)。"""
    ref = (device.get("params") or {}).get("ref_id")
    if isinstance(ref, str) and ref:
        return ref
    return f"d{device.get('id')}"


def _model_ref(type_id: str) -> str:
    """类型 id → 模型引用(注册表有版本 → id@version;未注册 → 裸 id)。"""
    try:
        spec = get_device_type(type_id)
        return f"{type_id}@{spec.version}"
    except NotFoundError:
        # 未注册: 装配继续按裸 id 串行, 由下游装配检查模块显式阻断(RR-P2-05)。
        return type_id


def _fmt_num(x: Any) -> str:
    """确定性浮点格式(整数 → 整数文本;其余用最短往返表示)。"""
    f = float(x)
    if not math.isfinite(f):
        raise ValueError(f"不可序列化的非有限数值: {x!r}")
    if f.is_integer() and abs(f) < 1e15:
        return str(int(f))
    return repr(f)


def _yaml_scalar(value: Any) -> str:
    """标量 → YAML 子集文本(字符串按需引号,保证 parse 互逆)。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _fmt_num(value)
    text = str(value)
    if text == "" or text != text.strip():
        return _quoted(text)
    if text[0] in "{['\"#-?|>&*!%@`]":
        return _quoted(text)
    if _looks_numeric(text) or text.lower() in ("null", "true", "false", "yes", "no", "~", "on", "off"):
        return _quoted(text)
    if " #" in text or ":" in text:
        return _quoted(text)
    return text


def _quoted(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _looks_numeric(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def _flow_map(mapping: dict) -> str:
    """确定性流式映射(键排序,值递归标量化)。"""
    items = []
    for key in sorted(mapping):
        value = mapping[key]
        items.append(f"{key}: {_flow_value(value)}")
    return "{" + ", ".join(items) + "}"


def _flow_value(value: Any) -> str:
    if isinstance(value, dict):
        return _flow_map(value)
    if isinstance(value, list):
        return "[" + ", ".join(_flow_value(v) for v in value) + "]"
    return _yaml_scalar(value)


# ---------------------------------------------------------------------------
# 项目图 → AssemblySpec
# ---------------------------------------------------------------------------


def _build_data_refs(device: dict) -> list[DataRef]:
    """从设备参数提取数据集引用:
    - 引用类参数值为 dict 含 dataset_version_id → DataRef(key=参数名, ...);
    - 显式 "data_refs" 参数(列表)→ 逐项解析;
    - 遗留字符串引用 "dataset:e_load" → (key=参数名, 占位 vid=0, 列名派生)。
    """
    params = device.get("params") or {}
    refs: list[DataRef] = []
    for key, value in params.items():
        if key in _INTERNAL_PARAM_KEYS:
            continue
        if isinstance(value, dict) and value.get("dataset_version_id") is not None:
            vid = value["dataset_version_id"]
            refs.append(
                DataRef(
                    key=key,
                    dataset_version_id=int(vid),
                    dataset_name=str(value.get("dataset_name", "")),
                    columns=[c for c in value.get("columns", []) if isinstance(c, str)],
                    unit=str(value.get("unit", "")),
                    resolution=str(value.get("resolution", "")),
                )
            )
        elif isinstance(value, str) and value.startswith("dataset:"):
            # 遗留字符串引用 "dataset:e_load": 列名 = 冒号后段(先到先得, 见 _merge_dataset_meta)
            col = value.split(":", 1)[1].strip() or key
            refs.append(
                DataRef(key=key, dataset_version_id=0, columns=[col], dataset_name=col)
            )
        # 注:纯数值的 *_profile 参数(如 cop_profile: 0 = 恒定 COP)是参数而非数据集引用,不转换
    for i, item in enumerate(params.get("data_refs") or []):
        if isinstance(item, dict) and item.get("dataset_version_id") is not None:
            refs.append(
                DataRef(
                    key=str(item.get("key", f"data{i}")),
                    dataset_version_id=int(item["dataset_version_id"]),
                    dataset_name=str(item.get("dataset_name", "")),
                    columns=[c for c in item.get("columns", []) if isinstance(c, str)],
                    unit=str(item.get("unit", "")),
                    resolution=str(item.get("resolution", "")),
                )
            )
        elif isinstance(item, int):
            refs.append(DataRef(key=f"data{i}", dataset_version_id=item))
    return refs


def _merge_dataset_meta(refs: list[DataRef], datasets: dict[int, dict] | None) -> list[DataRef]:
    """用数据集元信息补齐 data_refs 的列/单位/分辨率(缺失时保留声明值)。

    遗留占位引用(dataset_version_id=0, 来自 "dataset:col" 字符串)按列名匹配
    绑定的数据集版本(先到先得): 命中则回填真实版本 id/单位/分辨率 —— 该解析
    在装配检查/装配文本阶段完成, 保证遗留内容可过闸门且装配文本自洽。
    """
    if not datasets:
        return refs
    # 占位引用: 列名 → 首个提供该列的数据集版本; 单位按列取
    col_vid: dict[str, int] = {}
    col_unit: dict[str, str] = {}
    meta_by_vid: dict[int, dict] = {}
    for vid, meta in datasets.items():
        meta_by_vid[vid] = meta
        if not isinstance(meta, dict):
            continue
        col_units = meta.get("column_units") if isinstance(meta.get("column_units"), dict) else {}
        for col in meta.get("columns", []):
            if not isinstance(col, str):
                continue
            col_vid.setdefault(col, vid)
            unit = col_units.get(col)
            if isinstance(unit, str) and unit:
                col_unit.setdefault(col, unit)
    merged: list[DataRef] = []
    for ref in refs:
        meta = datasets.get(ref.dataset_version_id)
        resolved_vid = ref.dataset_version_id
        if ref.dataset_version_id == 0 and ref.columns:
            # 遗留占位: 按列名定位绑定数据集
            vid = next((col_vid[c] for c in ref.columns if c in col_vid), None)
            if vid is not None:
                resolved_vid = vid
                meta = meta_by_vid.get(vid)
        if isinstance(meta, dict):
            # 数据集元信息为权威单位(codex 二次审核 Medium-8: 显式 unit 只是
            # 期望声明, 元信息缺失该列单位时才用声明值, 不一致由检查器报阻断)
            col_unit_for_ref = next(
                (col_unit[c] for c in ref.columns if c in col_unit), ""
            ) or ref.unit
            merged.append(
                DataRef(
                    key=ref.key,
                    dataset_version_id=resolved_vid or int(meta.get("id", 0)),
                    dataset_name=ref.dataset_name or str(meta.get("name", "")),
                    columns=ref.columns or [c for c in meta.get("columns", []) if isinstance(c, str)],
                    unit=col_unit_for_ref,
                    resolution=ref.resolution or str(meta.get("resolution", "")),
                )
            )
        else:
            merged.append(ref)
    return merged


def _clean_params(device: dict) -> dict[str, object]:
    """清洗设备参数:去掉内部键(下划线前缀)与数据集引用键。"""
    params = device.get("params") or {}
    clean: dict[str, object] = {}
    for key, value in params.items():
        if key in _INTERNAL_PARAM_KEYS or key.startswith("_"):
            continue
        if isinstance(value, dict) and value.get("dataset_version_id") is not None:
            continue  # 引用类参数已转为 data_refs
        clean[key] = value
    return clean


def _explicit_ports_from_graph(ports_raw: list[dict], device_ref_of: dict[int, str]) -> list[AssemblyPort]:
    """图端口 → 显式端口声明(仅带 capacity 的端口;无 capacity 由注册表推导)。"""
    explicit: list[AssemblyPort] = []
    for p in ports_raw:
        capacity = _clean_num(p.get("capacity"))
        if capacity is None:
            continue
        device_id = p.get("device_id")
        ref = device_ref_of.get(device_id)
        if ref is None or not p.get("name"):
            continue
        carrier = PORT_TYPE_TO_CARRIER.get(str(p.get("port_type", "")), "electric")
        qty, unit = CARRIER_DEFAULT_QUANTITY_UNIT.get(carrier, ("power", "W"))
        explicit.append(
            AssemblyPort(
                device=ref,
                name=str(p["name"]),
                carrier=carrier,
                direction=str(p.get("direction", "in")),
                quantity=qty,
                unit=unit,
                nature=NATURE_INSTANT,
                capacity=capacity,
            )
        )
    return explicit


def build_assembly(
    graph: dict,
    *,
    datasets: dict[int, dict] | None = None,
    calc_config: dict | None = None,
    solver_options: dict | None = None,
    random_seed: int | None = None,
    source_graph_id: int | None = None,
) -> AssemblySpec:
    """项目内容 → 装配对象(不做检查,检查由 check_assembly 完成)。

    graph 为 get_graph 序列化结构 {devices, ports, connections, graph_id, name};
    requirements 从 calc_config.algorithm/solver_options/random_seed 填充
    (algorithm 缺省 "ies.algo.milp_hybrid@1.0.0";tolerances 缺省
    {mip_rel_gap:0.001, time_limit_s:600})。
    """
    devices_raw = graph.get("devices", []) or []
    ports_raw = graph.get("ports", []) or []
    conns_raw = graph.get("connections", []) or []
    calc_config = calc_config or {}
    solver_options = solver_options or {}

    # 端口索引:id → (device_ref, name, port_type, capacity)
    device_ref_of: dict[int, str] = {d.get("id"): _device_ref(d) for d in devices_raw}
    port_index: dict[int, dict] = {}
    for p in ports_raw:
        port_index[p.get("id")] = p

    # 设备实例(按 id 排序,确定性);带 capacity 的图端口转为显式端口声明
    explicit_ports = _explicit_ports_from_graph(ports_raw, device_ref_of)
    explicit_by_device: dict[str, list[AssemblyPort]] = {}
    for ep in explicit_ports:
        explicit_by_device.setdefault(ep.device, []).append(ep)

    devices: list[AssemblyDevice] = []
    for d in sorted(devices_raw, key=lambda x: _device_ref(x)):
        type_id = _resolve_type_id(d)
        stateful = (d.get("params") or {}).get("stateful")
        if isinstance(stateful, str):
            stateful = stateful.lower() in ("true", "1", "yes")
        method = (d.get("params") or {}).get("model_method")
        if not isinstance(method, str) or not method:
            method = MODEL_METHOD_MECHANISM
        raw_kind = str(d.get("kind", "existing"))
        kind = raw_kind if raw_kind in ("existing", "new") else "existing"
        devices.append(
            AssemblyDevice(
                id=_device_ref(d),
                model=_model_ref(type_id),
                kind=kind,
                model_method=method,
                stateful=bool(stateful),
                params=_clean_params(d),
                data_refs=_merge_dataset_meta(_build_data_refs(d), datasets),
                ports=explicit_by_device.get(_device_ref(d), []),
            )
        )

    # 边(连接):loss_rate > 0 → 包裹管道设备
    edges: list[AssemblyEdge] = []
    pipelines: list[AssemblyPipeline] = []
    for c in sorted(conns_raw, key=lambda x: str(x.get("id"))):
        edge_id = f"e{c.get('id')}"
        from_p = port_index.get(c.get("from_port_id"))
        to_p = port_index.get(c.get("to_port_id"))
        if from_p is None or to_p is None:
            continue
        from_ref = f"{device_ref_of.get(from_p.get('device_id'), '?')}.{from_p.get('name')}"
        to_ref = f"{device_ref_of.get(to_p.get('device_id'), '?')}.{to_p.get('name')}"
        loss_rate = _num_or_zero(c.get("loss_rate"))
        capacity = _clean_num(c.get("capacity"))
        conn_params = c.get("params") or {}
        if loss_rate > 0:
            # 包裹:原边 from→管道 in(瞬时),新增边 管道 out→to(延迟)
            pipe_id = f"{edge_id}_pipe"
            pipelines.append(
                AssemblyPipeline(
                    id=pipe_id,
                    model="ies.device.transport_pipe@1.0.0",
                    params={
                        "delay_steps": int(conn_params.get("delay_steps", 1) or 1),
                        "loss_per_step": loss_rate,
                    },
                )
            )
            edges.append(
                AssemblyEdge(
                    id=edge_id,
                    from_port=from_ref,
                    to_port=f"{pipe_id}.{_pipe_in_name()}",
                    capacity=capacity,
                )
            )
            edges.append(
                AssemblyEdge(
                    id=f"{edge_id}_out",
                    from_port=f"{pipe_id}.{_pipe_out_name()}",
                    to_port=to_ref,
                )
            )
        else:
            edges.append(
                AssemblyEdge(
                    id=edge_id,
                    from_port=from_ref,
                    to_port=to_ref,
                    capacity=capacity,
                )
            )

    # 时间轴与计算要求
    time_axis_raw = calc_config.get("time_axis") if isinstance(calc_config.get("time_axis"), dict) else {}
    spec = AssemblySpec(
        name=str(graph.get("name", "") or f"graph_{graph.get('graph_id')}"),
        format_version=FORMAT_VERSION,
        source_graph_id=source_graph_id if source_graph_id is not None else graph.get("graph_id"),
        time_axis=TimeAxisRef(
            resolution=str(time_axis_raw.get("resolution", "1h")),
            start=str(time_axis_raw.get("start", "2025-01-01T00:00:00Z")),
            timezone_offset_min=int(time_axis_raw.get("timezone_offset_min", 0) or 0),
        ),
        devices=devices,
        edges=edges,
        pipelines=pipelines,
        requirements=_build_requirements(calc_config, solver_options, random_seed),
    )
    return spec


# 管道端口名: 与 catalog/transport_pipe.yaml 一致(heat_in/heat_out) —
# 这是设备 YAML 唯一权威来源, 不再由装配模块维护内置常量。
_PIPE_IN_NAME = "heat_in"
_PIPE_OUT_NAME = "heat_out"


def _pipe_in_name() -> str:
    """管道入端口名。"""
    return _PIPE_IN_NAME


def _pipe_out_name() -> str:
    """管道出端口名。"""
    return _PIPE_OUT_NAME


def _num_or_zero(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clean_num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _build_requirements(
    calc_config: dict, solver_options: dict, random_seed: int | None
) -> CalcRequirements:
    """计算要求:算法/tolerances/seed 自 calc_config/solver_options/random_seed 填充。"""
    algorithm = str(calc_config.get("algorithm") or "ies.algo.milp_hybrid@1.0.0")
    tolerances_raw = calc_config.get("tolerances") or solver_options
    tolerances: dict[str, float] = {"mip_rel_gap": 0.001, "time_limit_s": 600.0}
    if isinstance(tolerances_raw, dict):
        for key in ("mip_rel_gap", "time_limit_s"):
            if key in tolerances_raw and _clean_num(tolerances_raw[key]) is not None:
                tolerances[key] = float(tolerances_raw[key])
    seed = random_seed
    if seed is None and _clean_num(calc_config.get("seed")) is not None:
        seed = int(calc_config["seed"])
    return CalcRequirements(algorithm=algorithm, tolerances=tolerances, seed=seed)


# ---------------------------------------------------------------------------
# AssemblySpec → 规范文本(确定性)
# ---------------------------------------------------------------------------


def dumps_assembly(spec: AssemblySpec) -> str:
    """AssemblySpec → 规范 YAML 1.2 文本(确定性;与 parse_assembly 互逆)。

    章节顺序固定:assembly / time_axis / devices / ports / edges / pipelines /
    constraints / requirements;键序固定;数值格式固定;列表按 id 排序。
    """
    lines: list[str] = []
    lines.append("assembly:")
    lines.append(f"  name: {_yaml_scalar(spec.name)}")
    lines.append(f"  format_version: {_yaml_scalar(spec.format_version)}")
    if spec.source_graph_id is not None:
        lines.append(f"  source_graph_id: {int(spec.source_graph_id)}")

    axis = spec.time_axis or TimeAxisRef(resolution="1h")
    lines.append("time_axis:")
    lines.append(f"  resolution: {axis.resolution}")
    lines.append(f"  start: {_yaml_scalar(axis.start)}")
    if axis.timezone_offset_min:
        lines.append(f"  timezone_offset_min: {axis.timezone_offset_min}")

    if spec.devices:
        lines.append("devices:")
        for dev in sorted(spec.devices, key=lambda d: d.id):
            lines.append(f"  - id: {_yaml_scalar(dev.id)}")
            lines.append(f"    model: {_yaml_scalar(dev.model)}")
            if dev.kind != "existing":
                lines.append(f"    kind: {dev.kind}")
            if dev.model_method != MODEL_METHOD_MECHANISM:
                lines.append(f"    model_method: {dev.model_method}")
            if dev.stateful:
                lines.append("    stateful: true")
            if dev.params:
                lines.append(f"    params: {_flow_map(dev.params)}")
            if dev.data_refs:
                lines.append("    data_refs:")
                for ref in sorted(dev.data_refs, key=lambda r: r.key):
                    lines.append(f"      - {_data_ref_flow(ref)}")

    # ports 章节:仅显式覆盖声明(capacity 等)才输出
    explicit_ports = [p for d in spec.devices for p in d.ports] + list(spec.explicit_pipeline_ports)
    if explicit_ports:
        lines.append("ports:")
        for port in sorted(explicit_ports, key=lambda p: p.ref):
            lines.append(f"  - device: {_yaml_scalar(port.device)}")
            lines.append(f"    name: {_yaml_scalar(port.name)}")
            lines.append(f"    carrier: {port.carrier}")
            lines.append(f"    direction: {port.direction}")
            lines.append(f"    quantity: {port.quantity}")
            lines.append(f"    unit: {_yaml_scalar(port.unit)}")
            lines.append(f"    nature: {port.nature}")
            if port.capacity is not None:
                lines.append(f"    capacity: {_fmt_num(port.capacity)}")

    if spec.edges:
        lines.append("edges:")
        for edge in sorted(spec.edges, key=lambda e: e.id):
            lines.append(f"  - id: {_yaml_scalar(edge.id)}")
            lines.append(f"    from: {_yaml_scalar(edge.from_port)}")
            lines.append(f"    to: {_yaml_scalar(edge.to_port)}")
            if edge.capacity is not None:
                lines.append(f"    capacity: {_fmt_num(edge.capacity)}")

    if spec.pipelines:
        lines.append("pipelines:")
        for pipe in sorted(spec.pipelines, key=lambda p: p.id):
            lines.append(f"  - id: {_yaml_scalar(pipe.id)}")
            lines.append(f"    model: {_yaml_scalar(pipe.model)}")
            if pipe.params:
                lines.append(f"    params: {_flow_map(pipe.params)}")

    if spec.constraints:
        lines.append("constraints:")
        for constraint in sorted(spec.constraints, key=lambda c: c.id):
            lines.append(f"  - id: {_yaml_scalar(constraint.id)}")
            lines.append(f"    type: {constraint.type}")
            lines.append(f"    expr: {_yaml_scalar(constraint.expr)}")
            if not constraint.enabled:
                lines.append("    enabled: false")

    if spec.requirements is not None:
        req = spec.requirements
        lines.append("requirements:")
        lines.append(f"  algorithm: {_yaml_scalar(req.algorithm)}")
        if req.tolerances:
            lines.append(f"  tolerances: {_flow_map(req.tolerances)}")
        if req.seed is not None:
            lines.append(f"  seed: {int(req.seed)}")
    return "\n".join(lines) + "\n"


def _data_ref_flow(ref: DataRef) -> str:
    """data_refs 项流式文本(键排序,确定性)。"""
    parts: list[str] = []
    if ref.key:
        parts.append(f"key: {_yaml_scalar(ref.key)}")
    parts.append(f"dataset_version_id: {ref.dataset_version_id}")
    if ref.dataset_name:
        parts.append(f"dataset_name: {_yaml_scalar(ref.dataset_name)}")
    if ref.columns:
        parts.append("columns: [" + ", ".join(_yaml_scalar(c) for c in ref.columns) + "]")
    if ref.unit:
        parts.append(f"unit: {_yaml_scalar(ref.unit)}")
    if ref.resolution:
        parts.append(f"resolution: {ref.resolution}")
    return "{" + ", ".join(parts) + "}"


def build_assembly_text(
    graph: dict,
    *,
    datasets: dict[int, dict] | None = None,
    calc_config: dict | None = None,
    solver_options: dict | None = None,
    random_seed: int | None = None,
    source_graph_id: int | None = None,
) -> str:
    """便捷入口:项目图 → 规范装配文本(供 API 与任务装配直接调用)。"""
    spec = build_assembly(
        graph,
        datasets=datasets,
        calc_config=calc_config,
        solver_options=solver_options,
        random_seed=random_seed,
        source_graph_id=source_graph_id,
    )
    return dumps_assembly(spec)


__all__ = [
    "build_assembly",
    "dumps_assembly",
    "build_assembly_text",
    "_fmt_num",
    "_yaml_scalar",
    "_flow_map",
]
