"""Worker 计算阶段命令(application/worker.compute_cases): 快照输入恢复与 1.2.0 阶段调用。

本模块提供 worker 驱动的 1.2.0 单向计算阶段命令，Worker 只按
返回的公开 contract 驱动状态机，不复制领域规则：

- ``load_compute_inputs``：把快照冻结的直接输入恢复为生成器可消费
  形态并按序返回（规范装配二件套结构恢复、逐实例数学贡献结构恢复、
  财务快照按冻结 profile/revision 取不可变行、计算配置显式字段恢复并烘焙
  快照冻结的有效种子、固定资源字节加载、非预测序列物化）。本层不包装第
  二套输入对象，不做缺省；冻结结构缺件即实际结构错误（KeyError/TypeError/
  ValueError 等）直接可见，不重跑输入校验。
- ``predict_round_stage`` / ``generate_bundle`` / ``execute_saved_bundle`` /
  ``restore_core_solution`` / ``run_algorithm_stage`` /
  ``run_evaluators_stage`` / ``run_single_evaluator``：经组合根
  给定的已发布目录与容器阶段运行时完成 1.2.0 阶段调用（其中
  ``execute_saved_bundle`` 由 ``checkpoints`` 提供并经 ``application.worker``
  统一导出）；本层不选型、不回退。
- ``seal_output_objects`` / ``attach_evidence_objects``：声明文件字节以
  存储对象无损封存并挂接证据引用（字节原样入对象，不做文本校验与编码
  猜测；媒体类型取 manifest/回执声明）。
- ``build_attempt_context`` / ``business_outcome_for``：回执身份派生与
  任务业务结局映射（只做 contract 允许的显式映射）。

本模块只 flush-free：只读 DB 与纯内存转换，不拥有事务、不 commit/
rollback（对象封存的提交由调用方短会话负责）。

依赖方向: worker → application → (configuration/dataset/project/
storage/领域门面 + computation 公开门面)。
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from iesplan import configuration as configuration_domain
from iesplan.application.ecosystem import (
    resolve_local_evaluator_produces,
    resolve_local_stage_descriptor,
)
from iesplan.application.worker.lease_cases import Claim, LeaseRejectedError
from iesplan.application.worker.transactions import run_worker_transaction
from iesplan.assembly import ValidatedAssemblyArtifact, ValidationReceipt
from iesplan.computation import (
    EVALUATION_REPORT_CONTRACT,
    FINANCIAL_EVALUATION_CONTRACT,
    AttemptContext,
    CalculationConfig,
    ComputationUnavailableError,
    ComputeResult,
    ExecutionReceipt,
    GeneratorRegistry,
    MaterializedPriceSeries,
    MaterializedSeries,
    MaterializedSet,
    PredictedTarget,
    PredictorRegistry,
    SolverBundle,
    SolverBundleManifest,
    StopProbe,
    collect_resource_ids,
    materialize_non_predictive_series,
    read_stop_reason,
    thaw_json_value,
)
from iesplan.computation.contracts.outputs import (
    validate_evaluation_report_doc,
    validate_financial_evaluation_doc,
)
from iesplan.computation.result_adapters import (
    FileContent,
    ResultAdapter,
    project_engineering_solution,
)
from iesplan.computation.runtime.stage_runner import StageExecutionSpec, StageStartError
from iesplan.core.content import (
    Content,
    GeneratedContent,
    MemoryContent,
    SizedContent,
    json_chunks,
    read_content,
)
from iesplan.core.contracts import horizon_point_count
from iesplan.core.diagnostics import Diagnostic
from iesplan.core.yamlmini import load as _yaml_load
from iesplan.finance import EffectiveFinanceConfig, EvaluationInput
from iesplan.modeling import DeviceMathContribution, contribution_from_dict
from iesplan.storage import add_ref, get_object, get_object_content, put_content
from iesplan.tasks import BUSINESS_OUTCOMES

__all__ = [
    "HOURLY_OBJECT_PATH",
    "ComputeExecutionPlane",
    "EvaluatedStageOutput",
    "FrozenSeriesInputs",
    "StageExecutionError",
    "attach_evidence_objects",
    "build_attempt_context",
    "build_hourly_files",
    "business_outcome_for",
    "complete_materialized",
    "generate_bundle",
    "load_compute_inputs",
    "predict_round_stage",
    "restore_core_solution",
    "result_step_series",
    "run_algorithm_stage",
    "run_evaluators_stage",
    "run_single_evaluator",
    "seal_output_objects",
]


class StageExecutionError(Exception):
    """Non-succeeded container stage outcome translated into worker terminal states."""

    def __init__(
        self,
        *,
        stage: str,
        status: str,
        termination_reason: str | None,
        detail: str,
        resources: Mapping[str, Any] | None = None,
    ) -> None:
        self.stage = stage
        self.status = status
        self.termination_reason = termination_reason
        self.detail = detail
        self.resources: dict[str, Any] = dict(resources or {})
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class EvaluatedStageOutput:
    """Container-produced algorithm outputs preserved without control-process full JSON reload."""

    result: ComputeResult
    hourly_ref: Mapping[str, Any] | None = None
    hourly_meta: Mapping[str, Any] | None = None
    planning_variables: tuple[Mapping[str, Any], ...] = ()
    resources: Mapping[str, Any] = field(default_factory=dict)
    solution_ref: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.result.to_dict()


@dataclass(frozen=True, slots=True)
class FrozenSeriesInputs:
    """Deferred frozen object handles + predicted series for container-side materialization."""

    operation_years: int
    fixed_resources: Mapping[str, Content] = field(default_factory=dict)
    predicted_series: tuple[MaterializedSeries, ...] = ()
    prices: tuple[MaterializedPriceSeries, ...] = ()


@dataclass(frozen=True, slots=True)
class ComputeExecutionPlane:
    """容器化计算执行面句柄（组合根装配、调度器/Worker 持有、阶段命令消费）。"""

    generator_registry: GeneratorRegistry
    predictor_registry: PredictorRegistry
    stage_runner: Any
    stage_catalog: Any
    solver_registry: Any | None = None
    evaluator_registry: Any | None = None
    algorithm_registry: Any | None = None
    evaluator_max_concurrency: int = 1
    session_factory: Any | None = None
    stage_descriptor_resolver: Callable[[str, str], Any] | None = None


def _receipt_from_frozen(data: Mapping[str, Any]) -> ValidationReceipt:
    """回执结构恢复（只恢复形态，不重跑装配校验）。

    形态以 ``ValidationReceipt.to_dict`` 为准：``schema``/``schema_version``、
    ``validator.{id,version}``、``canonical_algorithm.{id,version}``。
    冻结缺件即实际结构错误（KeyError/TypeError）直接可见，不兼容旧形态。
    """
    return ValidationReceipt(
        schema_id=data["schema"],
        schema_version=data["schema_version"],
        validator_id=data["validator"]["id"],
        validator_version=data["validator"]["version"],
        canonical_algorithm_id=data["canonical_algorithm"]["id"],
        canonical_algorithm_version=data["canonical_algorithm"]["version"],
        dependencies=dict(data["dependencies"]),
        resources=dict(data["resources"]),
        finance_snapshot=dict(data["finance_snapshot"]),
        planning=dict(data["planning"]),
        system_equations=dict(data["system_equations"]),
        diagnostics=tuple(Diagnostic(**dict(entry)) for entry in data["diagnostics"]),
    )


def _restore_artifact_resources_config(
    db: Session, task_project_id: int, snapshot: Any
) -> tuple[
    ValidatedAssemblyArtifact, dict[str, SizedContent], CalculationConfig, int,
]:
    """从快照恢复装配产物、固定资源只读内容句柄、计算配置与有效种子。

    finance 按回执冻结血缘 {profile_id, revision} 取不可变 Effective 行
    （与提交闸门同一来源：钉选 revision，不读 latest/current 指针）；
    固定资源按规范绑定 object_id 经 storage 公开门面取内容句柄（不预先读入
    全部字节）；有效种子取快照冻结值（配置缺省时不再静默用 0，而是提交
    闸门同一派生规则）。冻结缺件即实际结构错误直接可见，不重跑输入校验。
    """
    receipt = _receipt_from_frozen(snapshot.assembly_receipt)
    artifact = ValidatedAssemblyArtifact(canonical_text=snapshot.canonical_assembly_text, receipt=receipt)

    body = snapshot.calc_config_snapshot
    seed = snapshot.random_seed if body["seed"] is None else body["seed"]
    config = CalculationConfig.from_frozen({**body, "seed": seed})

    doc = json.loads(snapshot.canonical_assembly_text)
    resource_ids = collect_resource_ids(doc)
    resources: dict[str, SizedContent] = {}
    for object_id in resource_ids:
        key: int | str = (
            int(object_id) if object_id.isdigit() else object_id
        )
        resources[object_id] = get_object_content(db, key)
    return artifact, resources, config, seed


def _wanted_price_ids(doc: Mapping[str, Any], finance: EffectiveFinanceConfig) -> list[str]:
    wanted: list[str] = []
    for binding_id in sorted(doc.get("tariff_bindings") or {}):
        price_id = (doc["tariff_bindings"][binding_id] or {}).get("price")
        price = (finance.energy_prices or {}).get(price_id)
        if price is not None and price.kind in ("time_series", "monthly_daily") and price_id not in wanted:
            wanted.append(price_id)
    return wanted


def _collect_price_resource_ids(
    doc: Mapping[str, Any], finance: EffectiveFinanceConfig
) -> tuple[str, ...]:
    found: set[str] = set()
    for price_id in _wanted_price_ids(doc, finance):
        price = finance.energy_prices[price_id]
        if price.kind == "time_series" and price.ref:
            found.add(str(price.ref))
        elif price.kind == "monthly_daily" and price.months:
            for month in range(1, 13):
                found.add(str(price.months[str(month)].ref))
    return tuple(sorted(found))


def load_compute_inputs(
    db: Session,
    task_project_id: int,
    snapshot: Any,
    *,
    materialize_series: bool = True,
) -> tuple[
    ValidatedAssemblyArtifact,
    dict[str, DeviceMathContribution],
    MaterializedSet | FrozenSeriesInputs,
    EffectiveFinanceConfig,
    CalculationConfig,
    EvaluationInput,
]:
    """从快照恢复生成器直接输入并按序返回（不包装第二对象，不做缺省）。

    当 ``materialize_series=False`` 时不物化大序列浮点数组，而是返回携带
    不可变对象存储内容句柄的 ``FrozenSeriesInputs`` 供阶段容器直接消费。
    """
    artifact, resources, config, _seed = _restore_artifact_resources_config(
        db, task_project_id, snapshot
    )

    raw_models = snapshot.device_models
    models = {inst: contribution_from_dict(contrib) for inst, contrib in raw_models.items()}

    receipt = _receipt_from_frozen(snapshot.assembly_receipt)
    finance_blood = receipt.finance_snapshot
    finance_row = configuration_domain.get_effective_revision(db, task_project_id, finance_blood["revision"])
    if finance_row.profile_id != finance_blood["profile_id"]:
        raise KeyError(
            "快照财务血缘与不可变行不一致 "
            f"(profile={finance_blood['profile_id']!r}, "
            f"revision={finance_blood['revision']!r})"
        )
    finance = EffectiveFinanceConfig.from_dict(finance_row.content)

    operation_years = snapshot.evaluation["operation_years"]
    evaluation = EvaluationInput.from_frozen(snapshot.evaluation)
    if not materialize_series:
        doc = json.loads(artifact.canonical_text)
        fixed_resources: dict[str, Content] = dict(resources)
        for object_id in _collect_price_resource_ids(doc, finance):
            if object_id not in fixed_resources:
                key: int | str = int(object_id) if object_id.isdigit() else object_id
                fixed_resources[object_id] = get_object_content(db, key)
        frozen_inputs = FrozenSeriesInputs(
            operation_years=operation_years,
            fixed_resources=fixed_resources,
        )
        return artifact, models, frozen_inputs, finance, config, evaluation

    raw_resources = {oid: read_content(content) for oid, content in resources.items()}
    materialized = materialize_non_predictive_series(
        artifact, raw_resources, operation_years=operation_years
    )
    prices = _materialize_price_series(db, artifact, finance, operation_years=operation_years)
    if prices:
        materialized = MaterializedSet(
            operation_years=materialized.operation_years,
            series=materialized.series,
            pending_predict=materialized.pending_predict,
            prices=prices,
        )
    return artifact, models, materialized, finance, config, evaluation


def _materialize_price_series(
    db: Session,
    artifact: ValidatedAssemblyArtifact,
    finance: EffectiveFinanceConfig,
    *,
    operation_years: int | None = None,
) -> tuple[MaterializedPriceSeries, ...]:
    """Restore only tariff-bound price sources from the frozen finance revision.

    Full-term series remain untouched. Monthly day sources are interpreted by
    the actual Gregorian operating calendar, not by repeating an annual array.
    Input validity belongs to the single submit boundary; these reads are
    trusted and do not reparse the full upload protocol.
    """
    from iesplan.finance import expand_monthly_daily_prices, read_trusted_price_values

    doc = json.loads(artifact.canonical_text)
    baseline = doc["project_baseline"]
    wanted = _wanted_price_ids(doc, finance)
    decoded: dict[str, tuple[float, ...]] = {}

    def read_oid(oid: str) -> tuple[float, ...]:
        if oid not in decoded:
            decoded[oid] = read_trusted_price_values(get_object(db, oid))
        return decoded[oid]

    out: list[MaterializedPriceSeries] = []
    for price_id in wanted:
        price = finance.energy_prices[price_id]
        if price.kind == "time_series":
            values = read_oid(price.ref)
            meta = price.series_meta
            source = {"kind": "price_series", "object_id": price.ref}
        else:
            month_sources = price.months
            curves = {
                month: read_oid(month_sources[str(month)].ref)
                for month in range(1, 13)
            }
            values = expand_monthly_daily_prices(
                curves,
                resolution=baseline["resolution"],
                start_year=baseline["start_year"],
                operation_years=operation_years,
            )
            meta = month_sources["1"].series_meta
            source = {
                "kind": "monthly_daily",
                "months": {
                    str(month): month_sources[str(month)].ref
                    for month in range(1, 13)
                },
            }
        out.append(
            MaterializedPriceSeries(
                price_id=price_id,
                point_count=len(values),
                resolution=meta.resolution,
                unit=meta.unit,
                values=values,
                source=source,
            )
        )
    return tuple(out)


def complete_materialized(
    base: MaterializedSet | FrozenSeriesInputs, predicted: tuple[MaterializedSeries, ...]
) -> MaterializedSet | FrozenSeriesInputs:
    """预测产物并入为完成态集合（Generator 第三项输入形态）。

    年限沿用基集合携带的已确认值，不重读快照；预测阶段交付全部
    ``pending``（提交闸门 TASK-REQ-010/011 保证配置与绑定双向覆盖），
    此处只构成完成态，不复核。已物化价格区沿基集合透传。
    """
    if isinstance(base, FrozenSeriesInputs):
        return FrozenSeriesInputs(
            operation_years=base.operation_years,
            fixed_resources=base.fixed_resources,
            predicted_series=tuple(predicted),
            prices=base.prices,
        )
    return MaterializedSet(
        operation_years=base.operation_years,
        series=tuple(base.series) + tuple(predicted),
        pending_predict=(),
        prices=base.prices,
    )


def resolve_stage_descriptor(
    plane: ComputeExecutionPlane,
    ref: str,
    *,
    kind_label: str,
    session_factory: Callable[[], Session] | None = None,
) -> Any:
    """解析计算阶段容器描述符：先查内置 stage_catalog，未命中再查已审批本地生态插件镜像。"""
    try:
        return plane.stage_catalog.get_by_ref(ref)
    except KeyError:
        resolver = getattr(plane, "stage_descriptor_resolver", None)
        if resolver is not None:
            resolved = resolver(ref, kind_label)
            if resolved is not None:
                return resolved
        sf = session_factory or getattr(plane, "session_factory", None)
        if sf is not None:
            with sf() as db:
                resolved = resolve_local_stage_descriptor(db, ref, kind_label=kind_label)
                if resolved is not None:
                    return resolved
        raise ComputationUnavailableError(
            f"{kind_label}阶段容器未发布: {ref!r}",
            reason="no-provider",
        ) from None


_stage_descriptor = resolve_stage_descriptor


def _handle_non_succeeded_stage(
    stage: str,
    stage_result: Any,
    claim: Claim,
    number: int,
    session_factory: Callable[[], Session],
) -> int:
    if stage_result.status == "fenced":
        raise LeaseRejectedError("容器阶段执行期间租约已失效(fenced)")
    from iesplan.application.worker.checkpoints import save_stage_outcome_checkpoint

    return run_worker_transaction(
        session_factory,
        save_stage_outcome_checkpoint,
        claim,
        number,
        stage,
        stage_result,
    )


def _raise_stage_error(stage: str, stage_result: Any) -> None:
    raise StageExecutionError(
        stage=stage,
        status=str(stage_result.status),
        termination_reason=stage_result.termination_reason,
        detail=(
            f"阶段 {stage} 未成功"
            f"(status={stage_result.status}, reason={stage_result.termination_reason})"
        ),
        resources=dict(stage_result.resources or {}),
    )


def _serialize_materialized(materialized: MaterializedSet) -> dict[str, Any]:
    return {
        "operation_years": materialized.operation_years,
        "series": [
            {
                "device": item.device,
                "interface": item.interface,
                "mode": item.mode,
                "resolution": item.resolution,
                "point_count": item.point_count,
                "values": list(item.values),
                "source": thaw_json_value(item.source),
            }
            for item in materialized.series
        ],
        "pending_predict": [thaw_json_value(entry) for entry in materialized.pending_predict],
        "prices": [
            {
                "price_id": item.price_id,
                "point_count": item.point_count,
                "resolution": item.resolution,
                "unit": item.unit,
                "values": list(item.values),
                "source": thaw_json_value(item.source),
            }
            for item in materialized.prices
        ],
    }


def _attach_series_inputs(
    request_payload: dict[str, Any],
    input_files: dict[str, Content | bytes],
    materialized: MaterializedSet | FrozenSeriesInputs,
) -> None:
    """Attach deferred object handles (`frozen_series_inputs`) or serialized `MaterializedSet`."""
    if isinstance(materialized, FrozenSeriesInputs):
        fixed_map: dict[str, str] = {}
        for object_id in sorted(materialized.fixed_resources):
            rel = f"objects/{object_id}"
            fixed_map[object_id] = rel
            input_files[rel] = materialized.fixed_resources[object_id]
        request_payload["frozen_series_inputs"] = {
            "fixed_resources": fixed_map,
            "predicted_series": [item.to_dict() for item in materialized.predicted_series],
        }
        return
    request_payload["materialized"] = _serialize_materialized(materialized)


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def predict_round_stage(
    *,
    plane: ComputeExecutionPlane,
    task_project_id: int,
    snapshot: Any,
    claim: Claim,
    number: int,
    session_factory: Callable[[], Session],
    save_checkpoint: Callable[
        [Session, Claim, int, tuple[MaterializedSeries, ...], tuple[PredictedTarget, ...]],
        dict[str, dict[str, object]],
    ],
    save_group_checkpoint: Callable[
        [Session, Claim, int, str, tuple[MaterializedSeries, ...], tuple[PredictedTarget, ...]],
        dict[str, dict[str, object]],
    ] | None = None,
    load_group_checkpoint: Callable[
        [Session, Claim, int, str],
        tuple[
            tuple[MaterializedSeries, ...],
            tuple[PredictedTarget, ...],
            dict[str, dict[str, object]],
        ]
        | None,
    ] | None = None,
    publish_combined_checkpoint: Callable[
        [
            Session,
            Claim,
            int,
            tuple[MaterializedSeries, ...],
            tuple[PredictedTarget, ...],
            Mapping[str, Mapping[str, object]],
        ],
        dict[str, dict[str, object]],
    ] | None = None,
    entries: tuple[Mapping[str, object], ...] | None = None,
    stop: StopProbe | None = None,
    detach: Any = None,
    on_stage_resources: Callable[[Mapping[str, Any]], None] | None = None,
    cleanup_on_reuse: Callable[[str], None] | None = None,
) -> tuple[
    tuple[MaterializedSeries, ...],
    tuple[PredictedTarget, ...],
    dict[str, dict[str, object]],
]:
    """预测阶段单趟容器化执行：读取历史字节 → job_runner 预测阶段 → 检查点落库后清理。"""
    artifact, resources, config, seed = run_worker_transaction(
        session_factory, _restore_artifact_resources_config, task_project_id, snapshot
    )
    target_entries = tuple(config.predictions or ()) if entries is None else tuple(entries)
    allow_subset = entries is not None
    if not target_entries:
        return (), (), {}

    doc = json.loads(artifact.canonical_text)
    baseline = doc["project_baseline"]
    operation_years = int(snapshot.evaluation["operation_years"])
    point_count = horizon_point_count(baseline["resolution"], baseline["start_year"], operation_years)

    by_target: dict[tuple[str, str], Mapping[str, object]] = {}
    for entry in target_entries:
        target = entry["target"]
        by_target[(str(target["instance"]), str(target["interface"]))] = entry

    grouped: dict[str, tuple[Any, list[dict[str, Any]]]] = {}
    devices = doc.get("devices") or {}
    for device_id in sorted(devices):
        bindings = devices[device_id].get("predefined_interfaces") or {}
        for interface_id in sorted(bindings):
            binding = bindings[interface_id]
            if not isinstance(binding, Mapping) or binding.get("mode") != "data_predict":
                continue
            key = (str(device_id), str(interface_id))
            if key not in by_target:
                if allow_subset:
                    continue
                raise KeyError(key)
            entry = by_target[key]
            algo_ref = str(entry["algorithm"])
            descriptor = _stage_descriptor(
                plane, algo_ref, kind_label="predictor", session_factory=session_factory
            )
            try:
                plane.predictor_registry.get(algo_ref)
            except ComputationUnavailableError:
                if descriptor.image == getattr(
                    getattr(plane.stage_catalog, "get_by_kind", lambda _k: None)("predictor"),
                    "image",
                    None,
                ):
                    raise
            object_id = str(binding["content"]["object_id"])
            if object_id not in resources:
                raise ValueError(
                    "固定资源缺失（必要输入不完整）: "
                    f"{device_id!r} x {interface_id!r} x object {object_id!r}"
                )
            grouped.setdefault(algo_ref, (descriptor, []))[1].append(
                {
                    "instance": str(device_id),
                    "interface": str(interface_id),
                    "target_type": str(binding["target_type"]),
                    "history_object_id": object_id,
                    "history_csv_rel": f"history/{object_id}.csv",
                    "parameters": thaw_json_value(entry.get("parameters") or {}),
                }
            )

    all_predicted: list[PredictedTarget] = []
    all_series: list[MaterializedSeries] = []
    saved_artifacts: dict[str, dict[str, object]] = {}
    sorted_algos = sorted(grouped)

    for idx, algo_ref in enumerate(sorted_algos):
        descriptor, group_targets = grouped[algo_ref]
        stage_id = f"predict-r{number}" if len(sorted_algos) == 1 else f"predict-r{number}-{idx}"
        if len(sorted_algos) > 1 and load_group_checkpoint is not None:
            cached_group = run_worker_transaction(
                session_factory, load_group_checkpoint, claim, number, algo_ref,
            )
            if cached_group is not None:
                if cleanup_on_reuse is not None:
                    cleanup_on_reuse(stage_id)
                all_series.extend(cached_group[0])
                all_predicted.extend(cached_group[1])
                saved_artifacts.update(cached_group[2])
                continue

        input_files: dict[str, Content | bytes] = {
            "request.json": _json_bytes(
                {
                    "point_count": point_count,
                    "seed": seed,
                    "targets": group_targets,
                }
            )
        }
        for item in group_targets:
            oid = item["history_object_id"]
            csv_rel = item["history_csv_rel"]
            if csv_rel not in input_files:
                input_files[csv_rel] = resources[oid]

        group_series: list[MaterializedSeries] = []
        group_predicted: list[PredictedTarget] = []

        def _persist(stage_result: Any, *, group_key: str = algo_ref) -> Any:
            nonlocal group_series, group_predicted, saved_artifacts
            if stage_result.status != "succeeded":
                return _handle_non_succeeded_stage(
                    "predict", stage_result, claim, number, session_factory
                )
            payload = json.loads(read_content(stage_result.outputs["prediction.json"]).decode("utf-8"))
            parsed_series: list[MaterializedSeries] = []
            parsed_predicted: list[PredictedTarget] = []
            for pred in payload["predictions"]:
                artifact_bytes = read_content(stage_result.outputs[str(pred["artifact_path"])])
                values = tuple(float(v) for v in pred["values"])
                parsed_predicted.append(
                    PredictedTarget(
                        device=str(pred["instance"]),
                        interface=str(pred["interface"]),
                        target_type=str(pred["target_type"]),
                        values=values,
                        history_object_id=str(pred.get("history_object_id") or ""),
                        artifact=artifact_bytes,
                        artifact_media_type=str(pred.get("artifact_media_type") or "application/json"),
                        receipt=dict(pred.get("receipt") or {}),
                    )
                )
                parsed_series.append(
                    MaterializedSeries(
                        device=str(pred["instance"]),
                        interface=str(pred["interface"]),
                        mode="data_predict",
                        resolution=str(baseline["resolution"]),
                        point_count=point_count,
                        values=values,
                        source={
                            "kind": "data_predict",
                            "object_id": str(pred.get("history_object_id") or ""),
                        },
                    )
                )
            group_series = parsed_series
            group_predicted = parsed_predicted
            sorted_pair_p = tuple(
                sorted(parsed_predicted, key=lambda target: (target.device, target.interface))
            )
            sorted_pair_s = tuple(
                sorted(parsed_series, key=lambda target: (target.device, target.interface))
            )
            if len(sorted_algos) == 1:
                saved_artifacts = run_worker_transaction(
                    session_factory,
                    save_checkpoint,
                    claim,
                    number,
                    sorted_pair_s,
                    sorted_pair_p,
                )
            else:
                if save_group_checkpoint is None:
                    raise RuntimeError("save_group_checkpoint is required for multi-algorithm prediction")
                group_artifacts = run_worker_transaction(
                    session_factory,
                    save_group_checkpoint,
                    claim,
                    number,
                    group_key,
                    sorted_pair_s,
                    sorted_pair_p,
                )
                saved_artifacts.update(group_artifacts)
            return True

        try:
            stage_result = plane.stage_runner.run_stage(
                StageExecutionSpec(
                    attempt_id=str(claim.attempt_id),
                    stage_id=stage_id,
                    fencing_token=str(claim.lease_token),
                    image=descriptor.image,
                    command=descriptor.command,
                    input_files=input_files,
                    required_outputs=("prediction.json",),
                    collect_all_outputs=True,
                ),
                persist=_persist,
                stop=stop,
                **({"detach": detach} if detach is not None else {}),
            )
        except LeaseRejectedError:
            raise StageExecutionError(
                stage="predict",
                status="fenced",
                termination_reason="fenced",
                detail="预测阶段租约已失效(fenced)",
            ) from None
        if on_stage_resources is not None and stage_result.resources:
            on_stage_resources(stage_result.resources)
        if stage_result.status != "succeeded":
            _raise_stage_error("predict", stage_result)
        all_series.extend(group_series)
        all_predicted.extend(group_predicted)

    final_series = tuple(sorted(all_series, key=lambda item: (item.device, item.interface)))
    final_predicted = tuple(sorted(all_predicted, key=lambda item: (item.device, item.interface)))
    if len(sorted_algos) > 1:
        if publish_combined_checkpoint is None:
            raise RuntimeError(
                "publish_combined_checkpoint is required for multi-algorithm prediction"
            )
        saved_artifacts = run_worker_transaction(
            session_factory,
            publish_combined_checkpoint,
            claim,
            number,
            final_series,
            final_predicted,
            saved_artifacts,
        )
    return final_series, final_predicted, saved_artifacts


def generate_bundle(
    plane: ComputeExecutionPlane,
    snapshot: Any,
    artifact: ValidatedAssemblyArtifact,
    models: Mapping[str, DeviceMathContribution],
    materialized: MaterializedSet | FrozenSeriesInputs,
    finance: EffectiveFinanceConfig,
    config: CalculationConfig,
    evaluation: EvaluationInput,
    *,
    attempt: AttemptContext,
    claim: Claim,
    number: int,
    session_factory: Callable[[], Session],
    save_checkpoint: Callable[
        [Session, Claim, int, SolverBundle, AttemptContext],
        tuple[int, dict[str, Any]],
    ],
    stop: StopProbe | None = None,
    detach: Any = None,
    on_stage_resources: Callable[[Mapping[str, Any]], None] | None = None,
) -> tuple[int, dict[str, dict[str, object]]]:
    """经生成器阶段容器生成 1.2.0 契约解耦 SolverBundle，并在 cleanup 前带 Fencing Token 持久化。"""
    del models, evaluation
    if config.generator is None or config.solver is None:
        raise ValueError("generate_bundle requires config.generator and config.solver")
    gen_stage = _stage_descriptor(
        plane, config.generator, kind_label="generator", session_factory=session_factory
    )
    try:
        gen_ref = plane.generator_registry.get(config.generator).ref
    except ComputationUnavailableError:
        gen_ref = config.generator
    _stage_descriptor(plane, config.solver, kind_label="solver", session_factory=session_factory)
    for ev in config.evaluators:
        _stage_descriptor(
            plane, str(ev["evaluator"]), kind_label="evaluator", session_factory=session_factory
        )

    request_payload: dict[str, Any] = {
        "artifact": {
            "canonical_text": artifact.canonical_text,
            "receipt": thaw_json_value(snapshot.assembly_receipt),
        },
        "device_models": thaw_json_value(snapshot.device_models),
        "finance": finance.to_dict(),
        "calculation_config": config.to_dict(),
        "parameters": thaw_json_value(config.slot_options("generator")),
        "evaluation": thaw_json_value(snapshot.evaluation),
    }
    input_files: dict[str, Content | bytes] = {}
    _attach_series_inputs(request_payload, input_files, materialized)
    input_files["request.json"] = _json_bytes(request_payload)

    saved_info: tuple[int, dict[str, dict[str, object]]] | None = None

    def _persist(stage_result: Any) -> Any:
        nonlocal saved_info
        if stage_result.status != "succeeded":
            return _handle_non_succeeded_stage(
                "generate", stage_result, claim, number, session_factory
            )
        manifest_raw = _yaml_load(
            read_content(stage_result.outputs["bundle/bundle.yaml"]).decode("utf-8")
        )
        manifest = SolverBundleManifest(**manifest_raw)
        if manifest.bundle["generator"] != gen_ref:
            raise ComputationUnavailableError(
                "Bundle 声明生成器与计算配置不一致 "
                f"(bundle={manifest.bundle['generator']!r}, config={gen_ref!r})",
                reason="frozen-provider-mismatch",
            )
        bundle_files: dict[str, Content] = {
            rel.removeprefix("bundle/"): content
            for rel, content in stage_result.outputs.items()
            if rel.startswith("bundle/") and rel != "bundle/bundle.yaml"
        }
        bundle = SolverBundle(manifest, bundle_files)
        saved_info = run_worker_transaction(
            session_factory,
            save_checkpoint,
            claim,
            number,
            bundle,
            attempt,
        )
        return saved_info

    try:
        stage_result = plane.stage_runner.run_stage(
            StageExecutionSpec(
                attempt_id=attempt.attempt_id,
                stage_id=f"generate-r{number}",
                fencing_token=str(claim.lease_token),
                image=gen_stage.image,
                command=gen_stage.command,
                input_files=input_files,
                required_outputs=("bundle/bundle.yaml",),
                collect_all_outputs=True,
            ),
            persist=_persist,
            stop=stop,
            **({"detach": detach} if detach is not None else {}),
        )
    except LeaseRejectedError:
        raise StageExecutionError(
            stage="generate",
            status="fenced",
            termination_reason="fenced",
            detail="生成阶段租约已失效(fenced)",
        ) from None

    if on_stage_resources is not None and stage_result.resources:
        on_stage_resources(stage_result.resources)
    if stage_result.status != "succeeded":
        _raise_stage_error("generate", stage_result)
    assert saved_info is not None
    return saved_info


def restore_core_solution(
    bundle: SolverBundle,
    receipt: ExecutionReceipt,
    collected: Mapping[str, object],
    *,
    config: CalculationConfig,
    claim: Claim,
    number: int,
    session_factory: Callable[[], Session],
    save_checkpoint: Callable[..., dict[str, dict[str, object]]],
    stop: StopProbe | None = None,
) -> dict[str, Any]:
    """Run isolated ``spawn`` ResultAdapter restoration and seal engineering solution + hourly file."""
    with tempfile.TemporaryDirectory(prefix="ies-core-restore-") as tmp_dir:
        sol_path = Path(tmp_dir) / "solution.json"
        hourly_path = Path(tmp_dir) / "hourly_series.json"
        include_solution = "solution" in config.outputs
        outcome = ResultAdapter().restore_with_outcome(
            bundle,
            receipt,
            collected,
            solution_output_path=sol_path,
            hourly_output_path=(hourly_path if include_solution else None),
            stop=stop,
        )
        cr = outcome.compute_result
        cr_dict = cr.to_dict()
        feasible = cr.math_status in ("solved", "feasible")
        planning_vars = (
            [dict(item) for item in outcome.planning_variables]
            if feasible and include_solution
            else []
        )
        hourly_meta = (
            dict(outcome.hourly_meta)
            if feasible and include_solution and outcome.hourly_meta is not None
            else None
        )
        hourly_content = outcome.hourly_content if feasible and include_solution else None
        if planning_vars:
            cr_dict["outputs"] = {
                **cr_dict["outputs"],
                "solution": {"variables": planning_vars},
            }
            cr = ComputeResult(
                schema=cr.schema,
                schema_version=cr.schema_version,
                bundle_id=cr.bundle_id,
                receipt_id=cr.receipt_id,
                execution_status=cr.execution_status,
                math_status=cr.math_status,
                outputs=cr_dict["outputs"],
            )
        sealed_files = run_worker_transaction(
            session_factory,
            save_checkpoint,
            claim,
            number,
            compute_result=cr_dict,
            solution_content=(
                FileContent(sol_path, sol_path.stat().st_size)
                if feasible and sol_path.is_file()
                else None
            ),
            hourly_content=hourly_content,
            hourly_meta=hourly_meta,
            planning_variables=planning_vars,
        )
        return {
            "compute_result": cr,
            "solution_ref": sealed_files.get("solution.json"),
            "hourly_ref": sealed_files.get("hourly/series.json"),
            "hourly_meta": hourly_meta,
            "planning_variables": planning_vars,
        }


def _slugify_stage_id(ref: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", ref).strip("-")[:40] or "eval"


def _build_evaluator_input_files(
    *,
    engineering_solution: Content,
    snapshot: Any,
    artifact: ValidatedAssemblyArtifact,
    finance: EffectiveFinanceConfig,
    materialized: MaterializedSet | FrozenSeriesInputs,
    parameters: Mapping[str, Any],
    receipt: Mapping[str, Any] | None,
    bundle_id: str,
    receipt_id: str,
) -> dict[str, Content | bytes]:
    request_payload: dict[str, Any] = {
        "artifact": {
            "canonical_text": artifact.canonical_text,
            "receipt": thaw_json_value(snapshot.assembly_receipt),
        },
        "device_models": thaw_json_value(snapshot.device_models),
        "finance": finance.to_dict(),
        "evaluation": thaw_json_value(snapshot.evaluation),
        "parameters": thaw_json_value(parameters),
        "bundle_id": bundle_id,
        "receipt_id": receipt_id,
    }
    if receipt is not None:
        request_payload["receipt"] = dict(receipt)
    input_files: dict[str, Content | bytes] = {
        "solution.json": engineering_solution,
    }
    _attach_series_inputs(request_payload, input_files, materialized)
    input_files["request.json"] = _json_bytes(request_payload)
    return input_files


_UNSET = object()


def _reject_eval_json_constant(token: str) -> Any:
    raise ValueError(f"评价器输出包含非有限 JSON 常量: {token!r}")


def _parse_evaluator_outputs(
    stage_outputs: Mapping[str, Any],
    *,
    evaluator_ref: str,
    declared_produces: tuple[str, ...],
) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
    """Validate evaluator outputs once at ingestion against declared `produces` contract."""
    if FINANCIAL_EVALUATION_CONTRACT in declared_produces:
        if "financial_evaluation.json" not in stage_outputs:
            raise ValueError("缺少规范输出文件 financial_evaluation.json")
        raw_doc = json.loads(
            read_content(stage_outputs["financial_evaluation.json"]).decode("utf-8"),
            parse_constant=_reject_eval_json_constant,
        )
        return FINANCIAL_EVALUATION_CONTRACT, None, validate_financial_evaluation_doc(raw_doc)

    if EVALUATION_REPORT_CONTRACT in declared_produces:
        if "evaluation_report.json" not in stage_outputs:
            raise ValueError("缺少规范输出文件 evaluation_report.json")
        raw_doc = json.loads(
            read_content(stage_outputs["evaluation_report.json"]).decode("utf-8"),
            parse_constant=_reject_eval_json_constant,
        )
        validated_report = validate_evaluation_report_doc(raw_doc, expected_evaluator=evaluator_ref)
        return EVALUATION_REPORT_CONTRACT, validated_report, None

    raise ValueError(f"评价器 {evaluator_ref!r} 未声明受支持的输出契约: {declared_produces!r}")


def _persist_isolated_evaluator_checkpoint(
    *,
    session_factory: Callable[[], Session],
    save_checkpoint: Callable[..., int],
    claim: Claim,
    number: int,
    idx: int,
    result_entry: Mapping[str, Any],
    report_doc: Mapping[str, Any] | None,
    fin_eval: Mapping[str, Any] | None,
    db_lock: threading.Lock | None,
) -> dict[str, Any]:
    payload_doc = {
        "result_entry": dict(result_entry),
        "report": dict(report_doc) if report_doc is not None else None,
        "financial_evaluation": dict(fin_eval) if fin_eval is not None else None,
    }
    if db_lock is not None:
        with db_lock:
            run_worker_transaction(
                session_factory,
                save_checkpoint,
                claim,
                number,
                idx,
                result_entry=result_entry,
                report=report_doc,
                financial_evaluation=fin_eval,
            )
    else:
        run_worker_transaction(
            session_factory,
            save_checkpoint,
            claim,
            number,
            idx,
            result_entry=result_entry,
            report=report_doc,
            financial_evaluation=fin_eval,
        )
    return payload_doc


def _execute_one_evaluator(
    *,
    plane: ComputeExecutionPlane,
    attempt: AttemptContext,
    claim: Claim,
    number: int,
    idx: int,
    evaluator_ref: str,
    parameters: Mapping[str, Any],
    solution_ref: Mapping[str, Any],
    snapshot: Any,
    artifact: ValidatedAssemblyArtifact,
    finance: EffectiveFinanceConfig,
    materialized: MaterializedSet | FrozenSeriesInputs,
    receipt_dict: Mapping[str, Any] | None,
    session_factory: Callable[[], Session],
    load_checkpoint: Callable[[Session, Claim, int, int], dict[str, Any] | None],
    save_checkpoint: Callable[..., int],
    stop: StopProbe | None = None,
    detach: Any = None,
    cleanup_on_reuse: Callable[[str], None] | None = None,
    preloaded_cached: Any = _UNSET,
    preloaded_sol_content: Any = _UNSET,
    db_lock: threading.Lock | None = None,
) -> dict[str, Any]:
    """Run one evaluator container stage with checkpoint reuse and isolated error capture."""
    cached = (
        run_worker_transaction(session_factory, load_checkpoint, claim, number, idx)
        if preloaded_cached is _UNSET
        else preloaded_cached
    )
    stage_id = f"eval-r{number}-{idx}-{_slugify_stage_id(evaluator_ref)}"
    if cached is not None:
        if cleanup_on_reuse is not None:
            cleanup_on_reuse(stage_id)
        return cached

    sol_content = (
        run_worker_transaction(
            session_factory, get_object_content, int(solution_ref["object_id"])
        )
        if preloaded_sol_content is _UNSET
        else preloaded_sol_content
    )

    outcome_box: list[dict[str, Any]] = []
    stage_result: Any = None

    def _fail_start(exc: BaseException) -> dict[str, Any]:
        if isinstance(exc, ComputationUnavailableError) and exc.reason in ("cancelled", "fenced"):
            raise exc
        if detach is not None and detach.is_set():
            raise exc
        if read_stop_reason(stop) in ("cancelled", "fenced"):
            raise exc
        return _persist_isolated_evaluator_checkpoint(
            session_factory=session_factory,
            save_checkpoint=save_checkpoint,
            claim=claim,
            number=number,
            idx=idx,
            result_entry={
                "evaluator": evaluator_ref,
                "status": "failed",
                "contract": None,
                "error": {
                    "code": "EVAL-START-001",
                    "message": f"评价器 {evaluator_ref} 启动失败: {exc}",
                    "detail": {
                        "reason": getattr(exc, "reason", None) or "evaluator_start_failed",
                        "error_type": type(exc).__name__,
                    },
                },
                "attempt_id": attempt.attempt_id,
                "finished_at": None,
            },
            report_doc=None,
            fin_eval=None,
            db_lock=db_lock,
        )

    try:
        eval_stage = _stage_descriptor(
            plane, evaluator_ref, kind_label="evaluator", session_factory=session_factory
        )
        declared_produces: tuple[str, ...] = ()
        if plane.evaluator_registry is not None:
            try:
                declared_produces = tuple(plane.evaluator_registry.get(evaluator_ref).produces)
            except ComputationUnavailableError:
                with session_factory() as db:
                    local_prod = resolve_local_evaluator_produces(db, evaluator_ref)
                if local_prod is None:
                    raise
                declared_produces = local_prod
        input_files = _build_evaluator_input_files(
            engineering_solution=sol_content,
            snapshot=snapshot,
            artifact=artifact,
            finance=finance,
            materialized=materialized,
            parameters=parameters,
            receipt=receipt_dict,
            bundle_id=attempt.bundle_id,
            receipt_id=f"receipt-{attempt.attempt_id}-{stage_id}",
        )
    except (ComputationUnavailableError, ValueError) as exc:
        return _fail_start(exc)

    def _persist(stage_res: Any) -> Any:
        if stage_res.status == "fenced":
            raise LeaseRejectedError("评价器阶段租约已失效(fenced)")
        if stage_res.status == "cancelled":
            raise StageExecutionError(
                stage="evaluate",
                status="cancelled",
                termination_reason=stage_res.termination_reason,
                detail=f"评价器阶段 {evaluator_ref!r} 已取消",
                resources=dict(stage_res.resources or {}),
            )

        report_doc: dict[str, Any] | None = None
        fin_eval: dict[str, Any] | None = None
        result_entry: dict[str, Any]
        if stage_res.status == "succeeded":
            try:
                contract, report_doc, fin_eval = _parse_evaluator_outputs(
                    stage_res.outputs,
                    evaluator_ref=evaluator_ref,
                    declared_produces=declared_produces,
                )
                result_entry = {
                    "evaluator": evaluator_ref,
                    "status": "succeeded",
                    "contract": contract,
                    "error": None,
                    "attempt_id": attempt.attempt_id,
                    "finished_at": stage_res.finished_at,
                }
            except (KeyError, ValueError, TypeError, json.JSONDecodeError, OSError) as exc:
                report_doc = None
                fin_eval = None
                result_entry = {
                    "evaluator": evaluator_ref,
                    "status": "failed",
                    "contract": None,
                    "error": {
                        "code": "EVAL-PROTOCOL-001",
                        "message": f"评价器 {evaluator_ref} 输出协议校验失败: {exc}",
                        "detail": {"reason": "output_protocol_failed"},
                    },
                    "attempt_id": attempt.attempt_id,
                    "finished_at": stage_res.finished_at,
                }
        else:
            mapped_status = "timed_out" if stage_res.status == "timed_out" else "failed"
            result_entry = {
                "evaluator": evaluator_ref,
                "status": mapped_status,
                "contract": None,
                "error": {
                    "code": f"EVAL-{str(stage_res.status).upper()}",
                    "message": (
                        f"评价器 {evaluator_ref} 执行失败 "
                        f"(status={stage_res.status}, reason={stage_res.termination_reason})"
                    ),
                    "detail": {
                        "termination_reason": stage_res.termination_reason,
                        "exit_code": stage_res.exit_code,
                    },
                },
                "attempt_id": attempt.attempt_id,
                "finished_at": stage_res.finished_at,
            }
        payload_doc = _persist_isolated_evaluator_checkpoint(
            session_factory=session_factory,
            save_checkpoint=save_checkpoint,
            claim=claim,
            number=number,
            idx=idx,
            result_entry=result_entry,
            report_doc=report_doc,
            fin_eval=fin_eval,
            db_lock=db_lock,
        )
        outcome_box.append(payload_doc)
        return payload_doc

    try:
        stage_result = plane.stage_runner.run_stage(
            StageExecutionSpec(
                attempt_id=attempt.attempt_id,
                stage_id=stage_id,
                fencing_token=str(claim.lease_token),
                image=eval_stage.image,
                command=eval_stage.command,
                input_files=input_files,
                required_outputs=(),
                collect_all_outputs=True,
            ),
            persist=_persist,
            stop=stop,
            **({"detach": detach} if detach is not None else {}),
        )
    except LeaseRejectedError:
        raise StageExecutionError(
            stage="evaluate",
            status="fenced",
            termination_reason="fenced",
            detail=f"评价器阶段 {evaluator_ref!r} 租约已失效(fenced)",
        ) from None
    except StageStartError as exc:
        return _fail_start(exc)

    if not outcome_box:
        if stage_result.status == "cancelled":
            raise StageExecutionError(
                stage="evaluate",
                status="cancelled",
                termination_reason=stage_result.termination_reason or "cancelled",
                detail=f"评价器阶段 {evaluator_ref!r} 已取消",
            )
        mapped_status = "timed_out" if stage_result.status == "timed_out" else "failed"
        return _persist_isolated_evaluator_checkpoint(
            session_factory=session_factory,
            save_checkpoint=save_checkpoint,
            claim=claim,
            number=number,
            idx=idx,
            result_entry={
                "evaluator": evaluator_ref,
                "status": mapped_status,
                "contract": None,
                "error": {
                    "code": f"EVAL-{str(stage_result.status).upper()}",
                    "message": (
                        f"评价器阶段 {evaluator_ref!r} 未产出有效检查点 "
                        f"(status={stage_result.status})"
                    ),
                    "detail": {
                        "termination_reason": stage_result.termination_reason,
                        "exit_code": stage_result.exit_code,
                    },
                },
                "attempt_id": attempt.attempt_id,
                "finished_at": stage_result.finished_at,
            },
            report_doc=None,
            fin_eval=None,
            db_lock=db_lock,
        )
    return outcome_box[0]


def run_evaluators_stage(
    *,
    plane: ComputeExecutionPlane,
    attempt: AttemptContext,
    claim: Claim,
    number: int,
    config: CalculationConfig,
    has_feasible_solution: bool,
    solution_ref: Mapping[str, Any] | None,
    snapshot: Any,
    artifact: ValidatedAssemblyArtifact,
    finance: EffectiveFinanceConfig,
    materialized: MaterializedSet | FrozenSeriesInputs,
    receipt_dict: Mapping[str, Any] | None,
    session_factory: Callable[[], Session],
    load_checkpoint: Callable[[Session, Claim, int, int], dict[str, Any] | None],
    save_checkpoint: Callable[..., int],
    stop: StopProbe | None = None,
    detach: Any = None,
    cleanup_on_reuse: Callable[[str], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
    """Execute configured ``evaluators[]`` in parallel or mark skipped when core solution is infeasible."""
    evaluators = tuple(config.evaluators or ())
    if not evaluators:
        return [], [], None
    if not has_feasible_solution or solution_ref is None:
        skipped = [
            {
                "evaluator": str(ev["evaluator"]),
                "status": "skipped",
                "contract": None,
                "error": None,
                "attempt_id": attempt.attempt_id,
                "finished_at": None,
            }
            for ev in evaluators
        ]
        return skipped, [], None

    # Preload checkpoints and sealed solution on the caller thread so parallel container
    # threads never contend on a shared SQLite StaticPool connection.
    preloaded_checkpoints = {
        idx: run_worker_transaction(session_factory, load_checkpoint, claim, number, idx)
        for idx in range(len(evaluators))
    }
    sol_content = run_worker_transaction(
        session_factory, get_object_content, int(solution_ref["object_id"])
    )
    db_lock = threading.Lock()

    def _run_idx(idx_and_ev: tuple[int, Mapping[str, object]]) -> dict[str, Any]:
        idx, ev = idx_and_ev
        return _execute_one_evaluator(
            plane=plane,
            attempt=attempt,
            claim=claim,
            number=number,
            idx=idx,
            evaluator_ref=str(ev["evaluator"]),
            parameters=thaw_json_value(ev.get("parameters") or {}),  # type: ignore[arg-type]
            solution_ref=solution_ref,
            snapshot=snapshot,
            artifact=artifact,
            finance=finance,
            materialized=materialized,
            receipt_dict=receipt_dict,
            session_factory=session_factory,
            load_checkpoint=load_checkpoint,
            save_checkpoint=save_checkpoint,
            stop=stop,
            detach=detach,
            cleanup_on_reuse=cleanup_on_reuse,
            preloaded_cached=preloaded_checkpoints[idx],
            preloaded_sol_content=sol_content,
            db_lock=db_lock,
        )

    items = list(enumerate(evaluators))
    concurrency = int(plane.evaluator_max_concurrency)
    if concurrency <= 0:
        raise ValueError(f"evaluator_max_concurrency must be positive, got {concurrency}")
    if len(items) == 1 or concurrency == 1:
        raw_outcomes = [_run_idx(item) for item in items]
    else:
        with ThreadPoolExecutor(max_workers=min(len(items), concurrency)) as pool:
            futures = [pool.submit(_run_idx, item) for item in items]
            raw_outcomes = []
            for future in futures:
                try:
                    raw_outcomes.append(future.result())
                except BaseException:
                    for pending in futures:
                        pending.cancel()
                    raise

    evaluator_results: list[dict[str, Any]] = []
    evaluation_reports: list[dict[str, Any]] = []
    financial_evaluation: dict[str, Any] | None = None
    for item in raw_outcomes:
        evaluator_results.append(dict(item["result_entry"]))
        if item.get("report") is not None:
            evaluation_reports.append(dict(item["report"]))
        if item.get("financial_evaluation") is not None:
            financial_evaluation = dict(item["financial_evaluation"])
    return evaluator_results, evaluation_reports, financial_evaluation


def run_single_evaluator(
    *,
    plane: ComputeExecutionPlane,
    attempt: AttemptContext,
    claim: Claim,
    idx: int = 0,
    evaluator_ref: str,
    parameters: Mapping[str, Any],
    solution_ref: Mapping[str, Any],
    snapshot: Any,
    artifact: ValidatedAssemblyArtifact,
    finance: EffectiveFinanceConfig,
    materialized: MaterializedSet | FrozenSeriesInputs,
    receipt_dict: Mapping[str, Any] | None,
    session_factory: Callable[[], Session],
    load_checkpoint: Callable[[Session, Claim, int, int], dict[str, Any] | None],
    save_checkpoint: Callable[..., int],
    stop: StopProbe | None = None,
    detach: Any = None,
    cleanup_on_reuse: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Execute one targeted evaluator during a single-evaluator retry attempt."""
    return _execute_one_evaluator(
        plane=plane,
        attempt=attempt,
        claim=claim,
        number=1,
        idx=idx,
        evaluator_ref=evaluator_ref,
        parameters=parameters,
        solution_ref=solution_ref,
        snapshot=snapshot,
        artifact=artifact,
        finance=finance,
        materialized=materialized,
        receipt_dict=receipt_dict,
        session_factory=session_factory,
        load_checkpoint=load_checkpoint,
        save_checkpoint=save_checkpoint,
        stop=stop,
        detach=detach,
        cleanup_on_reuse=cleanup_on_reuse,
    )


def run_algorithm_stage(
    *,
    plane: ComputeExecutionPlane,
    attempt: AttemptContext,
    claim: Claim,
    snapshot: Any,
    artifact: ValidatedAssemblyArtifact,
    materialized: MaterializedSet | FrozenSeriesInputs,
    finance: EffectiveFinanceConfig,
    config: CalculationConfig,
    session_factory: Callable[[], Session],
    save_checkpoint: Callable[..., int],
    stop: StopProbe | None = None,
    detach: Any = None,
) -> EvaluatedStageOutput:
    """一体化 algorithm 模式容器化执行，消费标准 ``solution.json`` 并在 cleanup 前持久化检查点。"""
    if config.algorithm is None:
        raise ValueError("run_algorithm_stage requires config.algorithm")
    algo_stage = _stage_descriptor(
        plane, config.algorithm, kind_label="algorithm", session_factory=session_factory
    )
    if plane.algorithm_registry is not None:
        try:
            plane.algorithm_registry.get(config.algorithm)
        except ComputationUnavailableError:
            if algo_stage is None:
                raise

    request_payload: dict[str, Any] = {
        "artifact": {
            "canonical_text": artifact.canonical_text,
            "receipt": thaw_json_value(snapshot.assembly_receipt),
        },
        "device_models": thaw_json_value(snapshot.device_models),
        "finance": finance.to_dict(),
        "calculation_config": config.to_dict(),
        "parameters": thaw_json_value(config.slot_options("algorithm")),
        "evaluation": thaw_json_value(snapshot.evaluation),
        "time_limit_seconds": int(config.time_limit_seconds),
        "seed": int(config.seed if config.seed is not None else snapshot.random_seed),
        "attempt": {
            "attempt_id": attempt.attempt_id,
            "bundle_id": attempt.bundle_id,
            "assembly_id": attempt.assembly_id,
            "calculation_config_id": attempt.calculation_config_id,
        },
    }
    input_files: dict[str, Content | bytes] = {}
    _attach_series_inputs(request_payload, input_files, materialized)
    input_files["request.json"] = _json_bytes(request_payload)

    evaluated: EvaluatedStageOutput | None = None

    def _persist(stage_result: Any) -> Any:
        nonlocal evaluated
        if stage_result.status != "succeeded":
            return _handle_non_succeeded_stage(
                "algorithm", stage_result, claim, 1, session_factory
            )
        include_solution = "solution" in config.outputs
        with tempfile.TemporaryDirectory(prefix="ies-algo-project-") as tmp_dir:
            sol_path = Path(tmp_dir) / "solution.json"
            hourly_path = Path(tmp_dir) / "hourly_series.json"
            outcome = project_engineering_solution(
                stage_result.outputs["solution.json"],
                bundle_id=attempt.bundle_id,
                receipt_id=f"receipt-{attempt.attempt_id}-algorithm-r1",
                outputs=config.outputs,
                solution_output_path=sol_path,
                hourly_output_path=(hourly_path if include_solution else None),
                stop=stop,
            )
            cr = outcome.compute_result
            cr_dict = cr.to_dict()
            feasible = cr.math_status in ("solved", "feasible")
            planning_vars = (
                [dict(item) for item in outcome.planning_variables]
                if feasible and include_solution
                else []
            )
            hourly_meta = (
                dict(outcome.hourly_meta)
                if feasible and include_solution and outcome.hourly_meta is not None
                else None
            )
            hourly_content = outcome.hourly_content if feasible and include_solution else None
            if planning_vars:
                cr_dict["outputs"] = {
                    **cr_dict["outputs"],
                    "solution": {"variables": planning_vars},
                }
                cr = ComputeResult(
                    schema=cr.schema,
                    schema_version=cr.schema_version,
                    bundle_id=cr.bundle_id,
                    receipt_id=cr.receipt_id,
                    execution_status=cr.execution_status,
                    math_status=cr.math_status,
                    outputs=cr_dict["outputs"],
                )
            evaluated = EvaluatedStageOutput(
                result=cr,
                hourly_meta=hourly_meta,
                planning_variables=tuple(planning_vars),
                resources=dict(stage_result.resources or {}),
            )
            # Durably persist container algorithm payload with Fencing Token BEFORE cleanup_stage.
            run_worker_transaction(
                session_factory,
                save_checkpoint,
                claim,
                cr_dict,
                solution_content=FileContent(sol_path, sol_path.stat().st_size),
                hourly_content=hourly_content,
                hourly_meta=hourly_meta,
                planning_variables=planning_vars,
            )
            return evaluated

    try:
        stage_result = plane.stage_runner.run_stage(
            StageExecutionSpec(
                attempt_id=attempt.attempt_id,
                stage_id="algorithm-r1",
                fencing_token=str(claim.lease_token),
                image=algo_stage.image,
                command=algo_stage.command,
                input_files=input_files,
                required_outputs=("solution.json",),
                collect_all_outputs=True,
            ),
            persist=_persist,
            stop=stop,
            **({"detach": detach} if detach is not None else {}),
        )
    except LeaseRejectedError:
        raise StageExecutionError(
            stage="algorithm",
            status="fenced",
            termination_reason="fenced",
            detail="一体化算法阶段租约已失效(fenced)",
        ) from None

    if stage_result.status != "succeeded":
        _raise_stage_error("algorithm", stage_result)
    assert evaluated is not None
    return evaluated


def build_attempt_context(task: Any, claim: Claim, snapshot: Any) -> AttemptContext:
    """派生回执四项身份（快照/任务派生，不透明字符串，不做 hash）。

    assembly_id 直接取规范文本装配 id；calculation_config_id 直接取快照
    config_ref（project:revision）。提交闸门本应冻结真实 id/revision：
    缺失即实际结构错误（KeyError）按快照故障可见失败，不伪造回落身份。
    bundle_id 指向本次生成的 Bundle（快照:尝试派生；Bundle 本体在执行
    时创建，无闸门冻结值可恢复）。
    """
    doc = json.loads(snapshot.canonical_assembly_text)
    ref = snapshot.calc_config_snapshot["config_ref"]
    return AttemptContext(
        attempt_id=str(claim.attempt_id),
        bundle_id=f"snapshot-{snapshot.id}:attempt-{claim.attempt_id}",
        assembly_id=str(doc["assembly"]["id"]),
        calculation_config_id=f"project-{ref['project_id']}:config-rev-{ref['revision']}",
    )


#: hourly 结果对象在输出文件组内的规范路径与媒体类型。
HOURLY_OBJECT_PATH = "hourly/series.json"
HOURLY_OBJECT_MEDIA_TYPE = "application/json"


def result_step_series(outputs: Mapping[str, object]) -> dict[str, dict]:
    """Project the two adapter maps into the public discriminated directory."""
    result = {}
    for kind, key in (("device", "series"), ("system", "system_series")):
        for identity, entry in outputs.get(key, {}).items():
            result[identity] = {"series_id": identity, "kind": kind, **entry}
    return result


def build_hourly_files(
    outputs: Mapping[str, object],
) -> dict[str, tuple[Content, str]]:
    """已恢复统一序列 → hourly 对象字节（只序列化已恢复序列）。

    消费 ``ComputeResult.outputs["series"]``（ResultAdapter 按生成器声明
    用同一次真实解恢复的业务单位统一序列）；无该键或为空时返回空组
    （metrics-only/无可发布步变量诚实缺席，不伪造空对象）。
    对象形状复用既有 ``{data, meta.units}`` 语义，并保留序列身份元数据
    （``meta.series``），供读取 DTO 公开；键排序规范 JSON。
    本函数不解释求解列、不做长度/hash 复核；残缺即自然错误可见。
    （输入为冻结映射，判定用 ``Mapping`` 而非 ``dict``。）
    无 ``series`` 键即未请求该输出（metrics-only），合法缺席；
    键存在但集合为空即无可发布步变量，同样缺席；其余形态直接消费。
    """
    series = result_step_series(outputs)
    if not series:
        return {}
    data: dict[str, Sequence[float]] = {}
    units: dict[str, str] = {}
    identities: dict[str, dict[str, str]] = {}
    for series_id in sorted(series):
        entry = series[series_id]
        data[series_id] = entry["values"]  # immutable shared sequence, no second value array
        units[series_id] = entry["unit"]  # type: ignore[index]
        identities[series_id] = {key: value for key, value in entry.items() if key != "values"}
    value = {"data": data, "meta": {"units": units, "series": identities}}
    return {HOURLY_OBJECT_PATH: (GeneratedContent(partial(json_chunks, value)), HOURLY_OBJECT_MEDIA_TYPE)}


def seal_output_objects(
    db: Session, files: Mapping[str, tuple[Content | bytes, str]], *, actor_id: int | None
) -> dict[str, dict[str, object]]:
    """声明文件字节以存储对象无损封存，返回按路径索引的描述子。

    字节原样入对象（不做文本校验与编码猜测）；媒体类型取 manifest/回执
    的声明值，由调用方按声明传入。提交由调用方短会话负责。
    """
    sealed: dict[str, dict[str, object]] = {}
    for path in sorted(files):
        data, media_type = files[path]
        handle = put_content(
            db,
            MemoryContent(data) if isinstance(data, bytes) else data,
            media_type,
            source_category="evidence",
            purpose="evidence_output",
            actor_id=actor_id,
        )
        sealed[path] = {
            "object_id": handle.id,
            "media_type": media_type,
            "size_bytes": handle.size_bytes,
        }
    return sealed


def attach_evidence_objects(
    db: Session,
    evidence_id: int,
    sealed: Mapping[str, Mapping[str, object]],
    *,
    actor_id: int | None,
) -> None:
    """封存对象挂接证据引用（防清理误删；只 flush，提交由调用方负责）。"""
    for path in sorted(sealed):
        add_ref(
            db,
            int(sealed[path]["object_id"]),
            "evidence_package",
            evidence_id,
            purpose="evidence_output",
            actor_id=actor_id,
        )


def business_outcome_for(result: ComputeResult) -> str | None:
    """统一结果 → 任务业务结局显式映射（非 completed 执行返回 None）。

    completed + solved → normal_completion；completed + feasible →
    restricted_results（可用解、未证最优）；completed + 不可行/无界 →
    no_recommendation（正常完成、无可推荐方案）；completed + 数值失败/
    未求解 → insufficient_evidence（正常完成、无可用证据）。未知状态
    返回 None（调用方按执行失败可见处理，不猜测）。
    """
    if result.execution_status != "completed":
        return None
    math_status = result.math_status
    if math_status == "solved":
        outcome = "normal_completion"
    elif math_status == "feasible":
        outcome = "restricted_results"
    elif math_status in ("infeasible", "unbounded"):
        outcome = "no_recommendation"
    elif math_status in ("numeric_failure", "not_solved"):
        outcome = "insufficient_evidence"
    else:
        return None
    if outcome not in BUSINESS_OUTCOMES:
        return None
    return outcome
