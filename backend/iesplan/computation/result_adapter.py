"""ResultAdapterProvider 独立原子注册表(0.10 切片 A)。

- 注册表注册真实 provider 实例, 不只是 descriptor；目录快照与
  ``to_dict`` 仍只公开 descriptor 数据；
- 候选 provider 必须完整构建并全部校验通过后一次性发布；任一候选
  协议不符、重复 ref、descriptor/ref 不一致或不可用即整体拒绝,
  不发布部分状态；
- ``resolve(adapter_ref)`` 按 manifest/回执固定的精确引用选中 provider；
- 注册表实例由组合根持有并发布；API/Worker 不得自行发现或动态加载
  provider, 只消费组合根给定的已发布实例；
- 无可用 provider（空表或引用未知）统一抛
  ``ComputationUnavailableError``, 不回退、不猜测。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.protocols import (
    ResultAdapter,
    check_provider_ref,
)


@dataclass(frozen=True, slots=True)
class ResultAdapterDescriptor:
    """单个 ResultAdapterProvider 的公开能力描述(不可变值对象)。

    ``ref`` 为 ``<稳定ID>@<语义化版本>``；manifest 与回执固定的
    ``result_adapter`` 选择与本 ``ref`` 相等才表示选中本 provider,
    provider 自身不再另存第二份引用。精确选择只需要这两项, 不预置
    求解器兼容表或选项 schema（具体适配语义归后续实现切片）。
    """

    adapter_id: str
    version: str

    def __post_init__(self) -> None:
        check_provider_ref(f"{self.adapter_id}@{self.version}", "adapter")

    @property
    def ref(self) -> str:
        """适配器精确引用(``<稳定ID>@<语义化版本>``)。"""
        return f"{self.adapter_id}@{self.version}"

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 兼容独立副本。"""
        return {"adapter_id": self.adapter_id, "version": self.version}


class ResultAdapterRegistry:
    """已发布的适配器目录(不可变快照语义, 实例级持有, 无模块全局状态)。"""

    __slots__ = ("_providers",)

    def __init__(self, providers: Mapping[str, ResultAdapter]) -> None:
        self._providers = dict(providers)

    def __len__(self) -> int:
        return len(self._providers)

    def snapshot(self) -> tuple[ResultAdapterDescriptor, ...]:
        """确定性快照（按稳定 ID 排序, 只公开 descriptor 数据, 供组合根
        健康探针与审计使用）。"""
        return tuple(self._providers[key].descriptor for key in sorted(self._providers))

    def get(self, ref: str) -> ResultAdapter:
        """按精确引用取真实 provider；未知引用即明确不可用。"""
        try:
            return self._providers[ref]
        except KeyError:
            raise ComputationUnavailableError(
                f"结果适配器不可用（未注册）: {ref!r}", reason="no-result-adapter"
            ) from None

    def resolve(self, adapter_ref: str) -> ResultAdapter:
        """按 manifest/回执固定的精确引用选中真实 provider。

        未知即 ``ComputationUnavailableError``, 不回退其他适配器。
        返回选中的 provider 实例。
        """
        return self.get(adapter_ref)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 兼容独立副本（能力目录公布, 只含 descriptor
        数据, 不含实现）。"""
        return {"result_adapters": [d.to_dict() for d in self.snapshot()]}


def build_result_adapter_registry(
    candidates: Iterable[ResultAdapter],
) -> ResultAdapterRegistry:
    """原子构建并发布适配器目录。

    全部候选先校验再一次性发布：须符合 ``ResultAdapter`` 协议、
    公开 ``ResultAdapterDescriptor`` 且 ``ref`` 与描述符引用单一来源一致、
    当前可用、引用唯一；任一候选不符即整体拒绝, 不发布部分表。
    空候选集发布空表（选择时即明确不可用）。
    """
    providers: dict[str, ResultAdapter] = {}
    for candidate in candidates:
        if not isinstance(candidate, ResultAdapter):
            raise TypeError(f"适配器候选须符合 ResultAdapter 协议, 得到 {type(candidate).__name__}")
        descriptor = getattr(candidate, "descriptor", None)
        if not isinstance(descriptor, ResultAdapterDescriptor):
            raise TypeError(f"适配器候选须公开 ResultAdapterDescriptor, 得到 {type(descriptor).__name__}")
        if candidate.ref != descriptor.ref:
            raise ValueError(f"适配器 ref 与描述符引用不一致: {candidate.ref!r} x {descriptor.ref!r}")
        if not candidate.available():
            raise ComputationUnavailableError(
                f"适配器候选当前不可用: {candidate.ref!r}",
                reason="no-result-adapter",
            )
        if candidate.ref in providers:
            raise ValueError(f"适配器重复注册: {candidate.ref!r}")
        providers[candidate.ref] = candidate
    return ResultAdapterRegistry(providers)


__all__ = [
    "ResultAdapterDescriptor",
    "ResultAdapterRegistry",
    "build_result_adapter_registry",
]
