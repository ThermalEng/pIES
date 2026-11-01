"""C2 同次解财务事实：声明→主解恢复→封存评价的真实消费者闭环。

只在 Docker 内运行其中真实求解部分（需镜像内 HiGHS 二进制）。
全部金额为合成手算数，不读外部实例。生产不得反解 LP 命名充当
公共输出；本测试读取 outputs.financial_facts 仅为对照恢复精确性。
提交 007 保持，本文件不走提交入口，只走真实生成→求解→适配→封存链。
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_highs_result_adapter import (
    SOLUTION_PATH,
    _receipt,
)
from test_highs_result_adapter import (
    _bundle as _wrap_bundle,
)
from test_income_annual import (
    INC_CARRY_YEARS,
    INC_RATE,
    _equiv_payloads,
    _gen_finance_payload,
    _gen_inputs,
    _gen_pretax_npv,
    _mix_bases,
    _mix_payload,
)
from test_npv_objective import RATE
from test_vat_monthly import (
    _hand_chain,
    _hand_monthly,
    _hand_npv_with_vat,
    _inputs,
    _solve_internal,
)

from iesplan.application.worker.compute_cases import build_financial_evaluation
from iesplan.computation import HIGHS_MILP_PROVIDER
from iesplan.computation.protocols import ComputeResult
from iesplan.computation.result_adapters.highs import (
    ADAPTER_REF,
    HIGHS_ADAPTER,
)
from iesplan.finance.tax import income_tax_payable, use_losses

SOLVER_FILES = ("input/problem.mps", "input/parameters.json")


def _snapshot(*, years=3, construction=1, operation=2, rate=0.08):
    """封存所需的冻结评价映射桩（仅函数消费的四个字段）。"""
    return SimpleNamespace(
        evaluation={
            "construction_period": construction,
            "operation_years": operation,
            "evaluation_years": years,
            "discount_rate": rate,
        }
    )


def _finance_stub():
    return SimpleNamespace(
        currency="CNY",
        price_basis="tax_inclusive",
        base_year=2025,
        profile_id="stub",
    )


def _seal(bundle, solution_raw, finance, snapshot):
    """生产 Adapter（4.0.0）恢复 + 封存评价，返回（结果，封存）。"""
    adapted = HIGHS_ADAPTER.adapt(
        _wrap_bundle({path: bundle.files[path] for path in SOLVER_FILES}),
        _receipt(result_adapter_ref=ADAPTER_REF),
        {SOLUTION_PATH: solution_raw},
    )
    assert adapted.execution_status == "completed"
    assert adapted.math_status == "solved"
    sealed = build_financial_evaluation(snapshot, finance, adapted)
    assert sealed is not None
    facts = adapted.to_dict()["outputs"]["financial_facts"]
    return adapted, sealed, facts


def _amount(value):
    """公开事实金额字符串 → float（测试内比较，不舍入生产值）。"""
    return float(value)


def _assert_facts_close(left, right):
    """两份事实数值近似一致、字面量精确一致（测试内比较，不舍入生产值）。"""
    if isinstance(left, dict):
        assert set(left) == set(right)
        for key in left:
            _assert_facts_close(left[key], right[key])
        return
    if isinstance(left, list):
        assert len(left) == len(right)
        for first, second in zip(left, right, strict=True):
            _assert_facts_close(first, second)
        return
    if isinstance(left, str) and isinstance(right, str):
        try:
            left_num, right_num = float(left), float(right)
        except ValueError:
            assert left == right
        else:
            assert left_num == pytest.approx(right_num, rel=1e-6)
        return
    assert left == right


def test_facts_mix_seal_matches_hand_and_objective(tmp_path) -> None:
    """混合全链：事实恢复对手算，封存 NPV 与求解目标同源一致。"""
    artifact, models, materialized, finance, config, evaluation = _inputs(payload=_mix_payload())
    bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
    assert bundle.manifest.result_adapter == ADAPTER_REF
    out, inp = _hand_monthly(con_ratio=Decimal("0.5"))
    paids, carries = _hand_chain(out, inp)
    value, _, solution_raw = _solve_internal(bundle, tmp_path)
    adapted, sealed, facts = _seal(bundle, solution_raw, finance, _snapshot())
    assert sealed["schema"] == "ies.financial-evaluation"
    assert sealed["schema_version"] == "1.0.0"
    assert sealed["status"] == "evaluated"
    assert facts["currency"] == "CNY"
    # 公开形状：顶层键、tax_cash 嵌套、金额裸字符串、CF 顺序数组。
    assert set(facts) == {
        "currency",
        "construction_total",
        "annual_fixed_om",
        "payments_applied",
        "net_cashflows",
        "annual_operation",
        "monthly_demand",
        "tax_cash",
        "annual_net_flows",
        "construction_units",
    }
    assert facts["construction_units"] == []
    assert set(facts["tax_cash"]) == {"vat", "refunds", "income", "depreciation", "loss_balances"}
    assert isinstance(facts["construction_total"], str)
    assert isinstance(facts["annual_fixed_om"], str)
    assert all(isinstance(value, str) for value in facts["annual_net_flows"])
    assert len(facts["annual_net_flows"]) == 4
    vat_rows = {(row["pool"], row["month"]): row for row in facts["tax_cash"]["vat"]}
    for month in range(1, 37):
        row = vat_rows[("main_pool", month)]
        assert _amount(row["paid"]) == pytest.approx(float(paids[month - 1]), rel=1e-6)
        assert _amount(row["carry"]) == pytest.approx(float(carries[month - 1]), rel=1e-6)
    base_y1, base_y2, base_y3 = _mix_bases()
    year2 = use_losses(taxable_base=base_y2, balances=(), year=2, carryforward_years=INC_CARRY_YEARS)
    year3 = use_losses(
        taxable_base=base_y3,
        balances=year2.remaining,
        year=3,
        carryforward_years=INC_CARRY_YEARS,
    )
    assert year3.uses == (-base_y2,)
    tax = income_tax_payable(taxable_base=base_y3, loss_used=-base_y2, rate=INC_RATE)
    income_rows = {(row["rule"], row["year"]): row for row in facts["tax_cash"]["income"]}
    row_y2 = income_rows[("income_demo", 2)]
    assert _amount(row_y2["taxable_base"]) == pytest.approx(float(base_y2), rel=1e-6)
    assert _amount(row_y2["loss_generated"]) == pytest.approx(float(-base_y2), rel=1e-6)
    row_y3 = income_rows[("income_demo", 3)]
    assert _amount(row_y3["taxable_base"]) == pytest.approx(float(base_y3), rel=1e-6)
    assert _amount(row_y3["loss_used"]) == pytest.approx(float(-base_y2), rel=1e-6)
    assert _amount(row_y3["tax"]) == pytest.approx(float(tax), rel=1e-6)
    assert float(sealed["construction_total"]) == pytest.approx(6000.0, rel=1e-6)
    assert [float(p["amount"]) for p in sealed["payments_applied"]] == [
        pytest.approx(3600.0, rel=1e-6),
        pytest.approx(2400.0, rel=1e-6),
    ]
    assert sealed["annual_operation"][0]["maintenance"] == "1500.5"
    flows = [float(value) for value in sealed["annual_net_flows"]]
    assert len(flows) == 4
    assert flows[0] == pytest.approx(-3600.0, rel=1e-6)
    expected = _hand_npv_with_vat(paids) - tax / Decimal(str(1.0 + RATE)) ** 3
    assert float(sealed["evaluation"]["npv"]) == pytest.approx(float(expected), rel=1e-6)
    published = adapted.outputs["solution"]
    assert published["objective_value"] == pytest.approx(float(expected), rel=1e-6)
    # 封存 tax_cash/CF 为公开事实原样投影（结构与金额不重算、不改写）。
    assert sealed["tax_cash"] == json.loads(json.dumps(facts["tax_cash"]))
    assert sealed["annual_net_flows"] == list(facts["annual_net_flows"])


def test_source_out_sale_uses_only_its_meter_and_preserves_tax_seal(tmp_path) -> None:
    """Out-side sale is a declared transaction, not a sum of receiving inputs.

    A single electrical edge exposes both source.out and consumer.in with
    identical physical flows. Moving the settlement binding to source.out
    must preserve VAT, income tax, cash flow and the frozen evaluation, while
    the component declaration must name only the requested source interface.
    """
    reference = None
    for at_source in (False, True):
        artifact, models, materialized, finance, config, evaluation = _inputs(
            payload=_mix_payload(), sale_at_source=at_source
        )
        bundle = HIGHS_MILP_PROVIDER.generate(
            artifact, models, materialized, finance, config, evaluation
        )
        metadata = json.loads(bundle.files["input/parameters.json"])
        terms = metadata["components"]["export_meter__period_energy_sale"]["terms"]
        port = "grid1__supply" if at_source else "heater1__electricity_in"
        assert len(terms) == 17520
        assert all(key.startswith(f"{port}__t") for key in terms)
        assert terms[f"{port}__t0"] == pytest.approx(0.00005)
        _, _, raw_solution = _solve_internal(bundle, tmp_path)
        adapted, sealed, facts = _seal(bundle, raw_solution, finance, _snapshot())
        assert sealed["tax_cash"] == facts["tax_cash"]
        assert sealed["annual_net_flows"] == facts["annual_net_flows"]
        assert float(sealed["annual_operation"][0]["energy_net"]) != 0
        assert adapted.outputs["solution"]["objective_value"] == pytest.approx(
            float(sealed["evaluation"]["npv"]), rel=1e-6
        )
        if reference is None:
            reference = sealed
        else:
            for part in ("annual_operation", "annual_net_flows", "tax_cash"):
                _assert_facts_close(sealed[part], reference[part])
            assert float(sealed["evaluation"]["npv"]) == pytest.approx(
                float(reference["evaluation"]["npv"]), rel=1e-6
            )


def test_facts_gross_net_equivalence(tmp_path) -> None:
    """等价含税/净额陈述：两解封存事实与 NPV 互相一致。"""
    incl_payload, excl_payload, export_values = _equiv_payloads()
    base_y1, base_y2, base_y3 = _mix_bases(energy_credit=Decimal("0.5"))
    year2 = use_losses(taxable_base=base_y2, balances=(), year=2, carryforward_years=INC_CARRY_YEARS)
    year3 = use_losses(
        taxable_base=base_y3,
        balances=year2.remaining,
        year=3,
        carryforward_years=INC_CARRY_YEARS,
    )
    assert year3.uses == (-base_y2,)
    tax = income_tax_payable(taxable_base=base_y3, loss_used=-base_y2, rate=INC_RATE)
    out, inp = _hand_monthly(con_ratio=Decimal("0.5"), energy_ratio=Decimal("0.5"))
    paids, _ = _hand_chain(out, inp)
    expected = _hand_npv_with_vat(paids) - tax / Decimal(str(1.0 + RATE)) ** 3
    sealed_by_side = []
    for payload, values in ((incl_payload, None), (excl_payload, export_values)):
        artifact, models, materialized, finance, config, evaluation = _inputs(
            payload=payload, export_values=values
        )
        bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
        _, _, solution_raw = _solve_internal(bundle, tmp_path)
        _, sealed, _ = _seal(bundle, solution_raw, finance, _snapshot())
        sealed_by_side.append(sealed)
    first, second = sealed_by_side
    assert float(first["evaluation"]["npv"]) == pytest.approx(float(expected), rel=1e-6)
    assert float(second["evaluation"]["npv"]) == pytest.approx(float(expected), rel=1e-6)
    _assert_facts_close(first["annual_net_flows"], second["annual_net_flows"])
    _assert_facts_close(first["tax_cash"], second["tax_cash"])


def test_facts_two_pools_counted_once(tmp_path) -> None:
    """多池各计一次：经营池与建设池分离，建设抵扣仍进所得税。"""
    payload = _mix_payload()
    payload["credit_pools"]["pool2"] = {
        "opening_credit": {"value": "0", "unit": "CNY"},
        "receivable_policy": "end_receivable",
        "refunds": [],
    }
    payload["tax_rules"]["con_vat"]["credit_pool"] = "pool2"
    artifact, models, materialized, finance, config, evaluation = _inputs(payload=payload)
    bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
    out, inp = _hand_monthly(con_ratio=Decimal("0"))
    paids1, carries1 = _hand_chain(out, inp)
    con_total = Decimal("6000") * (Decimal("0.13") / Decimal("1.13")) * Decimal("0.5")
    carry2 = [Decimal("0")] * 36
    running = Decimal("0")
    for month in range(1, 37):
        if month == 1:
            running += Decimal("0.6") * con_total
        if month == 13:
            running += Decimal("0.4") * con_total
        carry2[month - 1] = running
    _, _, solution_raw = _solve_internal(bundle, tmp_path)
    adapted, sealed, facts = _seal(bundle, solution_raw, finance, _snapshot())
    by_pool: dict[str, list] = {}
    for row in facts["tax_cash"]["vat"]:
        by_pool.setdefault(row["pool"], []).append(row)
    assert sorted(by_pool) == ["main_pool", "pool2"]
    for month in range(1, 37):
        main = by_pool["main_pool"][month - 1]
        assert main["month"] == month
        assert _amount(main["paid"]) == pytest.approx(float(paids1[month - 1]), rel=1e-6)
        assert _amount(main["carry"]) == pytest.approx(float(carries1[month - 1]), rel=1e-6)
        second = by_pool["pool2"][month - 1]
        assert _amount(second["paid"]) == pytest.approx(0.0, abs=1e-6)
        assert _amount(second["carry"]) == pytest.approx(float(carry2[month - 1]), rel=1e-6)
    base_y1, base_y2, base_y3 = _mix_bases()
    income_rows = {(row["rule"], row["year"]): row for row in facts["tax_cash"]["income"]}
    assert _amount(income_rows[("income_demo", 3)]["taxable_base"]) == pytest.approx(float(base_y3), rel=1e-6)
    assert sealed["schema"] == "ies.financial-evaluation"


def test_facts_post_period_refund_retained_not_cash(tmp_path) -> None:
    """跨年与期后退款：确认/到账期间保留，期后到账不进期内现金。"""
    refunds = (
        {
            "period": 20,
            "amount": {"value": "1500", "unit": "CNY"},
            "arrival_period": 22,
        },
        {
            "period": 25,
            "amount": {"value": "2000", "unit": "CNY"},
            "arrival_period": 40,
        },
    )
    artifact, models, materialized, finance, config, evaluation = _inputs(refunds=refunds)
    bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
    out, inp = _hand_monthly()
    paids, carries = _hand_chain(out, inp, claims={20: Decimal("1500"), 25: Decimal("2000")})
    value, solved, solution_raw = _solve_internal(bundle, tmp_path)
    adapted, sealed, facts = _seal(bundle, solution_raw, finance, _snapshot())
    assert [(row["pool"], row["period"], row["arrival_period"]) for row in facts["tax_cash"]["refunds"]] == [
        ("main_pool", 20, 22),
        ("main_pool", 25, 40),
    ]
    assert _amount(facts["tax_cash"]["refunds"][0]["amount"]) == pytest.approx(1500.0, rel=1e-9)
    assert _amount(facts["tax_cash"]["refunds"][1]["amount"]) == pytest.approx(2000.0, rel=1e-9)
    # 发布 carry 为扣当月确认退款后的期末可结转余额（与下月 previous 同源）。
    vat_rows = {(row["pool"], row["month"]): row for row in facts["tax_cash"]["vat"]}
    before_m20 = solved["vat__main_pool__carry__m20"]
    assert _amount(vat_rows[("main_pool", 20)]["carry"]) == pytest.approx(
        before_m20 - 1500.0, rel=1e-6
    )
    assert _amount(vat_rows[("main_pool", 20)]["carry"]) == pytest.approx(
        float(carries[19]) - 1500.0, rel=1e-6
    )
    assert _amount(vat_rows[("main_pool", 21)]["carry"]) == pytest.approx(
        solved["vat__main_pool__carry__m21"], rel=1e-6
    )
    # 次月手算余额已以前月扣claim值前向传播，与发布值同源一致。
    assert _amount(vat_rows[("main_pool", 21)]["carry"]) == pytest.approx(
        float(carries[20]), rel=1e-6
    )
    before_m25 = solved["vat__main_pool__carry__m25"]
    assert _amount(vat_rows[("main_pool", 25)]["carry"]) == pytest.approx(
        before_m25 - 2000.0, rel=1e-6
    )
    assert _amount(vat_rows[("main_pool", 25)]["carry"]) == pytest.approx(
        float(carries[24]) - 2000.0, rel=1e-6
    )
    assert _amount(vat_rows[("main_pool", 26)]["carry"]) == pytest.approx(
        float(carries[25]), rel=1e-6
    )
    expected = _hand_npv_with_vat(paids, arrivals={22: Decimal("1500")})
    assert value == pytest.approx(float(expected), rel=1e-6)
    assert float(sealed["evaluation"]["npv"]) == pytest.approx(float(expected), rel=1e-6)
    assert sealed["tax_cash"]["income"] == []


def test_facts_empty_rules_full_cashflow(tmp_path) -> None:
    """空税则：事实数组为空是真实不适用，完整现金流仍封存。"""
    payload = _gen_finance_payload()
    payload["tax_rules"] = {}
    payload["credit_pools"] = {}
    inputs = _gen_inputs(
        {"sense": "maximize", "expression": "system.after_tax_npv"},
        cap_bounds=("10", "10"),
        payload=payload,
        price_y3=Decimal("2.0"),
    )
    artifact, models, materialized, finance, config, evaluation = inputs
    bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
    value, solved, solution_raw = _solve_internal(bundle, tmp_path, check_soc_pin=False)
    assert solved["gen_cap"] == pytest.approx(10000.0, rel=1e-6)
    adapted, sealed, facts = _seal(bundle, solution_raw, finance, _snapshot())
    assert facts["tax_cash"] == {
        "vat": [],
        "refunds": [],
        "income": [],
        "depreciation": [],
        "loss_balances": [],
    }
    assert sealed["tax_cash"] == {
        "vat": [],
        "refunds": [],
        "income": [],
        "depreciation": [],
        "loss_balances": [],
    }
    expected_cf = [
        Decimal("-10000"),
        Decimal("0"),
        Decimal("-100"),
        Decimal("2.0") * Decimal("8760") * Decimal("10") - Decimal("100"),
    ]
    assert [float(value) for value in sealed["annual_net_flows"]] == [
        pytest.approx(float(v), rel=1e-6) for v in expected_cf
    ]
    assert value == pytest.approx(float(_gen_pretax_npv(Decimal("10"), price_y3=Decimal("2.0"))), rel=1e-6)
    assert float(sealed["evaluation"]["npv"]) == pytest.approx(
        float(_gen_pretax_npv(Decimal("10"), price_y3=Decimal("2.0"))), rel=1e-6
    )


def test_facts_absence_keeps_old_semantics() -> None:
    """无解/metrics-only：封存返回 None，不补伪财务块（无需求解）。"""
    unsolved = ComputeResult(
        bundle_id="bundle-x",
        receipt_id="receipt-x",
        execution_status="completed",
        math_status="infeasible",
        outputs={},
    )
    assert build_financial_evaluation(_snapshot(), _finance_stub(), unsolved) is None
    metrics_only = ComputeResult(
        bundle_id="bundle-x",
        receipt_id="receipt-x",
        execution_status="completed",
        math_status="solved",
        outputs={"metrics": {"model_status": "HighsModelStatus.kOptimal"}},
    )
    assert build_financial_evaluation(_snapshot(), _finance_stub(), metrics_only) is None


def test_facts_non_npv_objective(tmp_path) -> None:
    """非 NPV 目标也有正确事实：约束精确性不依赖目标（此处不核对目标值）。"""
    ordinary = {
        "sense": "minimize",
        "expression": (
            "import_meter__period_energy_purchase"
            " + export_meter__period_energy_sale"
            " + 1 year * tank1__annual_fixed_om"
            " + tank1__period_variable_om"
            " + heater1__period_variable_om"
        ),
    }
    artifact, models, materialized, finance, config, evaluation = _inputs(
        objective=ordinary, payload=_mix_payload()
    )
    bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
    _, _, solution_raw = _solve_internal(bundle, tmp_path)
    adapted, sealed, facts = _seal(bundle, solution_raw, finance, _snapshot())
    base_y1, base_y2, base_y3 = _mix_bases()
    income_rows = {(row["rule"], row["year"]): row for row in facts["tax_cash"]["income"]}
    assert _amount(income_rows[("income_demo", 2)]["taxable_base"]) == pytest.approx(float(base_y2), rel=1e-6)
    assert _amount(income_rows[("income_demo", 3)]["taxable_base"]) == pytest.approx(float(base_y3), rel=1e-6)
    assert sealed["schema"] == "ies.financial-evaluation"
