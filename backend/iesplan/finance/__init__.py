"""财务计算模块门面(03-module-decoupling.md §7 / 05-architecture-overview.md §3.1)。

定位:2 层独立包,在计算模块逐时运行结果之上计算财务数据(现金流/NPV/IRR/LCOE/
回收期),产出 evidence `financial` 块;不依赖 engines(依赖方向 engines→finance 单向)。

公共入口(与 03 §7.2 对齐):
- metrics:npv / cashflow_irr / build_project_cashflows / project_npv / project_irr /
  build_equity_cashflows / equity_irr / IRRStatus(自 metrics/financial.py 迁入);
- hourly:compute_financials / compute_lcoe / compute_payback / FinancialResult(新增);
- params:FinanceParams / finance_params_from_config。
"""

from __future__ import annotations

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
from iesplan.finance.hourly import FinancialResult, compute_financials, compute_lcoe, compute_payback
from iesplan.finance.params import FinanceParams, finance_params_from_config

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
]
