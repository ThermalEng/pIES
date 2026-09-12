"""计算分析模块(4 层,03 §8 / 05 §3.2,审查意见第 7 条)。

扫描点结果消费与纯分析聚合,用于单因素敏感性、多场景/多参数组合跑的
结果结构化输出。

组成:
  - wrapper.py: SweepSpec/SweepResult/BatchResult + apply_param/
    summarize_sweep/summarize_batch(纯计算,无 DB;只消费调用方提供的
    不可变扫描点结果,不构造 plan、不调用引擎、不执行财务计算;
    批量扇出与调度预留 application/0.8);
  - sensitivity.py: 指标对参数的变化率与影响排序(rank_indicators/rank_parameters)
    + 任务命令定义(build_sensitivity_task_config,纯 dict)+ 证据载荷
    (build_analysis_payload);
  - indicators.py: 能效/排放指标门面(自 metrics.engineering/environmental 迁入);
  - assessment.py: 四维评估门面 + check_financial(读 evidence financial 块);
  - _minfinance.py: 财务依赖(finance 包 M5 落地前的最小实现,接口 03 §7.2)。

依赖(Wave 4-B 后): wrapper 只消费计算结果、回执和声明输出,只经 core
聚合,不导入 engines/services/assembly.plan/finance/metrics/worker;
assessment/indicators/_minfinance 的 metrics 门面转发为残余债务,随门面
归属收尾。门面: summarize_sweep / summarize_batch /
build_analysis_payload / build_sensitivity_task_config。
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
)
from iesplan.analysis.wrapper import (
    AnalysisError,
    BatchResult,
    SweepResult,
    SweepSpec,
    apply_param,
    change_rate,
    financial_to_dict,
    jsonable_kpi,
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
    "financial_to_dict",
    "jsonable_kpi",
    "check_financial",
    "energy_balance_summary",
    "load_met_ratio",
    "operational_emissions",
    "peak_demand",
    "rank_indicators",
    "rank_parameters",
    "summarize_batch",
    "summarize_four_dimensions",
    "summarize_sweep",
]
