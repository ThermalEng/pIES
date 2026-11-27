"""本地生态模型与插件集成辅助：选择器、配置校验、提交锁互斥及容器阶段解析。

backend 进程永不 import 用户 Python 源码；仅解析冻结或已批准的元数据，
通过统一契约（DeviceModelDocument / StagePluginSpec / BuiltinStageDescriptor）参与校验与容器执行。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from iesplan import ecosystem as ecosystem_domain
from iesplan.computation.config import (
    AlgorithmDescriptor,
    AlgorithmRegistry,
    EvaluatorRegistry,
    SolverRegistry,
    build_algorithm_registry,
    build_evaluator_registry,
    build_solver_registry,
    check_parameters_against_schema,
)
from iesplan.computation.contracts.compatibility import (
    PLUGIN_KINDS as COMPAT_PLUGIN_KINDS,
)
from iesplan.computation.contracts.compatibility import (
    StagePluginSpec,
)
from iesplan.computation.generators.descriptor import GeneratorDescriptor
from iesplan.computation.generators.registry import GeneratorRegistry, build_registry
from iesplan.computation.predictors import (
    PredictorDescriptor,
    PredictorRegistry,
    build_predictor_registry,
)
from iesplan.computation.runtime.stage_runner import BuiltinStageDescriptor
from iesplan.devices import DeviceModelDocument, from_dict
from iesplan.ecosystem import PLUGIN_KINDS, LocalArtifactRecord
from iesplan.storage import get_object
from iesplan.tasks import InvalidRequestError


@dataclass(frozen=True, slots=True)
class _MetadataPredictor:
    descriptor: PredictorDescriptor
    stage_spec: StagePluginSpec

    def validate_parameters(self, parameters: Mapping[str, object]) -> None:
        if self.descriptor.parameters_schema is not None:
            check_parameters_against_schema(
                self.descriptor.parameters_schema,
                parameters,
                location_label="predictions/parameters",
            )

    def predict(self, **kwargs: Any) -> Any:
        raise RuntimeError("生态预测插件仅允许在隔离容器阶段内运行，禁止控制面直调")


@dataclass(frozen=True, slots=True)
class _MetadataGeneratorProvider:
    descriptor: GeneratorDescriptor

    @property
    def ref(self) -> str:
        return self.descriptor.ref

    def available(self) -> bool:
        return True

    def generate(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("生态生成器插件仅允许在隔离容器阶段内运行，禁止控制面直调")


@dataclass(frozen=True, slots=True)
class _MetadataAlgorithmProvider:
    descriptor: AlgorithmDescriptor

    @property
    def ref(self) -> str:
        return self.descriptor.ref

    def available(self) -> bool:
        return True


def _stage_spec_from_artifact(record: LocalArtifactRecord) -> StagePluginSpec:
    """将持久化元数据无损投影为 StagePluginSpec（忠实保留 advanced / extensions / companions）。"""
    meta = record.metadata_json
    advanced = meta.get("advanced") or {}
    extensions = meta.get("extensions") or {}

    raw_modes = advanced.get("supported_modes") if "supported_modes" in advanced else meta.get("modes")
    if raw_modes is not None:
        modes = tuple(raw_modes)
    elif record.kind in ("generator", "algorithm"):
        modes = ("planning",)
    else:
        modes = ()

    raw_precisions = (
        advanced.get("supported_precisions")
        if "supported_precisions" in advanced
        else meta.get("precisions")
    )
    if raw_precisions is not None:
        precisions = tuple(raw_precisions)
    elif record.kind in ("generator", "algorithm"):
        precisions = ("low", "medium", "high")
    else:
        precisions = ()

    raw_outputs = (
        extensions.get("supported_outputs")
        if "supported_outputs" in extensions
        else meta.get("outputs")
    )
    if raw_outputs is not None:
        outputs = tuple(raw_outputs)
    elif record.kind in ("generator", "algorithm", "evaluator"):
        outputs = ("solution", "metrics")
    else:
        outputs = ()

    return StagePluginSpec(
        plugin_id=record.stable_id,
        version=record.version,
        kind=record.kind,
        consumes=tuple(meta.get("consumes") or ()),
        produces=tuple(meta.get("produces") or ()),
        modes=modes,
        precisions=precisions,
        outputs=outputs,
        parameters_schema=dict(
            meta.get("parameters_schema")
            or {"type": "object", "properties": {}, "additionalProperties": False}
        ),
        companions=dict(advanced.get("companions") or {}),
    )


def _generator_descriptor_from_artifact(record: LocalArtifactRecord) -> GeneratorDescriptor:
    meta = record.metadata_json
    spec = _stage_spec_from_artifact(record)
    advanced = meta.get("advanced") or {}
    advanced_ext = advanced.get("extensions") or {}
    extensions = meta.get("extensions") or {}
    companions = dict(spec.companions)

    solvers = list(extensions.get("solvers") or advanced_ext.get("solvers") or meta.get("solvers") or ())
    if "solver" in companions and companions["solver"] not in solvers:
        solvers.append(companions["solver"])
    evaluators = list(
        extensions.get("evaluators") or advanced_ext.get("evaluators") or meta.get("evaluators") or ()
    )
    if "evaluator" in companions and companions["evaluator"] not in evaluators:
        evaluators.append(companions["evaluator"])

    return GeneratorDescriptor(
        generator_id=record.stable_id,
        version=record.version,
        options_schema=spec.parameters_schema,
        outputs=spec.outputs,
        assembly_schema_version=str(extensions.get("assembly_schema_version") or "1.0.0"),
        modes=spec.modes,
        precisions=spec.precisions,
        capabilities=tuple(
            extensions.get("capabilities")
            or advanced_ext.get("capabilities")
            or meta.get("capabilities")
            or ()
        ),
        solvers=tuple(solvers),
        executors=tuple(extensions.get("executors") or (record.ref,)),
        equation_contracts=tuple(extensions.get("equation_contracts") or ()),
        evaluators=tuple(evaluators),
        deterministic=bool(extensions.get("deterministic", True)),
        resource_limits=dict(meta.get("resources") or {}),
        unit_system=str(extensions.get("unit_system") or advanced_ext.get("unit_system") or "SI"),
        media_types=spec.produces or ("ies.solver-bundle.mps@1.0.0",),
        seed_semantics=str(
            extensions.get("seed_semantics")
            or advanced_ext.get("seed_semantics")
            or "config.seed 透传至隔离容器 /work/in/request.json"
        ),
        consumes=spec.consumes or ("ies.stage-input@1.0.0",),
        produces=spec.produces or ("ies.solver-bundle.mps@1.0.0",),
        companions=companions,
    )


def _algorithm_descriptor_from_artifact(record: LocalArtifactRecord) -> AlgorithmDescriptor:
    meta = record.metadata_json
    spec = _stage_spec_from_artifact(record)
    advanced = meta.get("advanced") or {}
    advanced_ext = advanced.get("extensions") or {}
    extensions = meta.get("extensions") or {}
    companions = dict(spec.companions)

    evaluators = list(
        extensions.get("evaluators") or advanced_ext.get("evaluators") or meta.get("evaluators") or ()
    )
    if "evaluator" in companions and companions["evaluator"] not in evaluators:
        evaluators.append(companions["evaluator"])

    return AlgorithmDescriptor(
        algorithm_id=record.stable_id,
        version=record.version,
        parameters_schema=spec.parameters_schema,
        modes=spec.modes,
        precisions=spec.precisions,
        outputs=spec.outputs,
        capabilities=tuple(
            extensions.get("capabilities")
            or advanced_ext.get("capabilities")
            or meta.get("capabilities")
            or ()
        ),
        evaluators=tuple(evaluators) if evaluators else None,
        consumes=spec.consumes or ("ies.stage-input@1.0.0",),
        produces=spec.produces or ("ies.engineering-solution@1.0.0",),
        companions=companions,
    )


def _deserialize_device_document(db: Session, rec: LocalArtifactRecord) -> DeviceModelDocument:
    raw = get_object(db, rec.object_id)
    return from_dict(json.loads(raw.decode("utf-8")))


def _parse_positive_revision_str(raw: object) -> str | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return str(raw) if raw >= 1 else None
    if isinstance(raw, str) and raw.strip().isdigit():
        val = int(raw.strip())
        return str(val) if val >= 1 else None
    return None


def list_user_enabled_device_documents(
    db: Session, user_id: int
) -> list[tuple[LocalArtifactRecord, DeviceModelDocument]]:
    """查询成员已安装且启用（approved + ready）的本地设备模型，按最新版本汇总供默认选择。"""
    records = ecosystem_domain.list_enabled_artifacts_for_user(db, user_id, kind="device_model")
    latest_by_id: dict[str, LocalArtifactRecord] = {}
    for rec in records:
        prev = latest_by_id.get(rec.stable_id)
        if prev is None or int(rec.version) >= int(prev.version):
            latest_by_id[rec.stable_id] = rec
    return [
        (latest_by_id[stable_id], _deserialize_device_document(db, latest_by_id[stable_id]))
        for stable_id in sorted(latest_by_id)
    ]


def resolve_user_local_device_revision(
    db: Session,
    *,
    user_id: int,
    stable_id: str,
    pinned_revision: object | None = None,
) -> LocalArtifactRecord | None:
    """解析成员已安装且启用的本地设备模型；若已钉选 revision 则精确匹配该版本，绝不悄然跳最新版本。"""
    if pinned_revision is not None:
        rev_str = _parse_positive_revision_str(pinned_revision)
        if rev_str is None:
            return None
        rec = ecosystem_domain.get_artifact_by_identity(db, "device_model", stable_id, rev_str)
        if rec is None or rec.approval_status != "approved" or rec.install_status != "ready":
            return None
        install = ecosystem_domain.get_member_install(db, user_id, rec.id)
        if install is None or not install.enabled:
            return None
        return rec

    records = [
        rec
        for rec in ecosystem_domain.list_enabled_artifacts_for_user(db, user_id, kind="device_model")
        if rec.stable_id == stable_id
    ]
    if not records:
        return None
    return max(records, key=lambda item: int(item.version))


def validate_and_normalize_local_device_pins(
    db: Session,
    *,
    user_id: int,
    pins: object,
) -> dict[str, str]:
    """校验项目 extensions.local_device_models 精确版本钉选映射并规范化为 {stable_id: 'rev'}。"""
    if not isinstance(pins, Mapping):
        raise InvalidRequestError(
            "extensions.local_device_models 须为对象映射", code="PROJ-CMD-005"
        )
    normalized: dict[str, str] = {}
    for raw_id, raw_rev in pins.items():
        if not isinstance(raw_id, str) or not raw_id:
            raise InvalidRequestError("local_device_models 键须为非空设备稳定 ID", code="PROJ-CMD-005")
        rev_str = _parse_positive_revision_str(raw_rev)
        if rev_str is None:
            raise InvalidRequestError(
                f"设备 {raw_id!r} 的修订号须为正整数: {raw_rev!r}",
                code="PROJ-CMD-005",
                params={"device_id": raw_id, "revision": raw_rev},
            )
        rec = resolve_user_local_device_revision(
            db, user_id=user_id, stable_id=raw_id, pinned_revision=rev_str
        )
        if rec is None:
            raise InvalidRequestError(
                f"引用的本地设备模型 {raw_id!r} (revision={rev_str}) 未审批、已下架或未对当前成员启用",
                code="PROJ-CMD-005",
                params={"device_id": raw_id, "revision": rev_str},
            )
        normalized[raw_id] = rev_str
    return normalized


def load_pinned_local_device_records(
    db: Session, content: Mapping[str, Any]
) -> dict[str, tuple[LocalArtifactRecord, DeviceModelDocument]]:
    """按项目草稿/版本内容 extensions.local_device_models 精确加载已审批本地设备模型（不折叠最新版）。"""
    pins = (content.get("extensions") or {}).get("local_device_models")
    if not isinstance(pins, Mapping) or not pins:
        return {}
    out: dict[str, tuple[LocalArtifactRecord, DeviceModelDocument]] = {}
    for stable_id in sorted(pins):
        rev_str = _parse_positive_revision_str(pins[stable_id])
        if rev_str is None:
            continue
        rec = ecosystem_domain.get_artifact_by_identity(
            db, "device_model", str(stable_id), rev_str
        )
        if rec is not None and rec.approval_status == "approved":
            out[str(stable_id)] = (rec, _deserialize_device_document(db, rec))
    return out


def load_pinned_local_device_documents(
    db: Session, content: Mapping[str, Any]
) -> dict[str, DeviceModelDocument]:
    """供选择器/预检/规划读取项目内容精确钉选的本地设备模型文档。"""
    return {
        stable_id: doc
        for stable_id, (_rec, doc) in load_pinned_local_device_records(db, content).items()
    }


def augment_stage_registries_for_user(
    db: Session,
    *,
    user_id: int,
    predictor_registry: Any | None = None,
    generator_registry: Any | None = None,
    solver_registry: Any | None = None,
    evaluator_registry: Any | None = None,
    algorithm_registry: Any | None = None,
) -> tuple[
    PredictorRegistry | Any | None,
    GeneratorRegistry | Any | None,
    SolverRegistry | Any | None,
    EvaluatorRegistry | Any | None,
    AlgorithmRegistry | Any | None,
]:
    """将当前成员已安装启用的 ready 本地算法插件叠加入内置阶段目录（纯元数据，不执行源码）。"""
    enabled = ecosystem_domain.list_enabled_artifacts_for_user(db, user_id, kinds=PLUGIN_KINDS)
    if not enabled:
        return (
            predictor_registry,
            generator_registry,
            solver_registry,
            evaluator_registry,
            algorithm_registry,
        )

    by_kind: dict[str, list[LocalArtifactRecord]] = {k: [] for k in COMPAT_PLUGIN_KINDS}
    for rec in enabled:
        if rec.kind in by_kind:
            by_kind[rec.kind].append(rec)

    if by_kind["predictor"] and predictor_registry is not None:
        existing_preds = [
            predictor_registry.get(desc.ref) for desc in predictor_registry.snapshot()
        ]
        existing_refs = {p.descriptor.ref for p in existing_preds}
        for rec in by_kind["predictor"]:
            if rec.ref in existing_refs:
                continue
            meta = rec.metadata_json
            advanced = meta.get("advanced") or {}
            advanced_ext = advanced.get("extensions") or {}
            extensions = meta.get("extensions") or {}
            spec = _stage_spec_from_artifact(rec)
            desc = PredictorDescriptor(
                ref=rec.ref,
                depends_on_solve_result=bool(
                    advanced.get(
                        "depends_on_solve_result",
                        advanced_ext.get(
                            "depends_on_solve_result",
                            meta.get("depends_on_solve_result", False),
                        ),
                    )
                ),
                accepts_any_target_type=bool(
                    advanced_ext.get(
                        "accepts_any_target_type",
                        extensions.get("accepts_any_target_type", meta.get("accepts_any_target_type", True)),
                    )
                ),
                target_types=tuple(
                    advanced_ext.get("target_types")
                    or extensions.get("target_types")
                    or meta.get("target_types")
                    or ()
                ),
                needs_history=bool(
                    advanced_ext.get(
                        "needs_history",
                        extensions.get("needs_history", meta.get("needs_history", True)),
                    )
                ),
                description=str(meta.get("description") or ""),
                parameters_schema=spec.parameters_schema,
            )
            existing_preds.append(_MetadataPredictor(desc, stage_spec=spec))
        predictor_registry = build_predictor_registry(existing_preds)

    if by_kind["generator"] and generator_registry is not None:
        existing_gens = [
            generator_registry.get(desc.ref) for desc in generator_registry.snapshot()
        ]
        existing_refs = {g.ref for g in existing_gens}
        for rec in by_kind["generator"]:
            if rec.ref not in existing_refs:
                existing_gens.append(
                    _MetadataGeneratorProvider(_generator_descriptor_from_artifact(rec))
                )
        generator_registry = build_registry(existing_gens)

    if by_kind["solver"] and solver_registry is not None:
        specs = list(solver_registry.snapshot())
        existing_refs = {s.ref for s in specs}
        for rec in by_kind["solver"]:
            if rec.ref not in existing_refs:
                specs.append(_stage_spec_from_artifact(rec))
        solver_registry = build_solver_registry(specs)

    if by_kind["evaluator"] and evaluator_registry is not None:
        specs = list(evaluator_registry.snapshot())
        existing_refs = {s.ref for s in specs}
        for rec in by_kind["evaluator"]:
            if rec.ref not in existing_refs:
                specs.append(_stage_spec_from_artifact(rec))
        evaluator_registry = build_evaluator_registry(specs)

    if by_kind["algorithm"]:
        existing_algos = (
            [algorithm_registry.get(desc.ref) for desc in algorithm_registry.snapshot()]
            if algorithm_registry is not None
            else []
        )
        existing_refs = {a.ref for a in existing_algos}
        for rec in by_kind["algorithm"]:
            if rec.ref not in existing_refs:
                existing_algos.append(
                    _MetadataAlgorithmProvider(_algorithm_descriptor_from_artifact(rec))
                )
        algorithm_registry = build_algorithm_registry(existing_algos)

    return (
        predictor_registry,
        generator_registry,
        solver_registry,
        evaluator_registry,
        algorithm_registry,
    )


def _split_ref(ref: str) -> tuple[str, str] | None:
    if not isinstance(ref, str) or "@" not in ref:
        return None
    stable_id, version = ref.split("@", 1)
    return (stable_id, version) if stable_id and version else None


def lock_and_load_submission_artifacts(
    db: Session,
    *,
    user_id: int,
    calc_config: Mapping[str, Any],
    device_type_ids: set[str] | tuple[str, ...] = (),
    pinned_device_revisions: Mapping[str, Any] | None = None,
) -> tuple[dict[str, str], dict[str, DeviceModelDocument]]:
    """装配前对消费的生态插件与项目精确钉选设备模型加 FOR UPDATE 锁，并发互斥与文档一致性一次完成。"""
    plugin_identities: list[tuple[str, str, str]] = []
    for entry in calc_config.get("predictions") or ():
        if isinstance(entry, Mapping) and entry.get("algorithm"):
            parts = _split_ref(str(entry["algorithm"]))
            if parts is not None:
                plugin_identities.append(("predictor", parts[0], parts[1]))
    for kind, key in (("generator", "generator"), ("solver", "solver"), ("algorithm", "algorithm")):
        if calc_config.get(key):
            parts = _split_ref(str(calc_config[key]))
            if parts is not None:
                plugin_identities.append((kind, parts[0], parts[1]))
    for entry in calc_config.get("evaluators") or ():
        if isinstance(entry, Mapping) and entry.get("evaluator"):
            parts = _split_ref(str(entry["evaluator"]))
            if parts is not None:
                plugin_identities.append(("evaluator", parts[0], parts[1]))

    user_installs = ecosystem_domain.list_member_installs(db, user_id, enabled_only=True)
    user_install_ids = {rec.artifact_id: rec for rec in user_installs}
    pins = dict(pinned_device_revisions or {})

    target_device_identities: dict[str, tuple[str, str, str]] = {}
    for stable_id in sorted(set(device_type_ids)):
        if stable_id in pins:
            rev_str = _parse_positive_revision_str(pins[stable_id])
            if rev_str is None:
                raise InvalidRequestError(
                    f"项目钉选的本地设备模型 {stable_id!r} 修订号非法: {pins[stable_id]!r}",
                    code="TASK-REQ-007",
                    params={"device_id": stable_id, "revision": pins[stable_id]},
                )
            target_device_identities[stable_id] = ("device_model", stable_id, rev_str)
            continue
        matching_installs = [
            inst
            for inst in user_installs
            if inst.kind == "device_model" and inst.stable_id == stable_id
        ]
        if matching_installs:
            chosen = max(matching_installs, key=lambda inst: int(inst.version))
            target_device_identities[stable_id] = ("device_model", stable_id, chosen.version)
            continue
        if ecosystem_domain.list_artifacts(db, kind="device_model", stable_id=stable_id):
            raise InvalidRequestError(
                f"引用的本地设备模型 {stable_id!r} 未对当前成员安装或启用，无法提交计算任务",
                code="TASK-REQ-007",
                params={"device_id": stable_id},
            )

    all_identities = list(plugin_identities) + list(target_device_identities.values())
    if not all_identities:
        return {}, {}

    locked = ecosystem_domain.lock_artifacts_for_submission(db, identities=all_identities)
    locked_map = {(rec.kind, rec.stable_id, rec.version): rec for rec in locked}

    for ident in plugin_identities:
        rec = locked_map.get(ident)
        if rec is None:
            continue  # 内置插件不在 local_artifacts
        if (
            rec.approval_status != "approved"
            or rec.install_status != "ready"
            or rec.id not in user_install_ids
        ):
            raise InvalidRequestError(
                f"引用的生态插件 {rec.ref!r} 已下架或未启用，无法提交计算任务",
                code="TASK-REQ-007",
                params={"ref": rec.ref, "install_status": rec.install_status},
            )

    frozen_dev_versions: dict[str, str] = {}
    locked_dev_docs: dict[str, DeviceModelDocument] = {}
    for stable_id, ident in target_device_identities.items():
        rec = locked_map.get(ident)
        if (
            rec is None
            or rec.approval_status != "approved"
            or rec.install_status != "ready"
            or rec.id not in user_install_ids
        ):
            raise InvalidRequestError(
                f"引用的本地设备模型 {stable_id!r} (revision={ident[2]}) "
                "已下架、未审批或未启用，无法提交计算任务",
                code="TASK-REQ-007",
                params={"device_id": stable_id, "revision": ident[2]},
            )
        frozen_dev_versions[rec.stable_id] = rec.version
        locked_dev_docs[rec.stable_id] = _deserialize_device_document(db, rec)
    return frozen_dev_versions, locked_dev_docs


def lock_and_verify_submission_artifacts(
    db: Session,
    *,
    user_id: int,
    calc_config: Mapping[str, Any],
    device_type_ids: set[str] | tuple[str, ...] = (),
    pinned_device_revisions: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """兼容调用入口：对引用的本地生态制品加 FOR UPDATE 锁并返回冻结设备修订映射。"""
    frozen, _docs = lock_and_load_submission_artifacts(
        db,
        user_id=user_id,
        calc_config=calc_config,
        device_type_ids=device_type_ids,
        pinned_device_revisions=pinned_device_revisions,
    )
    return frozen


def resolve_local_stage_descriptor(
    db: Session,
    ref: str,
    *,
    kind_label: str | None = None,
) -> BuiltinStageDescriptor | None:
    """Worker 解析已审批本地插件容器描述符（不限当前 install_status，保障在途快照不被打断）。"""
    parts = _split_ref(ref)
    if parts is None:
        return None
    stable_id, version = parts
    kinds = (kind_label,) if kind_label in PLUGIN_KINDS else PLUGIN_KINDS
    for kind in kinds:
        rec = ecosystem_domain.get_artifact_by_identity(db, kind, stable_id, version)
        if rec is not None and rec.approval_status == "approved" and rec.image_ref:
            entrypoint = str(rec.metadata_json.get("entrypoint") or "")
            return BuiltinStageDescriptor(
                plugin_id=rec.stable_id,
                version=rec.version,
                kind=rec.kind,
                image=rec.image_ref,
                entrypoint=entrypoint,
                manifest_path="/plugin/plugin.yaml",
            )
    return None


def resolve_local_evaluator_produces(db: Session, ref: str) -> tuple[str, ...] | None:
    """读取已批准本地评价器的 produces 契约声明。"""
    parts = _split_ref(ref)
    if parts is None:
        return None
    rec = ecosystem_domain.get_artifact_by_identity(db, "evaluator", parts[0], parts[1])
    if rec is None or rec.approval_status != "approved":
        return None
    return tuple(rec.metadata_json.get("produces") or ())
