"""受控执行公开 contract 与独立注册(0.9 切片 A)。

只声明 ExecutorProvider 形状、描述符与原子注册表，不实现任何真实
executor，不启动进程。执行器选择与装配只归 bootstrap 组合根（首个
真实 executor 与 runtime 可同时工作时再接线）；API/Worker 不得自行
发现或动态加载 provider。无可用 provider 时统一抛
``ComputationUnavailableError``。
"""

from iesplan.computation.runtime.descriptor import ExecutorDescriptor
from iesplan.computation.runtime.registry import (
    ExecutorRegistry,
    build_executor_registry,
)

__all__ = [
    "ExecutorDescriptor",
    "ExecutorRegistry",
    "build_executor_registry",
]
