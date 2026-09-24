"""计算边界门面（0.8 切片 A：稳定公共协议、算法选择契约与生成器目录）。

公开符号(选择性重导出, 仅此门面为算法注册表、生成器目录与计算组合的
调用入口):

- 注册表: ``DEFAULT_ALGORITHM`` / ``AlgorithmSpec`` /
  ``get_algorithm`` / ``list_algorithms``(实现见 ``registry``);
- 生成器目录: ``GeneratorDescriptor`` / ``GeneratorRegistry`` /
  ``build_registry``(实现见 ``generators``; 实例由 bootstrap 组合根
  唯一装配发布, API/Worker 不得自行发现或动态加载);
- Bundle 契约: ``load_bundle_schema`` / ``normalize_bundle`` /
  ``validate_bundle`` / ``manifest_to_yaml_bytes`` /
  ``export_bundle_files``(实现见 ``bundle``; 校验与导出只针对 manifest
  本体，完整聚合为 ``SolverBundle``);
- 计算配置契约: ``load_calculation_config_schema`` /
  ``validate_calculation_config``(实现见 ``config``);
- 序列物化: ``MaterializedSeries`` / ``MaterializedSet`` /
  ``materialize_non_predictive_series``(实现见 ``sequences``);
- 边界协议与载体: ``GeneratorProvider`` / ``ExecutorProvider`` /
  ``ResultAdapter`` / ``CalculationConfig`` /
  ``SolverBundleManifest`` / ``SolverBundle`` / ``ExecutionReceipt`` /
  ``ComputeResult``(宪法 §4.5 三段式边界);
- 执行器独立注册: ``ExecutorDescriptor`` / ``ExecutorRegistry`` /
  ``build_executor_registry``(实现见 ``runtime``);
- 首个真实生成器: ``HighsMilpProvider`` /
  ``HIGHS_MILP_PROVIDER``(实现见 ``generators.highs_milp``);
- 共享校验: ``check_provider_ref``;
- 不可用语义: ``ComputationUnavailableError``;

首个真实 provider 已实现并由组合根显式注册；运行期接线(0.9)与快照
转换尚未实现，未注册引用选择时即明确不可用，计算 Worker 在组合根
明确拒绝启动(执行编排属于 0.9)。
"""

from iesplan.computation.bundle import (
    export_bundle_files,
    load_bundle_schema,
    manifest_to_yaml_bytes,
    normalize_bundle,
    validate_bundle,
)
from iesplan.computation.config import (
    load_calculation_config_schema,
    validate_calculation_config,
)
from iesplan.computation.errors import ComputationUnavailableError
from iesplan.computation.generators import (
    HIGHS_MILP_PROVIDER,
    GeneratorDescriptor,
    GeneratorRegistry,
    HighsMilpProvider,
    build_project_model,
    build_registry,
    write_mps,
)
from iesplan.computation.protocols import (
    CALCULATION_CONFIG_SCHEMA,
    CALCULATION_CONFIG_VERSION,
    COMPUTE_RESULT_SCHEMA,
    COMPUTE_RESULT_VERSION,
    EXECUTION_RECEIPT_SCHEMA,
    EXECUTION_RECEIPT_VERSION,
    SOLVER_BUNDLE_SCHEMA,
    SOLVER_BUNDLE_VERSION,
    CalculationConfig,
    ComputeResult,
    ExecutionReceipt,
    ExecutorProvider,
    GeneratorProvider,
    ResultAdapter,
    SolverBundle,
    SolverBundleManifest,
    check_provider_ref,
)
from iesplan.computation.registry import (
    DEFAULT_ALGORITHM,
    AlgorithmSpec,
    get_algorithm,
    list_algorithms,
)
from iesplan.computation.runtime import (
    ExecutorDescriptor,
    ExecutorRegistry,
    build_executor_registry,
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
    "EXECUTION_RECEIPT_SCHEMA",
    "EXECUTION_RECEIPT_VERSION",
    "HIGHS_MILP_PROVIDER",
    "SOLVER_BUNDLE_SCHEMA",
    "SOLVER_BUNDLE_VERSION",
    "AlgorithmSpec",
    "CalculationConfig",
    "ComputationUnavailableError",
    "ComputeResult",
    "ExecutionReceipt",
    "ExecutorDescriptor",
    "ExecutorProvider",
    "ExecutorRegistry",
    "GeneratorDescriptor",
    "GeneratorProvider",
    "GeneratorRegistry",
    "HighsMilpProvider",
    "MaterializedSeries",
    "MaterializedSet",
    "ResultAdapter",
    "SolverBundle",
    "SolverBundleManifest",
    "build_executor_registry",
    "build_project_model",
    "build_registry",
    "check_provider_ref",
    "export_bundle_files",
    "get_algorithm",
    "list_algorithms",
    "load_bundle_schema",
    "load_calculation_config_schema",
    "manifest_to_yaml_bytes",
    "materialize_non_predictive_series",
    "normalize_bundle",
    "validate_bundle",
    "validate_calculation_config",
    "write_mps",
]
