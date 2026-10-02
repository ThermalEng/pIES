"""Worker 计算阶段命令(application/worker.compute_cases): 快照输入恢复与三段调用。

本模块提供 worker.md 规定的生成 / 执行 / 适配公开阶段命令，Worker 只按
返回的公开 contract 驱动状态机，不复制领域规则：

- ``load_compute_inputs``：把快照冻结的五项直接输入恢复为生成器可消费
  形态（规范装配二件套结构恢复、逐实例数学贡献结构恢复、财务快照按冻结
  profile/revision 取不可变行、计算配置显式字段恢复、固定资源字节加载、
  非预测序列物化）。缺失冻结件返回 None（调用方按快照不可用失败）；
  封存形态损坏让实际恢复错误可见，不补默认。
- ``generate_bundle`` / ``execute_bundle`` / ``adapt_result``：经组合根
  给定的已发布目录与运行时完成三段调用，本层不选型、不回退。
- ``build_attempt_context`` / ``business_outcome_for``：回执身份派生与
  任务业务结局映射（只做 contract 允许的显式映射）。

本模块只 flush-free：只读 DB 与纯内存转换，不拥有事务、不 commit/
rollback。预测需求（``pending_predict`` 或配置 ``predictions`` 非空）
走 ``ComputationUnavailableError(reason="prediction-not-supported")``
明确报告，不 stub、不回退旧链。

依赖方向: worker → application → (configuration/dataset/project/
storage/领域门面 + computation 公开门面)。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
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
    MaterializedSeries,
    SolverBundle,
    SolverRuntime,
    StopProbe,
    materialize_non_predictive_series,
)
from iesplan.computation.result_adapter import ResultAdapterRegistry
from iesplan.core.diagnostics import Diagnostic
from iesplan.finance import EffectiveFinanceConfig
from iesplan.modeling import DeviceMathContribution, contribution_from_dict
from iesplan.storage import get_object
from iesplan.tasks import BUSINESS_OUTCOMES

__all__ = [
    "ComputeExecutionPlane",
    "ComputeInputs",
    "adapt_result",
    "build_attempt_context",
    "business_outcome_for",
    "execute_bundle",
    "generate_bundle",
    "load_compute_inputs",
]


@dataclass(frozen=True, slots=True)
class ComputeExecutionPlane:
    """计算执行面句柄（组合根装配、Worker 持有、阶段命令消费）。

    四个成员均为已发布/已装配实例：生成器目录、执行器目录、结果适配器
    目录与通用运行时。API 进程不装配执行面（执行器目录/运行时/策略只归
    计算 Worker 组合根）。
    """

    generator_registry: GeneratorRegistry
    executor_registry: ExecutorRegistry
    adapter_registry: ResultAdapterRegistry
    runtime: SolverRuntime


@dataclass(frozen=True, slots=True)
class ComputeInputs:
    """生成器五项直接输入（快照冻结恢复，不可变值对象）。"""

    artifact: ValidatedAssemblyArtifact
    models: Mapping[str, DeviceMathContribution] = field(default_factory=dict)
    series: tuple[MaterializedSeries, ...] = ()
    finance: EffectiveFinanceConfig | None = None
    config: CalculationConfig | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "series", tuple(self.series))


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


def _receipt_from_frozen(data: Mapping[str, Any]) -> ValidationReceipt | None:
    """回执结构恢复（只恢复形态，不重跑装配校验；字段缺失返回 None）。

    形态以 ``ValidationReceipt.to_dict`` 为准：``schema``/``schema_version``、
    ``validator.{id,version}``、``canonical_algorithm.{id,version}``。
    """
    validator = data.get("validator")
    canonical = data.get("canonical_algorithm")
    scalars = {
        "schema_id": data.get("schema"),
        "schema_version": data.get("schema_version"),
        "validator_id": validator.get("id") if isinstance(validator, Mapping) else None,
        "validator_version": (validator.get("version") if isinstance(validator, Mapping) else None),
        "canonical_algorithm_id": canonical.get("id") if isinstance(canonical, Mapping) else None,
        "canonical_algorithm_version": (canonical.get("version") if isinstance(canonical, Mapping) else None),
    }
    if any(not isinstance(value, str) or not value for value in scalars.values()):
        return None
    for name in ("dependencies", "resources", "finance_snapshot", "planning"):
        if not isinstance(data.get(name), Mapping):
            return None
    diagnostics = []
    for entry in data.get("diagnostics") or ():
        if not isinstance(entry, Mapping):
            return None
        diagnostics.append(Diagnostic(**dict(entry)))
    return ValidationReceipt(
        **scalars,
        dependencies=dict(data["dependencies"]),
        resources=dict(data["resources"]),
        finance_snapshot=dict(data["finance_snapshot"]),
        planning=dict(data["planning"]),
        diagnostics=tuple(diagnostics),
    )


def _collect_resource_ids(doc: Mapping[str, Any]) -> set[str]:
    """从规范装配 devices 绑定收集 content.object_id（字符串身份集合）。"""
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


def load_compute_inputs(db: Session, task_project_id: int, snapshot: Any) -> ComputeInputs | None:
    """从快照恢复生成器五项直接输入；冻结件缺失返回 None。

    finance 按回执冻结血缘 {profile_id, revision} 取不可变 Effective 行
    （与提交闸门同一来源：钉选 revision，不读 latest/current 指针）；
    固定资源按规范绑定 object_id 经 storage 公开门面读字节。
    """
    text = snapshot.canonical_assembly_text
    receipt_map = snapshot.assembly_receipt
    if not text or not isinstance(receipt_map, Mapping) or not receipt_map:
        return None
    receipt = _receipt_from_frozen(receipt_map)
    if receipt is None:
        return None
    artifact = ValidatedAssemblyArtifact(canonical_text=text, receipt=receipt)

    raw_models = snapshot.device_models
    if not isinstance(raw_models, Mapping) or not raw_models:
        return None
    models = {inst: contribution_from_dict(contrib) for inst, contrib in raw_models.items()}

    finance_blood = receipt.finance_snapshot
    profile_id = finance_blood.get("profile_id")
    revision = finance_blood.get("revision")
    if (
        not isinstance(profile_id, str)
        or not profile_id
        or not isinstance(revision, int)
        or isinstance(revision, bool)
    ):
        return None
    finance_row = configuration_domain.get_effective_revision(db, task_project_id, revision)
    if finance_row is None or finance_row.profile_id != profile_id:
        return None
    finance = EffectiveFinanceConfig.from_dict(finance_row.content)

    body = snapshot.calc_config_snapshot
    if not isinstance(body, Mapping):
        return None
    try:
        picked = {name: body[name] for name in _CONFIG_FIELDS}
    except KeyError:
        return None
    config = CalculationConfig(**picked)

    doc = json.loads(text)
    resources = {object_id: get_object(db, object_id) for object_id in sorted(_collect_resource_ids(doc))}
    materialized = materialize_non_predictive_series(artifact, resources)
    if materialized.pending_predict or config.predictions:
        raise ComputationUnavailableError(
            "该快照需要预测序列（data_predict/阶段二），本版尚未提供公开预测能力，不执行、不回退旧链",
            reason="prediction-not-supported",
        )
    return ComputeInputs(
        artifact=artifact,
        models=models,
        series=materialized.series,
        finance=finance,
        config=config,
    )


def generate_bundle(plane: ComputeExecutionPlane, inputs: ComputeInputs) -> SolverBundle:
    """经生成器目录选中精确 provider 并生成完整 Bundle（注册表 resolve
    即通用选择门禁的唯一真实边界）。

    未注册/不兼容即 ``ComputationUnavailableError``；用户计算选项非法为
    ``ValueError``（封存配置与目录漂移的可见失败，不回退）。
    """
    provider = plane.generator_registry.resolve(inputs.config)
    return provider.generate(inputs.artifact, inputs.models, inputs.series, inputs.finance, inputs.config)


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

    assembly 取规范文本装配 id；calculation_config 取快照 config_ref
    （project:revision），缺失时回落到快照派生身份（仍可追溯，不伪装
    成配置 revision）。
    """
    doc = json.loads(snapshot.canonical_assembly_text)
    assembly_id = (doc.get("assembly") or {}).get("id") or f"snapshot-{snapshot.id}"
    ref = snapshot.calc_config_snapshot.get("config_ref") or {}
    if (
        isinstance(ref.get("project_id"), int)
        and not isinstance(ref.get("project_id"), bool)
        and isinstance(ref.get("revision"), int)
        and not isinstance(ref.get("revision"), bool)
    ):
        config_id = f"project-{ref['project_id']}:config-rev-{ref['revision']}"
    else:
        config_id = f"snapshot-{snapshot.id}:config"
    return AttemptContext(
        attempt_id=str(claim.attempt_id),
        bundle_id=f"snapshot-{snapshot.id}:attempt-{claim.attempt_id}",
        assembly_id=str(assembly_id),
        calculation_config_id=config_id,
    )


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
