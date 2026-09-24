"""部署执行策略：执行层实际消费的门禁与上限。

只包含本切片执行层真正检查的项：executable 精确引用 allowlist、
环境变量名 allowlist、资源需求上限（超时/内存/CPU）与网络开关。
超时取消的真实实施、输出体积上限与插件隔离状态随后续执行器切片
落地，不在此预留字段。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from iesplan.computation.protocols import check_provider_ref


@dataclass(frozen=True, slots=True)
class DeploymentPolicy:
    """部署批准的执行策略（组合根侧提供，执行层只读消费）。

    ``executables`` 为允许执行的 executable 精确引用集合；
    ``environment_vars`` 为允许注入的环境变量名集合（可为空集合，
    表示不允许任何环境变量）；``max_*`` 为命令声明需求不得超过的
    硬上限（命令未声明某项需求时该项无需求可比，直接通过）；
    ``allow_network`` 为是否允许命令声明联网（默认拒绝）。
    """

    executables: tuple[str, ...] = ()
    environment_vars: tuple[str, ...] = ()
    max_timeout_seconds: float = 0.0
    max_memory_mib: float = 0.0
    max_cpu_cores: float = 0.0
    allow_network: bool = False

    def __post_init__(self) -> None:
        executables = tuple(self.executables)
        if not executables:
            raise ValueError("executables 不得为空：策略必须明确允许项")
        for executable_ref in executables:
            check_provider_ref(executable_ref, "executables")
        object.__setattr__(self, "executables", executables)
        environment_vars = tuple(self.environment_vars)
        for name in environment_vars:
            if not isinstance(name, str) or not name or "=" in name:
                raise ValueError(f"环境变量名非法: {name!r}")
        object.__setattr__(self, "environment_vars", environment_vars)
        for name in (
            "max_timeout_seconds",
            "max_memory_mib",
            "max_cpu_cores",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value <= 0
            ):
                raise ValueError(f"{name} 须为正数: {value!r}")
            object.__setattr__(self, name, float(value))
        if not isinstance(self.allow_network, bool):
            raise TypeError("allow_network 须为布尔值")

    def allows_executable(self, executable_ref: str) -> bool:
        """该 executable 精确引用是否在 allowlist 内（精确相等）。"""
        return executable_ref in self.executables

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 兼容独立副本（随回执封存实际施加的策略）。"""
        return {
            "executables": list(self.executables),
            "environment_vars": list(self.environment_vars),
            "max_timeout_seconds": self.max_timeout_seconds,
            "max_memory_mib": self.max_memory_mib,
            "max_cpu_cores": self.max_cpu_cores,
            "allow_network": self.allow_network,
        }


__all__ = ["DeploymentPolicy"]
