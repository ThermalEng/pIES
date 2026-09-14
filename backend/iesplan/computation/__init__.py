"""计算边界门面(0.8 延期: 注册表 + 空 provider 目录 + 边界协议)。

公开符号(选择性重导出, 仅此门面为算法注册表的调用入口):

- 注册表: ``DEFAULT_ALGORITHM`` / ``AlgorithmSpec`` /
  ``get_algorithm`` / ``list_algorithms``(实现见 ``registry``);
- 边界协议与载体: ``GeneratorProvider`` / ``SolverRuntime`` /
  ``ResultAdapter`` / ``CalculationConfig`` / ``SolverBundle`` /
  ``ExecutionReceipt`` / ``ComputeResult``(宪法 §4.5 三段式边界);
- 不可用语义: ``ComputationUnavailableError``;
- provider 目录: ``available_providers``(当前恒为空, 明确无可用
  provider; 组合根经此取目录)。

约束: 本包不实现任何 0.8 求解器/生成器算法, 不包装旧引擎函数。
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
from iesplan.computation.providers import available_providers
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
    "available_providers",
    "get_algorithm",
    "list_algorithms",
]
