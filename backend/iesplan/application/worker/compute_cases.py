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
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from iesplan import configuration as configuration_domain
from iesplan.application.worker.lease_cases import Claim
from iesplan.assembly import ValidatedAssemblyArtifact, ValidationReceipt
from iesplan.computation import (
    AttemptContext,
    CalculationConfig,
    ComputationUnavailableError,
    ComputeResult,
    ExecutionReceipt,
    ExecutorRegistry,
    GeneratorRegistry,
    MaterializedPriceSeries,
    MaterializedSeries,
    MaterializedSet,
    PredictedTarget,
    PredictorRegistry,
    SolverBundle,
    SolverRuntime,
    StopProbe,
    check_convergence,
    materialize_non_predictive_series,
    materialize_predicted_series,
    prediction_delta,
    split_predictions_by_dependency,
    thaw_json_value,
)
from iesplan.computation.result_adapter import ResultAdapterRegistry
from iesplan.core.contracts import horizon_point_count
from iesplan.core.jsonutil import canonical_json
from iesplan.core.diagnostics import Diagnostic
from iesplan.finance import EffectiveFinanceConfig, EvaluationInput
from iesplan.modeling import DeviceMathContribution, contribution_from_dict
from iesplan.storage import add_ref, get_object, put_object
from iesplan.tasks import BUSINESS_OUTCOMES

__all__ = [
    "HOURLY_OBJECT_PATH",
    "ComputeExecutionPlane",
    "adapt_result",
    "attach_evidence_objects",
    "build_attempt_context",
    "build_financial_evaluation",
    "build_hourly_files",
    "business_outcome_for",
    "complete_materialized",
    "convergence_for_round",
    "execute_bundle",
    "generate_bundle",
    "load_compute_inputs",
    "predict_selected_round",
    "predict_target_round",
    "prediction_delta",
    "seal_output_objects",
    "split_predictions_by_dependency",
]


@dataclass(frozen=True, slots=True)
class ComputeExecutionPlane:
    """计算执行面句柄（组合根装配、Worker 持有、阶段命令消费）。

    五个成员均为已发布/已装配实例：生成器目录、执行器目录、结果适配器
    目录、预测算法目录与通用运行时。API 进程不装配执行面（执行器目录/
    运行时/策略只归计算 Worker 组合根；预测目录随生成器目录在 API 与
    Worker 上下文同时装配，供提交相容核对与执行预测使用）。
    """

    generator_registry: GeneratorRegistry
    executor_registry: ExecutorRegistry
    adapter_registry: ResultAdapterRegistry
    runtime: SolverRuntime
    predictor_registry: PredictorRegistry


#: 快照计算配置正文恢复的显式字段（task_params/config_ref 等提交
#: 绑定键不进入 CalculationConfig，不在白名单的键直接丢弃而非猜测）。
_CONFIG_FIELDS = (
    "schema",
    "schema_version",
    "mode",
    "predictions",
    "generator",
    "solver",
    "precision",
    "convergence",
    "time_limit_seconds",
    "options",
    "seed",
    "outputs",
)


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
        diagnostics=tuple(Diagnostic(**dict(entry)) for entry in data["diagnostics"]),
    )


def _collect_resource_ids(doc: Mapping[str, Any]) -> set[str]:
    """从规范装配 devices 绑定收集 content.object_id（字符串身份集合）。

    规范文本中的引用身份恒为字符串（提交侧由整数主键稳定序列化）；
    读取时数字串转回整数主键走主键通道，非数字串走 oid 通道。
    """
    found: set[str] = set()

    def collect(node: object) -> None:
        if isinstance(node, Mapping):
            content = node.get("content")
            if isinstance(content, Mapping) and "object_id" in content:
                found.add(str(content["object_id"]))
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    devices = doc.get("devices")
    if isinstance(devices, Mapping):
        collect(devices)
    return found


def _restore_artifact_resources_config(
    db: Session, task_project_id: int, snapshot: Any
) -> tuple[
    ValidatedAssemblyArtifact, dict[str, bytes], CalculationConfig, int,
]:
    """从快照恢复装配产物、固定资源字节、计算配置与有效种子。

    finance 按回执冻结血缘 {profile_id, revision} 取不可变 Effective 行
    （与提交闸门同一来源：钉选 revision，不读 latest/current 指针）；
    固定资源按规范绑定 object_id 经 storage 公开门面读字节；有效种子取
    快照冻结值（配置缺省时不再静默用 0，而是提交闸门同一派生规则）。
    冻结缺件即实际结构错误直接可见，不重跑输入校验。
    """
    receipt = _receipt_from_frozen(snapshot.assembly_receipt)
    artifact = ValidatedAssemblyArtifact(canonical_text=snapshot.canonical_assembly_text, receipt=receipt)

    body = snapshot.calc_config_snapshot
    picked = {name: body[name] for name in _CONFIG_FIELDS}
    seed = picked.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        seed = snapshot.random_seed
    config = CalculationConfig(**{**picked, "seed": seed})

    doc = json.loads(snapshot.canonical_assembly_text)
    resource_ids = _collect_resource_ids(doc)
    resources = {}
    for object_id in sorted(resource_ids):
        key: int | str = (
            int(object_id) if object_id.isdigit() else object_id
        )
        resources[object_id] = get_object(db, key)
    return artifact, resources, config, seed


def load_compute_inputs(
    db: Session, task_project_id: int, snapshot: Any
) -> tuple[
    ValidatedAssemblyArtifact,
    dict[str, DeviceMathContribution],
    MaterializedSet,
    EffectiveFinanceConfig,
    CalculationConfig,
    EvaluationInput,
]:
    """从快照恢复生成器直接输入并按序返回（不包装第二对象，不做缺省）。

    finance 按回执冻结血缘取不可变 Effective 行；第三项为物化集合本身
    （预测目标序列不在此物化，``pending_predict`` 保留），由
    ``predict_target_round`` 经公开 predictor 完成后再与此处集合并为
    完成态。序列按快照顶层评价确认块的已确认运营年限物化为运营期长度；
    评价确认经 ``EvaluationInput.from_frozen`` 从同一冻结块直接恢复
    类型化事实（只还原类型，不重跑提交校验），独立直传。提交边界唯一
    校验，此处只读不验。冻结缺件即实际结构错误直接可见，不重跑输入校验。
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
    materialized = materialize_non_predictive_series(
        artifact, resources, operation_years=operation_years
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
    wanted: list[str] = []
    for binding_id in sorted(doc.get("tariff_bindings") or {}):
        price_id = (doc["tariff_bindings"][binding_id] or {}).get("price")
        price = (finance.energy_prices or {}).get(price_id)
        if price is not None and price.kind in ("time_series", "monthly_daily") and price_id not in wanted:
            wanted.append(price_id)
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
    base: MaterializedSet, predicted: tuple[MaterializedSeries, ...]
) -> MaterializedSet:
    """预测产物并入为完成态集合（Generator 第三项输入形态）。

    年限沿用基集合携带的已确认值，不重读快照；预测阶段交付全部
    ``pending``（提交闸门 TASK-REQ-010/011 保证配置与绑定双向覆盖），
    此处只构成完成态，不复核。已物化价格区沿基集合透传。
    """
    return MaterializedSet(
        operation_years=base.operation_years,
        series=tuple(base.series) + tuple(predicted),
        pending_predict=(),
        prices=base.prices,
    )


def _predict_entries(
    artifact: ValidatedAssemblyArtifact,
    resources: Mapping[str, bytes],
    entries: tuple[Mapping[str, object], ...],
    resolve_predictor: Any,
    seed: int | None,
    *,
    operation_years: int,
    previous: Mapping[str, object] | None = None,
    allow_subset: bool = False,
) -> tuple[tuple[MaterializedSeries, ...], tuple[PredictedTarget, ...]]:
    """按给定配置条目经公开 predictor 物化运营期序列（无数据库写入）。

    历史列按绑定接口名投影，目标类型不透明透传；返回均按（实例，接口）
    稳定排序。运营期点数取权威推导，不以 predictor 输出长度自洽。
    ``allow_subset`` 为真时只物化给定条目命中的绑定（耦合轮次用）。
    """
    if not entries:
        return (), ()
    predicted = materialize_predicted_series(
        artifact, resources, tuple(entries), resolve_predictor, seed,
        operation_years=operation_years,
        previous=previous,
        allow_subset=allow_subset,
    )
    baseline_data = json.loads(artifact.canonical_text)["project_baseline"]
    # 运营期点数取权威推导（与非预测物化同一来源），不以 predictor 输出
    # 长度自洽：短/空输出由 MaterializedSeries 自身长度不变式自然拒绝。
    point_count = horizon_point_count(
        baseline_data["resolution"], baseline_data["start_year"], operation_years
    )
    series = tuple(
        MaterializedSeries(
            device=item.device,
            interface=item.interface,
            mode="data_predict",
            resolution=baseline_data["resolution"],
            point_count=point_count,
            values=item.values,
            source={"kind": "data_predict", "object_id": item.history_object_id},
        )
        for item in predicted
    )
    return series, predicted


def predict_target_round(
    db: Session,
    plane: ComputeExecutionPlane,
    task_project_id: int,
    snapshot: Any,
    *,
    previous: Mapping[str, object] | None = None,
) -> tuple[tuple[MaterializedSeries, ...], tuple[PredictedTarget, ...]]:
    """预测阶段命令：经公开 predictor 物化全部 data_predict 运营期序列。

    纯计算阶段（读快照与固定资源字节，无数据库写入）：按配置条目经
    执行面预测目录精确解析算法（未注册/目标类型不支持即明确不可用，
    不回退），历史列按绑定接口名投影，目标类型不透明透传。返回
    （生成器可消费序列，预测产物〈序列值/训练产物字节/回执基底〉），
    均按（实例，接口）稳定排序。训练产物引用由提交密封后回填。
    """
    artifact, resources, config, seed = _restore_artifact_resources_config(
        db, task_project_id, snapshot
    )
    return _predict_entries(
        artifact, resources, tuple(config.predictions or ()),
        plane.predictor_registry.get, seed,
        operation_years=snapshot.evaluation["operation_years"],
        previous=previous,
    )


def predict_selected_round(
    db: Session,
    plane: ComputeExecutionPlane,
    task_project_id: int,
    snapshot: Any,
    entries: tuple[Mapping[str, object], ...],
    *,
    previous: Mapping[str, object] | None = None,
) -> tuple[tuple[MaterializedSeries, ...], tuple[PredictedTarget, ...]]:
    """预测阶段命令：只重算给定配置条目（耦合轮次用，独立产物由调用方复用）。

    与 ``predict_target_round`` 同一物化路径；调用方传入已按依赖声明
    拆出的条目（见 ``split_predictions_by_dependency``），此处不重复分流。
    """
    artifact, resources, _config, seed = _restore_artifact_resources_config(
        db, task_project_id, snapshot
    )
    return _predict_entries(
        artifact, resources, tuple(entries),
        plane.predictor_registry.get, seed,
        operation_years=snapshot.evaluation["operation_years"],
        previous=previous,
        allow_subset=True,
    )


def convergence_for_round(
    plane: ComputeExecutionPlane,
    coupled_entries: tuple[Mapping[str, object], ...],
    previous_predicted: Mapping[tuple[str, str], PredictedTarget],
    current_predicted: Mapping[tuple[str, str], PredictedTarget],
    previous_outputs: Mapping[str, object] | None,
    current_outputs: Mapping[str, object],
) -> tuple[float, tuple[dict[str, object], ...]]:
    """逐耦合目标调用算法收敛度量并汇总（求解完成后的公开输出边界）。

    每项目返回值经 ``check_convergence`` 校验；汇总值为各目标无量纲值
    的最大值，调用方以同一 ``tolerance`` 比较（全部不大于才收敛）。
    参数取各条目已固定的 ``parameters``，此处不重复跑参数校验。
    返回（汇总值，逐目标记录）：记录为
    ``{device, interface, algorithm, metric, value, unit}`` 按目标排序。
    """
    records: list[dict[str, object]] = []
    for entry in coupled_entries:
        target = entry["target"]
        key = (str(target["instance"]), str(target["interface"]))
        algorithm = str(entry["algorithm"])
        predictor = plane.predictor_registry.get(algorithm)
        # 可用性已由注册边界确定（耦合必有收敛度量），此处直接调用。
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
    materialized: MaterializedSet,
    finance: EffectiveFinanceConfig,
    config: CalculationConfig,
    evaluation: EvaluationInput,
) -> SolverBundle:
    """经生成器目录选中精确 provider 生成完整 Bundle，并消费快照冻结引用。

    第三项为已完成物化的集合（调用方在预测产物合并后构成，此处只透传，
    不复核）。冻结评价确认独立直传（无默认值；调用方传同一任务冻结块
    恢复的值，不读最新草稿）。注册表 resolve 即通用选择门禁的唯一真实
    边界（未注册/不兼容即 ``ComputationUnavailableError``；用户计算选项
    非法为 ``ValueError``）。生成后核对 Bundle 声明的执行器/适配器与快照
    冻结精确引用一致，并预先解析两者（不可解析即明确失败，不等到求解后；
    不读当前可变目录）。
    """
    provider = plane.generator_registry.resolve(config)
    bundle = provider.generate(artifact, models, materialized, finance, config, evaluation)
    declared_executor = bundle.manifest.command["executor"]
    if declared_executor != snapshot.executor or bundle.manifest.result_adapter != snapshot.result_adapter:
        raise ComputationUnavailableError(
            "Bundle 声明与快照冻结 provider 不一致 "
            f"(bundle={declared_executor!r}/{bundle.manifest.result_adapter!r}, "
            f"snapshot={snapshot.executor!r}/{snapshot.result_adapter!r})",
            reason="frozen-provider-mismatch",
        )
    plane.executor_registry.get(snapshot.executor)
    plane.adapter_registry.get(snapshot.result_adapter)
    return bundle


def execute_bundle(
    *,
    plane: ComputeExecutionPlane,
    bundle: SolverBundle,
    attempt: AttemptContext,
    stop: StopProbe | None = None,
) -> tuple[ExecutionReceipt, dict[str, bytes]]:
    """经组合根装配的通用运行时执行完整 Bundle（调用方停止探针透传）。"""
    return plane.runtime.run(bundle, attempt, stop=stop)


def adapt_result(
    plane: ComputeExecutionPlane,
    bundle: SolverBundle,
    receipt: ExecutionReceipt,
    collected: Mapping[str, object],
) -> ComputeResult:
    """按回执固定的精确引用选中配套适配器并解释（只凭三元组，不回退）。"""
    return plane.adapter_registry.resolve(receipt.result_adapter_ref).adapt(bundle, receipt, collected)


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


def build_hourly_files(
    outputs: Mapping[str, object],
) -> dict[str, tuple[bytes, str]]:
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
    if "series" not in outputs:
        return {}
    series = outputs["series"]
    if not series:
        return {}
    data: dict[str, list[float]] = {}
    units: dict[str, str] = {}
    identities: dict[str, dict[str, str]] = {}
    for series_id in sorted(series):
        entry = series[series_id]
        data[series_id] = list(entry["values"])  # type: ignore[index]
        units[series_id] = entry["unit"]  # type: ignore[index]
        identities[series_id] = {
            "instance_id": entry["instance_id"],  # type: ignore[index]
            "variable_id": entry["variable_id"],  # type: ignore[index]
        }
    blob = canonical_json({"data": data, "meta": {"units": units, "series": identities}})
    return {HOURLY_OBJECT_PATH: (blob.encode("utf-8"), HOURLY_OBJECT_MEDIA_TYPE)}


def seal_output_objects(
    db: Session, files: Mapping[str, tuple[bytes, str]], *, actor_id: int | None
) -> dict[str, dict[str, object]]:
    """声明文件字节以存储对象无损封存，返回按路径索引的描述子。

    字节原样入对象（不做文本校验与编码猜测）；媒体类型取 manifest/回执
    的声明值，由调用方按声明传入。提交由调用方短会话负责。
    """
    sealed: dict[str, dict[str, object]] = {}
    for path in sorted(files):
        data, media_type = files[path]
        handle = put_object(
            db,
            data,
            media_type,
            source_category="evidence",
            purpose="evidence_output",
            actor_id=actor_id,
        )
        sealed[path] = {
            "object_id": handle.id,
            "media_type": media_type,
            "size_bytes": len(data),
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
    """同次解财务事实 → 封存评价(ies.financial-evaluation@2.0.0)。

    只处理已求解且含 solution 输出的结果; 其余状态返回 None(调用方
    不写财务块, 不把不可行/缺证据结果伪装为有现金流的方案, 缺块语义
    沿既有 ``financial_absence``)。本函数只投影适配器恢复的同次解财务
    事实并调用 finance 既有评价纯函数, 不重建税基/VAT/亏损算法, 不读
    DB/最新配置, 不重跑提交校验: 配套 5.0.0 事实缺件即实际结构错误
    直接可见失败, 不补空、不回退。
    """
    from decimal import Decimal

    from iesplan.finance import evaluate_project_investment

    if result.math_status != "solved":
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
    assessed = evaluate_project_investment(
        [Decimal(value) for value in flows],
        rate,
        years,
        currency=currency,
        price_basis=finance.price_basis,
        base_year=finance.base_year,
    )
    return {
        "schema": "ies.financial-evaluation",
        "schema_version": "2.0.0",
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
        "evaluation": assessed.to_dict(),
    }


def business_outcome_for(result: ComputeResult) -> str | None:
    """统一结果 → 任务业务结局显式映射（非 completed 执行返回 None）。

    completed + solved → normal_completion；completed + 不可行/无界 →
    no_recommendation（正常完成、无可推荐方案）；completed + 数值失败/
    未求解 → insufficient_evidence（正常完成、无可用证据）。未知状态
    返回 None（调用方按执行失败可见处理，不猜测）。
    """
    if result.execution_status != "completed":
        return None
    math_status = result.math_status
    if math_status == "solved":
        outcome = "normal_completion"
    elif math_status in ("infeasible", "unbounded"):
        outcome = "no_recommendation"
    elif math_status in ("numeric_failure", "not_solved"):
        outcome = "insufficient_evidence"
    else:
        return None
    if outcome not in BUSINESS_OUTCOMES:
        return None
    return outcome
