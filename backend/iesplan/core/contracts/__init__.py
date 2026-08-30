"""core/contracts: 无状态纯类型(宪法 4.1)。

- ``ParameterSpec``、``ProjectBaseline`` 等公共数据类型归属本包, 不携带
  注册状态;
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
from iesplan.core.contracts.parameters import ParameterSpec

__all__ = [
    "BASELINE_CANON_ALGORITHM_ID",
    "BASELINE_CANON_ALGORITHM_VERSION",
    "DEFAULT_SCENARIO_MODE",
    "RESOLUTION_VALUES",
    "SCENARIO_MODES",
    "ProjectBaseline",
    "ProjectBaselineError",
    "ParameterSpec",
]
