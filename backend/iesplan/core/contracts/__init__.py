"""core/contracts: 无状态纯类型(宪法 4.1)。

- ``ParameterSpec``、``ProjectBaseline``、``FinanceConfig``、
  ``PlanningConfig`` 等公共数据类型归属本包, 不携带注册状态;
- core 包禁止导入任何业务模块。
"""

from iesplan.core.contracts.baseline import (
    BASELINE_CANON_ALGORITHM_ID,
    BASELINE_CANON_ALGORITHM_VERSION,
    DEFAULT_SCENARIO_MODE,
    RESOLUTION_VALUES,
    SCENARIO_MODES,
    ProjectBaseline,
    ProjectBaselineError,
)
from iesplan.core.contracts.finance_config import (
    CURRENCIES,
    ENERGY_PRICE_KEYS,
    FINANCE_CANON_ALGORITHM_ID,
    FINANCE_CANON_ALGORITHM_VERSION,
    DeviceFinanceParams,
    FinanceConfig,
    FinanceConfigError,
    MoneyAmount,
)
from iesplan.core.contracts.parameters import ParameterSpec
from iesplan.core.contracts.planning_config import (
    CONSTRAINT_TYPES,
    OBJECTIVE_SENSES,
    PLANNING_CANON_ALGORITHM_ID,
    PLANNING_CANON_ALGORITHM_VERSION,
    Constraint,
    Objective,
    PlanningConfig,
    PlanningConfigError,
    PlanningVariable,
)

__all__ = [
    "BASELINE_CANON_ALGORITHM_ID",
    "BASELINE_CANON_ALGORITHM_VERSION",
    "CONSTRAINT_TYPES",
    "CURRENCIES",
    "DEFAULT_SCENARIO_MODE",
    "ENERGY_PRICE_KEYS",
    "FINANCE_CANON_ALGORITHM_ID",
    "FINANCE_CANON_ALGORITHM_VERSION",
    "OBJECTIVE_SENSES",
    "PLANNING_CANON_ALGORITHM_ID",
    "PLANNING_CANON_ALGORITHM_VERSION",
    "RESOLUTION_VALUES",
    "SCENARIO_MODES",
    "Constraint",
    "DeviceFinanceParams",
    "FinanceConfig",
    "FinanceConfigError",
    "MoneyAmount",
    "Objective",
    "ParameterSpec",
    "PlanningConfig",
    "PlanningConfigError",
    "PlanningVariable",
    "ProjectBaseline",
    "ProjectBaselineError",
]
