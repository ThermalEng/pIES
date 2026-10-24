"""C1d 所得税年度计税/折旧/亏损结转同次建模：B0 语义等价约束与手算对照。

只在 Docker 内运行其中真实求解部分（需镜像内 HiGHS 二进制）。
全部金额为合成手算数，不读外部实例。生产不得反解 LP 命名充当
公共输出；本测试读取内部解仅为对照约束精确性（tax_states 发布归 C2）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from test_npv_objective import GEN_YAML, RATE, _contribution
from test_vat_monthly import (
    ELEC_KW,
    GROSS_FACTOR,
    HOURS_2026,
    HOURS_2027,
    _finance_payload,
    _hand_chain,
    _hand_monthly,
    _hand_npv_with_vat,
    _inputs,
    _price_values,
    _solve_internal,
)

from iesplan.assembly import validate_assembly_doc
from iesplan.computation import (
    HIGHS_MILP_PROVIDER,
    CalculationConfig,
    MaterializedPriceSeries,
    MaterializedSet,
    materialize_non_predictive_series,
)
from iesplan.computation.generators.highs_milp import GENERATOR_REF
from iesplan.core.yamlmini import load as yaml_load
from iesplan.devices import init_registry
from iesplan.devices.parser2 import parse_device_model_v2
from iesplan.finance import (
    ConstructionPayment,
    EvaluationInput,
    FinanceProfile,
    merge_effective,
)
from iesplan.finance.tax import LossBalance, income_tax_payable, use_losses

NPV_MARKER = "system.after_tax_npv"

#: 所得税关系需要计量步有限界（与 VAT heater 的 maximum: 100 同理）；
#: C1b 共享 GEN_YAML 的 meter_in 为 null，此处派生有限界副本，不动共享夹具。
INCOME_GEN_YAML = GEN_YAML.replace(
    "maximum: null}",
    "maximum: 100}",
)

#: 合成口径：K=1、N=2、E=3、r=0.08、所得税率 0.25；gen 选择容量 0..10kW，
#: 建设 1000/kW（t0 全付）、固定 OM 10/kW/a、倒送价第 3 年 0.17；
#: 折旧 10 年直线、残值 0、运营年第 1 年起计。
GEN_PRICE_Y3 = Decimal("0.17")
GEN_CAP_MAX = Decimal("10")
GEN_UPFRONT = Decimal("1000")
GEN_FIXED = Decimal("10")
GEN_HOURS = Decimal("8760")
INC_RATE = Decimal("0.25")
INC_CARRY_YEARS = 5
DEPR_LIFE = 10


def _gen_finance_payload(*, opening=(), carry_years=INC_CARRY_YEARS):
    return {
        "schema": "ies.finance-profile",
        "schema_version": "4.0.0",
        "profile": {
            "id": "income-gen",
            "region": "CN-North",
            "currency": "CNY",
            "base_year": 2025,
            "price_basis": "tax_inclusive",
            "cost_method": "fixed_plus_linear",
        },
        "finance_types": {
            "gen_choice": {
                "upfront_capex": {"linear": {"cap": {"unit_cost": {"value": "1000", "unit": "CNY/kW"}}}},
                "annual_fixed_om": {"linear": {"cap": {"unit_cost": {"value": "10", "unit": "CNY/kW/a"}}}},
            },
        },
        "energy_prices": {
            "gen_sale_ii": {
                "carrier": "electricity",
                "direction": "sale",
                "kind": "time_series",
                "ref": "e" * 64,
                "series_meta": {
                    "resolution": "1h",
                    "point_count": 17520,
                    "unit": "CNY/kWh",
                },
            },
        },
        "demand_charges": {},
        "taxes": {
            "income_ii": {
                "display_name": "合成所得税",
                "tax_type": "income_tax",
                "applies_to": "gen_sale_ii",
            },
        },
        "tax_rules": {
            "income_gen": {
                "kind": "income_tax",
                "label_ref": "income_ii",
                "rate": {"value": "0.25", "unit": "1"},
                "revenue": ["gen_sale_ii"],
                "energy_cost": [],
                "annual_om": ["gen_choice.annual_fixed_om"],
                "variable_om": [],
                "deductible_maintenance": {"mode": "explicit_items", "items": []},
                "depreciation": [
                    {
                        "asset": "gen_choice",
                        "base_from": "gen_choice.upfront_capex_net",
                        "method": "straight_line",
                        "life_years": DEPR_LIFE,
                        "residual": {"value": "0", "unit": "CNY"},
                        "first_year": 1,
                    },
                ],
                "loss_carryforward_years": carry_years,
                "opening_losses": [
                    {"amount": {"value": str(amount), "unit": "CNY"}, "last_usable_year": last}
                    for amount, last in opening
                ],
            },
        },
        "credit_pools": {},
    }


def _gen_doc(objective: dict, *, cap_bounds: tuple[str, str] = ("0", "10")) -> dict:
    return {
        "schema": "ies.assembly",
        "schema_version": "4.0.0",
        "assembly": {"id": "income-gen", "name": "所得税选择算例"},
        "project_baseline": {
            "resolution": "1h",
            "start_year": 2026,
            "scenario_mode": "single",
        },
        "devices": {
            "gen1": {
                "definition": {"id": "acme.device.gen"},
                "asset_origin": "new",
                "properties": {},
                "predefined_interfaces": {},
            },
        },
        "connections": {
            "link1": {"from": "gen1.power_out", "to": "gen1.meter_in"},
        },
        "finance": {
            "ref": {"kind": "object", "object_id": "obj-finance", "media_type": "application/yaml"},
            "profile_id": "income-gen",
        },
        "finance_binding": {
            "instances": {
                "gen1": {
                    "type": "costed",
                    "finance_type": "gen_choice",
                    "drivers": {"cap": {"property": "cap_kw"}},
                },
            }
        },
        "tariff_bindings": {
            "meter": {
                "price": "gen_sale_ii",
                "instance": "gen1",
                "interface": "meter_in",
                "aggregation": "integrate_positive",
            },
        },
        "demand_bindings": {},
        "planning": {
            "objective": objective,
            "variables": {
                "gen_cap": {
                    "device_ref": "gen1",
                    "parameter": "cap_kw",
                    "lower_bound": cap_bounds[0],
                    "upper_bound": cap_bounds[1],
                    "unit": "kW",
                },
            },
            "constraints": {},
        },
        "outputs": {"series": [], "metrics": []},
        "extensions": {},
    }


def _gen_inputs(
    objective: dict,
    *,
    cap_bounds: tuple[str, str] = ("0", "10"),
    payload: dict | None = None,
    price_y3: Decimal = GEN_PRICE_Y3,
):
    doc = _gen_doc(objective, cap_bounds=cap_bounds)
    models = {"gen1": _contribution(INCOME_GEN_YAML)}
    finance = merge_effective(
        FinanceProfile.from_dict(payload if payload is not None else _gen_finance_payload()),
        None,
    )
    init_registry()
    parsed = parse_device_model_v2(yaml_load(INCOME_GEN_YAML), file="test.yaml")
    assert parsed.ok, parsed.diagnostics
    result = validate_assembly_doc(
        doc,
        finance_snapshots={"obj-finance": finance},
        device_documents={"acme.device.gen": parsed.document},
    )
    assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]
    artifact = result.artifact
    materialized = materialize_non_predictive_series(artifact, {}, operation_years=2)
    prices = (
        MaterializedPriceSeries(
            price_id="gen_sale_ii",
            point_count=17520,
            resolution="1h",
            unit="CNY/kWh",
            values=(0.0,) * 8760 + (float(price_y3),) * 8760,
            source={"kind": "price_series", "object_id": "e" * 64},
        ),
    )
    materialized = MaterializedSet(
        operation_years=materialized.operation_years,
        series=materialized.series,
        pending_predict=materialized.pending_predict,
        prices=prices,
    )
    config = CalculationConfig(
        mode="planning",
        generator=GENERATOR_REF,
        solver="ies.solver.highs@1.7.2",
        precision="medium",
        time_limit_seconds=600,
        outputs=["solution"],
    )
    evaluation = EvaluationInput(
        evaluation_years=3,
        construction_period=1,
        operation_years=2,
        discount_rate=RATE,
        construction_payments=(ConstructionPayment(t=0, ratio="1.0"),),
        maintenance_expenses=(),
    )
    return artifact, models, materialized, finance, config, evaluation


def _gen_bases(cap_kw: Decimal, *, price_y3: Decimal = GEN_PRICE_Y3) -> tuple[Decimal, ...]:
    """评价年 1..3 应税基础手算（容量 kW 为 Decimal，公式独立书写）。"""
    revenue_y3 = price_y3 * GEN_HOURS * cap_kw
    capex = GEN_UPFRONT * cap_kw
    fixed = GEN_FIXED * cap_kw
    depr = capex / Decimal(DEPR_LIFE)
    base_y1 = Decimal("0")
    base_y2 = -fixed - depr
    base_y3 = revenue_y3 - fixed - depr
    return base_y1, base_y2, base_y3


def _gen_pretax_npv(cap_kw: Decimal, *, price_y3: Decimal = GEN_PRICE_Y3) -> Decimal:
    """税前 NPV 手算（无税现金流折现，与模型 gross 口径一致）。"""
    capex = GEN_UPFRONT * cap_kw
    fixed = GEN_FIXED * cap_kw
    revenue_y3 = price_y3 * GEN_HOURS * cap_kw
    total = -capex
    total -= fixed / Decimal(str(1.0 + RATE)) ** 2
    total += (revenue_y3 - fixed) / Decimal(str(1.0 + RATE)) ** 3
    return total


def _balances(opening: tuple) -> tuple[LossBalance, ...]:
    return tuple(LossBalance(amount=Decimal(str(amount)), last_usable_year=last) for amount, last in opening)


def _gen_solve(inputs, tmp_path):
    """真实求解，返回（目标值 + 偏移，列名 → 取值）。"""
    bundle = HIGHS_MILP_PROVIDER.generate(*inputs)
    return _solve_internal(bundle, tmp_path, check_soc_pin=False)[:2]


def test_income_reversal_selects_post_tax_optimum(tmp_path) -> None:
    """税后求解选正确配置：税前建满最优，税后空建最优。

    每 kW 税前边际 +165.78（建满），税后边际 −86.13（折旧与亏损抵扣后
    仍为负）。求解器在税后目标下必须选 0，而不是税前最优的邻域。
    """
    inputs = _gen_inputs({"sense": "maximize", "expression": NPV_MARKER})
    value, solved = _gen_solve(inputs, tmp_path)
    assert solved["gen_cap"] == pytest.approx(0.0, abs=1e-6)
    assert value == pytest.approx(0.0, abs=1e-6)
    pre_full = _gen_pretax_npv(GEN_CAP_MAX)
    assert pre_full > Decimal("0")
    post_full = (
        pre_full
        - INC_RATE
        * (_gen_bases(GEN_CAP_MAX)[2] - Decimal("110") * GEN_CAP_MAX)
        / Decimal(str(1.0 + RATE)) ** 3
    )
    assert post_full < Decimal("0")


def test_income_reversal_pretax_prefers_build(tmp_path) -> None:
    """税前（空税则）求解建满：同一模型的税前最优与税后最优排序反转。"""
    payload = _gen_finance_payload()
    payload["tax_rules"] = {}
    payload["credit_pools"] = {}
    inputs = _gen_inputs({"sense": "maximize", "expression": NPV_MARKER}, payload=payload)
    value, solved = _gen_solve(inputs, tmp_path)
    assert solved["gen_cap"] == pytest.approx(10000.0, rel=1e-6)
    assert value == pytest.approx(float(_gen_pretax_npv(GEN_CAP_MAX)), rel=1e-6)


def test_income_loss_expiry_and_order(tmp_path) -> None:
    """期初亏损使用与到期 + 先到期先用：B0 语义逐年对照。

    开口 ((500, last2), (800, last7))：第 2 年无所得不用；第 3 年先用
    800（先到期），再用当年新生 110；500 过期核销（若可用税额应为 0，
    实际 2973，差值即核销证明）。
    """
    opening = ((500, 2), (800, 7))
    payload = _gen_finance_payload(opening=opening)
    inputs = _gen_inputs(
        {"sense": "maximize", "expression": NPV_MARKER},
        cap_bounds=("10", "10"),
        payload=payload,
    )
    value, solved = _gen_solve(inputs, tmp_path)
    base_y1, base_y2, base_y3 = _gen_bases(GEN_CAP_MAX)
    year2 = use_losses(
        taxable_base=base_y2,
        balances=_balances(opening),
        year=2,
        carryforward_years=INC_CARRY_YEARS,
    )
    assert year2.uses == (Decimal("0"), Decimal("0"))
    assert year2.generated == -base_y2
    year3 = use_losses(
        taxable_base=base_y3,
        balances=year2.remaining,
        year=3,
        carryforward_years=INC_CARRY_YEARS,
    )
    assert year3.write_offs == (Decimal("500"), Decimal("0"), Decimal("0"))
    assert year3.uses == (Decimal("0"), Decimal("800"), Decimal("1100"))
    tax = income_tax_payable(taxable_base=base_y3, loss_used=sum(year3.uses), rate=INC_RATE)
    assert tax == Decimal("2973")
    prefix = "inc__income_gen"
    assert solved[f"{prefix}__profit__y2"] == pytest.approx(0.0, abs=1e-6)
    assert solved[f"{prefix}__gen__y2"] == pytest.approx(1100.0, rel=1e-6)
    assert solved[f"{prefix}__use__y2__v0"] == pytest.approx(0.0, abs=1e-6)
    assert solved[f"{prefix}__use__y2__v1"] == pytest.approx(0.0, abs=1e-6)
    assert solved[f"{prefix}__use__y3__v1"] == pytest.approx(800.0, rel=1e-6)
    assert solved[f"{prefix}__use__y3__v2"] == pytest.approx(1100.0, rel=1e-6)
    expected = _gen_pretax_npv(GEN_CAP_MAX) - tax / Decimal(str(1.0 + RATE)) ** 3
    assert value == pytest.approx(float(expected), rel=1e-6)


def test_income_same_expiry_input_order(tmp_path) -> None:
    """同到期亏损保留输入顺序：先到期先用的同序精确领取。

    开口 ((8000, last7), (8000, last7)) + 当年新生 1100：应税 13792 下
    第 0 笔取满 8000，第 1 笔取 5792，新生不动；倒序则为 5792/8000。
    """
    opening = ((8000, 7), (8000, 7))
    payload = _gen_finance_payload(opening=opening)
    inputs = _gen_inputs(
        {"sense": "maximize", "expression": NPV_MARKER},
        cap_bounds=("10", "10"),
        payload=payload,
    )
    value, solved = _gen_solve(inputs, tmp_path)
    _, _, base_y3 = _gen_bases(GEN_CAP_MAX)
    year2 = use_losses(
        taxable_base=_gen_bases(GEN_CAP_MAX)[1],
        balances=_balances(opening),
        year=2,
        carryforward_years=INC_CARRY_YEARS,
    )
    year3 = use_losses(
        taxable_base=base_y3,
        balances=year2.remaining,
        year=3,
        carryforward_years=INC_CARRY_YEARS,
    )
    assert year3.uses == (Decimal("8000"), Decimal("5792"), Decimal("0"))
    prefix = "inc__income_gen"
    assert solved[f"{prefix}__use__y3__v0"] == pytest.approx(8000.0, rel=1e-6)
    assert solved[f"{prefix}__use__y3__v1"] == pytest.approx(5792.0, rel=1e-6)
    assert solved[f"{prefix}__use__y3__v2"] == pytest.approx(0.0, abs=1e-6)
    assert value == pytest.approx(float(_gen_pretax_npv(GEN_CAP_MAX)), rel=1e-6)


def test_income_generated_loss_expires_without_carry(tmp_path) -> None:
    """新生亏损过最后可用年即核销：结转 0 年时第 2 年新生不可用于第 3 年。"""
    payload = _gen_finance_payload(carry_years=0)
    inputs = _gen_inputs(
        {"sense": "maximize", "expression": NPV_MARKER},
        cap_bounds=("10", "10"),
        payload=payload,
    )
    value, solved = _gen_solve(inputs, tmp_path)
    _, _, base_y3 = _gen_bases(GEN_CAP_MAX)
    year3 = use_losses(
        taxable_base=base_y3,
        balances=(LossBalance(amount=Decimal("1100"), last_usable_year=2),),
        year=3,
        carryforward_years=0,
    )
    assert year3.uses == (Decimal("0"),)
    assert year3.write_offs == (Decimal("1100"),)
    tax = income_tax_payable(taxable_base=base_y3, loss_used=Decimal("0"), rate=INC_RATE)
    assert tax == Decimal("3448")
    prefix = "inc__income_gen"
    assert solved.get(f"{prefix}__use__y3__v0", 0.0) == pytest.approx(0.0, abs=1e-6)
    assert solved[f"{prefix}__profit__y3"] == pytest.approx(float(base_y3), rel=1e-6)
    expected = _gen_pretax_npv(GEN_CAP_MAX) - tax / Decimal(str(1.0 + RATE)) ** 3
    assert value == pytest.approx(float(expected), rel=1e-6)


def test_income_non_npv_objective_keeps_exact_balances(tmp_path) -> None:
    """非 NPV 目标仍精确：普通目标下亏损领取与税额不受目标驱动。

    同一固定容量与开口下，普通 minimize（售电记账为负，功率仍 pin 满）
    的逐年领取必须与 NPV 求解一致。
    """
    opening = ((500, 2), (800, 7))
    payload = _gen_finance_payload(opening=opening)
    ordinary = {
        "sense": "minimize",
        "expression": "gen1__upfront_capex + 2 year * gen1__annual_fixed_om + meter__period_energy_sale",
    }
    inputs = _gen_inputs(ordinary, cap_bounds=("10", "10"), payload=payload)
    _, solved = _gen_solve(inputs, tmp_path)
    prefix = "inc__income_gen"
    assert solved[f"{prefix}__profit__y3"] == pytest.approx(13792.0, rel=1e-6)
    assert solved[f"{prefix}__use__y3__v1"] == pytest.approx(800.0, rel=1e-6)
    assert solved[f"{prefix}__use__y3__v2"] == pytest.approx(1100.0, rel=1e-6)


def _mix_payload():
    """VAT 全规则（含税）+ 建设可抵 50% + 所得税规则的混合载荷。"""
    payload = _finance_payload()
    payload["tax_rules"]["con_vat"]["deductible_ratio"] = {"value": "0.5", "unit": "1"}
    payload["taxes"]["income_demo"] = {
        "display_name": "合成所得税",
        "tax_type": "income_tax",
        "applies_to": "export_ts",
    }
    payload["tax_rules"]["income_demo"] = {
        "kind": "income_tax",
        "label_ref": "income_demo",
        "rate": {"value": "0.25", "unit": "1"},
        "revenue": ["export_ts"],
        "energy_cost": ["heat_import"],
        "annual_om": ["sys1.annual_fixed_om"],
        "variable_om": ["sys1.period_variable_om", "sys_var.period_variable_om"],
        "deductible_maintenance": {
            "mode": "explicit_items",
            "items": [
                {"year": 2, "position": 1, "deductible": True},
                {"year": 2, "position": 2, "deductible": False},
            ],
        },
        "depreciation": [
            {
                "asset": "sys1",
                "base_from": "sys1.upfront_capex_net",
                "method": "straight_line",
                "life_years": 20,
                "residual": {"value": "0", "unit": "CNY"},
                "first_year": 1,
            },
        ],
        "loss_carryforward_years": 5,
        "opening_losses": [],
    }
    return payload


VAT_RATE = Decimal("0.13")
VAT_UNIT = Decimal("1") + VAT_RATE


def _mix_bases(*, energy_credit=Decimal("1"), residual=Decimal("0")):
    """混合夹具评价年 1..3 应税基础手算（含税；建设可抵 50%）。

    计税收入 = 实际销售收款 − 已声明销项税；可扣购能 = 实际含税付款 −
    已声明可抵进项（不可抵部分保留）；年固定 OM 可抵半、tank 可变全抵、
    sys_var 可变无声明（按陈述）；维护 (2,1) 可扣净额、(2,2) 不可扣但
    现金不变；折旧 =（6000 − 建设可抵半 − 残值）/ 20，残值须由调用方
    保证不超过计税基础。负价保留符号。
    """
    energy_kwh = Decimal(str(ELEC_KW)) * Decimal("8760")
    heat_kwh = Decimal("7") * Decimal("8760")
    gross_y2 = Decimal("-0.05") * energy_kwh
    rev_y2 = gross_y2 - gross_y2 * VAT_RATE / VAT_UNIT
    gross_y3 = Decimal("2.0") * energy_kwh
    rev_y3 = gross_y3 - gross_y3 * VAT_RATE / VAT_UNIT
    gross_heat = Decimal("0.7") * heat_kwh
    energy = gross_heat - gross_heat * GROSS_FACTOR * energy_credit
    om_fixed = Decimal("2100") * (Decimal("1") - GROSS_FACTOR * Decimal("0.5"))
    var_thr = Decimal("5") * Decimal("8760") * Decimal("0.01") * (Decimal("1") - GROSS_FACTOR)
    var_e = energy_kwh * Decimal("0.02")
    maint_y2 = Decimal("1200.50") * (Decimal("1") - GROSS_FACTOR)
    depr = (Decimal("6000") - Decimal("6000") * GROSS_FACTOR * Decimal("0.5") - residual) / Decimal("20")
    base_y1 = Decimal("0")
    base_y2 = rev_y2 - energy - om_fixed - var_thr - var_e - maint_y2 - depr
    base_y3 = rev_y3 - energy - om_fixed - var_thr - var_e - depr
    return base_y1, base_y2, base_y3


def test_income_vat_mix_matches_hand_and_b0(tmp_path) -> None:
    """VAT 与所得税混合真实处理：月链、B0 逐年、NPV 同源一致。"""
    artifact, models, materialized, finance, config, evaluation = _inputs(payload=_mix_payload())
    bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
    out, inp = _hand_monthly(con_ratio=Decimal("0.5"))
    paids, carries = _hand_chain(out, inp)
    value, solved, _ = _solve_internal(bundle, tmp_path)
    for month in range(1, 37):
        assert solved[f"vat__main_pool__paid__m{month}"] == pytest.approx(float(paids[month - 1]), rel=1e-6)
        assert solved[f"vat__main_pool__carry__m{month}"] == pytest.approx(
            float(carries[month - 1]), rel=1e-6
        )
    base_y1, base_y2, base_y3 = _mix_bases()
    year1 = use_losses(taxable_base=base_y1, balances=(), year=1, carryforward_years=INC_CARRY_YEARS)
    assert year1.generated == Decimal("0")
    year2 = use_losses(
        taxable_base=base_y2,
        balances=year1.remaining,
        year=2,
        carryforward_years=INC_CARRY_YEARS,
    )
    assert year2.uses == ()
    assert year2.generated == -base_y2 > Decimal("0")
    year3 = use_losses(
        taxable_base=base_y3,
        balances=year2.remaining,
        year=3,
        carryforward_years=INC_CARRY_YEARS,
    )
    assert year3.uses == (-base_y2,)
    tax = income_tax_payable(taxable_base=base_y3, loss_used=-base_y2, rate=INC_RATE)
    prefix = "inc__income_demo"
    assert solved[f"{prefix}__profit__y2"] == pytest.approx(0.0, abs=1e-6)
    assert solved[f"{prefix}__gen__y2"] == pytest.approx(float(-base_y2), rel=1e-6)
    assert solved[f"{prefix}__profit__y3"] == pytest.approx(float(base_y3), rel=1e-6)
    assert solved[f"{prefix}__use__y3__v0"] == pytest.approx(float(-base_y2), rel=1e-6)
    expected = _hand_npv_with_vat(paids) - tax / Decimal(str(1.0 + RATE)) ** 3
    assert value == pytest.approx(float(expected), rel=1e-6)


def _mix_payload_exclusive_half_energy():
    """不含税混合载荷：同值陈述视作净额，购能可抵比例改为 50%。

    覆盖不含税口径的销售（销项已声明）、部分可抵购能、年度/使用 OM
    加成、维护恒含税、建设部分可抵折旧。
    """
    payload = _mix_payload()
    payload["profile"]["price_basis"] = "tax_exclusive"
    payload["tax_rules"]["vat_op"]["in_tax"][0]["deductible_ratio"] = {
        "value": "0.5",
        "unit": "1",
    }
    return payload


def _mix_bases_exclusive_half_energy():
    """不含税混合手算：实际收付 = 陈述 × 1.13，再减明确税额/抵免。

    购能可抵 50%、年固定 OM 可抵 50%、tank 可变全抵、sys_var 无声明、
    维护恒含税拆价、折旧基础 = 6000×1.13 − 6000×0.13×0.5。
    """
    markup = VAT_UNIT
    energy_kwh = Decimal(str(ELEC_KW)) * Decimal("8760")
    heat_kwh = Decimal("7") * Decimal("8760")
    rev_y2 = Decimal("-0.05") * energy_kwh
    rev_y3 = Decimal("2.0") * energy_kwh
    stated_heat = Decimal("0.7") * heat_kwh
    energy = stated_heat * markup - stated_heat * VAT_RATE * Decimal("0.5")
    stated_fixed = Decimal("2100")
    om_fixed = stated_fixed * markup - stated_fixed * VAT_RATE * Decimal("0.5")
    stated_thr = Decimal("5") * Decimal("8760") * Decimal("0.01")
    var_thr = stated_thr * markup - stated_thr * VAT_RATE
    var_e = energy_kwh * Decimal("0.02")
    maint_y2 = Decimal("1200.50") * (Decimal("1") - GROSS_FACTOR)
    depr = (Decimal("6000") * markup - Decimal("6000") * VAT_RATE * Decimal("0.5")) / Decimal("20")
    base_y1 = Decimal("0")
    base_y2 = rev_y2 - energy - om_fixed - var_thr - var_e - maint_y2 - depr
    base_y3 = rev_y3 - energy - om_fixed - var_thr - var_e - depr
    return base_y1, base_y2, base_y3


def _hand_monthly_exclusive_half_energy():
    """不含税 + 购能半抵的 36 个月 (out, in) 手算（业务 Decimal）。

    税额系数取净额税率 0.13；维护恒按含税拆价；其余与含税链同口径。
    """
    out = [Decimal("0")] * 36
    inp = [Decimal("0")] * 36
    deductible_total = Decimal("6000") * VAT_RATE * Decimal("0.5")
    inp[0] += Decimal("0.6") * deductible_total
    inp[12] += Decimal("0.4") * deductible_total
    for index, hours in enumerate(HOURS_2026):
        month = 13 + index
        energy = Decimal("7") * Decimal(str(hours))
        inp[month - 1] += energy * Decimal("0.7") * VAT_RATE * Decimal("0.5")
        inp[month - 1] += Decimal("5") * Decimal(str(hours)) * Decimal("0.01") * VAT_RATE
        out[month - 1] += Decimal(str(ELEC_KW)) * Decimal(str(hours)) * Decimal("-0.05") * VAT_RATE
    for index, hours in enumerate(HOURS_2027):
        month = 25 + index
        energy = Decimal("7") * Decimal(str(hours))
        inp[month - 1] += energy * Decimal("0.7") * VAT_RATE * Decimal("0.5")
        inp[month - 1] += Decimal("5") * Decimal(str(hours)) * Decimal("0.01") * VAT_RATE
        out[month - 1] += Decimal(str(ELEC_KW)) * Decimal(str(hours)) * Decimal("2.0") * VAT_RATE
    inp[23] += Decimal("2100") * VAT_RATE * Decimal("0.5")
    inp[35] += Decimal("2100") * VAT_RATE * Decimal("0.5")
    inp[23] += Decimal("1200.50") * GROSS_FACTOR
    return out, inp


def test_income_vat_mix_exclusive_matches_hand_and_b0(tmp_path) -> None:
    """不含税口径混合全链：税基按净额语义，税后 NPV 同源一致。

    销售/购能/OM 先形成实际收付再扣明确税额；购能半抵与年固定 OM
    半抵验证部分可抵；无声明的 sys_var 按陈述；维护现金不变。
    """
    artifact, models, materialized, finance, config, evaluation = _inputs(
        payload=_mix_payload_exclusive_half_energy()
    )
    assert finance.price_basis == "tax_exclusive"
    bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
    out, inp = _hand_monthly_exclusive_half_energy()
    paids, carries = _hand_chain(out, inp)
    value, solved, _ = _solve_internal(bundle, tmp_path)
    for month in range(1, 37):
        assert solved[f"vat__main_pool__paid__m{month}"] == pytest.approx(float(paids[month - 1]), rel=1e-6)
        assert solved[f"vat__main_pool__carry__m{month}"] == pytest.approx(
            float(carries[month - 1]), rel=1e-6
        )
    base_y1, base_y2, base_y3 = _mix_bases_exclusive_half_energy()
    year1 = use_losses(taxable_base=base_y1, balances=(), year=1, carryforward_years=INC_CARRY_YEARS)
    assert year1.generated == Decimal("0")
    year2 = use_losses(
        taxable_base=base_y2,
        balances=year1.remaining,
        year=2,
        carryforward_years=INC_CARRY_YEARS,
    )
    assert year2.generated == -base_y2 > Decimal("0")
    year3 = use_losses(
        taxable_base=base_y3,
        balances=year2.remaining,
        year=3,
        carryforward_years=INC_CARRY_YEARS,
    )
    assert year3.uses == (-base_y2,)
    tax = income_tax_payable(taxable_base=base_y3, loss_used=-base_y2, rate=INC_RATE)
    prefix = "inc__income_demo"
    assert solved[f"{prefix}__profit__y2"] == pytest.approx(0.0, abs=1e-6)
    assert solved[f"{prefix}__gen__y2"] == pytest.approx(float(-base_y2), rel=1e-6)
    assert solved[f"{prefix}__profit__y3"] == pytest.approx(float(base_y3), rel=1e-6)
    assert solved[f"{prefix}__use__y3__v0"] == pytest.approx(float(-base_y2), rel=1e-6)
    expected = _hand_npv_with_vat(paids, cash=Decimal("1.13")) - tax / Decimal(str(1.0 + RATE)) ** 3
    assert value == pytest.approx(float(expected), rel=1e-6)


def _equiv_payloads():
    """等价陈述对：同一实际交易，含税总额 vs 不含税净额（÷1.13）。

    购能可抵比例两边同取 50%；仅已声明税率的陈述值（购能价、建设/
    年固定/使用 OM 单价、倒送序列）按净额缩放，无声明的 sys_var 单价
    与维护实际金额两边相同。
    """
    half = {"value": "0.5", "unit": "1"}
    incl = _mix_payload()
    incl["tax_rules"]["vat_op"]["in_tax"][0]["deductible_ratio"] = dict(half)
    excl = _mix_payload()
    excl["tax_rules"]["vat_op"]["in_tax"][0]["deductible_ratio"] = dict(half)
    excl["profile"]["price_basis"] = "tax_exclusive"

    def as_net(stated: str) -> str:
        # 净额保留 10 位小数（公开金额精度门禁要求；引入相对误差约
        # 1e-12，不影响 1e-6 对照）。
        return str((Decimal(stated) / VAT_UNIT).quantize(Decimal("0.0000000001")))

    heat = excl["energy_prices"]["heat_import"]["value"]
    heat["value"] = as_net(heat["value"])
    sys1 = excl["finance_types"]["sys1"]
    sys1["upfront_capex"]["linear"]["cap"]["unit_cost"]["value"] = as_net(
        sys1["upfront_capex"]["linear"]["cap"]["unit_cost"]["value"]
    )
    sys1["annual_fixed_om"]["linear"]["cap"]["unit_cost"]["value"] = as_net(
        sys1["annual_fixed_om"]["linear"]["cap"]["unit_cost"]["value"]
    )
    sys1["period_variable_om"]["linear"]["thr"]["unit_cost"]["value"] = as_net(
        sys1["period_variable_om"]["linear"]["thr"]["unit_cost"]["value"]
    )
    export_values = tuple(float(Decimal(str(v)) / VAT_UNIT) for v in _price_values())
    return incl, excl, export_values


def test_income_equivalent_gross_net_statement_matches(tmp_path) -> None:
    """等价含税/不含税陈述：计税基础与税后 NPV 一致。

    两边独立手算期望（销售 gross−销项、购能半抵、年度/使用 OM、
    维护、折旧），再断言两求解的税基变量、月税链与目标值互相一致。
    """
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
    prefix = "inc__income_demo"
    wanted = {
        f"{prefix}__gen__y2": float(-base_y2),
        f"{prefix}__profit__y3": float(base_y3),
        f"{prefix}__use__y3__v0": float(-base_y2),
    }
    out, inp = _hand_monthly(con_ratio=Decimal("0.5"), energy_ratio=Decimal("0.5"))
    paids, carries = _hand_chain(out, inp)
    # 同一实际交易按绝对值应得同一月链：不含税净额 × 税率恒等于含税
    # 总额 × 含税拆价，故双方共用同一份手算期望。
    expected = _hand_npv_with_vat(paids) - tax / Decimal(str(1.0 + RATE)) ** 3

    artifact, models, materialized, finance, config, evaluation = _inputs(payload=incl_payload)
    bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
    value_incl, solved_incl, _ = _solve_internal(bundle, tmp_path)
    artifact, models, materialized, finance, config, evaluation = _inputs(
        payload=excl_payload, export_values=export_values
    )
    assert finance.price_basis == "tax_exclusive"
    bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
    value_excl, solved_excl, _ = _solve_internal(bundle, tmp_path)
    for month in range(1, 37):
        assert solved_incl[f"vat__main_pool__paid__m{month}"] == pytest.approx(
            float(paids[month - 1]), rel=1e-6
        )
        assert solved_excl[f"vat__main_pool__paid__m{month}"] == pytest.approx(
            float(paids[month - 1]), rel=1e-6
        )
    for name, want in wanted.items():
        assert solved_incl[name] == pytest.approx(want, rel=1e-6)
        assert solved_excl[name] == pytest.approx(want, rel=1e-6)
        assert solved_excl[name] == pytest.approx(solved_incl[name], rel=1e-6)
    assert solved_incl[f"{prefix}__profit__y2"] == pytest.approx(0.0, abs=1e-6)
    assert solved_excl[f"{prefix}__profit__y2"] == pytest.approx(0.0, abs=1e-6)
    assert value_incl == pytest.approx(float(expected), rel=1e-6)
    assert value_excl == pytest.approx(float(expected), rel=1e-6)
    assert value_excl == pytest.approx(value_incl, rel=1e-6)


def _residual_payload(base, residual: str):
    """复制载荷并将首条折旧残值改写（不改其他业务）。"""
    payload = base()
    payload["tax_rules"]["income_gen" if "income_gen" in payload["tax_rules"] else "income_demo"][
        "depreciation"
    ][0]["residual"] = {"value": residual, "unit": "CNY"}
    return payload


def test_income_depreciation_zero_build_no_fictitious_income(tmp_path) -> None:
    """零容量 + 正残值：静态已知未形成资产，不计提且无虚构所得。

    容量固定为 0、残值 100：旧实现给出 profit_y2=10 的反例；修正后
    两年所得与目标值均为零。
    """
    payload = _residual_payload(_gen_finance_payload, "100")
    inputs = _gen_inputs(
        {"sense": "maximize", "expression": NPV_MARKER},
        cap_bounds=("0", "0"),
        payload=payload,
    )
    value, solved = _gen_solve(inputs, tmp_path)
    assert solved["gen_cap"] == pytest.approx(0.0, abs=1e-6)
    assert solved["inc__income_gen__profit__y2"] == pytest.approx(0.0, abs=1e-6)
    assert solved["inc__income_gen__profit__y3"] == pytest.approx(0.0, abs=1e-6)
    assert value == pytest.approx(0.0, abs=1e-6)


def test_income_depreciation_variable_capacity_positive_residual_rejected(tmp_path) -> None:
    """可变容量 + 固定正残值：无法静态判定是否形成资产，明确拒绝。

    不猜形成状态，不提高容量下界强迫建设，不把残值擅改比例。
    """
    from iesplan.computation.generators.linear import LoweringUnsupportedError

    payload = _residual_payload(_gen_finance_payload, "100")
    inputs = _gen_inputs(
        {"sense": "maximize", "expression": NPV_MARKER},
        cap_bounds=("0", "10"),
        payload=payload,
    )
    with pytest.raises(LoweringUnsupportedError, match="可变容量"):
        HIGHS_MILP_PROVIDER.generate(*inputs)


def test_income_depreciation_static_residual_matches_hand(tmp_path) -> None:
    """静态已形成资产 + 合法正残值：残值 100 在计税基础内，精确计提。

    固定 60kW 上限资产计税基础 6000 − 建设可抵半；折旧少提 5/年，
    两年税基各 +5，税后 NPV 同源变化。
    """
    payload = _residual_payload(_mix_payload, "100")
    artifact, models, materialized, finance, config, evaluation = _inputs(payload=payload)
    bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
    out, inp = _hand_monthly(con_ratio=Decimal("0.5"))
    paids, _ = _hand_chain(out, inp)
    value, solved, _ = _solve_internal(bundle, tmp_path)
    base_y1, base_y2, base_y3 = _mix_bases(residual=Decimal("100"))
    year2 = use_losses(taxable_base=base_y2, balances=(), year=2, carryforward_years=INC_CARRY_YEARS)
    year3 = use_losses(
        taxable_base=base_y3,
        balances=year2.remaining,
        year=3,
        carryforward_years=INC_CARRY_YEARS,
    )
    assert year3.uses == (-base_y2,)
    tax = income_tax_payable(taxable_base=base_y3, loss_used=-base_y2, rate=INC_RATE)
    prefix = "inc__income_demo"
    assert solved[f"{prefix}__gen__y2"] == pytest.approx(float(-base_y2), rel=1e-6)
    assert solved[f"{prefix}__profit__y3"] == pytest.approx(float(base_y3), rel=1e-6)
    assert solved[f"{prefix}__use__y3__v0"] == pytest.approx(float(-base_y2), rel=1e-6)
    expected = _hand_npv_with_vat(paids) - tax / Decimal(str(1.0 + RATE)) ** 3
    assert value == pytest.approx(float(expected), rel=1e-6)


def test_income_depreciation_excess_residual_rejected(tmp_path) -> None:
    """已形成资产但残值超计税基础：不得以负折旧放行，明确拒绝。"""
    from iesplan.computation.generators.linear import LoweringUnsupportedError

    payload = _residual_payload(_mix_payload, "100000")
    artifact, models, materialized, finance, config, evaluation = _inputs(payload=payload)
    with pytest.raises(LoweringUnsupportedError, match="超过最小计税基础"):
        HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)


def test_income_depreciation_skipped_without_new_asset(tmp_path) -> None:
    """无本课题新增可折旧资产：既有资产不计提，残值无影响。

    tank 转既有后建设/固定 OM 进项与折旧同步消失；收入/购能/可变
    OM/维护口径不变，税基与 NPV 与显式手算一致。
    """
    payload = _residual_payload(_mix_payload, "100")
    artifact, models, materialized, finance, config, evaluation = _inputs(
        payload=payload, tank_origin="existing"
    )
    bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
    out, inp = _hand_monthly(upfront=Decimal("0"), fixed_om=Decimal("0"), con_ratio=Decimal("0.5"))
    paids, _ = _hand_chain(out, inp)
    value, solved, _ = _solve_internal(bundle, tmp_path)
    energy_kwh = Decimal(str(ELEC_KW)) * Decimal("8760")
    heat_kwh = Decimal("7") * Decimal("8760")
    gross_y2 = Decimal("-0.05") * energy_kwh
    rev_y2 = gross_y2 - gross_y2 * VAT_RATE / VAT_UNIT
    gross_y3 = Decimal("2.0") * energy_kwh
    rev_y3 = gross_y3 - gross_y3 * VAT_RATE / VAT_UNIT
    gross_heat = Decimal("0.7") * heat_kwh
    energy = gross_heat - gross_heat * GROSS_FACTOR
    var_thr = Decimal("5") * Decimal("8760") * Decimal("0.01") * (Decimal("1") - GROSS_FACTOR)
    var_e = energy_kwh * Decimal("0.02")
    maint_y2 = Decimal("1200.50") * (Decimal("1") - GROSS_FACTOR)
    base_y2 = rev_y2 - energy - var_thr - var_e - maint_y2
    base_y3 = rev_y3 - energy - var_thr - var_e
    year2 = use_losses(taxable_base=base_y2, balances=(), year=2, carryforward_years=INC_CARRY_YEARS)
    year3 = use_losses(
        taxable_base=base_y3,
        balances=year2.remaining,
        year=3,
        carryforward_years=INC_CARRY_YEARS,
    )
    assert year3.uses == (-base_y2,)
    tax = income_tax_payable(taxable_base=base_y3, loss_used=-base_y2, rate=INC_RATE)
    prefix = "inc__income_demo"
    assert solved[f"{prefix}__gen__y2"] == pytest.approx(float(-base_y2), rel=1e-6)
    assert solved[f"{prefix}__profit__y3"] == pytest.approx(float(base_y3), rel=1e-6)
    expected = _hand_npv_with_vat(paids, upfront=Decimal("0"), fixed_om=Decimal("0"))
    expected -= tax / Decimal(str(1.0 + RATE)) ** 3
    assert value == pytest.approx(float(expected), rel=1e-6)
