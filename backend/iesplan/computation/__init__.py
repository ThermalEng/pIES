"""计算边界门面（0.8 延期：只保留稳定公共协议与算法选择契约）。

公开符号(选择性重导出, 仅此门面为算法注册表与计算组合的调用入口):

- 注册表: ``DEFAULT_ALGORITHM`` / ``AlgorithmSpec`` /
  ``get_algorithm`` / ``list_algorithms``(实现见 ``registry``);
- 边界协议与载体: ``GeneratorProvider`` / ``SolverRuntime`` /
  ``ResultAdapter`` / ``CalculationConfig`` / ``SolverBundle`` /
  ``ExecutionReceipt`` / ``ComputeResult``(宪法 §4.5 三段式边界);
- 不可用语义: ``ComputationUnavailableError``;

当前不提供 provider 目录、运行期接线或快照转换；计算 Worker 在组合根
明确拒绝启动。真实资源加载与三段式编排随 0.8 一次实现。
"""

from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.protocols import (
    CALCULATION_CONFIG_SCHEMA,
    CALCULATION_CONFIG_VERSION,
    COMPUTE_RESULT_SCHEMA,
    COMPUTE_RESULT_VERSION,
    SOLVER_BUNDLE_SCHEMA,
    SOLVER_BUNDLE_VERSION,
    CalculationConfig,
    ComputeResult,
    ExecutionReceipt,
    GeneratorProvider,
    ResultAdapter,
    SolverBundle,
    SolverRuntime,
)
from iesplan.computation.registry import (
    DEFAULT_ALGORITHM,
    AlgorithmSpec,
    get_algorithm,
    list_algorithms,
)

__all__ = [
    "CALCULATION_CONFIG_SCHEMA",
    "CALCULATION_CONFIG_VERSION",
    "COMPUTE_RESULT_SCHEMA",
    "COMPUTE_RESULT_VERSION",
    "DEFAULT_ALGORITHM",
    "SOLVER_BUNDLE_SCHEMA",
    "SOLVER_BUNDLE_VERSION",
    "AlgorithmSpec",
    "CalculationConfig",
    "ComputationUnavailableError",
    "ComputeResult",
    "ExecutionReceipt",
    "GeneratorProvider",
    "ResultAdapter",
    "SolverBundle",
    "SolverRuntime",
    "get_algorithm",
    "list_algorithms",
]
