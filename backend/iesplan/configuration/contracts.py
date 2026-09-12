"""配置域公开契约(财务 Profile/覆盖/有效快照/规划配置/计算配置表，归属 configuration)。

包名说明：目标文本称本域为 config，但 `iesplan.config` 已被项目 settings 模块
占用，故包名为 configuration；表归属（门禁 8）不受影响。

- revision 类表只 INSERT、不原地修改；指针移动由 project 域的指针字段承担；
- 只含不可变值对象与领域错误；不导入 ORM、Session、services 或 application。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from iesplan.core.errors import ConflictError, NotFoundError


class ConfigurationNotFoundError(NotFoundError):
    """配置/Profile/revision 不存在（沿用基类诊断码，不新增码）。"""


class ConfigurationConflictError(ConflictError):
    """配置唯一冲突或冻结冲突（沿用基类诊断码，不新增码）。"""


@dataclass(frozen=True, slots=True)
class FinanceProfileRecord:
    """地区 FinanceProfile 注册行（finance_profiles 表公开视图）。"""

    id: int
    profile_id: str
    region: str
    content: dict[str, Any]
    object_id: int
    created_by: int = 0


@dataclass(frozen=True, slots=True)
class OverridesRevisionRecord:
    """项目 FinanceOverrides revision（finance_overrides 表，不可变）。"""

    id: int
    project_id: int
    revision: int
    content: dict[str, Any]
    profile_id: str
    receipt_object_id: int | None = None
    created_by: int = 0


@dataclass(frozen=True, slots=True)
class EffectiveRevisionRecord:
    """EffectiveFinanceConfig revision（effective_finance_revisions 表，不可变）。"""

    id: int
    project_id: int
    revision: int
    content: dict[str, Any]
    profile_id: str
    receipt_object_id: int | None = None
    created_by: int = 0


@dataclass(frozen=True, slots=True)
class PlanningRevisionRecord:
    """规划配置 revision（planning_configs 表，不可变）。"""

    id: int
    project_id: int
    revision: int
    content: dict[str, Any]
    receipt_object_id: int | None = None
    created_by: int = 0


@dataclass(frozen=True, slots=True)
class CalcConfigRecord:
    """计算配置行（calc_configs 表公开视图，归属本域）。

    注：该表物理上在 models.calc 中（与任务表同文件），但其语义是用户可编辑
    的计算配置，由本域 repository 读写；tasks 域只消费其快照，不直接读写本表。
    """

    id: int
    project_id: int
    name: str
    params: dict[str, Any]
    variables: list[Any]
    objectives: list[Any]
    constraints: list[Any]
    tolerances: dict[str, Any]
    description: str | None = None
    min_irr: float | None = None
    algorithm: str | None = None
    solver: str | None = None
    random_seed: int | None = None
    status: str = "draft"
    version: int = 1
    updated_by: int = 0
    updated_at: str | None = None
