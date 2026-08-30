"""planning 域: 规划配置领域规则(宪法 4.6 / 0.6.5 事项 3)。

公开门面只导出领域校验能力; 纯值对象契约位于
``core.contracts.planning_config``(core 边界)。
"""

from iesplan.planning.contracts import (
    PLAN_VAR_REF_INVALID,
    validate_planning_domain,
)

__all__ = ["PLAN_VAR_REF_INVALID", "validate_planning_domain"]
