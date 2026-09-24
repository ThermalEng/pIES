"""受控执行公开 contract、独立注册与通用运行时。

公开 ExecutorProvider 身份与唯一调用形状、描述符、原子注册表，以及
执行层消费的 attempt 上下文、部署策略与具体通用 ``SolverRuntime``。
执行器选择与装配只归 bootstrap 组合根；API/Worker 不得自行发现或动态
加载 provider。无可用 provider 时统一抛 ``ComputationUnavailableError``。
"""

from iesplan.computation.runtime.context import AttemptContext
from iesplan.computation.runtime.descriptor import ExecutorDescriptor
from iesplan.computation.runtime.policy import DeploymentPolicy
from iesplan.computation.runtime.registry import (
    ExecutorRegistry,
    build_executor_registry,
)
from iesplan.computation.runtime.solver_runtime import SolverRuntime

__all__ = [
    "AttemptContext",
    "DeploymentPolicy",
    "ExecutorDescriptor",
    "ExecutorRegistry",
    "SolverRuntime",
    "build_executor_registry",
]
