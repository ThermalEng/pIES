"""数据库 schema 版本化迁移包。"""

from __future__ import annotations

from iesplan.migrations.runner import MIGRATIONS, applied_versions, apply_migrations
from iesplan.migrations.v1_2_0_calc_config import (
    MIGRATION_VERSION,
    NON_TERMINAL_TASK_STATUSES,
    UpgradeDrainError,
    assert_no_inflight_tasks,
    convert_calc_config_115_to_120,
    list_inflight_tasks,
    upgrade_calc_configs,
)

__all__ = [
    "MIGRATIONS",
    "MIGRATION_VERSION",
    "NON_TERMINAL_TASK_STATUSES",
    "UpgradeDrainError",
    "applied_versions",
    "apply_migrations",
    "assert_no_inflight_tasks",
    "convert_calc_config_115_to_120",
    "list_inflight_tasks",
    "upgrade_calc_configs",
]
