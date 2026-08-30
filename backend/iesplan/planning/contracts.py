"""planning 域: 规划配置领域规则层(宪法 4.6 / 0.6.5 事项 3)。

本模块是 planning 域的**领域规则**层, 消费 ``core.contracts.planning_config``
的纯值对象, 补充装配/规划完整性所需的领域校验:

- 规划变量设备引用格式(小写字母数字 + '_' + '.' + '-', 与设备 ID 命名空间
  一致); 设备存在性与容量上下界落在设备技术参数有效区间属于装配阶段 4
  (规划与财务完整性)校验, 不在本层;
- 目标/约束表达式语法本体属 modeling/装配域, 本层只做形状与白名单复核
  (核心契约已拦截结构错误), 领域层补充聚合完整性(存在但全部未启用的约束);
- 规划与结果财务计算必须固定同一 FinanceConfig revision: 一致性校验见
  ``finance.contracts.check_finance_revision``(本层不重复实现)。

本模块不依赖 HTTP、数据库或前端, 不反向依赖应用服务。
"""

from __future__ import annotations

import re

from iesplan.core.contracts import PlanningConfig
from iesplan.core.diagnostics import Diagnostic, make_diag

#: 规划变量设备引用格式(与设备 ID 命名空间一致)。
_DEVICE_REF_RE: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")

#: 领域诊断码(登记于 core.diagnostics NEW_DIAG_CODES)。
PLAN_VAR_REF_INVALID = "PROJ-PLAN-003"


def validate_planning_domain(config: PlanningConfig) -> list[Diagnostic]:
    """PlanningConfig 领域校验(设备引用格式 + 聚合完整性)。

    返回阻断诊断列表(非法时非空)。
    """
    diags: list[Diagnostic] = []
    for var_name, variable in sorted(config.variables.items()):
        if not _DEVICE_REF_RE.fullmatch(variable.device_ref):
            diags.append(
                make_diag(
                    PLAN_VAR_REF_INVALID,
                    params={
                        "detail": (
                            f"规划变量 {var_name} 的设备引用 "
                            f"{variable.device_ref!r} 格式非法"
                        ),
                    },
                    location={
                        "object_type": "planning_config",
                        "field": f"variables.{var_name}.device_ref",
                    },
                )
            )
    if config.constraints and not any(c.enabled for c in config.constraints.values()):
        diags.append(
            make_diag(
                PLAN_VAR_REF_INVALID,
                params={"detail": "规划配置存在约束但全部未启用"},
                location={"object_type": "planning_config", "field": "constraints"},
            )
        )
    return diags
