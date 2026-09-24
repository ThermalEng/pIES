"""ExecutorProvider 独立原子注册表(0.9 切片 A)。

- 注册表注册真实 provider 实例，不只是 descriptor；目录快照与
  ``to_dict`` 仍只公开 descriptor 数据；
- 候选 provider 必须完整构建并全部校验通过后一次性发布；任一候选
  协议不符、重复 ref、descriptor/ref 不一致或不可用即整体拒绝，
  不发布部分状态；
- ``resolve(executor_ref, executable_ref)`` 精确选择 executor 并确认
  ``command.executable`` 受支持，返回可调用 provider；
- 注册表实例由组合根持有并发布（首个真实 executor 与 runtime 可同时
  工作时再接线）；API/Worker 不得自行发现或动态加载 provider，只消费
  组合根给定的已发布实例；
- 无可用 provider（空表或引用未知）或 executable 不受支持统一抛
  ``ComputationUnavailableError``，不回退、不猜测。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.protocols import ExecutorProvider
from iesplan.computation.runtime.descriptor import ExecutorDescriptor


class ExecutorRegistry:
    """已发布的执行器目录(不可变快照语义，实例级持有，无模块全局状态)。"""

    __slots__ = ("_providers",)

    def __init__(self, providers: Mapping[str, ExecutorProvider]) -> None:
        self._providers = dict(providers)

    def __len__(self) -> int:
        return len(self._providers)

    def snapshot(self) -> tuple[ExecutorDescriptor, ...]:
        """确定性快照（按稳定 ID 排序，只公开 descriptor 数据，供组合根
        健康探针与审计使用）。"""
        return tuple(
            self._providers[key].descriptor for key in sorted(self._providers)
        )

    def get(self, ref: str) -> ExecutorProvider:
        """按精确引用取真实 provider；未知引用即明确不可用。"""
        try:
            return self._providers[ref]
        except KeyError:
            raise ComputationUnavailableError(
                f"执行器不可用（未注册）: {ref!r}", reason="no-executor"
            ) from None

    def resolve(
        self, executor_ref: str, executable_ref: str
    ) -> ExecutorProvider:
        """按精确引用选中真实 provider 并确认 executable 受支持。

        1. 精确引用选中 provider；未知即 ``ComputationUnavailableError``；
        2. 用 descriptor 确认 ``executable_ref`` 受支持，不支持即
           ``ComputationUnavailableError``，不回退其他 executor。
        返回可直接调用 ``execute`` 的 provider 实例。
        """
        provider = self.get(executor_ref)
        if not provider.descriptor.supports_executable(executable_ref):
            raise ComputationUnavailableError(
                f"执行器不支持所选可执行文件: {executor_ref!r} x {executable_ref!r}",
                reason="unsupported-executable",
            )
        return provider

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 兼容独立副本（能力目录公布，只含 descriptor
        数据，不含实现）。"""
        return {"executors": [d.to_dict() for d in self.snapshot()]}


def build_executor_registry(
    candidates: Iterable[ExecutorProvider],
) -> ExecutorRegistry:
    """原子构建并发布执行器目录。

    全部候选先校验再一次性发布：须符合 ``ExecutorProvider`` 协议、
    公开 ``ExecutorDescriptor`` 且 ``ref`` 与描述符引用单一来源一致、
    当前可用、引用唯一；任一候选不符即整体拒绝，不发布部分表。
    空候选集发布空表（选择时即明确不可用）。
    """
    providers: dict[str, ExecutorProvider] = {}
    for candidate in candidates:
        if not isinstance(candidate, ExecutorProvider):
            raise TypeError(
                "执行器候选须符合 ExecutorProvider 协议，"
                f"得到 {type(candidate).__name__}"
            )
        descriptor = getattr(candidate, "descriptor", None)
        if not isinstance(descriptor, ExecutorDescriptor):
            raise TypeError(
                "执行器候选须公开 ExecutorDescriptor，"
                f"得到 {type(descriptor).__name__}"
            )
        if candidate.ref != descriptor.ref:
            raise ValueError(
                "执行器 ref 与描述符引用不一致: "
                f"{candidate.ref!r} x {descriptor.ref!r}"
            )
        if not candidate.available():
            raise ComputationUnavailableError(
                f"执行器候选当前不可用: {candidate.ref!r}",
                reason="no-executor",
            )
        if candidate.ref in providers:
            raise ValueError(f"执行器重复注册: {candidate.ref!r}")
        providers[candidate.ref] = candidate
    return ExecutorRegistry(providers)


__all__ = ["ExecutorRegistry", "build_executor_registry"]
