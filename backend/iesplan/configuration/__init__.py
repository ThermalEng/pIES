"""配置域公开门面（财务 Profile/覆盖/有效快照/规划配置/计算配置表，归属 configuration）。

外部只允许经本门面消费 contract、repository 协议与 repository 实现函数；
不得导入 `iesplan.models`、services 或其他域的内部模块。
"""

from __future__ import annotations

from iesplan.configuration import calc, persistence
from iesplan.configuration.calc import (
    ALGO_DB_CLASS,
    DEFAULT_CONFIG_NAME,
    ECONOMIC_PARAM_SPECS,
    ENVIRONMENTAL_PARAM_SPECS,
    OBJECTIVE_METRICS,
    PREDEFINED_CONSTRAINT_KINDS,
    SOLVER_ID,
    VARIABLE_TYPES,
    normalize_config,
    row_to_config,
    validate_config,
)
from iesplan.configuration.contracts import (
    CalcConfigRecord,
    ConfigurationConflictError,
    ConfigurationNotFoundError,
    EffectiveRevisionRecord,
    FinanceProfileRecord,
    OverridesRevisionRecord,
    PlanningRevisionRecord,
)

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

#: 领域不可变表清单(唯一真相归 persistence 所有, 本门面只做引用重导出)。
IMMUTABLE_TABLES = persistence.IMMUTABLE_TABLES


def install_triggers() -> tuple[str, ...]:
    """公开生命周期钩子: 返回本域触发器部署语句(按执行序, 供组合根编排收集)。"""
    return persistence.install_triggers()


__all__ = [
    "ALGO_DB_CLASS",
    "DEFAULT_CONFIG_NAME",
    "ECONOMIC_PARAM_SPECS",
    "ENVIRONMENTAL_PARAM_SPECS",
    "OBJECTIVE_METRICS",
    "PREDEFINED_CONSTRAINT_KINDS",
    "SOLVER_ID",
    "VARIABLE_TYPES",
    "CalcConfigRecord",
    "ConfigurationConflictError",
    "ConfigurationNotFoundError",
    "EffectiveRevisionRecord",
    "FinanceProfileRecord",
    "OverridesRevisionRecord",
    "PlanningRevisionRecord",
    "append_effective",
    "append_overrides",
    "append_planning",
    "calc",
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
    "normalize_config",
    "register_profile",
    "row_to_config",
    "update_calc_config",
    "validate_config",
    "IMMUTABLE_TABLES",
    "install_triggers",
]
