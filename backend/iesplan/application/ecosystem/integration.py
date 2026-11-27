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
    meta = record.metadata_json
    return StagePluginSpec(
        plugin_id=record.stable_id,
        version=record.version,
        kind=record.kind,
        consumes=tuple(meta.get("consumes") or ()),
        produces=tuple(meta.get("produces") or ()),
        modes=tuple(meta.get("modes") or ("calc", "optimization")),
        precisions=tuple(meta.get("precisions") or ("low", "medium", "high")),
        outputs=tuple(meta.get("outputs") or ("solution", "metrics")),
        parameters_schema=dict(
            meta.get("parameters_schema")
            or {"type": "object", "properties": {}, "additionalProperties": False}
        ),
    )


def _generator_descriptor_from_artifact(record: LocalArtifactRecord) -> GeneratorDescriptor:
    meta = record.metadata_json
    params_schema = dict(
        meta.get("parameters_schema")
        or {"type": "object", "properties": {}, "additionalProperties": False}
    )
    return GeneratorDescriptor(
        generator_id=record.stable_id,
        version=record.version,
        options_schema=params_schema,
        outputs=tuple(meta.get("outputs") or ("solution", "metrics")),
        assembly_schema_version="1.0.0",
        modes=tuple(meta.get("modes") or ("calc", "optimization")),
        precisions=tuple(meta.get("precisions") or ("low", "medium", "high")),
        capabilities=tuple(meta.get("capabilities") or ()),
        solvers=tuple(meta.get("solvers") or ()),
        executors=(f"{record.stable_id}@{record.version}",),
        evaluators=tuple(meta.get("evaluators") or ()),
        media_types=tuple(meta.get("produces") or ("ies.solver-bundle.mps@1.0.0",)),
    )


def _algorithm_descriptor_from_artifact(record: LocalArtifactRecord) -> AlgorithmDescriptor:
    meta = record.metadata_json
    params_schema = dict(
        meta.get("parameters_schema")
        or {"type": "object", "properties": {}, "additionalProperties": False}
    )
    raw_evs = meta.get("evaluators")
    return AlgorithmDescriptor(
        algorithm_id=record.stable_id,
        version=record.version,
        parameters_schema=params_schema,
        modes=tuple(meta.get("modes") or ("planning",)),
        precisions=tuple(meta.get("precisions") or ("low", "medium", "high")),
        outputs=tuple(meta.get("outputs") or ("solution", "metrics")),
        capabilities=tuple(meta.get("capabilities") or ()),
        evaluators=tuple(raw_evs) if raw_evs else None,
        consumes=tuple(meta.get("consumes") or ("ies.stage-input@1.0.0",)),
        produces=tuple(meta.get("produces") or ("ies.engineering-solution@1.0.0",)),
    )


def list_user_enabled_device_documents(
    db: Session, user_id: int
) -> list[tuple[LocalArtifactRecord, DeviceModelDocument]]:
    """查询成员已安装且启用（approved + ready）的本地设备模型，按最新版本去重。"""
    records = ecosystem_domain.list_enabled_artifacts_for_user(db, user_id, kind="device_model")
    latest_by_id: dict[str, LocalArtifactRecord] = {}
    for rec in records:
        prev = latest_by_id.get(rec.stable_id)
        if prev is None or int(rec.version) >= int(prev.version):
            latest_by_id[rec.stable_id] = rec
    out: list[tuple[LocalArtifactRecord, DeviceModelDocument]] = []
    for stable_id in sorted(latest_by_id):
        rec = latest_by_id[stable_id]
        raw = get_object(db, rec.object_id)
        doc = from_dict(json.loads(raw.decode("utf-8")))
        out.append((rec, doc))
    return out


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
            desc = PredictorDescriptor(
                ref=rec.ref,
                depends_on_solve_result=bool(meta.get("depends_on_solve_result", False)),
                accepts_any_target_type=bool(meta.get("accepts_any_target_type", True)),
                target_types=tuple(meta.get("target_types") or ()),
                needs_history=bool(meta.get("needs_history", True)),
                description=str(meta.get("description") or ""),
                parameters_schema=dict(
                    meta.get("parameters_schema")
                    or {"type": "object", "properties": {}, "additionalProperties": False}
                ),
            )
            existing_preds.append(_MetadataPredictor(desc))
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


def lock_and_verify_submission_artifacts(
    db: Session,
    *,
    user_id: int,
    calc_config: Mapping[str, Any],
    device_type_ids: set[str] | tuple[str, ...] = (),
) -> dict[str, str]:
    """提交冻结快照前对引用的本地生态制品加 FOR UPDATE 锁，与卸载互斥并发竞态。

    返回冻结快照应记录的本地设备模型版本映射 ``{stable_id: version}``。
    """
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

    local_devices = list_user_enabled_device_documents(db, user_id)
    local_dev_map = {
        rec.stable_id: rec
        for rec, _doc in local_devices
        if rec.stable_id in device_type_ids
    }
    all_identities = list(plugin_identities) + [
        ("device_model", rec.stable_id, rec.version) for rec in local_dev_map.values()
    ]
    if not all_identities:
        return {}

    locked = ecosystem_domain.lock_artifacts_for_submission(db, identities=all_identities)
    locked_map = {(rec.kind, rec.stable_id, rec.version): rec for rec in locked}
    user_installs = {
        rec.artifact_id: rec
        for rec in ecosystem_domain.list_member_installs(db, user_id, enabled_only=True)
    }

    for ident in plugin_identities:
        rec = locked_map.get(ident)
        if rec is None:
            continue  # 内置插件不在 local_artifacts
        if (
            rec.approval_status != "approved"
            or rec.install_status != "ready"
            or rec.id not in user_installs
        ):
            raise InvalidRequestError(
                f"引用的生态插件 {rec.ref!r} 已下架或未启用，无法提交计算任务",
                code="TASK-REQ-007",
                params={"ref": rec.ref, "install_status": rec.install_status},
            )

    frozen_dev_versions: dict[str, str] = {}
    for stable_id, orig in local_dev_map.items():
        rec = locked_map.get(("device_model", orig.stable_id, orig.version))
        if (
            rec is None
            or rec.approval_status != "approved"
            or rec.install_status != "ready"
            or rec.id not in user_installs
        ):
            raise InvalidRequestError(
                f"引用的本地设备模型 {stable_id!r} 已下架或未启用，无法提交计算任务",
                code="TASK-REQ-007",
                params={"device_id": stable_id},
            )
        frozen_dev_versions[rec.stable_id] = rec.version
    return frozen_dev_versions


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
