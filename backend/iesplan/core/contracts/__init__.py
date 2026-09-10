"""core/contracts: 无状态纯类型(宪法 4.1)。

- ``ParameterSpec``、``ProjectBaseline``、``PlanningConfig``
  等公共数据类型归属本包, 不携带注册状态;
- 财务三件套(FinanceProfile / FinanceOverrides / EffectiveFinanceConfig)
  属 finance 模块领域结构(0.6.5 条目 1, finance-yaml@1.0.0), 见
  ``iesplan.finance.triplet``; 旧单体 FinanceConfig 契约已退役;
- core 包禁止导入任何业务模块。
"""

from iesplan.core.contracts.baseline import (
    BASELINE_CANON_ALGORITHM_ID,
    BASELINE_CANON_ALGORITHM_VERSION,
    DEFAULT_SCENARIO_MODE,
    RESOLUTION_VALUES,
    SCENARIO_MODES,
    TIMELINE_STEP_DURATION_VAR,
    ProjectBaseline,
    ProjectBaselineError,
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
    "TIMELINE_STEP_DURATION_VAR",
    "BASELINE_CANON_ALGORITHM_ID",
    "BASELINE_CANON_ALGORITHM_VERSION",
    "CONSTRAINT_TYPES",
    "DEFAULT_SCENARIO_MODE",
    "OBJECTIVE_SENSES",
    "PLANNING_CANON_ALGORITHM_ID",
    "PLANNING_CANON_ALGORITHM_VERSION",
    "RESOLUTION_VALUES",
    "SCENARIO_MODES",
    "Constraint",
    "Objective",
    "ParameterSpec",
    "PlanningConfig",
    "PlanningConfigError",
    "PlanningVariable",
    "ProjectBaseline",
    "ProjectBaselineError",
]
