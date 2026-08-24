"""ies.assembly 1.0.0 GUI 项目导出构造器(roadmap 0.7.0 事项 2)。

将项目内容(设备/端口/连接/数据集绑定/计算配置)映射为 ies.assembly 1.0.0
文档;损耗/延迟的连接自动包裹为 transport_pipe 设备实例,与手写 YAML
进入同一校验入口(validator.validate_project_export)。

不与 builder.py 共享私有符号(避免跨模块私有导入与隐式耦合);本模块提供
独立的 _device_ref / _model_ref / _resolve_data_bindings 辅助。

依赖: devices 公开门面(get_device_descriptor)、core 公共契约、
assembly.canonicalizer 公开纯函数、core.units;不依赖 services 与 ORM。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from iesplan.assembly.canonicalizer import parse_iso8601_utc
from iesplan.assembly.diags import (
    ASM_CONV_UNMAPPABLE,
)
from iesplan.assembly.diags import make_asm_diag as make_diag
from iesplan.core.diagnostics import Diagnostic
from iesplan.core.errors import NotFoundError

#: 旧形态 internal 参数键(builder.py 的 _INTERNAL_PARAM_KEYS 一致含义)
_INTERNAL_PARAM_KEYS: tuple[str, ...] = (
    "type_detail",
    "model_method",
    "stateful",
    "ref_id",
    "data_refs",
    "__layout",
    "_assembly_source",
)

#: 旧链推导的求解器引用(legacy scipy.optimize.milp / HiGHS);
#: 0.8.0 GeneratorProvider/SolverRuntime 注册表建立后由注册表能力核对取代。
LEGACY_SOLVER_REF = "ies.solver.highs@1.7.2"

#: 年步数 / 步长秒数(与 core/timeaxis.py RESOLUTIONS 对齐)
_STEPS_PER_YEAR: dict[str, int] = {"15min": 35040, "30min": 17520, "1h": 8760}
_STEP_SECONDS: dict[str, int] = {"15min": 900, "30min": 1800, "1h": 3600}

#: 管道模型与端口名(与 devices/catalog/transport_pipe.yaml 一致)
_PIPE_MODEL = "ies.device.transport_pipe@1.0.0"
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
    solver: str | None = None,
    generator: str | None = None,
) -> BuildDocResult:
    """项目内容 → ies.assembly 1.0.0 文档(roadmap 0.7.0 事项 2)。

    输入结构(兼容旧服务层):
      content = {"model": {"devices": [...], "ports": [...], "connections": [...]},
                 "calc_config": {...}}
      或 content 直接含 devices/ports/connections(扁平图)。

    字段映射:
    - assembly.id/name: graph.id/name(无 id 则 'legacy_export');
    - time_axis: calc_config.time_axis 派生(start/end/endpoint/resolution);
    - resources.datasets: 从 dataset_bindings + datasets 元信息(必须含 sha256+media_type)
      构造为内容寻址对象;
    - devices: 每个实例 → {model: <id>@<version>, parameters: 已清洗参数, data: ...};
    - connections: 每个图连接 → 新格式映射;loss_rate > 0 自动包裹 transport_pipe 设备实例;
    - calculation: mode/generator/solver/options/random_seed。

    返回 (doc, diagnostics)。任一阻断错误 → doc=None(不进入校验器)。
    """
    diags: list[Diagnostic] = []
    model_part = content.get("model") if isinstance(content.get("model"), Mapping) else content
    devices_raw = (model_part.get("devices") or []) if isinstance(model_part, Mapping) else []
    ports_raw = (model_part.get("ports") or []) if isinstance(model_part, Mapping) else []
    conns_raw = (model_part.get("connections") or []) if isinstance(model_part, Mapping) else []
    calc_cfg = content.get("calc_config") if isinstance(content.get("calc_config"), Mapping) else {}

    graph_id = content.get("graph_id")
    name = str(content.get("name") or (f"graph_{graph_id}" if graph_id else "legacy_export"))

    # 1) 时间轴(start 带偏移则换算为 UTC Z;end 旧形态未声明,从 start + 年步长推导)
    axis_raw = calc_cfg.get("time_axis") if isinstance(calc_cfg.get("time_axis"), Mapping) else {}
    resolution = str(axis_raw.get("resolution", "1h"))
    start_raw = str(axis_raw.get("start", "2025-01-01T00:00:00Z"))
    try:
        start_utc = parse_iso8601_utc(start_raw)
    except ValueError:
        diags.append(
            make_diag(
                ASM_CONV_UNMAPPABLE,
                severity="error",
                blocking=True,
                params={"reason": "time_axis_start_invalid", "value": start_raw},
                location={"object_type": "assembly", "field": "time_axis.start"},
            )
        )
        return BuildDocResult(doc=None, diagnostics=diags)
    steps = _STEPS_PER_YEAR.get(resolution, 8760)
    seconds = _STEP_SECONDS.get(resolution, 3600)
    from datetime import timedelta

    end_utc = start_utc + timedelta(seconds=seconds * steps)

    # 2) 数据集 → resources.datasets (object 形态;sha256/media_type 必须可获取)
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
        data_bindings, bind_diags = _resolve_data_bindings(d, datasets_map, type_id, dev_id)
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
    #    边 id 统一加 e 前缀,满足新格式局部 ID(lower_snake)约束
    connections_out: dict[str, dict] = {}
    for c in sorted(conns_raw, key=lambda x: str(x.get("id") or "")):
        raw_id = str(c.get("id") or "")
        if not raw_id:
            continue
        edge_id = f"e{raw_id}"
        from_p = port_index.get(c.get("from_port_id"))
        to_p = port_index.get(c.get("to_port_id"))
        if from_p is None or to_p is None:
            continue
        from_ref = f"{device_ref_of.get(from_p.get('device_id'), '?')}.{from_p.get('name')}"
        to_ref = f"{device_ref_of.get(to_p.get('device_id'), '?')}.{to_p.get('name')}"
        loss_rate = _num_or_zero(c.get("loss_rate"))
        conn_params = c.get("params") or {}
        if loss_rate > 0:
            pipe_id = f"{edge_id}_pipe"
            devices_out[pipe_id] = {
                "model": _PIPE_MODEL,
                "parameters": {
                    "delay_steps": int(conn_params.get("delay_steps", 1) or 1),
                    # transport_pipe 模型声明的参数名为 loss_rate(0.5.0 契约)
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

    # 6) calculation
    calc_out, calc_diags = _build_calculation(calc_cfg, generator=generator, solver=solver)
    diags.extend(calc_diags)

    # 7) outputs(派生为空列表;后续 GUI 入口可补充)
    outputs_out = {"series": [], "metrics": []}

    # 8) extensions(空映射)
    extensions_out: dict = {}

    # 9) 组装
    doc = {
        "schema": "ies.assembly",
        "schema_version": "1.0.0",
        "assembly": {"id": _slugify_id(name), "name": name},
        "time_axis": {
            "start": _format_utc(start_utc),
            "end": _format_utc(end_utc),
            "resolution": resolution,
            "endpoint": "left_closed_right_open",
        },
        "resources": {"datasets": resources_datasets},
        "devices": devices_out,
        "connections": connections_out,
        "constraints": constraints_out,
        "calculation": calc_out,
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
    params = device.get("params") or {}
    ref = params.get("ref_id")
    if isinstance(ref, str) and ref:
        return ref
    return f"d{device.get('id')}"


def _model_ref(type_id: str) -> str:
    from iesplan.devices import get_device_descriptor

    try:
        spec = get_device_descriptor(type_id)
        return f"{type_id}@{spec.version}"
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
    device: dict, datasets: dict[int, Mapping], type_id: str, dev_id: str
) -> tuple[dict | None, list[Diagnostic]]:
    """从设备的 reference 参数 + 显式 data_refs 列表构造新格式 data 绑定。

    新格式约定:data 绑定键 = 模型 data_inputs 列名;列名 = 数据集已校验列。
    旧形态遗留 `xxx_profile` reference 参数按"模型唯一 data_inputs 列"启发式
    映射到 data_inputs 列;多列或多对多关系无法唯一决定时返回阻断诊断。
    """
    from iesplan.devices import data_inputs_from_descriptor, get_device_descriptor

    diags: list[Diagnostic] = []
    try:
        descriptor = get_device_descriptor(type_id)
    except NotFoundError:
        # 未注册设备 → 校验器阻断,此处无法确定 data_inputs
        return None, []
    data_inputs = data_inputs_from_descriptor(descriptor)
    if not data_inputs:
        return None, diags

    bindings: dict[str, dict] = {}
    params = device.get("params") or {}

    for key, value in params.items():
        if key in _INTERNAL_PARAM_KEYS:
            continue
        if isinstance(value, dict) and value.get("dataset_version_id") is not None:
            vid = int(value["dataset_version_id"])
            meta = datasets.get(vid)
            columns = [c for c in (value.get("columns") or []) if isinstance(c, str)]
            column = columns[0] if columns else _first_meta_column(meta)
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
                            "model_data_inputs": [d.column_id for d in data_inputs],
                        },
                        location={"object_type": "device", "object_id": dev_id, "field": f"params.{key}"},
                    )
                )
                continue
            bindings[target_col.column_id] = {
                "dataset": _dataset_id_for(vid),
                "column": column or target_col.column_id,
            }
        elif isinstance(value, str) and value.startswith("dataset:"):
            col = value.split(":", 1)[1].strip() or key
            vid = next(
                (
                    v
                    for v, m in datasets.items()
                    if isinstance(m, Mapping) and col in (m.get("columns") or [])
                ),
                None,
            )
            if vid is None:
                diags.append(
                    make_diag(
                        ASM_CONV_UNMAPPABLE,
                        severity="error",
                        blocking=True,
                        params={"reason": "legacy_dataset_unresolved", "device": dev_id, "column": col},
                        location={"object_type": "device", "object_id": dev_id, "field": f"params.{key}"},
                    )
                )
                continue
            target_col = _resolve_data_input_column(data_inputs, key, col)
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
                            "model_data_inputs": [d.column_id for d in data_inputs],
                        },
                        location={"object_type": "device", "object_id": dev_id, "field": f"params.{key}"},
                    )
                )
                continue
            bindings[target_col.column_id] = {"dataset": _dataset_id_for(vid), "column": col}

    # 显式 data_refs 列表(与上面相同的列解析)
    for item in params.get("data_refs") or []:
        if isinstance(item, Mapping) and item.get("dataset_version_id") is not None:
            vid = int(item["dataset_version_id"])
            meta = datasets.get(vid)
            columns = [c for c in (item.get("columns") or []) if isinstance(c, str)]
            column = columns[0] if columns else _first_meta_column(meta)
            key = str(item.get("key") or f"data{len(bindings)}")
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
                            "model_data_inputs": [d.column_id for d in data_inputs],
                        },
                        location={"object_type": "device", "object_id": dev_id, "field": f"data_refs.{key}"},
                    )
                )
                continue
            bindings[target_col.column_id] = {
                "dataset": _dataset_id_for(vid),
                "column": column or target_col.column_id,
            }

    if any(d.blocking for d in diags):
        return None, diags
    return (bindings if bindings else None), diags


def _resolve_data_input_column(data_inputs, param_key: str, column: str | None):
    """唯一决定 data_inputs 列:旧参数名直接匹配模型列名;否则唯一 data_inputs 列;
    否则返回 None(无法唯一决定 → 阻断)。
    """
    if column:
        for d in data_inputs:
            if d.column_id == column:
                return d
    # 参数名直接匹配模型 data_inputs 列
    for d in data_inputs:
        if d.column_id == param_key:
            return d
    # 唯一 data_inputs 列:启发式映射(典型:负荷类设备)
    if len(data_inputs) == 1:
        return data_inputs[0]
    return None


def _first_meta_column(meta: Mapping | None) -> str | None:
    if not isinstance(meta, Mapping):
        return None
    for col in meta.get("columns") or []:
        if isinstance(col, str) and col:
            return col
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
        sha = str(meta.get("sha256") or meta.get("content_hash") or "")
        media = str(meta.get("media_type") or "text/csv")
        if not sha:
            diags.append(
                make_diag(
                    ASM_CONV_UNMAPPABLE,
                    severity="error",
                    blocking=True,
                    params={"reason": "dataset_sha256_required", "dataset_version_id": vid},
                    location={
                        "object_type": "assembly",
                        "field": f"resources.datasets.ds{vid}.source.sha256",
                    },
                )
            )
            continue
        out[_dataset_id_for(vid)] = {
            "source": {
                "kind": "object",
                "object_id": f"sha256:{sha}",
                "sha256": sha,
                "media_type": media,
            }
        }
    if any(d.blocking for d in diags):
        return {}, diags
    return out, diags


def _build_calculation(
    calc_cfg: Mapping, *, generator: str | None, solver: str | None
) -> tuple[dict, list[Diagnostic]]:
    diags: list[Diagnostic] = []
    mode = str(calc_cfg.get("mode") or "fixed_operation")
    gen = _generator_ref(calc_cfg, override=generator)
    sol = _solver_ref(calc_cfg, override=solver)
    tolerances = calc_cfg.get("tolerances") if isinstance(calc_cfg.get("tolerances"), Mapping) else {}
    options: dict[str, float] = {}
    if isinstance(tolerances, Mapping):
        rel_gap = tolerances.get("mip_rel_gap")
        time_lim = tolerances.get("time_limit_s")
        if isinstance(rel_gap, (int, float)):
            options["relative_gap"] = float(rel_gap)
        if isinstance(time_lim, (int, float)):
            options["time_limit_seconds"] = float(time_lim)
    if isinstance(calc_cfg.get("options"), Mapping):
        for k, v in calc_cfg["options"].items():
            if isinstance(v, (int, float)):
                options[str(k)] = float(v)
    seed = calc_cfg.get("random_seed")
    if seed is None:
        seed = calc_cfg.get("seed")
    out = {
        "mode": mode,
        "generator": gen,
        "solver": sol,
        "options": options,
    }
    if isinstance(seed, int) and not isinstance(seed, bool):
        out["random_seed"] = int(seed)
    return out, diags


def _generator_ref(calc_cfg: Mapping, *, override: str | None) -> str:
    """把 0.2-0.4 配置算法形态转换为 1.0.0 精确生成器引用。

    新字段 ``generator`` 与显式 override 必须自行提供精确版本；旧字段
    ``algorithm`` 可以是 ``{mode, name}`` 或未带版本的注册算法 id，此时只
    根据现有算法注册表签发其已声明版本。未知 id 原样保留，交给结构校验
    阻断，禁止静默回退到默认算法。
    """
    if override is not None:
        return str(override)
    explicit = calc_cfg.get("generator")
    if explicit not in (None, ""):
        return str(explicit)

    legacy = calc_cfg.get("algorithm")
    if isinstance(legacy, Mapping):
        legacy = legacy.get("name")
    candidate = str(legacy or "ies.algo.milp_hybrid")
    if "@" in candidate:
        return candidate

    from iesplan.engines.registry import get_algorithm

    try:
        spec = get_algorithm(candidate)
    except NotFoundError:
        return candidate
    return f"{spec.algo_id}@{spec.version}"


def _solver_ref(calc_cfg: Mapping, *, override: str | None) -> str:
    """把 0.2-0.4 的 ``highs`` 标识迁移为装配契约精确求解器引用。

    仅迁移项目现有且唯一受支持的 legacy HiGHS 别名；其他未版本化值原样
    进入结构校验并阻断，避免推测或静默替换求解器。
    """
    if override is not None:
        return str(override)
    candidate = str(calc_cfg.get("solver") or LEGACY_SOLVER_REF)
    if candidate in {"highs", "ies.solver.highs"}:
        return LEGACY_SOLVER_REF
    return candidate


def _format_utc(dt) -> str:
    """datetime → 带 Z 的 ISO 8601 UTC 字符串。"""
    from datetime import UTC

    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _slugify_id(text: str) -> str:
    """装配 id:仅保留 lower_snake/短横线字符;空时退回 'legacy_export'。"""
    import re

    cleaned = re.sub(r"[^a-z0-9_-]+", "_", text.strip().lower()).strip("-_") or "legacy_export"
    if not re.match(r"^[a-z][a-z0-9_-]*$", cleaned):
        cleaned = "legacy_export"
    return cleaned


def _num_or_zero(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


__all__ = [
    "BuildDocResult",
    "build_assembly_doc_from_content",
    "LEGACY_SOLVER_REF",
]
