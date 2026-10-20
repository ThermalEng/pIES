"""税额与余额的纯计算关系（B0；见 modules/finance.md 新节）。

公开纯函数＋必要不可变值对象：含税/不含税拆分、单期 VAT 结算、
亏损结转使用/产生/核销、所得税额、直线折旧期间金额。

输入是调用边界已类型化的 ``Decimal`` 金额/比例/期间与不可变余额，
本模块直接消费，不重复校验；不读库/文件/API/当前项目，不设税率/
期限默认值，计算链不按展示精度舍入，不原地修改输入。交易金额保留
正负符号。生成器未来等价约束的数值语义基础与独立对照，不是优化外
的税务任务。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from iesplan.finance.triplet import FinanceTripletError

# ---------------------------------------------------------------------------
# 含税/不含税拆分
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaxSplit:
    """金额拆分结果：net 不含税，tax 税额（保留输入符号）。"""

    net: Decimal
    tax: Decimal


def split_tax_amount(amount: Decimal, rate: Decimal, *, amount_includes_tax: bool) -> TaxSplit:
    """按显式口径拆分：含税 ``tax = amount*rate/(1+rate)``，否则 ``tax = amount*rate``。"""
    if amount_includes_tax:
        tax = amount * rate / (Decimal("1") + rate)
        return TaxSplit(net=amount - tax, tax=tax)
    return TaxSplit(net=amount, tax=amount * rate)


# ---------------------------------------------------------------------------
# 单期 VAT 结算
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VatSettlement:
    """单期结算：paid 实缴，carry_out 期末留抵，refund_applied 本期扣减退税，
    carry_next 结转下期初（= carry_out − refund_applied）。"""

    paid: Decimal
    carry_out: Decimal
    refund_applied: Decimal
    carry_next: Decimal


def settle_vat_period(
    *,
    opening_credit: Decimal,
    output_vat: Decimal,
    input_vat: Decimal,
    refund_claim: Decimal,
) -> VatSettlement:
    """``net = output − input − opening``；``paid = max(net, 0)``，
    ``carry_out = max(−net, 0)``；已声明退税按声明金额扣减，超过本期
    可用留抵为业务不可执行；未声明（claim = 0）不造退款；到账期间由
    调用方另行归集。"""
    net = output_vat - input_vat - opening_credit
    paid = max(net, Decimal("0"))
    carry = max(-net, Decimal("0"))
    if refund_claim > carry:
        raise FinanceTripletError(f"声明退税 {refund_claim} 超过本期可用留抵 {carry}，业务不可执行")
    return VatSettlement(
        paid=paid,
        carry_out=carry,
        refund_applied=refund_claim,
        carry_next=carry - refund_claim,
    )


# ---------------------------------------------------------------------------
# 亏损结转
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LossBalance:
    """一笔可抵扣亏损余额：amount 金额，last_usable_year 最后可用年度（含）。"""

    amount: Decimal
    last_usable_year: int


@dataclass(frozen=True, slots=True)
class LossUseResult:
    """uses/write_offs 与输入 balances 等长逐笔对应；remaining 为存续余额
    （输入顺序）＋本期新生（末尾）；generated 为本期新生亏损额。"""

    uses: tuple[Decimal, ...]
    write_offs: tuple[Decimal, ...]
    used_total: Decimal
    write_off_total: Decimal
    generated: Decimal
    remaining: tuple[LossBalance, ...]


def use_losses(
    *,
    taxable_base: Decimal,
    balances: tuple[LossBalance, ...],
    year: int,
    carryforward_years: int,
) -> LossUseResult:
    """先到期先用：``last_usable_year < year`` 先全额核销；未到期按
    ``(last_usable_year, 输入顺序)`` 抵扣当年正基数。新生亏损
    ``max(−base, 0)``，``last_usable_year = year + carryforward_years``，
    本期不参与抵扣、只进入以后可用余额。"""
    uses = [Decimal("0")] * len(balances)
    write_offs = [Decimal("0")] * len(balances)
    survivors: list[tuple[int, LossBalance]] = []
    for position, item in enumerate(balances):
        if item.last_usable_year < year:
            write_offs[position] = item.amount
        else:
            survivors.append((position, item))
    need = max(taxable_base, Decimal("0"))
    for position, item in sorted(survivors, key=lambda entry: (entry[1].last_usable_year, entry[0])):
        take = min(item.amount, need)
        uses[position] = take
        need -= take
    generated = max(-taxable_base, Decimal("0"))
    remaining: list[LossBalance] = []
    for position, item in enumerate(balances):
        left = item.amount - uses[position] - write_offs[position]
        if left > 0:
            remaining.append(LossBalance(amount=left, last_usable_year=item.last_usable_year))
    if generated > 0:
        remaining.append(LossBalance(amount=generated, last_usable_year=year + carryforward_years))
    return LossUseResult(
        uses=tuple(uses),
        write_offs=tuple(write_offs),
        used_total=sum(uses, Decimal("0")),
        write_off_total=sum(write_offs, Decimal("0")),
        generated=generated,
        remaining=tuple(remaining),
    )


def income_tax_payable(*, taxable_base: Decimal, loss_used: Decimal, rate: Decimal) -> Decimal:
    """所得税额 ``= rate × max(base − used, 0)``。"""
    return rate * max(taxable_base - loss_used, Decimal("0"))


# ---------------------------------------------------------------------------
# 直线折旧
# ---------------------------------------------------------------------------


def straight_line_period_amount(
    *,
    depreciable_base: Decimal,
    residual: Decimal,
    life_years: int,
    period_index: int,
    first_period: int,
) -> Decimal:
    """``(base − residual)/life``，仅 ``first_period ≤ period < first_period+life``
    计提，之外为 0。计税基础由调用方按选择/容量派生后传入。"""
    if first_period <= period_index < first_period + life_years:
        return (depreciable_base - residual) / Decimal(life_years)
    return Decimal("0")
