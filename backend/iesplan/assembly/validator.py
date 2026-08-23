"""ies.assembly 1.0.0 统一校验入口(roadmap 0.7.0 事项 2)。

四阶段校验 → 唯一签名成功产物 ``ValidatedAssemblyArtifact``。
- 手写装配 YAML(text)与 GUI 项目导出(content)进入同一校验入口,
  成功只签发由规范文本、SHA-256 与校验回执组成的不可变三件套;
- 校验失败不产生可执行产物,返回结构化诊断列表。

四阶段:
1. 结构(parser10.parse_assembly_doc + parser10 暴露的 _structure_checks):
   schema/schema_version、顶层节、字段类型、ID/版本/引用形状、禁止字段、
   资源路径、calculation.mode 枚举、outputs/refs、extensions 命名空间;
2. 模型与数据:设备模型精确版本注册、参数只允许已声明字段、必填参数非空、
   数据绑定(dataset/column/单位/分辨率)、负荷类设备必带 data、相对资源解析;
3. 图与系统:复用 iesplan.assembly.checker 端口推导 + rules.connection
   (run_phase_b) + rules.solvability (run_phase_d) + 约束表达式检查
   (run_constraint_checks);端口不可达与悬空输入由本模块独立补充;
4. 计算兼容:generator/solver 精确版本(随 schema 严格)、options 标量键值、
   outputs series/metrics 引用设备存在;GeneratorProvider/SolverRuntime 注册
   表能力核对在 0.8.0 引入。

输出格式:
  AssemblyValidationResult(diagnostics, artifact) — artifact 为 None 时校验失败。

模块边界:
- 跨模块仅消费 devices 公开门面(get_device_descriptor/list_device_descriptors/
  datacontract.data_inputs_from_descriptor);
- 复用 assembly 域内 checker/rules/canonicalizer/parser10/contracts;
- 不导入 services/ORM/存储私有路径。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from iesplan.assembly.builder10 import build_assembly_doc_from_content
from iesplan.assembly.canonicalizer import canonicalize_assembly_doc
from iesplan.assembly.contracts import (
    CANON_ALGORITHM_ID,
    CANON_ALGORITHM_VERSION,
    VALIDATOR_ID,
    VALIDATOR_VERSION,
    AssemblyValidationError,
    ValidationReceipt,
    ValidatedAssemblyArtifact,
)
from iesplan.assembly.diags import (
    ASM_CALC_OPTIONS,
    ASM_INPUT_DATA_UNIT,
    ASM_INPUT_LOAD_DATA,
    ASM_INPUT_PARAM,
    ASM_INPUT_RANGE,
    ASM_INPUT_UNDECLARED,
    ASM_INPUT_UNFED,
    ASM_OUTPUT_REF,
    ASM_REF_DATASET,
    ASM_REF_MODEL_UNREG,
    ASM_RES_INVALID,
)
from iesplan.assembly.parser10 import parse_assembly_doc, run_structure_checks
from iesplan.assembly.schema import (
    AssemblyConstraint,
    AssemblyDevice,
    AssemblyEdge,
    AssemblySpec,
    DataRef,
    TimeAxisRef,
)
from iesplan.core.diagnostics import Diagnostic, make_diag
from iesplan.core.errors import NotFoundError
from iesplan.devices.datacontract import data_inputs_from_descriptor


@dataclass(slots=True)
class AssemblyValidationResult:
    diagnostics: list[Diagnostic]
    artifact: ValidatedAssemblyArtifact | None

    @property
    def ok(self) -> bool:
        """校验通过且无阻断诊断(``artifact`` 必须非空)。"""
        return self.artifact is not None and not any(d.blocking for d in self.diagnostics)


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------


def validate_assembly_text(
    text: str,
    *,
    source_name: str = "assembly.yaml",
    package_dir: str | Path | None = None,
    datasets: Mapping[str, Mapping] | None = None,
) -> AssemblyValidationResult:
    """手写装配 YAML 文本 → 四阶段校验(roadmap 0.7.0 事项 2)。"""
    parsed = parse_assembly_doc(text, source_name=source_name)
    if parsed.doc is None:
        return AssemblyValidationResult(diagnostics=list(parsed.diagnostics), artifact=None)
    return _run_validation(parsed.doc, package_dir=package_dir, datasets=datasets)


def validate_assembly_doc(
    doc: Mapping[str, Any],
    *,
    package_dir: str | Path | None = None,
    datasets: Mapping[str, Mapping] | None = None,
) -> AssemblyValidationResult:
    """已校验的 ies.assembly 1.0.0 文档 → 四阶段校验。

    doc 必须已是安全解析后的 plain dict(由 parser10 产生或 builder10 构造);
    本入口补做结构阶段复检 + 资源/模型/数据 + 图/系统 + 计算兼容校验。
    """
    diags: list[Diagnostic] = []
    # 结构阶段复检:parser10 暴露的公开复检入口(避免跨模块私有符号);
    # 复检诊断并入本入口的诊断列表
    tree = run_structure_checks(dict(doc), source_name="<doc>", diags=diags)
    if tree is None:
        return AssemblyValidationResult(diagnostics=diags, artifact=None)
    return _run_validation(tree, package_dir=package_dir, datasets=datasets)


def validate_project_export(
    content: Mapping,
    *,
    datasets: Mapping[int, Mapping] | None = None,
    solver: str | None = None,
    generator: str | None = None,
) -> AssemblyValidationResult:
    """GUI 项目导出入口(roadmap 0.7.0 事项 2):

    项目内容 → builder10 构造 ies.assembly 1.0.0 文档 → 与手写文件同一校验
    入口。datasets 为 int 视频索引的元信息{vid: {columns, column_units,
    resolution, sha256, media_type}};solver/generator 显式覆盖旧链推导。
    """
    built = build_assembly_doc_from_content(content, datasets=datasets, solver=solver, generator=generator)
    if built.doc is None:
        return AssemblyValidationResult(diagnostics=list(built.diagnostics), artifact=None)
    # builder 内部产生的阻断诊断合并
    return validate_assembly_doc(built.doc, datasets=_string_keyed_datasets(datasets))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _run_validation(
    doc: dict[str, Any],
    *,
    package_dir: str | Path | None,
    datasets: Mapping[str, Mapping] | None,
) -> AssemblyValidationResult:
    diags: list[Diagnostic] = []
    datasets_meta = dict(datasets or {})
    registry = _device_registry()

    # --- 阶段 2:模型与数据(严格精确版本) --------------------------------
    _check_devices(doc, registry, diags)
    _check_data_bindings(doc, datasets_meta, registry, diags)
    _check_required_parameters(doc, registry, diags)

    if _any_blocking(diags):
        return AssemblyValidationResult(diagnostics=diags, artifact=None)

    # --- 资源解析(相对路径 → 内容寻址) ---------------------------------
    resolved_doc, resource_digests, res_diags = _resolve_resources(doc, package_dir)
    diags.extend(res_diags)
    if _any_blocking(diags):
        return AssemblyValidationResult(diagnostics=diags, artifact=None)

    # --- 阶段 3:图与系统(端口 / 边 / 母线 / 约束) -----------------------
    phase3 = _phase3_graph_system(resolved_doc, datasets_meta, registry, diags)
    if _any_blocking(diags):
        return AssemblyValidationResult(diagnostics=diags, artifact=None)

    # --- 阶段 4:计算兼容 --------------------------------------------------
    _phase4_outputs(doc, diags)

    if _any_blocking(diags):
        return AssemblyValidationResult(diagnostics=diags, artifact=None)

    # --- 规范化与产物签发 --------------------------------------------------
    canonical_text, digest = canonicalize_assembly_doc(resolved_doc)
    dependency_lock = _dependency_lock(doc, registry)
    receipt = ValidationReceipt(
        assembly_sha256=digest,
        dependencies=dependency_lock,
        resources=resource_digests,
        diagnostics=tuple(d for d in diags if not d.blocking),
    )
    artifact = ValidatedAssemblyArtifact(
        canonical_text=canonical_text, assembly_sha256=digest, receipt=receipt
    )
    if not artifact.verify():
        # 三件套内部一致性失败(理论上不可达,触发即内部 bug)
        diag = make_diag(
            "ASM-ART-001",
            severity="error",
            blocking=True,
            params={"reason": "self_verify_failed"},
            location={"object_type": "assembly", "field": "artifact"},
        )
        diags.append(diag)
        return AssemblyValidationResult(diagnostics=diags, artifact=None)
    return AssemblyValidationResult(diagnostics=diags, artifact=artifact)


# ---------------------------------------------------------------------------
# 阶段 2:模型与数据
# ---------------------------------------------------------------------------


def _check_devices(doc: dict, registry, diags: list[Diagnostic]) -> None:
    for dev_id, dev in (doc.get("devices") or {}).items():
        if not isinstance(dev, Mapping):
            continue
        model = dev.get("model")
        type_id, version = _split_ref(model) if isinstance(model, str) else ("", None)
        if type_id not in registry:
            diags.append(
                make_diag(
                    ASM_REF_MODEL_UNREG,
                    severity="error",
                    blocking=True,
                    params={"device": dev_id, "model": model, "type_id": type_id},
                    location={"object_type": "device", "object_id": dev_id, "field": "model"},
                )
            )
            continue
        descriptor = registry[type_id]
        declared_version = descriptor.version
        if version != declared_version:
            # ies.assembly 1.0.0 强制精确版本:版本不一致阻断
            diags.append(
                make_diag(
                    ASM_REF_MODEL_UNREG,
                    severity="error",
                    blocking=True,
                    params={
                        "device": dev_id,
                        "model": model,
                        "type_id": type_id,
                        "registered": f"{type_id}@{declared_version}",
                        "reason": "version_mismatch",
                    },
                    location={"object_type": "device", "object_id": dev_id, "field": "model"},
                )
            )


def _check_required_parameters(doc: dict, registry, diags: list[Diagnostic]) -> None:
    for dev_id, dev in (doc.get("devices") or {}).items():
        if not isinstance(dev, Mapping):
            continue
        model = dev.get("model")
        type_id, _ = _split_ref(model) if isinstance(model, str) else ("", None)
        descriptor = registry.get(type_id)
        if descriptor is None:
            continue
        # 参数只允许已声明字段
        params_raw = dev.get("parameters") or {}
        for name, value in params_raw.items():
            if name not in descriptor.parameters:
                diags.append(
                    make_diag(
                        ASM_INPUT_UNDECLARED,
                        severity="error",
                        blocking=True,
                        params={"device": dev_id, "param": name},
                        location={"object_type": "device", "object_id": dev_id, "field": f"parameters.{name}"},
                    )
                )
                continue
            ps = descriptor.parameters[name]
            # 非有限值阻断(不依赖本地约定;宪法定量契约)
            if isinstance(value, float) and not math.isfinite(value):
                diags.append(
                    make_diag(
                        ASM_INPUT_RANGE,
                        severity="error",
                        blocking=True,
                        params={"device": dev_id, "param": name, "value": str(value), "reason": "non_finite"},
                        location={"object_type": "device", "object_id": dev_id, "field": f"parameters.{name}"},
                    )
                )
                continue
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, (int, float)):
                if ps.min is not None and float(value) < ps.min:
                    diags.append(
                        make_diag(
                            ASM_INPUT_RANGE,
                            severity="error",
                            blocking=False,
                            params={"device": dev_id, "param": name, "value": float(value), "min": ps.min},
                            location={"object_type": "device", "object_id": dev_id, "field": f"parameters.{name}"},
                        )
                    )
                elif ps.max is not None and float(value) > ps.max:
                    diags.append(
                        make_diag(
                            ASM_INPUT_RANGE,
                            severity="error",
                            blocking=False,
                            params={"device": dev_id, "param": name, "value": float(value), "max": ps.max},
                            location={"object_type": "device", "object_id": dev_id, "field": f"parameters.{name}"},
                        )
                    )
            if ps.enum is not None and value not in ps.enum:
                diags.append(
                    make_diag(
                        ASM_INPUT_RANGE,
                        severity="error",
                        blocking=False,
                        params={"device": dev_id, "param": name, "value": value, "enum": list(ps.enum)},
                        location={"object_type": "device", "object_id": dev_id, "field": f"parameters.{name}"},
                    )
                )
        # 必填参数非空(默认 None 或 load 类 reference 参数);
        # 负荷类设备的 reference 参数由 data 绑定承载(新格式按 data_inputs 列绑定)
        data_keys = set((dev.get("data") or {}).keys())
        has_data_binding = bool(data_keys)
        required_names = [
            name
            for name, ps in descriptor.parameters.items()
            if ps.default is None or (ps.unit == "reference" and descriptor.is_load)
        ]
        for name in required_names:
            if name in params_raw or name in data_keys:
                continue
            ps = descriptor.parameters[name]
            if ps.unit == "reference" and descriptor.is_load and has_data_binding:
                continue
            diags.append(
                make_diag(
                    ASM_INPUT_PARAM,
                    severity="error",
                    blocking=True,
                    params={"device": dev_id, "param": name},
                    location={"object_type": "device", "object_id": dev_id, "field": f"parameters.{name}"},
                )
            )
        # 负荷类设备必带 data
        if descriptor.is_load and not (dev.get("data") or {}):
            diags.append(
                make_diag(
                    ASM_INPUT_LOAD_DATA,
                    severity="error",
                    blocking=True,
                    params={"device": dev_id},
                    location={"object_type": "device", "object_id": dev_id, "field": "data"},
                )
            )


def _check_data_bindings(
    doc: dict,
    datasets_meta: Mapping[str, Mapping],
    registry,
    diags: list[Diagnostic],
) -> None:
    declared_datasets = set((doc.get("resources") or {}).get("datasets") or {})
    for dev_id, dev in (doc.get("devices") or {}).items():
        if not isinstance(dev, Mapping):
            continue
        data_block = dev.get("data") or {}
        for col_key, binding in data_block.items():
            if not isinstance(binding, Mapping):
                continue
            ds_id = binding.get("dataset")
            column = binding.get("column")
            if not isinstance(ds_id, str) or ds_id not in declared_datasets:
                diags.append(
                    make_diag(
                        ASM_REF_DATASET,
                        severity="error",
                        blocking=True,
                        params={
                            "device": dev_id,
                            "ref": str(col_key),
                            "reason": "dataset_not_in_resources",
                            "dataset_id": str(ds_id),
                        },
                        location={"object_type": "device", "object_id": dev_id, "field": f"data.{col_key}"},
                    )
                )
                continue
            if not isinstance(column, str) or not column:
                diags.append(
                    make_diag(
                        ASM_REF_DATASET,
                        severity="error",
                        blocking=True,
                        params={
                            "device": dev_id,
                            "ref": str(col_key),
                            "reason": "column_undeclared",
                            "dataset_id": str(ds_id),
                        },
                        location={"object_type": "device", "object_id": dev_id, "field": f"data.{col_key}.column"},
                    )
                )
                continue
            meta = datasets_meta.get(ds_id) if datasets_meta else None
            if isinstance(meta, Mapping) and meta:
                ds_cols = meta.get("columns") or []
                if isinstance(ds_cols, list) and ds_cols and column not in ds_cols:
                    diags.append(
                        make_diag(
                            ASM_REF_DATASET,
                            severity="error",
                            blocking=True,
                            params={
                                "device": dev_id,
                                "ref": str(col_key),
                                "reason": "column_not_in_dataset",
                                "dataset_id": str(ds_id),
                                "column": column,
                            },
                            location={"object_type": "device", "object_id": dev_id, "field": f"data.{col_key}.column"},
                        )
                    )
                ds_resolution = meta.get("resolution")
                if isinstance(ds_resolution, str) and ds_resolution:
                    axis_res = (doc.get("time_axis") or {}).get("resolution")
                    if isinstance(axis_res, str) and axis_res and ds_resolution != axis_res:
                        diags.append(
                            make_diag(
                                ASM_REF_DATASET,
                                severity="error",
                                blocking=True,
                                params={
                                    "device": dev_id,
                                    "ref": str(col_key),
                                    "reason": "resolution_mismatch",
                                    "declared": ds_resolution,
                                    "time_axis": axis_res,
                                },
                                location={"object_type": "device", "object_id": dev_id, "field": f"data.{col_key}"},
                            )
                        )
                # 单位量纲一致(数据列单位 vs 模型 data_inputs 列单位)
                if isinstance(meta.get("column_units"), Mapping):
                    declared_unit = str(meta["column_units"].get(column) or "")
                    if declared_unit:
                        from iesplan.assembly.checker import units_compatible

                        descriptor = _descriptor_for(registry, dev.get("model"))
                        if descriptor is not None:
                            target_unit = next(
                                (
                                    d.unit
                                    for d in data_inputs_from_descriptor(descriptor)
                                    if d.column_id == col_key
                                ),
                                "",
                            )
                            if target_unit and not units_compatible(declared_unit, target_unit):
                                diags.append(
                                    make_diag(
                                        ASM_INPUT_DATA_UNIT,
                                        severity="error",
                                        blocking=True,
                                        params={
                                            "device": dev_id,
                                            "ref": str(col_key),
                                            "unit": declared_unit,
                                            "expected": target_unit,
                                        },
                                        location={
                                            "object_type": "device",
                                            "object_id": dev_id,
                                            "field": f"data.{col_key}",
                                        },
                                    )
                                )


# ---------------------------------------------------------------------------
# 资源解析
# ---------------------------------------------------------------------------


def _resolve_resources(
    doc: dict,
    package_dir: str | Path | None,
) -> tuple[dict, dict, list[Diagnostic]]:
    """resources.datasets:relative_file → 内容寻址对象(object 形态)。

    返回(resolved_doc, resource_digests, diagnostics)。
    resource_digests = {dataset_id: {"sha256", "media_type"}} → 回执。
    失败 → diag ASM_RES_INVALID 阻断。
    """
    diags: list[Diagnostic] = []
    resources = doc.get("resources") or {}
    datasets = dict(resources.get("datasets") or {})
    resource_digests: dict[str, dict] = {}
    for ds_id, entry in datasets.items():
        if not isinstance(entry, Mapping):
            continue
        src = entry.get("source") or {}
        kind = src.get("kind")
        if kind == "object":
            sha = str(src.get("sha256") or "")
            media = str(src.get("media_type") or "")
            if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
                diags.append(
                    make_diag(
                        ASM_RES_INVALID,
                        severity="error",
                        blocking=True,
                        params={"reason": "invalid_sha256", "dataset_id": str(ds_id)},
                        location={"object_type": "assembly", "field": f"resources.datasets.{ds_id}.source.sha256"},
                    )
                )
                continue
            if str(src.get("object_id") or "") != f"sha256:{sha}":
                diags.append(
                    make_diag(
                        ASM_RES_INVALID,
                        severity="error",
                        blocking=True,
                        params={"reason": "object_id_mismatch", "dataset_id": str(ds_id)},
                        location={"object_type": "assembly", "field": f"resources.datasets.{ds_id}.source.object_id"},
                    )
                )
                continue
            entry["source"] = {
                "kind": "object",
                "object_id": f"sha256:{sha}",
                "sha256": sha,
                "media_type": media,
            }
            resource_digests[str(ds_id)] = {"sha256": sha, "media_type": media}
            continue
        if kind == "relative_file":
            if package_dir is None:
                diags.append(
                    make_diag(
                        ASM_RES_INVALID,
                        severity="error",
                        blocking=True,
                        params={"reason": "package_dir_required", "dataset_id": str(ds_id)},
                        location={"object_type": "assembly", "field": f"resources.datasets.{ds_id}.source"},
                    )
                )
                continue
            rel_path = src.get("path") or ""
            full_path = Path(package_dir) / rel_path
            try:
                data = full_path.read_bytes()
            except OSError as exc:
                diags.append(
                    make_diag(
                        ASM_RES_INVALID,
                        severity="error",
                        blocking=True,
                        params={
                            "reason": "file_unreadable",
                            "dataset_id": str(ds_id),
                            "path": str(full_path),
                            "detail": str(exc),
                        },
                        location={"object_type": "assembly", "field": f"resources.datasets.{ds_id}.source.path"},
                    )
                )
                continue
            sha = hashlib.sha256(data).hexdigest()
            media = _infer_media_type(rel_path)
            entry["source"] = {
                "kind": "object",
                "object_id": f"sha256:{sha}",
                "sha256": sha,
                "media_type": media,
            }
            resource_digests[str(ds_id)] = {"sha256": sha, "media_type": media}
            continue
        # 未知 kind 已被结构阶段拒绝,此处忽略
    out = dict(doc)
    out["resources"] = {"datasets": datasets}
    return out, resource_digests, diags


def _infer_media_type(path: str) -> str:
    lower = path.lower()
    if lower.endswith(".csv"):
        return "text/csv"
    if lower.endswith((".yaml", ".yml")):
        return "application/yaml"
    return "application/octet-stream"


# ---------------------------------------------------------------------------
# 阶段 3:图与系统
# ---------------------------------------------------------------------------


def _phase3_graph_system(
    resolved_doc: dict,
    datasets_meta: Mapping[str, Mapping],
    registry,
    diags: list[Diagnostic],
) -> dict:
    """复用 check_assembly 的核心机制:转换 doc → AssemblySpec + CheckContext,
    调用 run_phase_b + 自己的输入完备检查 + run_phase_d + run_constraint_checks。
    """
    from iesplan.assembly.checker import (
        CheckContext,
        ensure_ports,
        run_constraint_checks,
    )
    from iesplan.assembly.rules import run_phase_b, run_phase_d

    spec = _spec_from_doc(resolved_doc, datasets_meta)
    resolution = (resolved_doc.get("time_axis") or {}).get("resolution", "1h")
    ctx = CheckContext(registry=registry, time_axis={"resolution": resolution})
    # run_phase_b 内部 ensure_ports(spec, ctx)
    b_diags = run_phase_b(spec, ctx)
    diags.extend(b_diags)
    # 端口不可达(从 registry 推导 + 显式 data 列) — 未声明端口在本模块补报
    _check_undefined_ports(spec, ctx, diags)
    # 输入完备(端口方向 in 的设备无来边)
    _check_input_unfed(spec, ctx, diags)
    # 阶段 D:母线/可解性
    d_diags, _buses = run_phase_d(spec, ctx)
    diags.extend(d_diags)
    # 约束表达式
    c_diags = run_constraint_checks(spec, ctx)
    diags.extend(c_diags)
    return {}


def _spec_from_doc(doc: dict, datasets_meta: Mapping[str, Mapping]) -> AssemblySpec:
    devices: list[AssemblyDevice] = []
    for dev_id, dev in (doc.get("devices") or {}).items():
        data_block = dev.get("data") or {}
        data_refs: list[DataRef] = []
        for col_key, binding in data_block.items():
            ds_id = str((binding or {}).get("dataset") or "")
            column = str((binding or {}).get("column") or "")
            meta = datasets_meta.get(ds_id) if isinstance(datasets_meta, Mapping) else None
            units = meta.get("column_units") if isinstance(meta, Mapping) else None
            data_refs.append(
                DataRef(
                    key=str(col_key),
                    dataset_version_id=0,
                    dataset_name=ds_id,
                    columns=[column] if column else [],
                    unit=str(units.get(column) or "") if isinstance(units, Mapping) else "",
                    resolution=str(meta.get("resolution") or "") if isinstance(meta, Mapping) else "",
                )
            )
        devices.append(
            AssemblyDevice(
                id=str(dev_id),
                model=str(dev.get("model") or ""),
                params=dict(dev.get("parameters") or {}),
                data_refs=data_refs,
            )
        )
    edges: list[AssemblyEdge] = []
    for edge_id, conn in (doc.get("connections") or {}).items():
        if not isinstance(conn, Mapping):
            continue
        edges.append(
            AssemblyEdge(
                id=str(edge_id),
                from_port=str(conn.get("from") or ""),
                to_port=str(conn.get("to") or ""),
            )
        )
    constraints: list[AssemblyConstraint] = []
    for cid, c in (doc.get("constraints") or {}).items():
        if not isinstance(c, Mapping):
            continue
        constraints.append(
            AssemblyConstraint(
                id=str(cid),
                type=str(c.get("type") or "generic"),
                expr=str(c.get("expr") or ""),
                enabled=bool(c.get("enabled", True)),
            )
        )
    time_axis_raw = doc.get("time_axis") or {}
    return AssemblySpec(
        name=str((doc.get("assembly") or {}).get("name") or ""),
        time_axis=TimeAxisRef(
            resolution=str(time_axis_raw.get("resolution") or "1h"),
            start=str(time_axis_raw.get("start") or "2025-01-01T00:00:00Z"),
        ),
        devices=devices,
        edges=edges,
        constraints=constraints,
    )


def _check_undefined_ports(spec: AssemblySpec, ctx, diags: list[Diagnostic]) -> None:
    """补 ASM-REF-003:run_phase_b 跳过未定义端口,本模块独立报告。"""
    ports = ensure_ports_ctx(spec, ctx)
    for edge in spec.edges:
        for side, ref in (("from", edge.from_port), ("to", edge.to_port)):
            if ref and ref not in ports:
                diags.append(
                    make_diag(
                        "ASM-REF-003",
                        severity="error",
                        blocking=True,
                        params={"edge": edge.id, "side": side, "ref": ref},
                        location={"object_type": "edge", "object_id": edge.id, "field": side},
                    )
                )


def _check_input_unfed(spec: AssemblySpec, ctx, diags: list[Diagnostic]) -> None:
    ports = ensure_ports_ctx(spec, ctx)
    in_edges: dict[str, list[str]] = {}
    for edge in spec.edges:
        in_edges.setdefault(edge.to_port, []).append(edge.id)
    for port_ref, port in ports.items():
        if port.direction != "in" or port.carrier == "solar":
            continue
        if not in_edges.get(port_ref):
            dev_id, _, _ = port_ref.partition(".")
            diags.append(
                make_diag(
                    ASM_INPUT_UNFED,
                    severity="error",
                    blocking=True,
                    params={"device": dev_id, "port": port.name},
                    location={"object_type": "device", "object_id": dev_id, "field": port.name},
                )
            )


def ensure_ports_ctx(spec: AssemblySpec, ctx):
    from iesplan.assembly.checker import ensure_ports

    return ensure_ports(spec, ctx)


# ---------------------------------------------------------------------------
# 阶段 4:计算兼容
# ---------------------------------------------------------------------------


def _phase4_outputs(doc: dict, diags: list[Diagnostic]) -> None:
    devices = set((doc.get("devices") or {}).keys())
    outputs = doc.get("outputs") or {}
    for list_key in ("series", "metrics"):
        for i, ref in enumerate(outputs.get(list_key) or []):
            if not isinstance(ref, str):
                continue
            scope, _, _name = ref.partition(".")
            if not scope:
                diags.append(
                    make_diag(
                        ASM_OUTPUT_REF,
                        severity="error",
                        blocking=True,
                        params={"ref": str(ref), "output": list_key, "reason": "empty_scope"},
                        location={"object_type": "assembly", "field": f"outputs.{list_key}[{i}]"},
                    )
                )
                continue
            if scope != "system" and scope not in devices:
                diags.append(
                    make_diag(
                        ASM_OUTPUT_REF,
                        severity="error",
                        blocking=True,
                        params={"ref": str(ref), "output": list_key, "reason": "device_undefined", "scope": scope},
                        location={"object_type": "assembly", "field": f"outputs.{list_key}[{i}]"},
                    )
                )
    # options 标量(结构已校验,此处仅做非有限浮点检查)
    calc = doc.get("calculation") or {}
    options = calc.get("options") or {}
    for k, v in options.items():
        if isinstance(v, float) and not math.isfinite(v):
            diags.append(
                make_diag(
                    ASM_CALC_OPTIONS,
                    severity="error",
                    blocking=True,
                    params={"option": str(k), "reason": "non_finite", "value": str(v)},
                    location={"object_type": "assembly", "field": f"calculation.options.{k}"},
                )
            )


# ---------------------------------------------------------------------------
# 依赖锁与辅助
# ---------------------------------------------------------------------------


def _dependency_lock(doc: dict, registry) -> dict:
    devices_lock: dict[str, str] = {}
    model_commands: dict[str, str] = {}
    for dev_id, dev in (doc.get("devices") or {}).items():
        model = dev.get("model")
        if not isinstance(model, str):
            continue
        type_id, version = _split_ref(model)
        if not version:
            continue
        devices_lock[type_id] = version
        descriptor = registry.get(type_id)
        if descriptor is not None:
            for capability, ref in descriptor.model_commands.items():
                model_commands[str(capability)] = str(ref)
    calculation = doc.get("calculation") or {}
    return {
        "devices": dict(sorted(devices_lock.items())),
        "model_commands": dict(sorted(model_commands.items())),
        "calculation": {
            "mode": str(calculation.get("mode") or ""),
            "generator": str(calculation.get("generator") or ""),
            "solver": str(calculation.get("solver") or ""),
        },
    }


def _descriptor_for(registry, model: Any):
    if not isinstance(model, str):
        return None
    type_id, _ = _split_ref(model)
    return registry.get(type_id)


def _device_registry() -> dict:
    """已注册的设备描述符快照(从 devices 公开门面构建)。"""
    from iesplan.devices import list_device_descriptors

    return {desc.type_id: desc for desc in list_device_descriptors()}


def _split_ref(ref: str) -> tuple[str, str | None]:
    if "@" in ref:
        type_id, _, version = ref.rpartition("@")
        return type_id, version or None
    return ref, None


def _any_blocking(diags: list[Diagnostic]) -> bool:
    return any(d.blocking for d in diags)


def _string_keyed_datasets(datasets: Mapping[int, Mapping] | None) -> dict[str, Mapping]:
    if not datasets:
        return {}
    return {f"ds{int(vid)}": meta for vid, meta in datasets.items()}


__all__ = [
    "AssemblyValidationResult",
    "validate_assembly_text",
    "validate_assembly_doc",
    "validate_project_export",
    "AssemblyValidationError",
]