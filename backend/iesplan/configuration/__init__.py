"""配置域公开门面（财务 Profile/覆盖/有效快照/规划配置/计算配置表，归属 configuration）。

外部只允许经本门面消费 contract、repository 协议与 repository 实现函数；
不得导入 `iesplan.models`、services 或其他域的内部模块。
"""

from __future__ import annotations

from iesplan.configuration import persistence
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

append_effective = persistence.append_effective
append_overrides = persistence.append_overrides
append_planning = persistence.append_planning
create_calc_config = persistence.create_calc_config
freeze_calc_config = persistence.freeze_calc_config
get_calc_config = persistence.get_calc_config
get_current_effective = persistence.get_current_effective
get_current_overrides = persistence.get_current_overrides
get_current_planning = persistence.get_current_planning
get_effective_revision = persistence.get_effective_revision
get_overrides_revision = persistence.get_overrides_revision
get_planning_revision = persistence.get_planning_revision
get_profile = persistence.get_profile
get_profile_row = persistence.get_profile_row
list_calc_configs = persistence.list_calc_configs
list_profiles = persistence.list_profiles
next_effective_revision = persistence.next_effective_revision
next_overrides_revision = persistence.next_overrides_revision
next_planning_revision = persistence.next_planning_revision
register_profile = persistence.register_profile
update_calc_config = persistence.update_calc_config

__all__ = [
    "CalcConfigRecord",
    "ConfigurationConflictError",
    "ConfigurationNotFoundError",
    "ConfigurationRepository",
    "EffectiveRevisionRecord",
    "FinanceProfileRecord",
    "OverridesRevisionRecord",
    "PlanningRevisionRecord",
    "append_effective",
    "append_overrides",
    "append_planning",
    "create_calc_config",
    "freeze_calc_config",
    "get_calc_config",
    "get_current_effective",
    "get_current_overrides",
    "get_current_planning",
    "get_effective_revision",
    "get_overrides_revision",
    "get_planning_revision",
    "get_profile",
    "get_profile_row",
    "list_calc_configs",
    "list_profiles",
    "next_effective_revision",
    "next_overrides_revision",
    "next_planning_revision",
    "register_profile",
    "update_calc_config",
]
