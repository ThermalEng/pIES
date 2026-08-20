"""计算分析模块(4 层,03 §8 / 05 §3.2,审查意见第 7 条)。

计算模块 + 财务计算模块的 wrapper,用于批量分析(单因素敏感性扫描、多场景/多参数
组合跑)与结果结构化输出。

组成:
  - wrapper.py: SweepSpec/SweepResult/BatchResult + apply_param/run_sweep/run_batch
    /summarize_sweep/summarize_batch(纯计算,无 DB);
  - sensitivity.py: 指标对参数的变化率与影响排序(rank_indicators/rank_parameters)
    + 任务编排(run_sensitivity_analysis,DB 层懒导入)+ 证据载荷
    (build_analysis_payload);
  - indicators.py: 能效/排放指标门面(自 metrics.engineering/environmental 迁入);
  - assessment.py: 四维评估门面 + check_financial(读 evidence financial 块);
  - _minfinance.py: 财务依赖(finance 包 M5 落地前的最小实现,接口 03 §7.2)。

依赖(单向无环,03 §11): analysis → engines/finance/assembly/core。
门面: run_sweep / run_batch / run_sensitivity_analysis / summarize_sweep /
summarize_batch / build_analysis_payload。
"""

from __future__ import annotations

from iesplan.analysis.assessment import (
    FinancialValidity,
    OptimalityValidity,
    PhysicalValidity,
    ReliabilityStatus,
    ValidityLevel,
    check_financial,
    summarize_four_dimensions,
)
from iesplan.analysis.indicators import (
    capacity_utilization,
    energy_balance_summary,
    load_met_ratio,
    operational_emissions,
    peak_demand,
)
from iesplan.analysis.sensitivity import (
    build_analysis_payload,
    build_sensitivity_task_config,
    rank_indicators,
    rank_parameters,
    run_sensitivity_analysis,
)
from iesplan.analysis.wrapper import (
    AnalysisError,
    BatchResult,
    SweepResult,
    SweepSpec,
    apply_param,
    change_rate,
    run_batch,
    run_sweep,
    summarize_batch,
    summarize_sweep,
)

__all__ = [
    "AnalysisError",
    "BatchResult",
    "FinancialValidity",
    "OptimalityValidity",
    "PhysicalValidity",
    "ReliabilityStatus",
    "SweepResult",
    "SweepSpec",
    "ValidityLevel",
    "apply_param",
    "build_analysis_payload",
    "build_sensitivity_task_config",
    "capacity_utilization",
    "change_rate",
    "check_financial",
    "energy_balance_summary",
    "load_met_ratio",
    "operational_emissions",
    "peak_demand",
    "rank_indicators",
    "rank_parameters",
    "run_batch",
    "run_sensitivity_analysis",
    "run_sweep",
    "summarize_batch",
    "summarize_four_dimensions",
    "summarize_sweep",
]
