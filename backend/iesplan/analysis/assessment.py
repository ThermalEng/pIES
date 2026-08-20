"""四维评估(03 §8.2/§7.4,审查意见第 6 条):自 metrics/validity.py 迁入。

- 枚举(ValidityLevel/PhysicalValidity/OptimalityValidity/FinancialValidity/
  ReliabilityStatus)与 summarize_four_dimensions 当前转发自 metrics.validity
  (metrics 目录保留转发兼容一个版本周期,03 §14.4);
- `check_financial` 新增实现: 读取 evidence `financial` 块(03 §7.4)计算财务
  维度,修复"财务恒 unknown"(缺失 → insufficient + 诊断说明)。

核心不变量 4: 汇总只派生"可用/受限使用/不可用"摘要,绝不隐藏任一维度。
"""

from __future__ import annotations

from iesplan.metrics.financial import IRRStatus
from iesplan.metrics.validity import (
    FinancialValidity,
    OptimalityValidity,
    PhysicalValidity,
    ReliabilityStatus,
    ValidityLevel,
    financial_validity_from_irr,
    from_db_value,
    summarize_four_dimensions,
)

__all__ = [
    "FinancialValidity",
    "IRRStatus",
    "OptimalityValidity",
    "PhysicalValidity",
    "ReliabilityStatus",
    "ValidityLevel",
    "check_financial",
    "financial_validity_from_irr",
    "from_db_value",
    "summarize_four_dimensions",
]


def check_financial(content: dict) -> tuple[FinancialValidity, dict]:
    """读 content['financial'](evidence 财务块,03 §7.4)计算财务维度(03 §8.2)。

    缺失/非 dict → (insufficient, {'reason': 'missing_financial'});
    irr_status 非法 → 按 insufficient 处理;合法值经 financial_validity_from_irr
    映射(REQ-FIN-005 细分: unique→passed / multiple→restricted / none→failed /
    degenerate→na / out_of_domain→failed / numerical_failure→restricted)。
    """
    fin = content.get("financial") if isinstance(content, dict) else None
    if not isinstance(fin, dict):
        return FinancialValidity.insufficient, {"reason": "missing_financial"}
    raw_status = fin.get("irr_status")
    irr_status: IRRStatus | None = None
    if raw_status is not None:
        try:
            irr_status = IRRStatus(str(raw_status))
        except ValueError:
            irr_status = None
    level = financial_validity_from_irr(irr_status)
    checks = {
        "irr": fin.get("irr"),
        "irr_status": irr_status.value if irr_status is not None else None,
        "irr_message": fin.get("irr_message"),
        "npv": fin.get("npv"),
        "investment": fin.get("investment"),
        "baseline_cost": fin.get("baseline_cost"),
        "cashflows_len": len(fin.get("cashflows") or []),
        "lcoe": fin.get("lcoe"),
        "payback_years": fin.get("payback_years"),
    }
    return level, checks
