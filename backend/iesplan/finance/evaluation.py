"""项目投资口径纯财务评价切片(0.10-D)。

范围(Roadmap 1.0.0 基本财务输出 + 0.10.0 第 3 项,最小切片):
固定年份净现金流(flows[0] 为 t=0 投资,flows[1..N] 为第 1..N 年净流,金额
Decimal)与用户显式折现率 → 逐年明细行(原始净流/贴现流/两种累计)与
NPV、IRR、静态/动态回收期。指标与明细同源:全部由同一行数据复算。

口径:
- 贴现流 d[t] = CF[t] / (1+r)^t,全程 Decimal 精确计算;
- NPV = Σd[t](即末年贴现累计);
- 动态回收期 = 对贴现流套用与静态回收期同一规则
  (hourly.compute_payback:累计首次非负、年内线性插值);r=0 时贴现流
  与原始流完全一致,动态与静态回收期相等;
- 未回收 → 值为 None 并附明确状态,不伪造数值。

IRR 状态名以生效的 modules/finance.md 公开语义为准
(normal/no_solution/multiple/degenerate/out_of_domain/numeric_failure);
数学求根复用 metrics.cashflow_irr,仅在本模块边界映射状态名,现有
metrics/metrics 包内的 IRRStatus 枚举一律不动(命名漂移交 Codex 裁决)。

本模块为纯计算,不读取项目、不读取最新财务配置、不做内部文件校验。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from iesplan.finance.hourly import compute_payback
from iesplan.finance.metrics import IRRStatus, cashflow_irr

EVALUATION_SCHEMA: Final[str] = "ies.project-evaluation"
EVALUATION_VERSION: Final[str] = "1.0.0"


class IRREvaluationStatus(StrEnum):
    """IRR 评价状态(modules/finance.md 公开语义名)。

    - normal:         唯一正实根,标准符号型现金流
    - no_solution:    无解——现金流符号无变化(全部同号)
    - multiple:       多解——符号变化 > 1 次,取最小正根为保守值
    - degenerate:     退化——现金流全零或常数(无时间价值差异)
    - out_of_domain:  超出定义域——存在符号变化但 r >= 0 上无正实根
    - numeric_failure: 数值失败——输入非法(空/非有限)或迭代不收敛
    """

    normal = "normal"
    no_solution = "no_solution"
    multiple = "multiple"
    degenerate = "degenerate"
    out_of_domain = "out_of_domain"
    numeric_failure = "numeric_failure"


class PaybackStatus(StrEnum):
    """回收期状态:recovered(已回收,值为年数) / not_recovered(未回收,值为 None)。"""

    recovered = "recovered"
    not_recovered = "not_recovered"


_IRR_STATUS_MAP: Final[dict[IRRStatus, IRREvaluationStatus]] = {
    IRRStatus.unique: IRREvaluationStatus.normal,
    IRRStatus.none: IRREvaluationStatus.no_solution,
    IRRStatus.multiple: IRREvaluationStatus.multiple,
    IRRStatus.degenerate: IRREvaluationStatus.degenerate,
    IRRStatus.out_of_domain: IRREvaluationStatus.out_of_domain,
    IRRStatus.numerical_failure: IRREvaluationStatus.numeric_failure,
}


@dataclass(frozen=True, slots=True)
class EvaluationYearRow:
    """逐年现金流明细行(金额 Decimal,币种见评价 contract.currency)。"""

    year: int
    net: Decimal
    discounted: Decimal
    cumulative: Decimal
    cumulative_discounted: Decimal


@dataclass(frozen=True, slots=True)
class ProjectEvaluation:
    """项目投资口径评价结果(不可变,版本化 contract)。

    属性:
        currency: 币种(金额单位,调用方自冻结快照传入,此处只回显)。
        price_basis/base_year: 金额口径与基年标签(来源回显)。
        discount_rate: 用户显式折现率(无量纲比例)。
        evaluation_years: 评价年限(年,== 明细行数 - 1)。
        rows: 逐年明细行(year 0..N)。
        npv: 净现值(== 末年贴现累计,金额)。
        irr: 内部收益率(无量纲比例,无解时为 None)。
        irr_status: IRR 评价状态(IRREvaluationStatus)。
        static_payback/dynamic_payback: 静态/动态回收期(年,未回收为 None)。
        static_status/dynamic_status: 回收期状态(PaybackStatus)。
    """

    schema: str = EVALUATION_SCHEMA
    schema_version: str = EVALUATION_VERSION
    currency: str = ""
    price_basis: str = ""
    base_year: int = 0
    discount_rate: Decimal = Decimal("0")
    evaluation_years: int = 0
    rows: tuple[EvaluationYearRow, ...] = ()
    npv: Decimal = Decimal("0")
    irr: float | None = None
    irr_status: IRREvaluationStatus = IRREvaluationStatus.numeric_failure
    static_payback: float | None = None
    static_status: PaybackStatus = PaybackStatus.not_recovered
    dynamic_payback: float | None = None
    dynamic_status: PaybackStatus = PaybackStatus.not_recovered

    def __post_init__(self) -> None:
        if self.schema != EVALUATION_SCHEMA:
            raise ValueError(f"评价 schema 非法: {self.schema!r}")
        if self.schema_version != EVALUATION_VERSION:
            raise ValueError(f"评价版本非法: {self.schema_version!r}")
        object.__setattr__(self, "rows", tuple(self.rows))

    def to_dict(self) -> dict:
        """序列化为 JSON 兼容独立副本(Decimal 转字符串,不舍入)。"""
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "currency": self.currency,
            "price_basis": self.price_basis,
            "base_year": self.base_year,
            "discount_rate": str(self.discount_rate),
            "evaluation_years": self.evaluation_years,
            "rows": [
                {
                    "year": r.year,
                    "net": str(r.net),
                    "discounted": str(r.discounted),
                    "cumulative": str(r.cumulative),
                    "cumulative_discounted": str(r.cumulative_discounted),
                }
                for r in self.rows
            ],
            "npv": str(self.npv),
            "npv_unit": self.currency,
            "irr": self.irr,
            "irr_status": str(self.irr_status),
            "static_payback": self.static_payback,
            "static_payback_unit": "year" if self.static_payback is not None else None,
            "static_status": str(self.static_status),
            "dynamic_payback": self.dynamic_payback,
            "dynamic_payback_unit": "year" if self.dynamic_payback is not None else None,
            "dynamic_status": str(self.dynamic_status),
        }


def _as_decimal(value: Decimal | float | int) -> Decimal:
    """统一转 Decimal(float 经字符串转换避免二进制误差);非有限值报错。"""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    if not d.is_finite():
        raise ValueError(f"现金流/折现率含非有限值: {value!r}")
    return d


def evaluate_project_investment(
    annual_net_flows: list[Decimal | float | int] | tuple[Decimal | float | int, ...],
    discount_rate: Decimal | float | int,
    *,
    currency: str,
    price_basis: str,
    base_year: int,
) -> ProjectEvaluation:
    """项目投资口径纯评价:固定年份净现金流 + 用户折现率 → 逐年明细与指标。

    参数:
        annual_net_flows: 年净现金流,[0] 为 t=0 投资(通常为负),[1..N]
            为第 1..N 年净流;至少含 t=0 与 1 个评价年;禁止把单年结果
            复制多年,调用方必须传入真实固定年份序列。
        discount_rate: 用户显式折现率(无量纲比例,>= 0);r=0 合法。
        currency/price_basis/base_year: 冻结来源的币种、口径与基年标签
            (只回显,不做第二权威读取)。

    返回:
        ProjectEvaluation;输入缺失/非法时大声失败(ValueError),不返回
        默认现金流或伪指标。
    """
    if not annual_net_flows or len(annual_net_flows) < 2:
        raise ValueError("annual_net_flows 至少含 t=0 投资与 1 个评价年")
    if not currency or not price_basis:
        raise ValueError("currency 与 price_basis 不能为空")
    if not isinstance(base_year, int) or isinstance(base_year, bool):
        raise ValueError(f"base_year 须为整数,实际 {base_year!r}")
    rate = _as_decimal(discount_rate)
    if rate < 0:
        raise ValueError(f"discount_rate 须 >= 0,实际 {rate}")
    flows = [_as_decimal(v) for v in annual_net_flows]

    base = Decimal("1") + rate
    denom = Decimal("1")
    rows: list[EvaluationYearRow] = []
    cum = Decimal("0")
    cum_disc = Decimal("0")
    discounted: list[Decimal] = []
    for t, f in enumerate(flows):
        d = f / denom
        discounted.append(d)
        cum += f
        cum_disc += d
        rows.append(
            EvaluationYearRow(
                year=t, net=f, discounted=d,
                cumulative=cum, cumulative_discounted=cum_disc,
            )
        )
        denom *= base

    irr_value, irr_raw_status, _message = cashflow_irr(flows)
    static = compute_payback(flows)
    dynamic = compute_payback(discounted)

    return ProjectEvaluation(
        currency=currency,
        price_basis=price_basis,
        base_year=base_year,
        discount_rate=rate,
        evaluation_years=len(flows) - 1,
        rows=tuple(rows),
        npv=cum_disc,
        irr=irr_value,
        irr_status=_IRR_STATUS_MAP[irr_raw_status],
        static_payback=static,
        static_status=(
            PaybackStatus.recovered if static is not None else PaybackStatus.not_recovered
        ),
        dynamic_payback=dynamic,
        dynamic_status=(
            PaybackStatus.recovered if dynamic is not None else PaybackStatus.not_recovered
        ),
    )


__all__ = [
    "EVALUATION_SCHEMA",
    "EVALUATION_VERSION",
    "EvaluationYearRow",
    "IRREvaluationStatus",
    "PaybackStatus",
    "ProjectEvaluation",
    "evaluate_project_investment",
]
