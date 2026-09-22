"""计算边界门面（0.8 切片 A：稳定公共协议、算法选择契约与生成器目录）。

公开符号(选择性重导出, 仅此门面为算法注册表、生成器目录与计算组合的
调用入口):

- 注册表: ``DEFAULT_ALGORITHM`` / ``AlgorithmSpec`` /
  ``get_algorithm`` / ``list_algorithms``(实现见 ``registry``);
- 生成器目录: ``GeneratorDescriptor`` / ``GeneratorRegistry`` /
  ``build_registry``(实现见 ``generators``; 实例由 bootstrap 组合根
  唯一装配发布, API/Worker 不得自行发现或动态加载);
- Bundle 契约: ``load_bundle_schema`` / ``normalize_bundle`` /
  ``validate_bundle``(实现见 ``bundle``);
- 计算配置契约: ``load_calculation_config_schema`` /
  ``validate_calculation_config``(实现见 ``config``);
- 序列物化: ``MaterializedSeries`` / ``MaterializedSet`` /
  ``materialize_non_predictive_series``(实现见 ``sequences``);
- 边界协议与载体: ``GeneratorProvider`` / ``SolverRuntime`` /
  ``ResultAdapter`` / ``CalculationConfig`` / ``SolverBundle`` /
  ``ExecutionReceipt`` / ``ComputeResult``(宪法 §4.5 三段式边界);
- 共享校验: ``check_provider_ref``;
- 不可用语义: ``ComputationUnavailableError``;

当前不提供真实 provider 实现、运行期接线或快照转换；空目录选择时即
明确不可用，计算 Worker 在组合根明确拒绝启动。
"""

from iesplan.computation.bundle import (
    load_bundle_schema,
    normalize_bundle,
    validate_bundle,
)
from iesplan.computation.config import (
    load_calculation_config_schema,
    validate_calculation_config,
)
from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.generators import (
    GeneratorDescriptor,
    GeneratorRegistry,
    build_project_model,
    build_registry,
    write_mps,
)
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
    check_provider_ref,
)
from iesplan.computation.registry import (
    DEFAULT_ALGORITHM,
    AlgorithmSpec,
    get_algorithm,
    list_algorithms,
)
from iesplan.computation.sequences import (
    MaterializedSeries,
    MaterializedSet,
    materialize_non_predictive_series,
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
    "GeneratorDescriptor",
    "GeneratorProvider",
    "GeneratorRegistry",
    "MaterializedSeries",
    "MaterializedSet",
    "ResultAdapter",
    "SolverBundle",
    "SolverRuntime",
    "build_project_model",
    "build_registry",
    "check_provider_ref",
    "get_algorithm",
    "list_algorithms",
    "load_bundle_schema",
    "load_calculation_config_schema",
    "materialize_non_predictive_series",
    "normalize_bundle",
    "validate_bundle",
    "validate_calculation_config",
    "write_mps",
]
