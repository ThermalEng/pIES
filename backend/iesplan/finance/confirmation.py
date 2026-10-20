"""财务评价确认不可变值对象（公开所有权归 finance）。

``ConstructionPayment`` / ``MaintenanceExpense`` / ``EvaluationInput`` 从
任务域迁移至此，单一定义，不复制第二套 DTO。任务提交边界仍负责 HTTP
解析、领域校验与 TASK-REQ-015 错误映射（见 ``iesplan.tasks``）；application
从任务冻结块经 ``EvaluationInput.from_frozen`` 直接恢复类型化事实（只还
原类型，不重跑提交校验），独立传给生成器与同次解评价。付款比例与维护
费用保持财务时点，不展开成运行步长序列。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ConstructionPayment:
    """单笔建设付款比例(开工起整数年时点 + 十进制比例)。"""

    t: int
    ratio: str  # Decimal 定点字符串; 同一输入内 Σratio 精确为 1


@dataclass(frozen=True, slots=True)
class MaintenanceExpense:
    """单项维护/换新支出(财务发生年 + 十进制金额 + 币种)。"""

    year: int
    amount: str  # Decimal 定点字符串, 非负
    currency: str


@dataclass(frozen=True, slots=True)
class EvaluationInput:
    """计算任务评价确认输入(任务级类型化输入)。

    由任务提交边界 ``parse_evaluation_input`` 从顶层任务输入 ``evaluation``
    块唯一解析, 不从宽泛任务参数字典读取, 也不混入封闭 ``CalculationConfig``
    正文。``evaluation_years`` 为确认的财务评价总年限(含建设期, 年);
    ``construction_period`` 为非负整数建设期(年, 显式 0 合法, 不缺省);
    ``operation_years`` 为正整数投产后运营年限(运行点数/物化/预测/生成
    唯一使用的年限); ``discount_rate`` 为确认的折现率(宪法 §7.3 比例口径
    ``0..1``); ``construction_payments``/``maintenance_expenses`` 为冻结
    的付款比例与维护清单(实际现金流计算由后续切片消费)。
    """

    evaluation_years: int
    construction_period: int
    operation_years: int
    discount_rate: float
    construction_payments: tuple[ConstructionPayment, ...] = ()
    maintenance_expenses: tuple[MaintenanceExpense, ...] = ()

    @classmethod
    def from_frozen(cls, frozen: Mapping[str, Any]) -> EvaluationInput:
        """从任务冻结块直接恢复类型化事实（只还原类型，不重跑提交校验）。

        调用方保证输入为提交边界已校验并冻结的内容；缺件即 KeyError 直接
        可见失败，不设默认值、不回退。此处不复核年限关系、比例合计、
        时点范围等领域规则（提交边界唯一权威）。
        """
        return cls(
            evaluation_years=frozen["evaluation_years"],
            construction_period=frozen["construction_period"],
            operation_years=frozen["operation_years"],
            discount_rate=frozen["discount_rate"],
            construction_payments=tuple(
                ConstructionPayment(t=entry["t"], ratio=entry["ratio"])
                for entry in frozen["construction_payments"]
            ),
            maintenance_expenses=tuple(
                MaintenanceExpense(year=entry["year"], amount=entry["amount"], currency=entry["currency"])
                for entry in frozen["maintenance_expenses"]
            ),
        )
