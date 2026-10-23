"""ies.assembly 3.0.0 统一校验入口(单管线:一次安全解析 + 一次结构/领域校验)。

手写装配 YAML(text)与 GUI 项目导出(content)进入同一校验入口,
成功只签发由规范文本与校验回执组成的不可变二件套
``ValidatedAssemblyArtifact``;校验失败不产生可执行产物,返回结构化
诊断列表。装配不包含 ``calculation``(计算配置在计算包生成时固定)。

阶段:
1. 结构(仅 ``validate_assembly_doc`` 内调用 ``run_structure_checks`` 一次):
   schema/schema_version、顶层节(含项目计算基线)、字段类型、ID/版本/
   引用形状、禁止字段、资源路径、outputs/refs、extensions 命名空间;
2. 模型与数据:设备模型精确版本注册、参数只允许已声明字段、必填参数非空、
   数据绑定(dataset/column/单位/分辨率)、负荷类设备必带 data、相对资源解析;
3. 图与系统:复用 iesplan.assembly.context 端口推导 + rules.connection
   (run_phase_b) + rules.solvability (run_phase_d);端口不可达与悬空输入
   由本模块独立补充;年步数由项目计算基线显式推导后传入上下文;
4. 输出引用 + 财务完整性:outputs 引用存在；finance 精确引用(对象注入
   或包内文件经 finance 公开边界解析)；finance_binding 实例全覆盖；
   tariff_bindings 计费绑定；planning 目标/变量/约束(公共表达式引擎)。

输出格式:
  AssemblyValidationResult(diagnostics, artifact) — artifact 为 None 时校验失败。

模块边界:
- 跨模块仅消费 devices 公开门面(get_device/list_devices);
- 复用 assembly 域内 context/rules/canonicalizer/parser/contracts;
- 不导入 services/ORM/存储私有路径。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from iesplan.assembly.builder import build_assembly_doc_from_content
from iesplan.assembly.canonicalizer import canonicalize_assembly_doc
from iesplan.assembly.context import units_compatible
from iesplan.assembly.contracts import (
    AssemblyValidationError,
    ValidatedAssemblyArtifact,
    ValidationReceipt,
)
from iesplan.assembly.diags import (
    ASM_BIND_INVALID,
    ASM_EDGE_BAD_ENDPOINT,
    ASM_INPUT_PROP_UNIT,
    ASM_INPUT_RANGE,
    ASM_INPUT_UNDECLARED,
    ASM_INPUT_UNFED,
    ASM_OUTPUT_REF,
    ASM_REF_MODEL_UNREG,
    ASM_RES_INVALID,
)
from iesplan.assembly.diags import make_asm_diag as make_diag
from iesplan.assembly.parser import parse_assembly_doc, run_structure_checks
from iesplan.assembly.schema import (
    AssemblyDevice,
    AssemblyEdge,
    AssemblySpec,
)
from iesplan.core.diagnostics import Diagnostic
from iesplan.finance import EffectiveFinanceConfig


@dataclass(slots=True)
class AssemblyValidationResult:
    diagnostics: list[Diagnostic]
    artifact: ValidatedAssemblyArtifact | None
    #: 本次校验实际解析的设备文档(``type_id → DeviceModelDocument``):
    #: 与各校验阶段共用同一次 ``_device_registry()`` 结果, 失败时为空。
    #: 调用方据此冻结逐实例数学贡献, 不得另行二次解析当前目录。
    device_documents: Mapping[str, Any] = field(default_factory=dict)

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
    dataset_contents: Mapping[str, Mapping[str, Any]] | None = None,
    finance_snapshots: Mapping[str, EffectiveFinanceConfig] | None = None,
    device_documents: Mapping[str, Any] | None = None,
) -> AssemblyValidationResult:
    """手写装配 YAML 文本 → 单管线校验(安全解析一次 + 结构/领域校验一次)。

    dataset_contents 为显式注入的预定义文件内容 {path: {object_id,
    media_type, data}}，供包外调用方(项目导出)提供对象存储字节；
    包内相对路径缺省从 package_dir 读取。finance_snapshots 为显式注入
    的有效财务快照 {object_id: EffectiveFinanceConfig}，供 object 引用解析。
    """
    parsed = parse_assembly_doc(text, source_name=source_name)
    if parsed.doc is None:
        return AssemblyValidationResult(diagnostics=list(parsed.diagnostics), artifact=None)
    return validate_assembly_doc(
        parsed.doc,
        package_dir=package_dir,
        dataset_contents=dataset_contents,
        finance_snapshots=finance_snapshots,
        device_documents=device_documents,
    )


def validate_assembly_doc(
    doc: Mapping[str, Any],
    *,
    package_dir: str | Path | None = None,
    dataset_contents: Mapping[str, Mapping[str, Any]] | None = None,
    finance_snapshots: Mapping[str, EffectiveFinanceConfig] | None = None,
    operation_years: int | None = None,
    device_documents: Mapping[str, Any] | None = None,
) -> AssemblyValidationResult:
    """ies.assembly 3.0.0 文档 → 单管线校验(结构一次 + 领域阶段)。

    doc 为安全解析后的 plain dict(手写文本经 ``parse_assembly_doc`` 产生,
    GUI 导出经构造器产生);本入口做唯一一次结构校验,随后进入模型/
    属性/预定义来源 + 图/系统 + 输出引用/财务完整性领域校验。手写与 GUI
    共用同一管线,不重复结构校验。``operation_years`` 透传提交时已确认
    运营年限(仅提交闸门传入，见 ``validate_project_export``)。
    """
    diags: list[Diagnostic] = []
    tree = run_structure_checks(dict(doc), source_name="<doc>", diags=diags)
    if tree is None:
        return AssemblyValidationResult(diagnostics=diags, artifact=None)
    return _run_validation(
        tree,
        package_dir=package_dir,
        dataset_contents=dataset_contents,
        finance_snapshots=finance_snapshots,
        operation_years=operation_years,
        device_documents=device_documents,
    )


def validate_project_export(
    content: Mapping,
    *,
    dataset_contents: Mapping[str, Mapping[str, Any]] | None = None,
    finance: Mapping[str, Any] | None = None,
    finance_binding: Mapping[str, Any] | None = None,
    tariff_bindings: Mapping[str, Any] | None = None,
    planning: Mapping[str, Any] | None = None,
    finance_snapshots: Mapping[str, EffectiveFinanceConfig] | None = None,
    operation_years: int | None = None,
    device_documents: Mapping[str, Any] | None = None,
    device_data_bindings: Mapping[str, list[dict]] | None = None,
) -> AssemblyValidationResult:
    """GUI 项目导出入口:

    项目内容 → 构造器构造 ies.assembly 3.0.0 文档 → 与手写文件同一校验
    入口(结构一次 + 领域阶段)。dataset_contents 为合成路径到对象字节的
    显式注入(供文档内 data 绑定消费)；finance /
    finance_binding / tariff_bindings / planning 为调用方显式提供的财务
    引用、成本绑定、计费绑定与规划(缺席则对应装配节缺失阻断，不猜测)；
    finance_snapshots 为密封快照显式注入(kind=object 用)。设备数据序列
    来源声明由独立入口提供(S2c)，构造器不做数据集列映射。
    ``device_documents`` 为调用方显式提供的本项目已保存模型文档，
    构造器与校验器与公开目录合并消费（同 ID 以显式文档为准），本入口不访问
    DB/storage。
    ``operation_years`` 为提交时已确认运营年限(仅提交闸门传入)：
    ``data_full`` 文件长度须等于其推导的运营期总点数；缺席时不核对
    运营期长度(项目导出等非提交调用不受影响)。
    """
    built = build_assembly_doc_from_content(
        content,
        finance=finance,
        finance_binding=finance_binding,
        tariff_bindings=tariff_bindings,
        planning=planning,
        device_documents=device_documents,
        device_data_bindings=device_data_bindings,
    )
    if built.doc is None:
        return AssemblyValidationResult(diagnostics=list(built.diagnostics), artifact=None)
    # builder 内部产生的阻断诊断合并
    return validate_assembly_doc(
        built.doc,
        dataset_contents=dataset_contents,
        finance_snapshots=finance_snapshots,
        operation_years=operation_years,
        device_documents=device_documents,
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _run_validation(
    doc: dict[str, Any],
    *,
    package_dir: str | Path | None,
    dataset_contents: Mapping[str, Mapping[str, Any]] | None,
    finance_snapshots: Mapping[str, EffectiveFinanceConfig] | None,
    operation_years: int | None = None,
    device_documents: Mapping[str, Any] | None = None,
) -> AssemblyValidationResult:
    from iesplan.assembly.rules.finance import (
        check_finance_binding,
        resolve_finance_snapshot,
    )
    from iesplan.assembly.rules.planning import check_planning
    from iesplan.assembly.rules.tariff import check_tariff_bindings

    diags: list[Diagnostic] = []
    registry = _device_registry(device_documents)

    # --- 阶段 2:模型 / 属性 / 预定义来源 --------------------------------
    _check_devices(doc, registry, diags)
    _check_properties(doc, registry, diags)
    resource_media = _check_predefined(
        doc, registry, package_dir, dataset_contents, diags,
        operation_years=operation_years,
    )

    if _any_blocking(diags):
        return AssemblyValidationResult(diagnostics=diags, artifact=None)

    # --- 阶段 3:图与系统(端口 / 边 / 母线 / 约束) -----------------------
    _check_connection_endpoints(doc, registry, diags)
    if _any_blocking(diags):
        return AssemblyValidationResult(diagnostics=diags, artifact=None)
    _phase3_graph_system(doc, registry, diags)
    if _any_blocking(diags):
        return AssemblyValidationResult(diagnostics=diags, artifact=None)

    # --- 阶段 4:输出引用 + 财务完整性 ------------------------------------
    _phase4_outputs(doc, diags)
    if _any_blocking(diags):
        return AssemblyValidationResult(diagnostics=diags, artifact=None)
    snapshot = resolve_finance_snapshot(doc, package_dir, finance_snapshots, diags)
    if _any_blocking(diags):
        return AssemblyValidationResult(diagnostics=diags, artifact=None)
    assert snapshot is not None
    baseline = doc.get("project_baseline") or {}
    resolution = baseline.get("resolution")
    check_tariff_bindings(
        doc, snapshot, registry,
        baseline_resolution=resolution,
        diags=diags,
    )
    if _any_blocking(diags):
        return AssemblyValidationResult(diagnostics=diags, artifact=None)
    check_finance_binding(doc, snapshot, registry, diags)
    if _any_blocking(diags):
        return AssemblyValidationResult(diagnostics=diags, artifact=None)
    check_planning(doc, snapshot, registry, diags)

    if _any_blocking(diags):
        return AssemblyValidationResult(diagnostics=diags, artifact=None)

    # --- 规范化与产物签发 --------------------------------------------------
    canonical_text = canonicalize_assembly_doc(doc)
    dependency_lock = _dependency_lock(doc, registry)
    finance_section = doc.get("finance") or {}
    finance_ref = finance_section.get("ref") or {}
    planning = doc.get("planning") or {}
    objective = planning.get("objective") or {}
    receipt = ValidationReceipt(
        dependencies=dependency_lock,
        resources=resource_media,
        finance_snapshot={
            "ref": {
                "kind": finance_ref.get("kind"),
                "object_id": finance_ref.get("object_id"),
                "media_type": finance_ref.get("media_type"),
                "path": finance_ref.get("path"),
            },
            "profile_id": snapshot.profile_id,
            "revision": finance_section.get("revision"),
            "schema": snapshot.schema,
            "schema_version": snapshot.schema_version,
            "currency": snapshot.currency,
        },
        planning={
            "objective_sense": objective.get("sense"),
            "variables": sorted(planning.get("variables") or {}),
            "constraints": sorted(planning.get("constraints") or {}),
        },
        diagnostics=tuple(d for d in diags if not d.blocking),
    )
    artifact = ValidatedAssemblyArtifact(
        canonical_text=canonical_text, receipt=receipt
    )
    return AssemblyValidationResult(
        diagnostics=diags, artifact=artifact, device_documents=registry
    )


# ---------------------------------------------------------------------------
# 阶段 2:模型与数据
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 阶段 2:模型 / 属性 / 预定义来源
# ---------------------------------------------------------------------------


def _definition_id(dev: Mapping) -> str:
    """实例 definition.id(裸稳定设备 ID；结构阶段已拒绝 @钉选)。"""
    definition = dev.get("definition")
    if isinstance(definition, Mapping):
        raw = definition.get("id")
        if isinstance(raw, str):
            return raw
    return ""


def _check_devices(doc: dict, registry, diags: list[Diagnostic]) -> None:
    for dev_id, dev in (doc.get("devices") or {}).items():
        if not isinstance(dev, Mapping):
            continue
        type_id = _definition_id(dev)
        if type_id not in registry:
            diags.append(
                make_diag(
                    ASM_REF_MODEL_UNREG,
                    severity="error",
                    blocking=True,
                    params={"device": dev_id, "definition": type_id},
                    location={"object_type": "device", "object_id": dev_id, "field": "definition.id"},
                )
            )


def _check_properties(doc: dict, registry, diags: list[Diagnostic]) -> None:
    """properties: 只允许已声明字段；数值有限；单位与模型量纲兼容；值域越界非阻断。"""
    for dev_id, dev in (doc.get("devices") or {}).items():
        if not isinstance(dev, Mapping):
            continue
        descriptor = registry.get(_definition_id(dev))
        if descriptor is None:
            continue
        props = dev.get("properties") or {}
        for name, entry in props.items():
            if name not in descriptor.properties:
                diags.append(
                    make_diag(
                        ASM_INPUT_UNDECLARED,
                        severity="error",
                        blocking=True,
                        params={"device": dev_id, "param": name},
                        location={
                            "object_type": "device",
                            "object_id": dev_id,
                            "field": f"properties.{name}",
                        },
                    )
                )
                continue
            ps = descriptor.properties[name]
            value = entry.get("value") if isinstance(entry, Mapping) else None
            unit = entry.get("unit") if isinstance(entry, Mapping) else ""
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if not math.isfinite(float(value)):
                    diags.append(
                        make_diag(
                            ASM_INPUT_RANGE,
                            severity="error",
                            blocking=True,
                            params={
                                "device": dev_id,
                                "param": name,
                                "value": str(value),
                                "reason": "non_finite",
                            },
                            location={
                                "object_type": "device",
                                "object_id": dev_id,
                                "field": f"properties.{name}.value",
                            },
                        )
                    )
                    continue
                if ps.minimum is not None and float(value) < ps.minimum:
                    diags.append(
                        make_diag(
                            ASM_INPUT_RANGE,
                            severity="error",
                            blocking=False,
                            params={"device": dev_id, "param": name, "value": float(value), "min": ps.minimum},
                            location={
                                "object_type": "device",
                                "object_id": dev_id,
                                "field": f"properties.{name}.value",
                            },
                        )
                    )
                elif ps.maximum is not None and float(value) > ps.maximum:
                    diags.append(
                        make_diag(
                            ASM_INPUT_RANGE,
                            severity="error",
                            blocking=False,
                            params={"device": dev_id, "param": name, "value": float(value), "max": ps.maximum},
                            location={
                                "object_type": "device",
                                "object_id": dev_id,
                                "field": f"properties.{name}.value",
                            },
                        )
                    )
            if isinstance(unit, str) and unit and not units_compatible(unit, ps.unit):
                diags.append(
                    make_diag(
                        ASM_INPUT_PROP_UNIT,
                        severity="error",
                        blocking=True,
                        params={"device": dev_id, "param": name, "unit": unit, "expected": ps.unit},
                        location={
                            "object_type": "device",
                            "object_id": dev_id,
                            "field": f"properties.{name}.unit",
                        },
                    )
                )


def _check_predefined(
    doc: dict,
    registry,
    package_dir: str | Path | None,
    dataset_contents: Mapping[str, Mapping[str, Any]] | None,
    diags: list[Diagnostic],
    *,
    operation_years: int | None = None,
) -> dict[str, dict]:
    """预定义来源声明: 槽覆盖 + 槽类型 + 文件内容确认。

    每个 predefined 接口必须恰好有一个显式绑定，缺失/多余/绑到非
    predefined 接口均阻断；不得回退到设备文件自带来源。文件绑定读取
    字节并按绑定上下文确认(device_id/mode/分辨率/step/列/单位/值域)。
    ``data_full`` 文件长度须等于已确认运营年限推导的运营期总点数
    （``operation_years`` 仅提交闸门传入；缺席时不核对运营期长度）。
    返回资源记录 {path: {"media_type"}}；文件绑定的 content 身份就地
    写入绑定映射(规范文本保留 path 与 content)。
    """
    resources: dict[str, dict] = {}
    contents = dict(dataset_contents or {})
    baseline = doc.get("project_baseline") or {}
    resolution = baseline.get("resolution")
    start_year = baseline.get("start_year")
    for dev_id, dev in (doc.get("devices") or {}).items():
        if not isinstance(dev, Mapping):
            continue
        type_id = _definition_id(dev)
        descriptor = registry.get(type_id)
        if descriptor is None:
            continue
        bindings = dev.get("predefined_interfaces") or {}
        for iface_id, binding in bindings.items():
            iface = descriptor.interfaces.get(iface_id)
            if iface is None:
                diags.append(
                    make_diag(
                        ASM_BIND_INVALID,
                        severity="error",
                        blocking=True,
                        params={"device": dev_id, "interface": str(iface_id), "reason": "unknown_interface"},
                        location={
                            "object_type": "device",
                            "object_id": dev_id,
                            "field": f"predefined_interfaces.{iface_id}",
                        },
                    )
                )
                continue
            if iface.type != "predefined":
                diags.append(
                    make_diag(
                        ASM_BIND_INVALID,
                        severity="error",
                        blocking=True,
                        params={
                            "device": dev_id,
                            "interface": iface_id,
                            "reason": "not_predefined_slot",
                            "actual_type": iface.type,
                        },
                        location={
                            "object_type": "device",
                            "object_id": dev_id,
                            "field": f"predefined_interfaces.{iface_id}",
                        },
                    )
                )
                continue
            if not isinstance(binding, Mapping):
                continue
            # 绑定形态(mode 枚举/键集合/target_type 非空)已由结构阶段严格
            # 判别联合阻断，此处直接使用，不重复设防。
            mode = binding.get("mode")
            if mode == "constant":
                continue
            path = binding.get("path")
            payload = contents.get(path) if isinstance(path, str) else None
            if payload is None and isinstance(path, str) and package_dir is not None:
                payload = _read_package_csv(package_dir, path, dev_id, iface_id, diags)
            if payload is None:
                diags.append(
                    make_diag(
                        ASM_RES_INVALID,
                        severity="error",
                        blocking=True,
                        params={
                            "reason": "content_unavailable",
                            "device": dev_id,
                            "interface": iface_id,
                            "path": str(path),
                        },
                        location={
                            "object_type": "device",
                            "object_id": dev_id,
                            "field": f"predefined_interfaces.{iface_id}.path",
                        },
                    )
                )
                continue
            if _check_csv_binding(
                dev_id, type_id, iface_id, iface, binding, payload, resolution, start_year, diags,
                operation_years=operation_years,
            ):
                object_id = payload.get("object_id", "")
                media_type = payload.get("media_type", "") or "text/csv"
                binding["content"] = {"object_id": object_id, "media_type": media_type}
                resources[str(path)] = {"media_type": media_type}
        for iface_id, iface in descriptor.interfaces.items():
            if iface.type == "predefined" and iface_id not in bindings:
                diags.append(
                    make_diag(
                        ASM_BIND_INVALID,
                        severity="error",
                        blocking=True,
                        params={"device": dev_id, "interface": iface_id, "reason": "missing_binding"},
                        location={
                            "object_type": "device",
                            "object_id": dev_id,
                            "field": f"predefined_interfaces.{iface_id}",
                        },
                    )
                )
    return resources


def _read_package_csv(
    package_dir: str | Path,
    path: str,
    dev_id: object,
    iface_id: object,
    diags: list[Diagnostic],
) -> Mapping[str, Any] | None:
    """包内相对 CSV 读取为 {object_id, media_type, data}；不可读返回 None(已记诊断)。"""
    full_path = Path(package_dir) / path
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
                    "device": str(dev_id),
                    "interface": str(iface_id),
                    "path": str(full_path),
                    "detail": str(exc),
                },
                location={
                    "object_type": "device",
                    "object_id": str(dev_id),
                    "field": f"predefined_interfaces.{iface_id}.path",
                },
            )
        )
        return None
    return {"object_id": f"file:{path}", "media_type": "text/csv", "data": data}


def _bind_fail(
    diags: list[Diagnostic],
    dev_id: str,
    iface_id: str,
    reason: str,
    extra: dict | None = None,
) -> None:
    params = {"device": dev_id, "interface": iface_id, "reason": reason}
    if extra:
        params.update(extra)
    diags.append(
        make_diag(
            ASM_BIND_INVALID,
            severity="error",
            blocking=True,
            params=params,
            location={
                "object_type": "device",
                "object_id": dev_id,
                "field": f"predefined_interfaces.{iface_id}",
            },
        )
    )


def _check_csv_binding(
    dev_id: str,
    type_id: str,
    iface_id: str,
    iface,
    binding: Mapping,
    payload: Mapping[str, Any],
    resolution: object,
    start_year: object,
    diags: list[Diagnostic],
    *,
    operation_years: int | None = None,
) -> bool:
    """绑定 CSV 内容确认(device_id/mode/分辨率/step/列/单位/值域)。"""
    from iesplan.core.contracts import horizon_point_count
    from iesplan.devices import parse_data_file_v2, repeat_cycle_lengths

    data = payload.get("data")
    if not isinstance(data, (bytes, bytearray)):
        _bind_fail(diags, dev_id, iface_id, "content_unavailable")
        return False
    parsed, parse_diags = parse_data_file_v2(bytes(data))
    diags.extend(parse_diags)
    if parsed is None or any(d.blocking for d in parse_diags):
        return False
    meta = parsed.meta
    mode = binding.get("mode")
    if meta.device_id != type_id:
        _bind_fail(
            diags, dev_id, iface_id, "csv_device_mismatch",
            {"expected": type_id, "actual": meta.device_id},
        )
        return False
    if meta.source_mode != mode:
        _bind_fail(
            diags, dev_id, iface_id, "csv_mode_mismatch",
            {"expected": mode, "actual": meta.source_mode},
        )
        return False
    if meta.resolution != resolution:
        _bind_fail(
            diags, dev_id, iface_id, "csv_resolution_mismatch",
            {"expected": resolution, "actual": meta.resolution},
        )
        return False
    header = [str(cell) for cell in parsed.header]
    col = iface_id.lower()
    if col not in header:
        _bind_fail(
            diags, dev_id, iface_id, "csv_column_missing",
            {"column": iface_id, "header": header},
        )
        return False
    col_idx = header.index(col)
    steps: list[int] = []
    for row_no, row in enumerate(parsed.rows):
        cell = row[0].strip() if len(row) > 0 else ""
        try:
            step = int(cell)
        except (TypeError, ValueError):
            _bind_fail(
                diags, dev_id, iface_id, "csv_step_gap",
                {"row": row_no + 1, "value": cell},
            )
            return False
        if step != len(steps):
            _bind_fail(
                diags, dev_id, iface_id, "csv_step_gap",
                {"row": row_no + 1, "expected": len(steps), "actual": step},
            )
            return False
        steps.append(step)
        if col_idx >= len(row):
            _bind_fail(diags, dev_id, iface_id, "csv_empty_value", {"row": row_no + 1})
            return False
        raw_val = row[col_idx].strip()
        if not raw_val:
            _bind_fail(diags, dev_id, iface_id, "csv_empty_value", {"row": row_no + 1})
            return False
        try:
            number = float(raw_val)
        except ValueError:
            _bind_fail(
                diags, dev_id, iface_id, "csv_bad_number",
                {"row": row_no + 1, "value": raw_val},
            )
            return False
        if not math.isfinite(number):
            _bind_fail(
                diags, dev_id, iface_id, "csv_bad_number",
                {"row": row_no + 1, "value": raw_val, "detail": "non_finite"},
            )
            return False
        minimum, maximum = iface.valid_range or (None, None)
        if (minimum is not None and number < minimum) or (maximum is not None and number > maximum):
            _bind_fail(
                diags, dev_id, iface_id, "csv_out_of_range",
                {"row": row_no + 1, "value": number, "min": minimum, "max": maximum},
            )
            return False
    unit = meta.units.get(iface_id, meta.units.get(col, ""))
    if not unit:
        _bind_fail(diags, dev_id, iface_id, "csv_unit_missing", {"column": iface_id})
        return False
    if not units_compatible(unit, iface.unit):
        _bind_fail(
            diags, dev_id, iface_id, "csv_unit_mismatch",
            {"unit": unit, "expected": iface.unit},
        )
        return False
    if mode == "data_repeat":
        day, week, year = repeat_cycle_lengths(str(resolution), start_year=int(start_year))
        if len(steps) not in (day, week, year):
            _bind_fail(
                diags, dev_id, iface_id, "csv_repeat_length",
                {"actual": len(steps), "expected": [day, week, year]},
            )
            return False
    if mode == "data_full" and operation_years is not None:
        expected = horizon_point_count(
            str(resolution), int(start_year), operation_years
        )
        if len(steps) != expected:
            _bind_fail(
                diags, dev_id, iface_id, "csv_full_length",
                {"actual": len(steps), "expected": expected},
            )
            return False
    return True


# ---------------------------------------------------------------------------
# 阶段 3:图与系统
# ---------------------------------------------------------------------------


def _check_connection_endpoints(doc: dict, registry, diags: list[Diagnostic]) -> None:
    """connections 端点类型: predefined/blind 永不进入 connections。"""
    dev_type: dict[str, str] = {}
    for dev_id, dev in (doc.get("devices") or {}).items():
        if isinstance(dev, Mapping):
            dev_type[str(dev_id)] = _definition_id(dev)
    for edge_id, conn in (doc.get("connections") or {}).items():
        if not isinstance(conn, Mapping):
            continue
        for side in ("from", "to"):
            ref = conn.get(side)
            if not isinstance(ref, str) or "." not in ref:
                continue
            dev_id, _, iface_id = ref.partition(".")
            descriptor = registry.get(dev_type.get(dev_id, ""))
            if descriptor is None:
                continue
            iface = descriptor.interfaces.get(iface_id)
            if iface is not None and iface.type in ("predefined", "blind"):
                diags.append(
                    make_diag(
                        ASM_EDGE_BAD_ENDPOINT,
                        severity="error",
                        blocking=True,
                        params={"edge": edge_id, "side": side, "ref": ref, "type": iface.type},
                        location={
                            "object_type": "edge",
                            "object_id": edge_id,
                            "field": side,
                        },
                    )
                )


def _phase3_graph_system(
    doc: dict,
    registry,
    diags: list[Diagnostic],
) -> dict:
    """转换 doc → AssemblySpec + CheckContext,调用 run_phase_b +
    输入完备检查 + run_phase_d。年步数由项目计算基线显式推导后传入。
    """
    from iesplan.assembly.context import CheckContext, steps_for_year
    from iesplan.assembly.rules import run_phase_b, run_phase_d

    spec = _spec_from_doc(doc)
    baseline = doc.get("project_baseline") or {}
    resolution = baseline.get("resolution")
    start_year = baseline.get("start_year")
    ctx = CheckContext(
        registry=registry,
        baseline_resolution=resolution,
        period_steps=steps_for_year(resolution, start_year),
    )
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
    return {}


def _spec_from_doc(doc: dict) -> AssemblySpec:
    devices: list[AssemblyDevice] = []
    for dev_id, dev in (doc.get("devices") or {}).items():
        if not isinstance(dev, Mapping):
            continue
        properties = dev.get("properties") or {}
        predefined = dev.get("predefined_interfaces") or {}
        devices.append(
            AssemblyDevice(
                id=str(dev_id),
                definition_id=_definition_id(dev),
                kind=str(dev.get("asset_origin") or "existing"),
                properties={str(k): dict(v) for k, v in properties.items() if isinstance(v, Mapping)},
                predefined={str(k): dict(v) for k, v in predefined.items() if isinstance(v, Mapping)},
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
    return AssemblySpec(
        name=str((doc.get("assembly") or {}).get("name") or ""),
        devices=devices,
        edges=edges,
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
    from iesplan.assembly.context import (
        EXOGENOUS_SUPPLY_CARRIERS,
        GRID_SIDE_PORTS,
        grid_side_used,
        resolve_model,
    )

    ports = ensure_ports_ctx(spec, ctx)
    in_edges: dict[str, list[str]] = {}
    for edge in spec.edges:
        in_edges.setdefault(edge.to_port, []).append(edge.id)
    model_of = {d.id: d.definition_id for d in spec.devices}
    grid_used_cache: dict[str, bool] = {}
    for port_ref, port in ports.items():
        if port.direction != "in" or port.carrier in EXOGENOUS_SUPPLY_CARRIERS:
            continue
        if port.name in GRID_SIDE_PORTS:
            if port.device not in grid_used_cache:
                type_spec = resolve_model(ctx, model_of.get(port.device, ""))
                grid_used_cache[port.device] = grid_side_used(type_spec, port.device, spec.edges)
            if grid_used_cache[port.device]:
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
    from iesplan.assembly.context import ensure_ports

    return ensure_ports(spec, ctx)


# ---------------------------------------------------------------------------
# 阶段 4:输出引用
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
                        params={
                            "ref": str(ref),
                            "output": list_key,
                            "reason": "device_undefined",
                            "scope": scope,
                        },
                        location={"object_type": "assembly", "field": f"outputs.{list_key}[{i}]"},
                    )
                )


# ---------------------------------------------------------------------------
# 依赖锁与辅助
# ---------------------------------------------------------------------------


def _dependency_lock(doc: dict, registry) -> dict:
    """依赖锁定: 设备内容锁 + 项目基线 + 方程契约身份。

    方程契约版本取 modeling 公开门面(本回执固定的方程语义版本)。
    """
    from iesplan.modeling import (
        CONTRACT_SCHEMA_ID,
        CONTRACT_SCHEMA_VERSION,
        EQUATION_AST_ID,
        EQUATION_AST_VERSION,
    )

    devices_lock: dict[str, str] = {}
    for dev in (doc.get("devices") or {}).values():
        if not isinstance(dev, Mapping):
            continue
        type_id = _definition_id(dev)
        descriptor = registry.get(type_id)
        if descriptor is not None:
            devices_lock[type_id] = descriptor.schema_version
    baseline = doc.get("project_baseline") or {}
    return {
        "devices": dict(sorted(devices_lock.items())),
        "baseline": {
            "resolution": baseline.get("resolution"),
            "start_year": baseline.get("start_year"),
            "scenario_mode": baseline.get("scenario_mode"),
        },
        "equations": {
            "contract": f"{CONTRACT_SCHEMA_ID}@{CONTRACT_SCHEMA_VERSION}",
            "ast": f"{EQUATION_AST_ID}@{EQUATION_AST_VERSION}",
        },
    }


def _device_registry(device_documents: Mapping[str, Any] | None = None) -> dict:
    """设备描述符快照：公开目录 ∪ 调用方显式提供的项目模型（同 ID 显式优先）。"""
    from iesplan.devices import list_devices

    registry = {
        desc.device.id: desc for desc in list_devices() if desc.device is not None
    }
    if device_documents is not None:
        registry.update(device_documents)
    return registry


def _any_blocking(diags: list[Diagnostic]) -> bool:
    return any(d.blocking for d in diags)


__all__ = [
    "AssemblyValidationResult",
    "validate_assembly_text",
    "validate_assembly_doc",
    "validate_project_export",
    "AssemblyValidationError",
]
