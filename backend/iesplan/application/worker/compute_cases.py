"""Worker 计算阶段网关(application/worker.compute_cases): 计算三段式真实调用链。

Worker daemon(``iesplan.worker``)只经本模块推进计算类任务, 不再直连任何
计算实现; 计算公共能力由组合根(``iesplan.bootstrap``)装配并经调用参数
显式注入, 本模块不持有模块全局 provider, 不做全局赋值。

真实最小调用链(宪法 §4.5, 阶段顺序固定):

1. 由快照已固定值准备计算输入(已签发装配产物 + 已固定资源 + 已校验
   ``CalculationConfig``; 表示映射只归 computation, 本模块不重建、不复判);
2. ``GeneratorProvider.generate`` → ``SolverBundle``;
3. ``SolverRuntime.run`` → ``(ExecutionReceipt, 原始输出)``;
4. ``ResultAdapter.adapt`` → ``ComputeResult`` → Worker 载荷 ``dict``。

不可用语义: provider 目录缺失/为空、必需能力缺键或形状不符、能力自报
不可用时, 一律抛 ``ComputationUnavailableError``(明确失败, 不伪造成功、
不回退)。输入形状问题(快照缺失、未签发的装配输入、非法配置/非法适配
结果)为调用方错误, 抛 ``ValueError``/``TypeError``, 同样绝不落成功。

本模块只做编排, 不知 provider 键: 键与组合解析唯一归
``iesplan.computation``(``resolve_capabilities`` 把不透明目录解析为
类型化能力); 本模块不导入 ``models.*`` 与 ``iesplan.services``; 只消费
computation 公开门面、tasks 域公开记录/结局词汇。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from iesplan.computation import (
    ComputationUnavailableError,
    ComputeResult,
    build_compute_inputs,
    resolve_capabilities,
)
from iesplan.tasks import BUSINESS_OUTCOMES, CalcSnapshotRecord

__all__ = [
    "ComputationUnavailableError",
    "run_compute_stage",
]

#: 阶段进度(供 Worker 进度短事务上报, 只描述阶段, 不解释业务)。
_STAGE_GENERATE = (20.0, "generate")
_STAGE_SOLVE = (60.0, "solve")
_STAGE_ADAPT = (90.0, "adapt")

#: 计算结果载荷种类(Worker 完成路径按显式 outcome 提交)。
COMPUTE_RESULT_KIND = "compute_result"


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
            传入的不透明目录, 不读模块全局; 无可用能力即结构化 unavailable)。
        progress_fn: 阶段进度回调(Worker 侧短事务上报; 可为空)。
    返回:
        Worker 完成载荷(含显式合法 ``outcome``, 供提交阶段网关落终态)。
    """
    capabilities = resolve_capabilities(providers)
    inputs = build_compute_inputs(snapshot)
    if progress_fn is not None:
        progress_fn(*_STAGE_GENERATE, None)
    bundle = capabilities.generator.generate(
        inputs.artifact, inputs.resources, inputs.config
    )
    if progress_fn is not None:
        progress_fn(*_STAGE_SOLVE, None)
    receipt, raw_outputs = capabilities.runtime.run(bundle)
    if progress_fn is not None:
        progress_fn(*_STAGE_ADAPT, None)
    result = capabilities.adapter.adapt(bundle, receipt, raw_outputs)
    return _result_payload(result)
