"""ExecutorDescriptor：执行器公开能力描述。

只描述执行器选择所需的两项：executor 精确引用身份，以及受支持的
executable 精确引用集合（核对 ``command.executable`` 的唯一依据）。
不含隔离实施、取消语义与参数模式。跨越边界的容器字段在构造时冻结。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from iesplan.computation.protocols import check_provider_ref


@dataclass(frozen=True, slots=True)
class ExecutorDescriptor:
    """单个 ExecutorProvider 的公开能力描述(不可变值对象)。

    ``ref`` 为 ``<稳定ID>@<语义化版本>``；``command.executor`` 选择与本
    ``ref`` 相等才表示选中本 provider，provider 自身不再另存第二份引用。
    ``executables`` 必须为非空有效引用元组：缺失或含非法引用即发布无效
    descriptor，当场拒绝。
    """

    executor_id: str
    version: str
    executables: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        check_provider_ref(f"{self.executor_id}@{self.version}", "executor")
        executables = tuple(self.executables)
        if not executables:
            raise ValueError("executables 不得为空：descriptor 必须声明受支持项")
        for executable_ref in executables:
            check_provider_ref(executable_ref, "executables")
        object.__setattr__(self, "executables", executables)

    @property
    def ref(self) -> str:
        """执行器精确引用(``<稳定ID>@<语义化版本>`` 形态)。"""
        return f"{self.executor_id}@{self.version}"

    def supports_executable(self, executable_ref: str) -> bool:
        """是否支持该 executable 精确引用(精确相等，无前缀匹配)。"""
        return executable_ref in self.executables

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 兼容独立副本。"""
        return {
            "executor_id": self.executor_id,
            "version": self.version,
            "executables": list(self.executables),
        }


__all__ = ["ExecutorDescriptor"]
