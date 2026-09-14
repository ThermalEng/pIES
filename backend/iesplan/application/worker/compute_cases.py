"""Worker 计算阶段网关(application/worker.compute_cases): 计算三段式真实调用链。

Worker daemon(``iesplan.worker``)只经本模块推进计算类任务, 不再直连任何
计算实现; 计算公共能力由组合根(``iesplan.bootstrap``)装配并经调用参数
显式注入, 本模块不持有模块全局 provider, 不做全局赋值。

真实最小调用链(宪法 §4.5, 阶段顺序固定):

1. 由快照已固定值准备计算输入(已签发装配产物 + 已固定资源 + 已校验
   ``CalculationConfig``);
2. ``GeneratorProvider.generate`` → ``SolverBundle``;
3. ``SolverRuntime.run`` → ``(ExecutionReceipt, 原始输出)``;
4. ``ResultAdapter.adapt`` → ``ComputeResult`` → Worker 载荷 ``dict``。

不可用语义: provider 目录缺失/为空、必需能力缺键、能力自报不可用时,
一律抛 ``ComputationUnavailableError``(明确失败, 不伪造成功、不回退)。
输入形状问题(快照缺失、未签发的装配输入、非法配置/非法适配结果)为调用
方错误, 抛 ``ValueError``/``TypeError``, 同样绝不落成功。

本模块不导入 ``models.*`` 与 ``iesplan.services``; 只消费 computation
公开门面、assembly 不可变 contract 与 tasks 域公开记录/结局词汇。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from iesplan.assembly.contracts import ValidatedAssemblyArtifact, ValidationReceipt
from iesplan.computation import CalculationConfig, ComputationUnavailableError, ComputeResult
from iesplan.core.diagnostics import make_diag
from iesplan.tasks import BUSINESS_OUTCOMES, CalcSnapshotRecord

__all__ = [
    "GENERATOR_PROVIDER_KEY",
    "RESULT_ADAPTER_KEY",
    "SOLVER_RUNTIME_KEY",
    "ComputationUnavailableError",
    "ComputeInputs",
    "build_compute_inputs",
    "run_compute_stage",
]

#: provider 目录稳定键(组合根装配时登记, 本阶段按键取用)。
GENERATOR_PROVIDER_KEY = "generator"
SOLVER_RUNTIME_KEY = "solver_runtime"
RESULT_ADAPTER_KEY = "result_adapter"

#: 阶段进度(供 Worker 进度短事务上报, 只描述阶段, 不解释业务)。
_STAGE_GENERATE = (20.0, "generate")
_STAGE_SOLVE = (60.0, "solve")
_STAGE_ADAPT = (90.0, "adapt")

#: 计算结果载荷种类(Worker 完成路径按显式 outcome 提交)。
COMPUTE_RESULT_KIND = "compute_result"


@dataclass(frozen=True, slots=True)
class ComputeInputs:
    """一次计算的已准备输入(快照已固定值, 不可变)。"""

    artifact: ValidatedAssemblyArtifact
    resources: Mapping[str, object]
    config: CalculationConfig


def _freeze_shallow(mapping: Mapping[str, object]) -> Mapping[str, object]:
    """冻结一层映射(值侧已由快照固定或为不可变元组, 不做深度猜测)。"""
    return MappingProxyType(dict(mapping))


def _rebuild_artifact(
    canonical_text: str, receipt_dict: Mapping[str, Any]
) -> ValidatedAssemblyArtifact:
    """由快照封存的规范文本与回执字典重建已签发装配产物。

    回执诊断按稳定字段重建(``make_diag``); 回执含阻断诊断意味着该装配从
    未被签发, 拒绝计算。时间戳等运行上下文不参与重建(回执本就不携带)。
    """
    diagnostics = []
    raw_diags = receipt_dict.get("diagnostics", [])
    if not isinstance(raw_diags, (list, tuple)):
        raise ValueError("装配回执 diagnostics 须为列表")
    for raw in raw_diags:
        if not isinstance(raw, Mapping):
            raise ValueError("装配回执诊断条目须为 Mapping")
        diagnostics.append(
            make_diag(
                raw["code"],
                severity=raw.get("severity", "error"),
                message_key=raw.get("message_key", ""),
                fix_hint_key=raw.get("fix_hint_key", ""),
                blocking=bool(raw.get("blocking", False)),
                params=raw.get("params") or {},
                location=raw.get("location"),
                ref_ids=tuple(raw.get("ref_ids") or ()),
            )
        )
    receipt = ValidationReceipt(
        dependencies=receipt_dict.get("dependencies", {}),
        resources=receipt_dict.get("resources", {}),
        diagnostics=tuple(diagnostics),
    )
    if any(diag.blocking for diag in receipt.diagnostics):
        raise ValueError("装配回执含阻断诊断(未签发的装配输入不得生成 Bundle)")
    return ValidatedAssemblyArtifact(canonical_text=canonical_text, receipt=receipt)


def build_compute_inputs(snapshot: CalcSnapshotRecord | None) -> ComputeInputs:
    """由快照记录准备计算输入(纯函数, 无会话、无事务)。

    - 快照缺失/规范装配文本缺失/回执缺失或非法 → ``ValueError``(未签发的
      装配输入不得生成 Bundle, 不产生部分产物);
    - 已固定资源 = 快照已固定的数据集版本清单与容差(计算 Worker 无存储
      能力, 不在此读取对象字节; 完整资源内容由调用方随 provider 显式传入)。
    """
    if snapshot is None:
        raise ValueError("计算阶段缺快照记录, 无法准备计算输入")
    canonical_text = snapshot.canonical_assembly_text
    if not canonical_text or not canonical_text.strip():
        raise ValueError("快照缺规范装配文本(未签发的装配输入不得生成 Bundle)")
    receipt_dict = snapshot.assembly_receipt
    if not isinstance(receipt_dict, Mapping):
        raise ValueError("快照缺装配回执(未签发的装配输入不得生成 Bundle)")
    artifact = _rebuild_artifact(canonical_text, receipt_dict)
    resources = _freeze_shallow(
        {
            "dataset_version_ids": tuple(snapshot.dataset_version_ids),
            "tolerances": dict(snapshot.tolerances or {}),
        }
    )
    config = CalculationConfig.from_snapshot(
        snapshot.calc_config_snapshot,
        seed=snapshot.random_seed,
        tolerances=snapshot.tolerances,
    )
    return ComputeInputs(artifact=artifact, resources=resources, config=config)


def _resolve_providers(
    providers: Mapping[str, object] | None,
) -> tuple[Any, Any, Any]:
    """按稳定键解析三段能力(缺失/自报不可用即结构化 unavailable)。

    - ``None``/非 Mapping/空目录/缺键/缺方法 → ``reason="no-provider"``;
    - 能力自报 ``available() is False`` → ``reason="deferred-0.8"``。
    """
    if not isinstance(providers, Mapping) or not providers:
        raise ComputationUnavailableError(reason="no-provider")
    missing = [
        key
        for key in (GENERATOR_PROVIDER_KEY, SOLVER_RUNTIME_KEY, RESULT_ADAPTER_KEY)
        if key not in providers
    ]
    if missing:
        raise ComputationUnavailableError(
            f"计算能力缺失(0.8 未实现): {sorted(missing)}", reason="no-provider"
        )
    generator = providers[GENERATOR_PROVIDER_KEY]
    runtime = providers[SOLVER_RUNTIME_KEY]
    adapter = providers[RESULT_ADAPTER_KEY]
    for key, capability, method in (
        (GENERATOR_PROVIDER_KEY, generator, "generate"),
        (SOLVER_RUNTIME_KEY, runtime, "run"),
        (RESULT_ADAPTER_KEY, adapter, "adapt"),
    ):
        if not callable(getattr(capability, method, None)):
            raise ComputationUnavailableError(
                f"计算能力无可用方法(0.8 未实现): {key}.{method}",
                reason="no-provider",
            )
    for key, capability in (
        (GENERATOR_PROVIDER_KEY, generator),
        (SOLVER_RUNTIME_KEY, runtime),
        (RESULT_ADAPTER_KEY, adapter),
    ):
        available = getattr(capability, "available", None)
        if callable(available) and available() is False:
            raise ComputationUnavailableError(
                f"计算能力明确延期未实现(0.8): {key}", reason="deferred-0.8"
            )
    return generator, runtime, adapter


def _result_payload(result: ComputeResult) -> dict[str, Any]:
    """把统一计算结果转为 Worker 完成载荷(显式业务结局, 无默认成功)。"""
    if result.business_outcome not in BUSINESS_OUTCOMES:
        raise ValueError(
            f"结果适配器返回非法业务结局: {result.business_outcome!r}"
        )
    return {
        "outcome": result.business_outcome,
        "result_kind": COMPUTE_RESULT_KIND,
        "status": result.status,
        "bundle_id": result.bundle_id,
        "outputs": result.to_dict()["outputs"],
    }


def run_compute_stage(
    snapshot: CalcSnapshotRecord | None,
    *,
    providers: Mapping[str, object] | None,
    progress_fn: Callable[[float, str, dict[str, Any] | None], None] | None = None,
) -> dict[str, Any]:
    """执行计算三段式(阶段顺序 generate → solve → adapt, 真实调用链)。

    参数:
        snapshot: 计算快照公开记录(任务唯一输入; 缺失即调用方错误)。
        providers: 组合根装配的 computation provider 目录(调用参数显式
            传入, 不读模块全局; 无可用能力即结构化 unavailable)。
        progress_fn: 阶段进度回调(Worker 侧短事务上报; 可为空)。
    返回:
        Worker 完成载荷(含显式合法 ``outcome``, 供提交阶段网关落终态)。
    """
    generator, runtime, adapter = _resolve_providers(providers)
    inputs = build_compute_inputs(snapshot)
    if progress_fn is not None:
        progress_fn(*_STAGE_GENERATE, None)
    bundle = generator.generate(inputs.artifact, inputs.resources, inputs.config)
    if progress_fn is not None:
        progress_fn(*_STAGE_SOLVE, None)
    receipt, raw_outputs = runtime.run(bundle)
    if progress_fn is not None:
        progress_fn(*_STAGE_ADAPT, None)
    result = adapter.adapt(bundle, receipt, raw_outputs)
    return _result_payload(result)
