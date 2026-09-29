"""无状态度量词汇公开入口(四维有效性状态、IRR 分类与纯换算函数)。

本包子模块(``financial``/``validity``/``engineering``/``environmental``)为
内部实现, 他域不得直接导入; 对外只经本入口复用以下稳定纯类型/函数
(无状态、无事务、无跨域数据, 不复制状态映射):

- 四维状态词汇: ``PhysicalValidity``、``OptimalityValidity``、
  ``FinancialValidity``、``ReliabilityStatus``、``ValidityLevel``;
- IRR 分类与换算: ``IRRStatus``、``financial_validity_from_irr``、
  ``from_db_value``、``summarize_four_dimensions``。
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
    "financial_validity_from_irr",
    "from_db_value",
    "summarize_four_dimensions",
]
