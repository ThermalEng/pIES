"""财务计算模块门面（见 ARCHITECTURE_CONSTITUTION.md §4.6 与 modules/finance.md）。

定位:2 层独立包,在计算模块逐时运行结果之上计算财务数据(现金流/NPV/IRR/LCOE/
回收期),产出 evidence `financial` 块;不依赖 engines(依赖方向 engines→finance 单向)。

公共入口:
- metrics:npv / cashflow_irr / build_project_cashflows / project_npv / project_irr /
  build_equity_cashflows / equity_irr / IRRStatus(自 metrics/financial.py 迁入);
- hourly:compute_financials / compute_lcoe / compute_payback / FinancialResult(新增);
- params:FinanceParams / finance_params_from_config;
- tax(B0 纯计算关系):split_tax_amount / settle_vat_period / use_losses /
  income_tax_payable / straight_line_period_amount 及不可变值对象;
- 财务三件套(finance-yaml@4.0.0):FinanceProfile / FinanceOverrides /
  EffectiveFinanceConfig 领域结构、规范化摘要与确定性合并器 merge_effective
  (0.6.5 契约切片,见 triplet 模块); B1a 税则/池规则 CreditPool / TaxRule。
"""

from __future__ import annotations

from iesplan.finance.confirmation import (
    ConstructionPayment,
    EvaluationInput,
    MaintenanceCostComponentShare,
    MaintenanceExpense,
    MaintenanceScaleUnitCost,
    MaintenanceTerm,
    MaintenanceUnitCost,
)
from iesplan.finance.evaluation import (
    EVALUATION_SCHEMA,
    EVALUATION_VERSION,
    EvaluationYearRow,
    IRREvaluationStatus,
    PaybackStatus,
    ProjectEvaluation,
    evaluate_project_investment,
)
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
from iesplan.finance.pricecsv import (
    SCHEMA_ID as PRICE_SERIES_SCHEMA_ID,
)
from iesplan.finance.pricecsv import (
    SCHEMA_VERSION as PRICE_SERIES_SCHEMA_VERSION,
)
from iesplan.finance.pricecsv import (
    ParsedPriceFile,
    PriceSeriesMeta,
    emit_price_series_csv,
    is_price_series_csv,
    parse_price_series_file,
    read_trusted_price_values,
)
from iesplan.finance.tax import (
    LossBalance,
    LossUseResult,
    TaxSplit,
    VatSettlement,
    income_tax_payable,
    settle_vat_period,
    split_tax_amount,
    straight_line_period_amount,
    use_losses,
)
from iesplan.finance.timeline import (
    AnnualBreakdown,
    InterfaceDriverFacts,
    TariffYearFacts,
    TimelineInputs,
    annual_boundaries,
    annual_energy_nets,
    annual_fixed_om_total,
    annual_variable_om,
    assemble_cashflows,
    combine_annual_nets,
    construction_total,
)
from iesplan.finance.triplet import (
    CONFIRM_METHODS,
    COST_METHOD_VALUES,
    CURRENCIES,
    DIRECTION_VALUES,
    FINANCE_COMPONENTS,
    KIND_VALUES,
    PRICE_BASIS_VALUES,
    RECEIVABLE_POLICIES,
    RESOLUTION_VALUES,
    SCHEMA_EFFECTIVE,
    SCHEMA_OVERRIDES,
    SCHEMA_PROFILE,
    SCHEMA_VERSION,
    TAX_RULE_KINDS,
    VAT_SETTLEMENTS,
    ConstructionConfirmation,
    ConstructionScheduleEntry,
    ConstructionVatRule,
    CostComponent,
    CreditPool,
    DeductibleMaintenance,
    DemandCharge,
    DemandInputTaxEntry,
    DepreciationEntry,
    EffectiveFinanceConfig,
    EnergyPrice,
    FinanceOverrides,
    FinanceProfile,
    FinanceTripletError,
    FinanceTypeEntry,
    IncomeTaxRule,
    InTaxEntry,
    MaintInputTaxEntry,
    MaintItem,
    OmInputTaxEntry,
    OpeningLoss,
    OutTaxEntry,
    PoolRefund,
    SeriesMeta,
    TaxRule,
    VatRule,
    merge_effective,
)
from iesplan.finance.yaml_io import effective_from_yaml, effective_to_yaml

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
    # confirmation(评价确认值对象，公开所有权)
    "ConstructionPayment",
    "EvaluationInput",
    "MaintenanceExpense",
    "MaintenanceUnitCost",
    "MaintenanceScaleUnitCost",
    "MaintenanceCostComponentShare",
    "MaintenanceTerm",
    # evaluation(0.10-D 纯评价切片)
    "EVALUATION_SCHEMA",
    "EVALUATION_VERSION",
    "EvaluationYearRow",
    "IRREvaluationStatus",
    "PaybackStatus",
    "ProjectEvaluation",
    "evaluate_project_investment",
    # timeline(0.12-T2 评价时间轴现金流)
    "AnnualBreakdown",
    "InterfaceDriverFacts",
    "TariffYearFacts",
    "TimelineInputs",
    "annual_boundaries",
    "annual_energy_nets",
    "annual_fixed_om_total",
    "annual_variable_om",
    "assemble_cashflows",
    "combine_annual_nets",
    "construction_total",
    # params
    "FinanceParams",
    "finance_params_from_config",
    # tax(B0 纯计算关系)
    "LossBalance",
    "LossUseResult",
    "TaxSplit",
    "VatSettlement",
    "income_tax_payable",
    "settle_vat_period",
    "split_tax_amount",
    "straight_line_period_amount",
    "use_losses",
    # 财务三件套(finance-yaml@3.0.0; 0.6.5 契约切片 + B1a 税则池规则)
    "FinanceProfile",
    "DemandCharge",
    "DemandInputTaxEntry",
    "FinanceOverrides",
    "EffectiveFinanceConfig",
    "EnergyPrice",
    "CostComponent",
    "FinanceTypeEntry",
    "FINANCE_COMPONENTS",
    "SeriesMeta",
    "FinanceTripletError",
    "merge_effective",
    "TAX_RULE_KINDS",
    "VAT_SETTLEMENTS",
    "CONFIRM_METHODS",
    "RECEIVABLE_POLICIES",
    "CreditPool",
    "PoolRefund",
    "VatRule",
    "ConstructionVatRule",
    "ConstructionConfirmation",
    "ConstructionScheduleEntry",
    "IncomeTaxRule",
    "InTaxEntry",
    "OutTaxEntry",
    "OmInputTaxEntry",
    "MaintInputTaxEntry",
    "MaintItem",
    "DeductibleMaintenance",
    "DepreciationEntry",
    "OpeningLoss",
    "TaxRule",
    "effective_from_yaml",
    "effective_to_yaml",
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
    # 价格序列 CSV(ies.price-series@1.0.0; 0.12-F3a 契约切片)
    "PRICE_SERIES_SCHEMA_ID",
    "PRICE_SERIES_SCHEMA_VERSION",
    "ParsedPriceFile",
    "PriceSeriesMeta",
    "emit_price_series_csv",
    "is_price_series_csv",
    "parse_price_series_file",
    "read_trusted_price_values",
]
