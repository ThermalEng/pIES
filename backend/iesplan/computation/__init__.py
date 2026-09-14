"""计算边界门面(0.8 延期: 类型化能力/输入契约 + 空 provider 目录 + 边界协议)。

公开符号(选择性重导出, 仅此门面为算法注册表与计算组合的调用入口):

- 注册表: ``DEFAULT_ALGORITHM`` / ``AlgorithmSpec`` /
  ``get_algorithm`` / ``list_algorithms``(实现见 ``registry``);
- 边界协议与载体: ``GeneratorProvider`` / ``SolverRuntime`` /
  ``ResultAdapter`` / ``CalculationConfig`` / ``SolverBundle`` /
  ``ExecutionReceipt`` / ``ComputeResult``(宪法 §4.5 三段式边界);
- 能力组合(键与解析唯一归属 computation): ``GENERATOR_PROVIDER_KEY`` /
  ``SOLVER_RUNTIME_KEY`` / ``RESULT_ADAPTER_KEY`` /
  ``ComputeCapabilities`` / ``resolve_capabilities``(不透明目录 →
  类型化能力, 见 ``capabilities``);
- 类型化输入契约(快照已签发事实的表示映射): ``ComputeResources`` /
  ``ComputeInputs`` / ``build_compute_inputs``(见 ``inputs``);
- 不可用语义: ``ComputationUnavailableError``;
- provider 目录: ``available_providers``(当前恒为空, 明确无可用
  provider; 组合根经此取目录)。

约束: 本包不实现任何 0.8 求解器/生成器算法, 不包装旧引擎函数。
"""

from iesplan.computation.capabilities import (
    GENERATOR_PROVIDER_KEY,
    RESULT_ADAPTER_KEY,
    SOLVER_RUNTIME_KEY,
    ComputeCapabilities,
    resolve_capabilities,
)
from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.inputs import ComputeInputs, ComputeResources, build_compute_inputs
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
    "GENERATOR_PROVIDER_KEY",
    "RESULT_ADAPTER_KEY",
    "SOLVER_BUNDLE_SCHEMA",
    "SOLVER_BUNDLE_VERSION",
    "SOLVER_RUNTIME_KEY",
    "AlgorithmSpec",
    "CalculationConfig",
    "ComputationUnavailableError",
    "ComputeCapabilities",
    "ComputeInputs",
    "ComputeResources",
    "ComputeResult",
    "ExecutionReceipt",
    "GeneratorProvider",
    "ResultAdapter",
    "SolverBundle",
    "SolverRuntime",
    "available_providers",
    "build_compute_inputs",
    "get_algorithm",
    "list_algorithms",
    "resolve_capabilities",
]
