"""受控执行公开 contract、独立注册、通用运行时与本地进程执行器。

公开 ExecutorProvider 身份与唯一调用形状、描述符、原子注册表，
执行层消费的 attempt 上下文、部署策略、具体通用 ``SolverRuntime``，
以及真实本地进程 ``LocalProcessProvider`` 与 executable 绑定。
执行器选择与装配只归 bootstrap 组合根；API/Worker 不得自行发现或动态
加载 provider。无可用 provider 时统一抛 ``ComputationUnavailableError``。
"""

from iesplan.computation.runtime.context import AttemptContext
from iesplan.computation.runtime.descriptor import ExecutorDescriptor
from iesplan.computation.runtime.local_process import (
    HIGHS_ADAPTER_PATH,
    HIGHS_EXECUTABLE_REF,
    HIGHS_VERSION,
    MAX_STREAM_BYTES,
    TERMINATE_GRACE_SECONDS,
    ExecutableReadiness,
    LocalExecutable,
    LocalProcessProvider,
    validate_highs_arguments,
)
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
    "HIGHS_ADAPTER_PATH",
    "HIGHS_EXECUTABLE_REF",
    "HIGHS_VERSION",
    "MAX_STREAM_BYTES",
    "TERMINATE_GRACE_SECONDS",
    "ExecutableReadiness",
    "LocalExecutable",
    "LocalProcessProvider",
    "SolverRuntime",
    "build_executor_registry",
    "validate_highs_arguments",
]
