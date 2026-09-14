"""计算能力组合解析(0.8 延期: 键与组合归 computation, application 只传不透明目录)。

provider 目录的稳定键与组合解析唯一归属本模块:

- 目录由组合根经 ``available_providers`` 取后原样透传, application 与
  Worker 只把它当作不透明目录经调用参数传递, 不读键、不猜方法;
- ``resolve_capabilities`` 把不透明目录解析为深层不可变的类型化能力
  ``ComputeCapabilities``(字段为三段式边界协议类型, 构造后不可重绑);
- 判定只用运行时 Protocol ``isinstance``(结构归属 computation 公开协议),
  不做方法名 ``getattr`` 猜测; 不可用语义只取真实边界值
  ``"no-provider"`` / ``"deferred-0.8"``, 不枚举假想畸形目录。

本模块不实现任何 0.8 求解器/生成器算法。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.protocols import GeneratorProvider, ResultAdapter, SolverRuntime

__all__ = [
    "GENERATOR_PROVIDER_KEY",
    "RESULT_ADAPTER_KEY",
    "SOLVER_RUNTIME_KEY",
    "ComputeCapabilities",
    "resolve_capabilities",
]

#: provider 目录稳定键(组合根装配时登记, 解析只归本模块)。
GENERATOR_PROVIDER_KEY = "generator"
SOLVER_RUNTIME_KEY = "solver_runtime"
RESULT_ADAPTER_KEY = "result_adapter"


@dataclass(frozen=True, slots=True)
class ComputeCapabilities:
    """一次计算的三段式类型化能力(深层不可变: 构造后不可重绑替换)。

    字段均为 computation 公开边界协议类型, 由 ``resolve_capabilities``
    对不透明目录做 ``isinstance`` 结构判定后装配; 调用方(``application``)
    只消费字段编排 generate → run → adapt, 不知键、不复判。
    """

    generator: GeneratorProvider
    runtime: SolverRuntime
    adapter: ResultAdapter


def _require_capability(
    directory: Mapping[str, object], key: str, protocol: Any
) -> Any:
    """按稳定键取能力并做协议结构判定(缺失或形状不符即无可用能力)。"""
    capability = directory.get(key)
    if not isinstance(capability, protocol):
        raise ComputationUnavailableError(
            f"计算能力缺失(0.8 未实现): {key}", reason="no-provider"
        )
    return capability


def resolve_capabilities(
    directory: Mapping[str, object] | None,
) -> ComputeCapabilities:
    """把不透明 provider 目录解析为类型化能力(组合解析唯一入口)。

    - 目录缺失/为空、必需能力缺键或形状不符 → ``reason="no-provider"``;
    - 能力自报 ``available() is False`` → ``reason="deferred-0.8"``。
    """
    if not isinstance(directory, Mapping) or not directory:
        raise ComputationUnavailableError(reason="no-provider")
    generator = _require_capability(directory, GENERATOR_PROVIDER_KEY, GeneratorProvider)
    runtime = _require_capability(directory, SOLVER_RUNTIME_KEY, SolverRuntime)
    adapter = _require_capability(directory, RESULT_ADAPTER_KEY, ResultAdapter)
    for key, capability in (
        (GENERATOR_PROVIDER_KEY, generator),
        (SOLVER_RUNTIME_KEY, runtime),
        (RESULT_ADAPTER_KEY, adapter),
    ):
        if capability.available() is False:
            raise ComputationUnavailableError(
                f"计算能力明确延期未实现(0.8): {key}", reason="deferred-0.8"
            )
    return ComputeCapabilities(generator=generator, runtime=runtime, adapter=adapter)
