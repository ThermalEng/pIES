"""受控执行公开 contract 与独立注册。

只公开 ExecutorProvider 身份形状、描述符与原子注册表，不含执行实现。
执行器选择与装配只归 bootstrap 组合根；API/Worker 不得自行发现或动态
加载 provider。无可用 provider 时统一抛 ``ComputationUnavailableError``。
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
