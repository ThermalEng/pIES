"""B0 税额与余额纯计算关系测试：全部预期独立手算，不调用被测函数生成期待。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from iesplan.finance import (
    LossBalance,
    TaxSplit,
    VatSettlement,
    income_tax_payable,
    settle_vat_period,
    split_tax_amount,
    straight_line_period_amount,
    use_losses,
)
from iesplan.finance.triplet import FinanceTripletError

RATE_13 = Decimal("0.13")
RATE_25 = Decimal("0.25")


# ---------------------------------------------------------------------------
# 含税/不含税拆分
# ---------------------------------------------------------------------------


def test_split_gross_113_at_13gives_net_100_tax_13():
    """113.00×0.13/1.13 = 13.00；net = 100.00。"""
    got = split_tax_amount(Decimal("113.00"), RATE_13, amount_includes_tax=True)
    assert got == TaxSplit(net=Decimal("100.00"), tax=Decimal("13.00"))
    assert got.net + got.tax == Decimal("113.00")


def test_split_net_100_at_13gives_tax_13():
    got = split_tax_amount(Decimal("100.00"), RATE_13, amount_includes_tax=False)
    assert got == TaxSplit(net=Decimal("100.00"), tax=Decimal("13.00"))


def test_split_zero_rate_is_identity():
    got = split_tax_amount(Decimal("100.00"), Decimal("0"), amount_includes_tax=True)
    assert got == TaxSplit(net=Decimal("100.00"), tax=Decimal("0"))


def test_split_keeps_full_precision():
    """10.00 @0.15：tax = 1.5/1.15 不截断，且 net+tax 精确回到 10.00。"""
    got = split_tax_amount(Decimal("10.00"), Decimal("0.15"), amount_includes_tax=True)
    assert got.tax == Decimal("1.5") / Decimal("1.15")
    assert got.net + got.tax == Decimal("10.00")


def test_split_rejects_negative_amount_and_bad_rate():
    with pytest.raises(FinanceTripletError):
        split_tax_amount(Decimal("-1"), RATE_13, amount_includes_tax=True)
    with pytest.raises(FinanceTripletError):
        split_tax_amount(Decimal("100"), Decimal("2"), amount_includes_tax=False)


# ---------------------------------------------------------------------------
# 单期 VAT 结算
# ---------------------------------------------------------------------------


def test_vat_first_period_builds_credit():
    """net = 0 − 6500 − 90000 = −96500 → 实缴 0，留抵 96500。"""
    got = settle_vat_period(
        opening_credit=Decimal("90000"),
        output_vat=Decimal("0"),
        input_vat=Decimal("6500"),
        refund_claim=Decimal("0"),
    )
    assert got == VatSettlement(
        paid=Decimal("0"),
        carry_out=Decimal("96500"),
        refund_applied=Decimal("0"),
        carry_next=Decimal("96500"),
    )


def test_vat_credit_consumed_then_paid():
    """期初 9400，当期 +35100 → net 25700 实缴，留抵清零。"""
    got = settle_vat_period(
        opening_credit=Decimal("9400"),
        output_vat=Decimal("39000"),
        input_vat=Decimal("3900"),
        refund_claim=Decimal("0"),
    )
    assert got.paid == Decimal("25700")
    assert (got.carry_out, got.carry_next) == (Decimal("0"), Decimal("0"))


def test_vat_scheduled_refund_deducts_and_arrival_is_caller_side():
    """留抵 2600，声明退 1500 → 扣 1500 转下期 1100；到账期间调用方另记。"""
    got = settle_vat_period(
        opening_credit=Decimal("2600"),
        output_vat=Decimal("0"),
        input_vat=Decimal("0"),
        refund_claim=Decimal("1500"),
    )
    assert (got.refund_applied, got.carry_next) == (Decimal("1500"), Decimal("1100"))
    assert got.paid == Decimal("0")


def test_vat_claim_capped_by_credit_and_zero_claim_invents_nothing():
    over = settle_vat_period(
        opening_credit=Decimal("2600"),
        output_vat=Decimal("0"),
        input_vat=Decimal("0"),
        refund_claim=Decimal("5000"),
    )
    assert (over.refund_applied, over.carry_next) == (Decimal("2600"), Decimal("0"))
    none = settle_vat_period(
        opening_credit=Decimal("2600"),
        output_vat=Decimal("0"),
        input_vat=Decimal("0"),
        refund_claim=Decimal("0"),
    )
    assert (none.refund_applied, none.carry_next) == (Decimal("0"), Decimal("2600"))


# ---------------------------------------------------------------------------
# 亏损结转：使用 / 顺序 / 到期 / 产生
# ---------------------------------------------------------------------------


def test_loss_review_case_uses_20_and_tax_2_5():
    """复审点名：B=30、余额 20 → 使用 20，税 = 0.25×(30−20) = 2.5。"""
    balances = (LossBalance(amount=Decimal("20"), last_usable_year=2030),)
    got = use_losses(taxable_base=Decimal("30"), balances=balances, year=2026, carryforward_years=5)
    assert got.uses == (Decimal("20"),)
    assert got.used_total == Decimal("20")
    assert got.write_off_total == Decimal("0")
    assert got.generated == Decimal("0")
    assert got.remaining == ()
    assert income_tax_payable(taxable_base=Decimal("30"), loss_used=got.used_total, rate=RATE_25) == Decimal(
        "2.5"
    )
    assert balances == (LossBalance(amount=Decimal("20"), last_usable_year=2030),)


def test_loss_uses_oldest_expiry_first_regardless_of_input_order():
    """输入先年轻后年老，抵扣仍先年老：base 150 → uses (50, 100)。"""
    balances = (
        LossBalance(amount=Decimal("100"), last_usable_year=2030),
        LossBalance(amount=Decimal("100"), last_usable_year=2027),
    )
    got = use_losses(taxable_base=Decimal("150"), balances=balances, year=2026, carryforward_years=5)
    assert got.uses == (Decimal("50"), Decimal("100"))
    assert got.remaining == (LossBalance(amount=Decimal("50"), last_usable_year=2030),)


def test_loss_same_expiry_keeps_input_order():
    balances = (
        LossBalance(amount=Decimal("100"), last_usable_year=2028),
        LossBalance(amount=Decimal("100"), last_usable_year=2028),
    )
    got = use_losses(taxable_base=Decimal("150"), balances=balances, year=2026, carryforward_years=5)
    assert got.uses == (Decimal("100"), Decimal("50"))


def test_loss_expired_balance_written_off_not_used():
    balances = (LossBalance(amount=Decimal("40"), last_usable_year=2025),)
    got = use_losses(taxable_base=Decimal("100"), balances=balances, year=2026, carryforward_years=5)
    assert got.uses == (Decimal("0"),)
    assert got.write_offs == (Decimal("40"),)
    assert got.remaining == ()
    assert income_tax_payable(taxable_base=Decimal("100"), loss_used=got.used_total, rate=RATE_25) == Decimal(
        "25"
    )


def test_loss_generated_from_negative_base_with_explicit_horizon():
    """base −50、Y=2、year=2026 → 新生 50，最后可用 2028，本期不用。"""
    got = use_losses(taxable_base=Decimal("-50"), balances=(), year=2026, carryforward_years=2)
    assert got.generated == Decimal("50")
    assert got.used_total == Decimal("0")
    assert got.remaining == (LossBalance(amount=Decimal("50"), last_usable_year=2028),)
    assert income_tax_payable(taxable_base=Decimal("-50"), loss_used=Decimal("0"), rate=RATE_25) == Decimal(
        "0"
    )


def test_loss_born_used_and_expired_across_years():
    """Y=2：Y1 基 −50 生 50（可用至 Y3）；Y2 基 30 用 30 剩 20；
    Y4 超期核销 20，全额缴 10.00（rate 0.25 × 基 40）。"""
    first = use_losses(taxable_base=Decimal("-50"), balances=(), year=1, carryforward_years=2)
    assert first.remaining == (LossBalance(amount=Decimal("50"), last_usable_year=3),)
    second = use_losses(
        taxable_base=Decimal("30"),
        balances=first.remaining,
        year=2,
        carryforward_years=2,
    )
    assert second.uses == (Decimal("30"),)
    assert second.remaining == (LossBalance(amount=Decimal("20"), last_usable_year=3),)
    fourth = use_losses(
        taxable_base=Decimal("40"),
        balances=second.remaining,
        year=4,
        carryforward_years=2,
    )
    assert fourth.write_offs == (Decimal("20"),)
    assert fourth.remaining == ()
    assert income_tax_payable(
        taxable_base=Decimal("40"), loss_used=fourth.used_total, rate=RATE_25
    ) == Decimal("10")


def test_loss_zero_and_positive_base_without_balances():
    empty = use_losses(taxable_base=Decimal("0"), balances=(), year=2026, carryforward_years=5)
    assert (empty.used_total, empty.generated, empty.remaining) == (Decimal("0"), Decimal("0"), ())
    assert income_tax_payable(taxable_base=Decimal("0"), loss_used=Decimal("0"), rate=RATE_25) == Decimal("0")
    assert income_tax_payable(taxable_base=Decimal("100"), loss_used=Decimal("0"), rate=RATE_25) == Decimal(
        "25"
    )


def test_loss_results_are_immutable_and_inputs_untouched():
    balances = (LossBalance(amount=Decimal("20"), last_usable_year=2030),)
    got = use_losses(taxable_base=Decimal("30"), balances=balances, year=2026, carryforward_years=5)
    with pytest.raises(FrozenInstanceError):
        got.used_total = Decimal("0")  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        balances[0].amount = Decimal("0")  # type: ignore[misc]
    assert balances[0].amount == Decimal("20")


def test_loss_rejects_non_balance_and_zero_carry_years():
    with pytest.raises(FinanceTripletError):
        use_losses(
            taxable_base=Decimal("30"),
            balances=("20",),
            year=2026,  # type: ignore[list-item]
            carryforward_years=5,
        )
    with pytest.raises(FinanceTripletError):
        use_losses(taxable_base=Decimal("-50"), balances=(), year=2026, carryforward_years=0)


# ---------------------------------------------------------------------------
# 直线折旧
# ---------------------------------------------------------------------------


def test_depreciation_annual_and_stops_after_life():
    """(1,000,000 − 0)/20 = 50,000；第 1..20 期计提，第 21 期与第 0 期为 0。"""
    kwargs: dict = {
        "depreciable_base": Decimal("1000000"),
        "residual": Decimal("0"),
        "life_years": 20,
        "first_period": 1,
    }
    assert straight_line_period_amount(period_index=1, **kwargs) == Decimal("50000")
    assert straight_line_period_amount(period_index=20, **kwargs) == Decimal("50000")
    assert straight_line_period_amount(period_index=21, **kwargs) == Decimal("0")
    assert straight_line_period_amount(period_index=0, **kwargs) == Decimal("0")


def test_depreciation_residual_and_life_one():
    """(1,000,000 − 100,000)/10 = 90,000；life=1 时首期全提、次期停。"""
    assert straight_line_period_amount(
        depreciable_base=Decimal("1000000"),
        residual=Decimal("100000"),
        life_years=10,
        period_index=3,
        first_period=1,
    ) == Decimal("90000")
    one = {
        "depreciable_base": Decimal("3200"),
        "residual": Decimal("0"),
        "life_years": 1,
        "first_period": 5,
    }
    assert straight_line_period_amount(period_index=5, **one) == Decimal("3200")
    assert straight_line_period_amount(period_index=6, **one) == Decimal("0")


def test_depreciation_rejects_residual_above_base():
    with pytest.raises(FinanceTripletError):
        straight_line_period_amount(
            depreciable_base=Decimal("100"),
            residual=Decimal("101"),
            life_years=10,
            period_index=1,
            first_period=1,
        )
