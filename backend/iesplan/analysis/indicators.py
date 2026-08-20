"""结果指标门面(03 §8.2,审查意见第 7 条):自 metrics/engineering.py、environmental.py 迁入。

能效指标: energy_balance_summary(电/热/冷能量平衡)/ peak_demand(峰值需量)/
capacity_utilization(容量利用率)/ load_met_ratio(负荷满足率);
环境指标: operational_emissions(运行期排放核算,REQ-ENV-001)。

迁移说明(M6): 当前为转发实现(metrics 目录保留转发兼容一个版本周期,03 §14.4),
引用方统一走本门面;metrics 退役时把实现整体迁入本文件、metrics 侧改转发。
所有指标输出携带 definition_version/unit/refs(REQ-RESULT-002)。
"""

from __future__ import annotations

from iesplan.metrics.engineering import (
    capacity_utilization,
    energy_balance_summary,
    load_met_ratio,
    peak_demand,
)
from iesplan.metrics.environmental import operational_emissions

__all__ = [
    "capacity_utilization",
    "energy_balance_summary",
    "load_met_ratio",
    "operational_emissions",
    "peak_demand",
]
