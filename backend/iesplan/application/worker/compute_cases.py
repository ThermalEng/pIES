"""Worker 计算阶段命令(application/worker.compute_cases): 快照输入恢复与三段调用。

本模块提供 worker.md 规定的生成 / 执行 / 适配公开阶段命令，Worker 只按
返回的公开 contract 驱动状态机，不复制领域规则：

- ``load_compute_inputs``：把快照冻结的直接输入恢复为生成器可消费
  形态并按序返回（规范装配二件套结构恢复、逐实例数学贡献结构恢复、
  财务快照按冻结 profile/revision 取不可变行、计算配置显式字段恢复并烘焙
  快照冻结的有效种子、固定资源字节加载、非预测序列物化）。本层不包装第
  二套输入对象，不做缺省；冻结结构缺件即实际结构错误（KeyError/TypeError/
  ValueError 等）直接可见，不重跑输入校验。
- ``generate_bundle`` / ``execute_bundle`` / ``adapt_result``：经组合根
  给定的已发布目录与运行时完成三段调用，本层不选型、不回退；生成后核对
  Bundle 声明与快照冻结 provider 一致并预先解析执行器/适配器（不可解析
  即明确失败，不等到求解后）。
- ``seal_output_objects`` / ``attach_evidence_objects``：声明文件字节以
  存储对象无损封存并挂接证据引用（字节原样入对象，不做文本校验与编码
  猜测；媒体类型取 manifest/回执声明）。
- ``build_attempt_context`` / ``business_outcome_for``：回执身份派生与
  任务业务结局映射（只做 contract 允许的显式映射）。
- ``build_financial_evaluation``：同次解财务事实投影与封存评价（只处理
  已求解且含 solution 输出的结果，其余返回 None；只投影适配器恢复的
  同次解财务事实并调用 finance 既有评价纯函数，不重建税基/VAT/亏损算法）。

本模块只 flush-free：只读 DB 与纯内存转换，不拥有事务、不 commit/
rollback（对象封存的提交由调用方短会话负责）。预测需求
（``pending_predict`` 或配置 ``predictions`` 非空）走
``ComputationUnavailableError(reason="prediction-not-supported")``
明确报告，不 stub、不回退旧链。

依赖方向: worker → application → (configuration/dataset/project/
storage/领域门面 + computation 公开门面)。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from iesplan import configuration as configuration_domain
from iesplan.application.worker.lease_cases import Claim, LeaseRejectedError
from iesplan.application.worker.transactions import run_worker_transaction
from iesplan.assembly import ValidatedAssemblyArtifact, ValidationReceipt
from iesplan.computation import (
    COMPUTE_RESULT_SCHEMA,
    COMPUTE_RESULT_VERSION,
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
    check_convergence,
    collect_resource_ids,
    materialize_non_predictive_series,
    prediction_delta,
    split_predictions_by_dependency,
    thaw_json_value,
)
from iesplan.computation.bundle import export_bundle_files
from iesplan.computation.generators.highs_milp import SOLVER_REF
from iesplan.computation.runtime.stage_runner import StageExecutionSpec
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
    "adapt_result",
    "attach_evidence_objects",
    "build_attempt_context",
    "build_financial_evaluation",
    "build_hourly_files",
    "business_outcome_for",
    "complete_materialized",
    "convergence_for_round",
    "generate_bundle",
    "hourly_files_from_payload",
    "load_compute_inputs",
    "predict_round_stage",
    "prediction_delta",
    "result_step_series",
    "run_algorithm_stage",
    "seal_output_objects",
    "split_predictions_by_dependency",
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
    """Container-produced evaluator / algorithm outputs preserved without backend recomputation."""

    result: ComputeResult
    hourly_series: Mapping[str, Any] | None = None
    financial_evaluation: Mapping[str, Any] | None = None
    resources: Mapping[str, Any] = field(default_factory=dict)

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
    algorithm_registry: Any | None = None


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
    seed = body["seed"]
    if not isinstance(seed, int) or isinstance(seed, bool):
        seed = snapshot.random_seed
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


def _stage_descriptor(plane: ComputeExecutionPlane, ref: str, *, kind_label: str) -> Any:
    try:
        return plane.stage_catalog.get_by_ref(ref)
    except KeyError:
        raise ComputationUnavailableError(
            f"{kind_label}阶段容器未发布: {ref!r}",
            reason="no-provider",
        ) from None


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


def _evaluated_output_from_result_json(
    raw: Mapping[str, Any],
    *,
    resources: Mapping[str, Any],
) -> EvaluatedStageOutput:
    """Validate `schema`/`schema_version` at the external boundary and construct `ComputeResult`."""
    schema = raw["schema"]
    schema_version = raw["schema_version"]
    if schema != COMPUTE_RESULT_SCHEMA:
        raise ValueError(f"结果 schema 非法: {schema!r}")
    if schema_version != COMPUTE_RESULT_VERSION:
        raise ValueError(f"结果版本非法: {schema_version!r}")
    outputs = dict(raw.get("outputs") or {})
    hourly_series = outputs.pop("hourly_series", None)
    financial_evaluation = outputs.pop("financial_evaluation", None)
    adapted = ComputeResult(
        schema=schema,
        schema_version=schema_version,
        bundle_id=str(raw["bundle_id"]),
        receipt_id=str(raw["receipt_id"]),
        execution_status=str(raw["execution_status"]),
        math_status=str(raw["math_status"]),
        outputs=outputs,
    )
    return EvaluatedStageOutput(
        result=adapted,
        hourly_series=hourly_series,
        financial_evaluation=financial_evaluation,
        resources=dict(resources),
    )


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
        [Session, Claim, int, int, tuple[MaterializedSeries, ...], tuple[PredictedTarget, ...]],
        dict[str, dict[str, object]],
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
    previous: Mapping[str, object] | None = None,
    stop: StopProbe | None = None,
    detach: Any = None,
    on_stage_resources: Callable[[Mapping[str, Any]], None] | None = None,
) -> tuple[
    tuple[MaterializedSeries, ...],
    tuple[PredictedTarget, ...],
    dict[str, dict[str, object]],
]:
    """预测阶段容器化执行：读取历史字节 → job_runner 预测阶段 → 检查点落库后清理。"""
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
            plane.predictor_registry.get(algo_ref)
            descriptor = _stage_descriptor(plane, algo_ref, kind_label="predictor")
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
        input_files: dict[str, Content | bytes] = {
            "request.json": _json_bytes(
                {
                    "point_count": point_count,
                    "seed": seed,
                    "previous": thaw_json_value(previous) if previous is not None else None,
                    "targets": group_targets,
                }
            )
        }
        for item in group_targets:
            oid = item["history_object_id"]
            csv_rel = item["history_csv_rel"]
            if csv_rel not in input_files:
                input_files[csv_rel] = resources[oid]

        stage_id = f"predict-r{number}" if len(sorted_algos) == 1 else f"predict-r{number}-{idx}"
        group_series: list[MaterializedSeries] = []
        group_predicted: list[PredictedTarget] = []

        def _persist(stage_result: Any, *, group_idx: int = idx) -> Any:
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
                    group_idx,
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


def convergence_for_round(
    plane: ComputeExecutionPlane,
    coupled_entries: tuple[Mapping[str, object], ...],
    previous_predicted: Mapping[tuple[str, str], PredictedTarget],
    current_predicted: Mapping[tuple[str, str], PredictedTarget],
    previous_outputs: Mapping[str, object] | None,
    current_outputs: Mapping[str, object],
) -> tuple[float, tuple[dict[str, object], ...]]:
    """逐耦合目标调用算法收敛度量并汇总（求解完成后的公开输出边界）。"""
    records: list[dict[str, object]] = []
    for entry in coupled_entries:
        target = entry["target"]
        key = (str(target["instance"]), str(target["interface"]))
        algorithm = str(entry["algorithm"])
        predictor = plane.predictor_registry.get(algorithm)
        convergence = check_convergence(
            predictor.measure_convergence(
                previous_prediction=previous_predicted[key],
                current_prediction=current_predicted[key],
                previous_outputs=previous_outputs,
                current_outputs=current_outputs,
                parameters=dict(entry.get("parameters") or {}),
            )
        )
        records.append(
            {
                "device": key[0],
                "interface": key[1],
                "algorithm": algorithm,
                "metric": convergence.metric,
                "value": convergence.value,
                "unit": convergence.unit,
            }
        )
    records.sort(key=lambda record: (str(record["device"]), str(record["interface"])))
    aggregate = max(float(record["value"]) for record in records)
    return aggregate, tuple(records)


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
    """经生成器阶段容器生成完整 Bundle，并在 cleanup 前带 Fencing Token 持久化检查点。"""
    del models, evaluation
    provider = plane.generator_registry.resolve(config)
    descriptor = provider.descriptor
    if snapshot.executor not in descriptor.executors or snapshot.result_adapter != descriptor.result_adapter:
        raise ComputationUnavailableError(
            "生成器声明与快照冻结 provider 不一致 "
            f"(snapshot={snapshot.executor!r}/{snapshot.result_adapter!r})",
            reason="frozen-provider-mismatch",
        )
    gen_stage = _stage_descriptor(plane, provider.ref, kind_label="generator")
    solver_ref = config.solver or SOLVER_REF
    _stage_descriptor(plane, solver_ref, kind_label="solver")
    _stage_descriptor(plane, snapshot.result_adapter, kind_label="evaluator")

    request_payload: dict[str, Any] = {
        "artifact": {
            "canonical_text": artifact.canonical_text,
            "receipt": thaw_json_value(snapshot.assembly_receipt),
        },
        "device_models": thaw_json_value(snapshot.device_models),
        "finance": finance.to_dict(),
        "calculation_config": config.to_dict(),
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
        declared_executor = manifest.command["executor"]
        if (
            declared_executor != snapshot.executor
            or manifest.result_adapter != snapshot.result_adapter
        ):
            raise ComputationUnavailableError(
                "Bundle 声明与快照冻结 provider 不一致 "
                f"(bundle={declared_executor!r}/{manifest.result_adapter!r}, "
                f"snapshot={snapshot.executor!r}/{snapshot.result_adapter!r})",
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


def adapt_result(
    plane: ComputeExecutionPlane,
    bundle: SolverBundle,
    receipt: ExecutionReceipt,
    collected: Mapping[str, object],
    *,
    claim: Claim,
    number: int,
    finance: EffectiveFinanceConfig,
    snapshot: Any,
    session_factory: Callable[[], Session],
    save_checkpoint: Callable[[Session, Claim, int, Mapping[str, Any]], int],
    stop: StopProbe | None = None,
    detach: Any = None,
) -> EvaluatedStageOutput:
    """经评价器阶段容器解释结果，在 cleanup 前带 Fencing Token 持久化检查点。"""
    eval_stage = _stage_descriptor(plane, receipt.result_adapter_ref, kind_label="evaluator")
    input_files: dict[str, Content | bytes] = {
        f"bundle/{rel}": content for rel, content in export_bundle_files(bundle).items()
    }
    for rel, content in collected.items():
        if isinstance(content, (Content, bytes)):
            # Part 1 evaluator searches directly under /work/in/outputs/ (non-recursive).
            staged_rel = Path(rel).name if rel.startswith("output/") else rel
            input_files[f"outputs/{staged_rel}"] = content
    input_files["receipt.json"] = _json_bytes(receipt.to_dict())
    input_files["request.json"] = _json_bytes(
        {
            "finance": finance.to_dict(),
            "evaluation": thaw_json_value(snapshot.evaluation),
        }
    )

    evaluated: EvaluatedStageOutput | None = None

    def _persist(stage_result: Any) -> Any:
        nonlocal evaluated
        if stage_result.status != "succeeded":
            return _handle_non_succeeded_stage(
                "evaluate", stage_result, claim, number, session_factory
            )
        raw = json.loads(read_content(stage_result.outputs["result.json"]).decode("utf-8"))
        evaluated = _evaluated_output_from_result_json(
            raw, resources=stage_result.resources or {},
        )
        # Durably persist container evaluation payload with Fencing Token BEFORE cleanup_stage.
        run_worker_transaction(session_factory, save_checkpoint, claim, number, raw)
        return evaluated

    try:
        stage_result = plane.stage_runner.run_stage(
            StageExecutionSpec(
                attempt_id=str(claim.attempt_id),
                stage_id=f"evaluate-r{number}",
                fencing_token=str(claim.lease_token),
                image=eval_stage.image,
                command=eval_stage.command,
                input_files=input_files,
                required_outputs=("result.json",),
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
            detail="结果适配阶段租约已失效(fenced)",
        ) from None

    if stage_result.status != "succeeded":
        _raise_stage_error("evaluate", stage_result)
    assert evaluated is not None
    return evaluated


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
    save_checkpoint: Callable[[Session, Claim, Mapping[str, Any]], int],
    stop: StopProbe | None = None,
    detach: Any = None,
) -> EvaluatedStageOutput:
    """一体化 algorithm 模式容器化执行，并在 cleanup 前持久化检查点。"""
    if config.algorithm is None:
        raise ValueError("run_algorithm_stage requires config.algorithm")
    if plane.algorithm_registry is not None:
        plane.algorithm_registry.resolve(config)
    algo_stage = _stage_descriptor(plane, config.algorithm, kind_label="algorithm")

    request_payload: dict[str, Any] = {
        "artifact": {
            "canonical_text": artifact.canonical_text,
            "receipt": thaw_json_value(snapshot.assembly_receipt),
        },
        "device_models": thaw_json_value(snapshot.device_models),
        "finance": finance.to_dict(),
        "calculation_config": config.to_dict(),
        "evaluation": thaw_json_value(snapshot.evaluation),
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
        raw = json.loads(read_content(stage_result.outputs["result.json"]).decode("utf-8"))
        evaluated = _evaluated_output_from_result_json(
            raw, resources=stage_result.resources or {},
        )
        # Durably persist container algorithm payload with Fencing Token BEFORE cleanup_stage.
        run_worker_transaction(session_factory, save_checkpoint, claim, raw)
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
                required_outputs=("result.json",),
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


def hourly_files_from_payload(
    hourly_series: Mapping[str, Any] | None,
) -> dict[str, tuple[Content, str]]:
    """Serialize container-produced `hourly_series` payload into output files without recomputation."""
    if not hourly_series:
        return {}
    return {
        HOURLY_OBJECT_PATH: (
            GeneratedContent(partial(json_chunks, dict(hourly_series))),
            HOURLY_OBJECT_MEDIA_TYPE,
        )
    }


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


def build_financial_evaluation(
    snapshot: Any,
    finance: EffectiveFinanceConfig,
    result: ComputeResult,
) -> dict[str, object] | None:
    """同次解财务事实 → 封存评价(ies.financial-evaluation@1.0.0)。

    只处理有可行解（已证最优或受限）且含 solution 输出的结果; 其余状态返回 None(调用方
    不写财务块, 不把不可行/缺证据结果伪装为有现金流的方案, 缺块语义
    沿既有 ``financial_absence``)。本函数只投影适配器恢复的同次解财务
    事实并调用 finance 既有评价纯函数, 不重建税基/VAT/亏损算法, 不读
    DB/最新配置, 不重跑提交校验: 配套 5.0.0 事实缺件即实际结构错误
    直接可见失败, 不补空、不回退。
    """
    from decimal import Decimal

    from iesplan.finance import evaluate_project_investment

    if result.math_status not in ("solved", "feasible"):
        return None
    # outputs 为 Mapping 由 ComputeResult 构造时保证; 此处只分支用户结果物
    # 发布选项(metrics-only 无 solution 即不评价, 不伪装)。
    if "solution" not in result.outputs:
        return None
    # financial_facts 由配套适配器用同一次主解恢复并保证; 缺件即自然
    # KeyError 可见, 不补空、不复核。
    facts = result.outputs["financial_facts"]
    evaluation = snapshot.evaluation
    period = int(evaluation["construction_period"])
    operation = int(evaluation["operation_years"])
    years = int(evaluation["evaluation_years"])
    rate = float(evaluation["discount_rate"])

    currency = finance.currency
    # 适配输出为协议冻结值（tuple/MappingProxy）；封存载荷面向 JSON，
    # 用协议层互逆函数解冻为普通结构，金额字符串本身不改写。公开事实
    # 已为手册形状，此处只取键投影，不重组税务结构、不改写金额。
    tax_cash = thaw_json_value(facts["tax_cash"])
    flows = [value for value in facts["annual_net_flows"]]
    comparison = thaw_json_value(facts["comparison"])
    evaluation_flows = comparison["evaluation_net_flows"] if comparison["mode"] == "difference" else flows
    assessed = evaluate_project_investment(
        [Decimal(value) for value in evaluation_flows],
        rate,
        years,
        currency=currency,
        price_basis=finance.price_basis,
        base_year=finance.base_year,
    )
    return {
        "schema": "ies.financial-evaluation",
        "schema_version": "1.0.0",
        "status": "evaluated",
        "evaluation_years": years,
        # K/N 回显冻结输入(展示时轴映射用, 不做第二权威读取)。
        "construction_period": period,
        "operation_years": operation,
        "currency": currency,
        "price_basis": finance.price_basis,
        "base_year": finance.base_year,
        "profile_id": finance.profile_id,
        "discount_rate": rate,
        "construction_total": facts["construction_total"],
        # 公开事实金额统摄于顶层 currency；封存按手册保留独立单位字段。
        "construction_total_unit": currency,
        "annual_fixed_om": facts["annual_fixed_om"],
        "payments_applied": [
            {"t": entry["t"], "ratio": entry["ratio"], "amount": entry["amount"]}
            for entry in facts["payments_applied"]
        ],
        "net_cashflows": thaw_json_value(facts["net_cashflows"]),
        "cost_items": thaw_json_value(facts["cost_items"]),
        "amounts": thaw_json_value(facts["amounts"]),
        "cash_movements": thaw_json_value(facts["cash_movements"]),
        "assets": thaw_json_value(facts["assets"]),
        "construction_units": thaw_json_value(facts["construction_units"]),
        "annual_operation": [
            {
                "financial_year": entry["financial_year"],
                "energy_net": entry["energy_net"],
                "variable_om": entry["variable_om"],
                "fixed_om": entry["fixed_om"],
                "maintenance": entry["maintenance"],
                "demand_charge": entry["demand_charge"],
                "net": entry["net"],
            }
            for entry in facts["annual_operation"]
        ],
        "monthly_demand": thaw_json_value(facts["monthly_demand"]),
        # 同次解税务事实原样投影(结构与金额不重算、不改写)。
        "tax_cash": tax_cash,
        # 完整税后现金流 CF[0..E](与目标同一模型表达式，未折现)。
        "annual_net_flows": list(flows),
        "comparison": comparison,
        "evaluation": assessed.to_dict(),
    }


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
