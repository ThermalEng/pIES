"""财务计算模块门面（见 ARCHITECTURE_CONSTITUTION.md §4.6 与 modules/finance.md）。

定位:2 层独立包,在计算模块逐时运行结果之上计算财务数据(现金流/NPV/IRR/LCOE/
回收期),产出 evidence `financial` 块;不依赖 engines(依赖方向 engines→finance 单向)。

公共入口:
- metrics:npv / cashflow_irr / build_project_cashflows / project_npv / project_irr /
  build_equity_cashflows / equity_irr / IRRStatus(自 metrics/financial.py 迁入);
- hourly:compute_financials / compute_lcoe / compute_payback / FinancialResult(新增);
- params:FinanceParams / finance_params_from_config;
- 财务三件套(finance-yaml@1.0.0):FinanceProfile / FinanceOverrides /
  EffectiveFinanceConfig 领域结构、规范化摘要与确定性合并器 merge_effective
  (0.6.5 契约切片,见 triplet 模块)。
"""

from __future__ import annotations

from iesplan.finance.hourly import FinancialResult, compute_financials, compute_lcoe, compute_payback
from iesplan.finance.metrics import (
    IRRStatus,
    build_equity_cashflows,
    build_project_cashflows,
    cashflow_irr,
    equity_irr,
    npv,
    project_irr,
    project_npv,
)
from iesplan.finance.params import FinanceParams, finance_params_from_config
from iesplan.finance.triplet import (
    COST_METHOD_VALUES,
    CURRENCIES,
    DIRECTION_VALUES,
    KIND_VALUES,
    PRICE_BASIS_VALUES,
    RESOLUTION_VALUES,
    SCHEMA_EFFECTIVE,
    SCHEMA_OVERRIDES,
    SCHEMA_PROFILE,
    SCHEMA_VERSION,
    EffectiveFinanceConfig,
    EnergyPrice,
    FinanceOverrides,
    FinanceProfile,
    FinanceTripletError,
    SeriesMeta,
    merge_effective,
)

__all__ = [
    # metrics
    "IRRStatus",
    "npv",
    "cashflow_irr",
    "build_project_cashflows",
    "project_npv",
    "project_irr",
    "build_equity_cashflows",
    "equity_irr",
    # hourly
    "FinancialResult",
    "compute_financials",
    "compute_lcoe",
    "compute_payback",
    # params
    "FinanceParams",
    "finance_params_from_config",
    # 财务三件套(finance-yaml@1.0.0; 0.6.5 契约切片)
    "FinanceProfile",
    "FinanceOverrides",
    "EffectiveFinanceConfig",
    "EnergyPrice",
    "SeriesMeta",
    "FinanceTripletError",
    "merge_effective",
    "SCHEMA_PROFILE",
    "SCHEMA_OVERRIDES",
    "SCHEMA_EFFECTIVE",
    "SCHEMA_VERSION",
    "CURRENCIES",
    "PRICE_BASIS_VALUES",
    "COST_METHOD_VALUES",
    "DIRECTION_VALUES",
    "KIND_VALUES",
    "RESOLUTION_VALUES",
]
