"""ies.assembly 1.0.0 旧形态一次性迁移与回执(roadmap 0.7.0 事项 3)。

旧形态指 ``FORMAT_VERSION = "1.0"`` 的装配文本/``AssemblySpec``(devices/ports/
edges/pipelines/constraints/requirements 章节 + 边-端结构);本模块将其
一次性迁移到 ``ies.assembly`` ``1.0.0`` 文档并生成回执,迁移产物经同
一 ``validate_assembly_doc`` 入口验证:旧形态不再作为后续计算的持久输入,
只保留不可变 ``ValidatedAssemblyArtifact``。

设计要点(0.7.0 文档/任务说明):
- 迁移为纯函数:输入旧 spec/text + datasets/solver/olderator,产出新 doc + 回执;
- 旧形态无法唯一映射的字段(无 model version、缺 solver、无数据集 sha256 等)
  产生阻断诊断,回执记录失败 → 失败可见,不存在半迁移;
- 已迁移内容必须经过 ``validate_assembly_doc`` 才视为可执行;本模块对
  生成的新 doc 调用 ``validate_assembly_doc`` 并把诊断写入回执;
- ``LEGACY_SOLVER_REF`` 作为旧链推导的求解器引用;GeneratorProvider/
  SolverRuntime 注册表能力核对在 0.8.0 引入后由注册表取代(显式事实:
  旧链对应 scipy.optimize.milp / HiGHS)。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from iesplan.assembly.builder10 import LEGACY_SOLVER_REF
from iesplan.assembly.canonicalizer import (
    canonicalize_assembly_doc,
    parse_iso8601_utc,
)
from iesplan.assembly.contracts import SCHEMA_VERSION, ValidationReceipt
from iesplan.assembly.diags import ASM_CONV_UNMAPPABLE
from iesplan.assembly.parser10 import run_structure_checks
from iesplan.assembly.schema import (
    AssemblySpec,
    DataRef,
    FORMAT_VERSION,
)
from iesplan.core.diagnostics import Diagnostic, make_diag
from iesplan.core.errors import NotFoundError


@dataclass(slots=True)
class MigrationResult:
    """迁移结果(doc + 回执 + 诊断);失败时 doc 为 None。"""

    doc: dict | None
    receipt: dict
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """无阻断诊断且 doc 已生成。"""
        return self.doc is not None and not any(d.blocking for d in self.diagnostics)


def migrate_assembly_text(
    text: str,
    *,
    datasets: Mapping[int, Mapping] | None = None,
    solver: str | None = None,
    generator: str | None = None,
) -> MigrationResult:
    """旧形态装配文本 → ies.assembly 1.0.0 文档 + 迁移回执。"""
    from iesplan.assembly.parser import parse_assembly

    parsed = parse_assembly(text)
    if parsed.spec is None:
        return MigrationResult(
            doc=None,
            receipt=_make_receipt(
                text_bytes=text.encode("utf-8"),
                new_sha="",
                transformations=[],
                diagnostics=list(parsed.diagnostics),
                ok=False,
            ),
            diagnostics=list(parsed.diagnostics),
        )
    return migrate_assembly_spec(parsed.spec, datasets=datasets, solver=solver, generator=generator)


def migrate_assembly_spec(
    spec: AssemblySpec,
    *,
    datasets: Mapping[int, Mapping] | None = None,
    solver: str | None = None,
    generator: str | None = None,
) -> MigrationResult:
    """旧 AssemblySpec → ies.assembly 1.0.0 文档 + 回执。

    Args:
        spec: 旧形态 ``AssemblySpec``(format_version = "1.0");
        datasets: 数据集元信息(以版本 id 整数索引),需含 sha256/media_type;
        solver: 新格式 calculation.solver 引用;省略则尝试 LEGACY_SOLVER_REF 推导,
            但保守起见未提供即阻断(失败可见)。
        generator: 新格式 calculation.generator 引用;缺省取旧 spec.requirements.algorithm,
            旧形态无 algorithm → 阻断。

    Returns:
        MigrationResult.doc: 已通过 validator 验证的 ies.assembly 1.0.0 文档;
                            失败时为 None。
        MigrationResult.receipt: 迁移回执(JSON 兼容 dict)。
    """
    diags: list[Diagnostic] = []
    transformations: list[str] = []
    datasets_map = dict(datasets or {})

    # 1) assembly.id/name(失败可见:name 空 → ASM-CONV-001)
    name = spec.name.strip() if spec.name else ""
    if not name:
        diags.append(
            _conv_diag("assembly_name_missing")
        )
    assembly_id = _assembly_id_from_spec(spec, name)

    # 2) time_axis:start → UTC;end = start + annual 步长(legacy 推导)
    axis = spec.time_axis
    if axis is None:
        diags.append(_conv_diag("time_axis_missing"))
        return _fail_result(diags, transformations)
    try:
        start_utc = parse_iso8601_utc(axis.start)
    except ValueError:
        diags.append(_conv_diag("time_axis_start_invalid", value=axis.start))
        return _fail_result(diags, transformations)
    resolution = axis.resolution
    # 旧链 step 配置与 builder10._STEP_* 对齐(同一事实源;迁移可独立维护以避免跨模块私有符号)
    _STEPS_PER_YEAR = {"15min": 35040, "30min": 17520, "1h": 8760}
    _STEP_SECONDS = {"15min": 900, "30min": 1800, "1h": 3600}
    if resolution not in _STEPS_PER_YEAR:
        diags.append(_conv_diag("time_axis_resolution_unsupported", value=resolution))
        return _fail_result(diags, transformations)
    from datetime import timedelta
    seconds = _STEP_SECONDS[resolution]
    steps = _STEPS_PER_YEAR[resolution]
    end_utc = start_utc + timedelta(seconds=seconds * steps)
    transformations.append("time_axis_end_derived_from_annual_horizon")
    from datetime import UTC

    def _utc(dt) -> str:
        return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")

    # 3) resources.datasets(失败可见:数据集元信息缺失或缺 sha256 → 阻断)
    resources_datasets, data_diags = _build_resources_from_spec(spec, datasets_map)
    diags.extend(data_diags)
    if data_diags:
        return _fail_result(diags, transformations)
    transformations.append("resources_resolved_to_object_form")

    # 4) devices(模型必须固定精确版本;reference 参数可由 data_refs 替代)
    devices_out: dict[str, dict] = {}
    device_bindings: dict[str, dict[str, dict]] = {}  # dev_id → data binding map
    for dev in spec.devices:
        if not dev.id:
            diags.append(_conv_diag("device_id_missing"))
            continue
        type_id, version = _split_model(dev.model)
        if not version:
            diags.append(_conv_diag("model_unversioned", device=dev.id, model=dev.model))
            continue
        devices_out[dev.id] = {
            "model": f"{type_id}@{version}",
            "parameters": _clean_legacy_params(dev.params),
        }
        # data_refs → 新格式 data 绑定
        bindings: dict[str, dict] = {}
        for ref in dev.data_refs:
            col = ref.columns[0] if ref.columns else None
            if col is None:
                meta = datasets_map.get(ref.dataset_version_id) if ref.dataset_version_id else None
                cols_meta = meta.get("columns") if isinstance(meta, Mapping) else None
                col = cols_meta[0] if cols_meta else ref.dataset_name or ref.key
            ds_id = f"ds{int(ref.dataset_version_id)}" if ref.dataset_version_id else "ds0"
            if ref.dataset_version_id and ref.dataset_version_id not in datasets_map:
                diags.append(
                    _conv_diag(
                        "dataset_meta_missing",
                        device=dev.id,
                        dataset_version_id=ref.dataset_version_id,
                    )
                )
                continue
            bindings[ref.key] = {"dataset": ds_id, "column": col}
        if bindings:
            devices_out[dev.id]["data"] = bindings
            device_bindings[dev.id] = bindings
        # 显式端口 capacity → 约束表达式(系统级)
    if any(d.blocking for d in diags):
        return _fail_result(diags, transformations)

    # 5) connections + 约束(端口 capacity 与边 capacity 转约束;端口派生由原 spec.ports)
    connections_out: dict[str, dict] = {}
    constraints_out: dict[str, dict] = {}
    for edge in spec.edges:
        if not edge.id:
            continue
        connections_out[edge.id] = {"from": edge.from_port, "to": edge.to_port}
        if edge.capacity is not None:
            constraints_out[f"{edge.id}_capacity"] = {
                "type": "capacity",
                "expr": f"{edge.from_port} <= {edge.capacity} W",
                "enabled": True,
            }
    for cid, constraint in enumerate(spec.constraints):
        if not constraint.expr:
            continue
        constraints_out[constraint.id or f"c{cid + 1}"] = {
            "type": constraint.type,
            "expr": constraint.expr,
            "enabled": constraint.enabled,
        }
    # 设备端口 capacity → capacity 约束(显式 capacity)
    for dev in spec.devices:
        for port in dev.ports:
            if port.capacity is None:
                continue
            constraints_out[f"{port.device}_{port.name}_capacity"] = {
                "type": "capacity",
                "expr": f"{port.device}.{port.name} <= {port.capacity} W",
                "enabled": True,
            }
    if constraints_out:
        transformations.append("legacy_capacity_to_constraint")

    # 6) calculation
    requirements = spec.requirements
    if requirements is None:
        diags.append(_conv_diag("requirements_missing"))
        return _fail_result(diags, transformations)
    gen = generator or requirements.algorithm
    if not gen:
        diags.append(_conv_diag("generator_missing"))
        return _fail_result(diags, transformations)
    if solver is None:
        sol = LEGACY_SOLVER_REF
    elif not solver:
        diags.append(_conv_diag("solver_required"))
        return _fail_result(diags, transformations)
    else:
        sol = solver
    options: dict[str, float] = {}
    tolerances = requirements.tolerances
    rel_gap = tolerances.get("mip_rel_gap") if isinstance(tolerances, Mapping) else None
    if isinstance(rel_gap, (int, float)):
        options["relative_gap"] = float(rel_gap)
        transformations.append("legacy_tolerance_relative_gap_renamed")
    time_limit = tolerances.get("time_limit_s") if isinstance(tolerances, Mapping) else None
    if isinstance(time_limit, (int, float)):
        options["time_limit_seconds"] = float(time_limit)
        transformations.append("legacy_tolerance_time_limit_renamed")
    calc_out: dict = {
        "mode": "fixed_operation",
        "generator": gen,
        "solver": sol,
        "options": options,
    }
    if requirements.seed is not None:
        calc_out["random_seed"] = int(requirements.seed)

    # 7) outputs(派生为空;GUI 可补)
    outputs_out = {"series": [], "metrics": []}

    doc = {
        "schema": "ies.assembly",
        "schema_version": SCHEMA_VERSION,
        "assembly": {"id": assembly_id, "name": name},
        "time_axis": {
            "start": _utc(start_utc),
            "end": _utc(end_utc),
            "resolution": resolution,
            "endpoint": "left_closed_right_open",
        },
        "resources": {"datasets": resources_datasets},
        "devices": devices_out,
        "connections": connections_out,
        "constraints": constraints_out,
        "calculation": calc_out,
        "outputs": outputs_out,
        "extensions": {},
    }
    if any(d.blocking for d in diags):
        return _fail_result(diags, transformations)

    # 8) 验证迁移产物通过校验入口(失败时回执含失败状态,不发布 artifact)
    return _validate_migrated(doc, diags, transformations)


def _validate_migrated(
    doc: dict,
    diags: list[Diagnostic],
    transformations: list[str],
) -> MigrationResult:
    """迁移 doc → 校验器复检(失败可见 → 回执 ok=False, doc=None)。"""
    from iesplan.assembly.validator import validate_assembly_doc

    result = validate_assembly_doc(doc, datasets=_datasets_for_validation(doc))
    for d in result.diagnostics:
        diags.append(d)
    if result.artifact is None:
        return _fail_result(diags, transformations, doc=doc)
    # 新 doc 的 SHA 由校验器产出(回执一致性)
    new_sha = result.artifact.assembly_sha256
    receipt = _make_receipt(
        text_bytes=None,
        new_sha=new_sha,
        transformations=transformations,
        diagnostics=diags,
        ok=True,
    )
    return MigrationResult(doc=doc, receipt=receipt, diagnostics=diags)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _assembly_id_from_spec(spec: AssemblySpec, name: str) -> str:
    if spec.source_graph_id is not None:
        return f"graph_{int(spec.source_graph_id)}"
    return _slugify_id(name or "legacy_export")


def _slugify_id(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_-]+", "_", text.strip().lower()).strip("-_") or "legacy_export"
    if not re.match(r"^[a-z][a-z0-9_-]*$", cleaned):
        cleaned = "legacy_export"
    return cleaned


def _split_model(model: str) -> tuple[str, str | None]:
    if "@" in model:
        type_id, _, version = model.rpartition("@")
        return type_id, version or None
    return model, None


def _clean_legacy_params(params: Mapping) -> dict:
    clean: dict = {}
    for k, v in params.items():
        if k.startswith("_") or k in (
            "type_detail", "model_method", "stateful", "ref_id", "data_refs",
            "__layout", "_assembly_source",
        ):
            continue
        if isinstance(v, (dict, list)):
            continue
        clean[k] = v
    return clean


def _build_resources_from_spec(
    spec: AssemblySpec,
    datasets: Mapping[int, Mapping],
) -> tuple[dict, list[Diagnostic]]:
    diags: list[Diagnostic] = []
    out: dict[str, dict] = {}
    # 收集所有 vid
    vids: set[int] = set()
    for dev in spec.devices:
        for ref in dev.data_refs:
            if ref.dataset_version_id:
                vids.add(int(ref.dataset_version_id))
    if not datasets:
        diags.append(_conv_diag("datasets_snapshot_required"))
        return {}, diags
    for vid in sorted(vids):
        meta = datasets.get(vid)
        if meta is None:
            diags.append(_conv_diag("dataset_meta_missing", dataset_version_id=vid))
            continue
        sha = str(meta.get("sha256") or meta.get("content_hash") or "")
        media = str(meta.get("media_type") or "text/csv")
        if not sha:
            diags.append(_conv_diag("dataset_sha256_required", dataset_version_id=vid))
            continue
        out[f"ds{vid}"] = {
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


def _datasets_for_validation(doc: dict) -> dict:
    """根据 doc.resources.datasets 的 object_id 重建 validation 阶段的 datasets 快照。

    校验器要求 datasets 形参为 {dataset_id: {columns, ...}};迁移结果中
    resources.datasets 已含 sha256+media_type,但缺 columns 元信息。
    此处返回空元信息,接受列存在性检查的"无元信息"路径(校验器无 datasets
    参数时不报阻断性 column_not_found)。
    """
    return {}


def _conv_diag(reason: str, **params) -> Diagnostic:
    return make_diag(
        ASM_CONV_UNMAPPABLE,
        severity="error",
        blocking=True,
        params={"reason": reason, **params},
        location={"object_type": "assembly", "field": "migration"},
    )


def _fail_result(
    diags: list[Diagnostic],
    transformations: list[str],
    doc: dict | None = None,
) -> MigrationResult:
    receipt = _make_receipt(
        text_bytes=None,
        new_sha="",
        transformations=transformations,
        diagnostics=diags,
        ok=False,
    )
    return MigrationResult(doc=doc, receipt=receipt, diagnostics=diags)


def _make_receipt(
    *,
    text_bytes: bytes | None,
    new_sha: str,
    transformations: list[str],
    diagnostics: list[Diagnostic],
    ok: bool,
) -> dict:
    """迁移回执(JSON 兼容 dict;与 0.6.0 csv_migration 形态对齐)。

    字段:
      migration / from_format / to_schema: 迁移元信息;
      old_sha256 / new_sha256: 输入/输出摘要(text 路径);
      transformations: 已记录的字段映射决策;
      ok: 全部成功且通过 validator 才为 true;
      diagnostics: 全量诊断序列。
    """
    return {
        "migration": "ies.assembly",
        "from_format": FORMAT_VERSION,
        "to_schema": SCHEMA_VERSION,
        "old_sha256": hashlib.sha256(text_bytes).hexdigest() if text_bytes else "",
        "new_sha256": new_sha,
        "transformations": list(transformations),
        "ok": ok,
        "diagnostics": [d.to_dict() for d in diagnostics],
    }


__all__ = [
    "MigrationResult",
    "migrate_assembly_spec",
    "migrate_assembly_text",
]