"""配置域 repository 协议（财务 Profile/覆盖/有效快照/规划配置/计算配置表）。

实现规则（切片 5 落实）：
- revision 类表只 INSERT；指针移动经 project 域；绝不 commit/rollback；
- 合并计算（EffectiveFinanceConfig 合成）是领域规则，不在本协议；
  本协议只读写 revision 行。
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy.orm import Session

from iesplan.configuration.contracts import (
    CalcConfigRecord,
    EffectiveRevisionRecord,
    FinanceProfileRecord,
    OverridesRevisionRecord,
    PlanningRevisionRecord,
)


class ConfigurationRepository(Protocol):
    """配置聚合 repository 协议（无状态方法组，db 由调用方事务拥有）。"""

    def get_profile(self, db: Session, profile_id: str) -> FinanceProfileRecord | None: ...

    def list_profiles(self, db: Session) -> list[FinanceProfileRecord]: ...

    def register_profile(
        self,
        db: Session,
        *,
        profile_id: str,
        region: str,
        content: dict[str, Any],
        object_id: int,
        created_by: int,
    ) -> FinanceProfileRecord:
        """登记地区 Profile；重复 profile_id 抛 ConfigurationConflictError。"""
        ...

    def get_overrides_revision(
        self, db: Session, project_id: int, revision: int
    ) -> OverridesRevisionRecord | None: ...

    def get_current_overrides(self, db: Session, project_id: int) -> OverridesRevisionRecord | None: ...

    def append_overrides(
        self,
        db: Session,
        *,
        project_id: int,
        content: dict[str, Any],
        profile_id: str,
        created_by: int,
        receipt_object_id: int | None = None,
    ) -> OverridesRevisionRecord:
        """追加覆盖 revision（含 revision 号分配）。"""
        ...

    def get_effective_revision(
        self, db: Session, project_id: int, revision: int
    ) -> EffectiveRevisionRecord | None: ...

    def get_current_effective(self, db: Session, project_id: int) -> EffectiveRevisionRecord | None: ...

    def append_effective(
        self,
        db: Session,
        *,
        project_id: int,
        content: dict[str, Any],
        profile_id: str,
        created_by: int,
        receipt_object_id: int | None = None,
    ) -> EffectiveRevisionRecord:
        """追加有效快照 revision（内容由领域合并器先算好再传入）。"""
        ...

    def get_planning_revision(
        self, db: Session, project_id: int, revision: int
    ) -> PlanningRevisionRecord | None: ...

    def get_current_planning(self, db: Session, project_id: int) -> PlanningRevisionRecord | None: ...

    def append_planning(
        self,
        db: Session,
        *,
        project_id: int,
        content: dict[str, Any],
        created_by: int,
        receipt_object_id: int | None = None,
    ) -> PlanningRevisionRecord: ...

    def get_calc_config(self, db: Session, config_id: int) -> CalcConfigRecord | None: ...

    def list_calc_configs(self, db: Session, project_id: int) -> list[CalcConfigRecord]: ...

    def create_calc_config(
        self,
        db: Session,
        *,
        project_id: int,
        name: str,
        params: dict[str, Any],
        variables: list[Any],
        objectives: list[Any],
        constraints: list[Any],
        tolerances: dict[str, Any],
        updated_by: int,
        description: str | None = None,
    ) -> CalcConfigRecord: ...

    def update_calc_config(
        self, db: Session, config_id: int, *, values: dict[str, Any], updated_by: int
    ) -> CalcConfigRecord:
        """更新草稿态配置；frozen 配置抛 ConfigurationConflictError。"""
        ...

    def freeze_calc_config(self, db: Session, config_id: int) -> CalcConfigRecord: ...
