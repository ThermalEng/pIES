"""计算边界协议(0.8 延期: 只定义真实边界, 无任何算法实现)。

本模块只声明三类可替换计算能力的形状与不可变数据载体:

- ``GeneratorProvider``: 方案生成能力的调用形状;
- ``SolverRuntime``: 求解执行能力的调用形状;
- ``ResultAdapter``: 统一结果的声明输出适配形状;
- ``SolverBundle`` / ``ComputeResult``: 跨越边界的不可变数据;
- 真实边界语义: 当前无任何可用 provider(见
  ``iesplan.computation.providers``), 任何实际计算请求都必须以
  ``ComputationUnavailableError`` 明确失败, 禁止静默回退、
  禁止猜测默认结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class GeneratorProvider(Protocol):
    """方案生成能力(0.8 未实现: 仅形状, 无实现)。"""

    @property
    def ref(self) -> str:
        """生成器精确引用(``<algo_id>@<version>`` 形态, 见装配契约)。"""
        ...

    def available(self) -> bool:
        """该 provider 当前是否可用(无实现时恒为 False 语义)。"""
        ...

    def generate(self, bundle: SolverBundle) -> ComputeResult:
        """由求解输入包生成统一计算结果; 无可用实现时抛
        ``ComputationUnavailableError``。"""
        ...


@runtime_checkable
class SolverRuntime(Protocol):
    """求解执行能力(0.8 未实现: 仅形状, 无实现)。"""

    @property
    def ref(self) -> str:
        """求解器精确引用(装配契约求解器引用形态)。"""
        ...

    def available(self) -> bool:
        """该 runtime 当前是否可用(无实现时恒为 False 语义)。"""
        ...

    def run(self, bundle: SolverBundle) -> ComputeResult:
        """执行求解; 无可用实现时抛 ``ComputationUnavailableError``。"""
        ...


@runtime_checkable
class ResultAdapter(Protocol):
    """统一计算结果的声明输出适配(0.8 未实现: 仅形状, 无实现)。"""

    def adapt(self, result: ComputeResult) -> dict[str, Any]:
        """把统一计算结果适配为声明输出; 无可用实现时抛
        ``ComputationUnavailableError``。"""
        ...


@dataclass(frozen=True, slots=True)
class SolverBundle:
    """跨越计算边界的求解输入包(不可变载体, 非算法实现)。"""

    generator_ref: str
    solver_ref: str
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ComputeResult:
    """跨越计算边界的统一计算结果(不可变载体, 非算法实现)。"""

    status: str
    outputs: dict[str, Any] = field(default_factory=dict)
    receipt: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "ComputeResult",
    "GeneratorProvider",
    "ResultAdapter",
    "SolverBundle",
    "SolverRuntime",
]
