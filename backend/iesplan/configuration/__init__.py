"""配置域公开门面（财务 Profile/覆盖/有效快照/规划配置/计算配置表，归属 configuration）。

外部只允许经本门面消费 contract 与 repository 协议；不得导入本域
repository 实现（切片 5 落实）、`iesplan.models` 或 services。
"""

from __future__ import annotations

from iesplan.configuration.contracts import (
    CalcConfigRecord,
    ConfigurationConflictError,
    ConfigurationNotFoundError,
    EffectiveRevisionRecord,
    FinanceProfileRecord,
    OverridesRevisionRecord,
    PlanningRevisionRecord,
)
from iesplan.configuration.repository import ConfigurationRepository

__all__ = [
    "CalcConfigRecord",
    "ConfigurationConflictError",
    "ConfigurationNotFoundError",
    "ConfigurationRepository",
    "EffectiveRevisionRecord",
    "FinanceProfileRecord",
    "OverridesRevisionRecord",
    "PlanningRevisionRecord",
]
