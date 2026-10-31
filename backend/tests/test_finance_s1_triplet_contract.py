"""财务三件套 6.0.0 S1 通用能力定向契约测试。

覆盖 S1 输入链全部核心要求：
1. annual_fixed_om 支持固定年费（单位货币/年如 CNY/a）与规模关联年费共存；拒绝纯币种金额冒充年率；
2. 折旧使用明确的 base_from（upfront_capex_net 与 maintenance_expense_net 两种判别联合）；拒绝旧 asset 字段；
3. 残值同时支持确定货币金额与无量纲比例（unit: '1'），按来源身份去重；
4. net_cashflows 时点（evaluation_end / financial_year）、收付方向与金额（fixed / source_share）完整形态；
5. VAT 规则支持对成本分量整项及明确子项（.fixed、.linear.<driver>）的引用，按实际覆盖范围识别重叠并拒绝冲突；
6. Overrides 稀疏覆盖对新形态的合法替换与越权覆盖拒绝；
7. 往返序列化与确定性合并 Effective 往返无损；
8. 任务提交边界对维护项来源归属、期间（换新首年须晚于支付年）、
   扣除冲突（资本化不得同时费用扣除）及未接通生成器能力门禁（TASK-REQ-007）的严格拦截。
"""

from decimal import Decimal

import pytest

from iesplan.application.tasks.submissions import (
    _check_tax_capabilities,
    _check_tax_evaluation_refs,
)
from iesplan.finance import (
    EvaluationEndAt,
    EffectiveFinanceConfig,
    FinanceOverrides,
    FinanceProfile,
    FinanceTripletError,
    FinancialYearAt,
    FixedCashflowAmount,
    MaintenanceExpenseNetBaseFrom,
    SourceShareCashflowAmount,
    UpfrontCapexNetBaseFrom,
    effective_from_yaml,
    effective_to_yaml,
    merge_effective,
)
from iesplan.finance.confirmation import EvaluationInput, MaintenanceExpense
from iesplan.tasks.contracts import InvalidRequestError


def _valid_profile_dict() -> dict:
    return {
        "schema": "ies.finance-profile",
        "schema_version": "6.0.0",
        "profile": {
            "id": "syn-600-base",
            "region": "CN-East",
            "currency": "CNY",
            "base_year": 2026,
            "price_basis": "tax_inclusive",
            "cost_method": "fixed_plus_linear",
        },
        "finance_types": {
            "pv_system": {
                "upfront_capex": {
                    "fixed": {"value": "200000", "unit": "CNY"},
                    "linear": {"capacity_kw": {"unit_cost": {"value": "3200", "unit": "CNY/kW"}}},
                },
                "annual_fixed_om": {
                    "fixed": {"value": "50000", "unit": "CNY/a"},
                    "linear": {"capacity_kw": {"unit_cost": {"value": "40", "unit": "CNY/kW/a"}}},
                },
                "period_variable_om": {
                    "linear": {"generated_kwh": {"unit_cost": {"value": "0.02", "unit": "CNY/kWh"}}},
                },
            },
            "storage_system": {
                "upfront_capex": {
                    "linear": {"capacity_kwh": {"unit_cost": {"value": "1200", "unit": "CNY/kWh"}}},
                },
                "annual_fixed_om": {
                    "linear": {"capacity_kwh": {"unit_cost": {"value": "25", "unit": "CNY/kWh/a"}}},
                },
            },
        },
        "energy_prices": {
            "grid_purchase": {
                "carrier": "electricity",
                "direction": "purchase",
                "kind": "constant",
                "value": {"value": "0.6", "unit": "CNY/kWh"},
            },
            "pv_sale": {
                "carrier": "electricity",
                "direction": "sale",
                "kind": "constant",
                "value": {"value": "0.45", "unit": "CNY/kWh"},
            },
        },
        "demand_charges": {
            "main_demand": {"rate": {"value": "30", "unit": "CNY/kW"}},
        },
        "taxes": {
            "vat_label": {
                "display_name": "增值税",
                "tax_type": "value_added_tax",
                "applies_to": "grid_purchase",
            },
            "cit_label": {
                "display_name": "企业所得税",
                "tax_type": "income_tax",
                "applies_to": "pv_system.upfront_capex",
            },
        },
        "credit_pools": {
            "main_pool": {
                "opening_credit": {"value": "0", "unit": "CNY"},
                "receivable_policy": "end_receivable",
                "refunds": [],
            },
        },
        "tax_rules": {
            "op_vat": {
                "kind": "vat",
                "label_ref": "vat_label",
                "settlement": "monthly",
                "in_tax": [
                    {
                        "price": "grid_purchase",
                        "rate": {"value": "0.13", "unit": "1"},
                        "deductible_ratio": {"value": "1", "unit": "1"},
                    },
                ],
                "out_tax": [
                    {"price": "pv_sale", "rate": {"value": "0.13", "unit": "1"}},
                ],
                "om_input_tax": [
                    {
                        "for": "pv_system.annual_fixed_om.fixed",
                        "rate": {"value": "0.06", "unit": "1"},
                        "deductible_ratio": {"value": "1", "unit": "1"},
                    },
                    {
                        "for": "pv_system.annual_fixed_om.linear.capacity_kw",
                        "rate": {"value": "0.06", "unit": "1"},
                        "deductible_ratio": {"value": "0.5", "unit": "1"},
                    },
                ],
                "maintenance_input_tax": [],
                "credit_pool": "main_pool",
                "confirmation": "same_period",
            },
            "build_vat": {
                "kind": "construction_input_vat",
                "label_ref": "vat_label",
                "for": "pv_system.upfront_capex.linear.capacity_kw",
                "rate": {"value": "0.09", "unit": "1"},
                "deductible_ratio": {"value": "1", "unit": "1"},
                "credit_pool": "main_pool",
                "confirmation": {"method": "payment_period"},
            },
            "cit_rule": {
                "kind": "income_tax",
                "label_ref": "cit_label",
                "rate": {"value": "0.25", "unit": "1"},
                "revenue": ["pv_sale"],
                "energy_cost": ["grid_purchase"],
                "annual_om": ["pv_system.annual_fixed_om"],
                "variable_om": ["pv_system.period_variable_om"],
                "deductible_maintenance": {"mode": "explicit_items", "items": []},
                "depreciation": [
                    {
                        "base_from": {"kind": "upfront_capex_net", "finance_type": "pv_system"},
                        "method": "straight_line",
                        "life_years": 20,
                        "residual": {"value": "0.05", "unit": "1"},
                        "first_year": 1,
                    },
                    {
                        "base_from": {"kind": "maintenance_expense_net", "year": 5, "position": 1},
                        "method": "straight_line",
                        "life_years": 8,
                        "residual": {"value": "0", "unit": "CNY"},
                        "first_year": 6,
                    },
                ],
                "loss_carryforward_years": 5,
                "opening_losses": [],
            },
        },
        "net_cashflows": {
            "salvage_recovery": {
                "at": {"kind": "evaluation_end"},
                "direction": "inflow",
                "amount": {
                    "kind": "source_share",
                    "base_from": {"kind": "upfront_capex_net", "finance_type": "pv_system"},
                    "ratio": {"value": "0.03", "unit": "1"},
                },
            },
            "year10_bonus": {
                "at": {"kind": "financial_year", "year": 10},
                "direction": "outflow",
                "amount": {
                    "kind": "fixed",
                    "value": {"value": "20000", "unit": "CNY"},
                },
            },
        },
    }


def test_600_annual_fixed_om_units():
    """1. annual_fixed_om 支持固定年费与规模关联年费，严格要求年率单位 CNY/a。"""
    p = _valid_profile_dict()
    profile = FinanceProfile.from_dict(p)
    assert profile.finance_types["pv_system"].annual_fixed_om.fixed.value == Decimal("50000")
    assert profile.finance_types["pv_system"].annual_fixed_om.fixed.unit == "CNY/a"

    # 拒绝纯货币金额冒充年率（如 CNY）
    p_bad = _valid_profile_dict()
    p_bad["finance_types"]["pv_system"]["annual_fixed_om"]["fixed"] = {"value": "50000", "unit": "CNY"}
    with pytest.raises(FinanceTripletError, match="不接受纯币种金额冒充年率"):
        FinanceProfile.from_dict(p_bad)

    # 拒绝在 period_variable_om 中声明 fixed
    p_bad_om = _valid_profile_dict()
    p_bad_om["finance_types"]["pv_system"]["period_variable_om"]["fixed"] = {"value": "100", "unit": "CNY"}
    with pytest.raises(FinanceTripletError, match="period_variable_om 不允许 fixed"):
        FinanceProfile.from_dict(p_bad_om)


def test_600_depreciation_and_residual():
    """2 & 3. 折旧 base_from 明确引用与残值多形态（固定金额与无量纲比例并存）。"""
    p = _valid_profile_dict()
    profile = FinanceProfile.from_dict(p)
    cit_rule = profile.tax_rules["cit_rule"]
    depr = cit_rule.depreciation
    assert len(depr) == 2
    # 条目 1: upfront_capex_net 比例残值
    assert isinstance(depr[0].base_from, UpfrontCapexNetBaseFrom)
    assert depr[0].base_from.finance_type == "pv_system"
    assert depr[0].residual.value == Decimal("0.05")
    assert depr[0].residual.unit == "1"

    # 条目 2: maintenance_expense_net 确定货币残值
    assert isinstance(depr[1].base_from, MaintenanceExpenseNetBaseFrom)
    assert depr[1].base_from.year == 5
    assert depr[1].base_from.position == 1
    assert depr[1].residual.value == Decimal("0")
    assert depr[1].residual.unit == "CNY"

    # 拒绝旧 asset 字段
    p_bad_asset = _valid_profile_dict()
    p_bad_asset["tax_rules"]["cit_rule"]["depreciation"][0]["asset"] = "pv_system"
    with pytest.raises(FinanceTripletError, match="未知字段:.*asset"):
        FinanceProfile.from_dict(p_bad_asset)

    # 拒绝残值比例非法单位
    p_bad_unit = _valid_profile_dict()
    p_bad_unit["tax_rules"]["cit_rule"]["depreciation"][0]["residual"] = {"value": "0.05", "unit": "USD"}
    with pytest.raises(FinanceTripletError, match="residual 单位须为 Profile 币种 'CNY' 或无量纲比例 '1'"):
        FinanceProfile.from_dict(p_bad_unit)

    # 拒绝同一来源身份重复计提折旧
    p_dup_source = _valid_profile_dict()
    p_dup_source["tax_rules"]["cit_rule"]["depreciation"].append({
        "base_from": {"kind": "upfront_capex_net", "finance_type": "pv_system"},
        "method": "straight_line",
        "life_years": 10,
        "residual": {"value": "0", "unit": "CNY"},
        "first_year": 1,
    })
    with pytest.raises(FinanceTripletError, match="来源身份重复"):
        FinanceProfile.from_dict(p_dup_source)


def test_600_net_cashflows_forms_and_overrides():
    """4. net_cashflows 完整形态、校验与 Overrides 稀疏覆盖。"""
    p = _valid_profile_dict()
    profile = FinanceProfile.from_dict(p)
    assert len(profile.net_cashflows) == 2
    nc1 = profile.net_cashflows["salvage_recovery"]
    assert isinstance(nc1.at, EvaluationEndAt)
    assert nc1.direction == "inflow"
    assert isinstance(nc1.amount, SourceShareCashflowAmount)
    assert nc1.amount.ratio.value == Decimal("0.03")

    nc2 = profile.net_cashflows["year10_bonus"]
    assert isinstance(nc2.at, FinancialYearAt)
    assert nc2.at.year == 10
    assert nc2.direction == "outflow"
    assert isinstance(nc2.amount, FixedCashflowAmount)
    assert nc2.amount.value.value == Decimal("20000")

    # Overrides 覆盖 net_cashflows
    overrides_dict = {
        "schema": "ies.finance-overrides",
        "schema_version": "6.0.0",
        "profile_ref": {"id": "syn-600-base"},
        "net_cashflows": {
            "salvage_recovery": {
                "amount": {"ratio": {"value": "0.04", "unit": "1"}},
            },
            "year10_bonus": {
                "at": {"kind": "financial_year", "year": 12},
                "amount": {"value": {"value": "25000", "unit": "CNY"}},
            },
        },
    }
    overrides = FinanceOverrides.from_dict(overrides_dict, profile=profile)
    effective = merge_effective(profile, overrides)
    assert effective.net_cashflows["salvage_recovery"].amount.ratio.value == Decimal("0.04")
    assert effective.net_cashflows["year10_bonus"].at.year == 12
    assert effective.net_cashflows["year10_bonus"].amount.value.value == Decimal("25000")

    # Overrides 禁止新增未知 cashflow_id
    bad_ov = dict(overrides_dict)
    bad_ov["net_cashflows"] = {"unknown_cf": {"at": {"kind": "evaluation_end"}}}
    with pytest.raises(FinanceTripletError, match="在 Profile 中不存在, 禁止新增"):
        FinanceOverrides.from_dict(bad_ov, profile=profile)


@pytest.mark.parametrize("bound", [False, True], ids=["unbound", "bound"])
def test_600_net_cashflow_override_typed_roundtrip(bound):
    """两条公开入口均签发同一合法 Effective；时点/金额与来源身份无损。"""
    profile = FinanceProfile.from_dict(_valid_profile_dict())
    payload = {
        "schema": "ies.finance-overrides", "schema_version": "6.0.0",
        "profile_ref": {"id": profile.profile_id},
        "net_cashflows": {
            "salvage_recovery": {
                "at": {"kind": "financial_year", "year": 8},
                "amount": {"ratio": {"value": "0.1", "unit": "1"}},
            },
            "year10_bonus": {"amount": {"value": {"value": "25001", "unit": "CNY"}}},
        },
    }
    override = FinanceOverrides.from_dict(payload, **({"profile": profile} if bound else {}))
    assert override.to_dict() == FinanceOverrides.from_dict(override.to_dict()).to_dict()
    effective = merge_effective(profile, override)
    assert effective.net_cashflows["salvage_recovery"].at.year == 8
    assert effective.net_cashflows["salvage_recovery"].direction == "inflow"
    assert effective.net_cashflows["salvage_recovery"].amount.base_from.finance_type == "pv_system"
    assert effective.net_cashflows["salvage_recovery"].amount.ratio.value == Decimal("0.1")
    assert effective.net_cashflows["year10_bonus"].amount.value.value == Decimal("25001")
    assert EffectiveFinanceConfig.from_dict(effective.to_dict()).to_dict() == effective.to_dict()


@pytest.mark.parametrize("bound", [False, True], ids=["unbound", "bound"])
@pytest.mark.parametrize("patch", [
    {"amount": {"value": {"value": "1", "unit": "USD"}}},
    {"amount": {"value": {"value": "1", "unit": "CNY"}, "unknown": "discard-me"}},
    {"amount": {"ratio": {"value": "0.2", "unit": "1"}}},
    {"amount": {"kind": "source_share", "ratio": {"value": "0.2", "unit": "1"}}},
    {"amount": {"value": {"value": "1", "unit": "CNY"},
                "base_from": {"kind": "upfront_capex_net", "finance_type": "pv_system"}}},
    {"direction": "inflow"},
], ids=["foreign-currency", "unknown-field", "kind-change", "kind-key", "base-from", "direction"])
def test_600_net_cashflow_override_rejects_scope(bound, patch):
    profile = FinanceProfile.from_dict(_valid_profile_dict())
    payload = {
        "schema": "ies.finance-overrides", "schema_version": "6.0.0",
        "profile_ref": {"id": profile.profile_id},
        "net_cashflows": {"year10_bonus": patch},
    }
    if bound:
        with pytest.raises(FinanceTripletError):
            FinanceOverrides.from_dict(payload, profile=profile)
    else:
        with pytest.raises(FinanceTripletError):
            merge_effective(profile, FinanceOverrides.from_dict(payload))


@pytest.mark.parametrize("bound", [False, True], ids=["unbound", "bound"])
def test_600_net_cashflow_source_share_cannot_change_base_from(bound):
    profile = FinanceProfile.from_dict(_valid_profile_dict())
    payload = {
        "schema": "ies.finance-overrides", "schema_version": "6.0.0",
        "profile_ref": {"id": profile.profile_id},
        "net_cashflows": {"salvage_recovery": {"amount": {
            "ratio": {"value": "0.08", "unit": "1"},
            "base_from": {"kind": "maintenance_expense_net", "year": 5, "position": 1},
        }}},
    }
    if bound:
        with pytest.raises(FinanceTripletError):
            FinanceOverrides.from_dict(payload, profile=profile)
    else:
        with pytest.raises(FinanceTripletError):
            merge_effective(profile, FinanceOverrides.from_dict(payload))


def test_600_net_cashflows_checked_without_tax_rules():
    """无税则也必须核对净现金的合法时点及评价维护清单来源。"""
    payload = _valid_profile_dict()
    payload["tax_rules"] = {}
    payload["credit_pools"] = {}
    payload["net_cashflows"] = {
        "replacement": {
            "at": {"kind": "financial_year", "year": 3},
            "direction": "inflow",
            "amount": {"kind": "source_share", "ratio": {"value": "0.5", "unit": "1"},
                       "base_from": {"kind": "maintenance_expense_net", "year": 3, "position": 1}},
        },
    }
    evaluation = EvaluationInput(
        evaluation_years=5, construction_period=1, operation_years=4,
        discount_rate=0.05, construction_payments=(),
        maintenance_expenses=(MaintenanceExpense(year=3, currency="CNY", amount="100", terms=()),),
    )
    _check_tax_evaluation_refs(evaluation, merge_effective(FinanceProfile.from_dict(payload), None))

    payload["net_cashflows"]["replacement"]["at"]["year"] = 999
    with pytest.raises(InvalidRequestError) as exc:
        _check_tax_evaluation_refs(evaluation, merge_effective(FinanceProfile.from_dict(payload), None))
    assert exc.value.code == "TASK-REQ-015"
    assert exc.value.params["cashflow_id"] == "replacement"
    assert exc.value.params["year"] == 999

    payload["net_cashflows"]["replacement"]["at"]["year"] = 3
    payload["net_cashflows"]["replacement"]["amount"]["base_from"]["position"] = 99
    with pytest.raises(InvalidRequestError) as exc:
        _check_tax_evaluation_refs(evaluation, merge_effective(FinanceProfile.from_dict(payload), None))
    assert exc.value.code == "TASK-REQ-015"
    assert exc.value.params["cashflow_id"] == "replacement"
    assert exc.value.params["position"] == 99


def test_600_maintenance_capitalization_conflicts_across_income_rules():
    """A 规则折旧与 B 规则当期费用扣除按全局维护身份冲突。"""
    payload = _valid_profile_dict()
    from copy import deepcopy

    second = deepcopy(payload["tax_rules"]["cit_rule"])
    second.update(revenue=[], energy_cost=[], annual_om=[], variable_om=[], depreciation=[])
    second["deductible_maintenance"] = {
        "mode": "explicit_items", "items": [{"year": 5, "position": 1, "deductible": True}],
    }
    payload["tax_rules"]["cit_rule_b"] = second
    effective = merge_effective(FinanceProfile.from_dict(payload), None)
    evaluation = EvaluationInput(
        evaluation_years=20, construction_period=1, operation_years=19,
        discount_rate=0.05, construction_payments=(),
        maintenance_expenses=(MaintenanceExpense(year=5, currency="CNY", amount="100", terms=()),),
    )
    with pytest.raises(InvalidRequestError) as exc:
        _check_tax_evaluation_refs(evaluation, effective)
    assert exc.value.code == "TASK-REQ-015"
    assert exc.value.params["rule"] == "cit_rule"
    assert exc.value.params["deductible_rule"] == "cit_rule_b"
    assert exc.value.params["year"] == 5
    assert exc.value.params["position"] == 1


def test_600_vat_subcomponent_reference_and_overlap():
    """5. VAT 对成本分量及明确子项的引用，按实际覆盖范围识别并拒绝重叠。"""
    p = _valid_profile_dict()
    # 正常：op_vat 的 om_input_tax 分别引用 fixed 和 linear.capacity_kw，两者不重叠
    profile = FinanceProfile.from_dict(p)
    assert len(profile.tax_rules["op_vat"].om_input_tax) == 2

    # 重叠测试 1：已有整项引用 pv_system.annual_fixed_om，又追加引用固定子项 pv_system.annual_fixed_om.fixed
    p_overlap = _valid_profile_dict()
    p_overlap["tax_rules"]["op_vat"]["om_input_tax"] = [
        {
            "for": "pv_system.annual_fixed_om",
            "rate": {"value": "0.06", "unit": "1"},
            "deductible_ratio": {"value": "1", "unit": "1"},
        },
        {
            "for": "pv_system.annual_fixed_om.fixed",
            "rate": {"value": "0.06", "unit": "1"},
            "deductible_ratio": {"value": "1", "unit": "1"},
        },
    ]
    with pytest.raises(FinanceTripletError, match="重复计算同一税基"):
        FinanceProfile.from_dict(p_overlap)

    # 重叠测试 2：建设进项已引用 upfront_capex.linear.capacity_kw，另一规则再次引用 upfront_capex 整项
    p_con_overlap = _valid_profile_dict()
    p_con_overlap["tax_rules"]["build_vat_2"] = {
        "kind": "construction_input_vat",
        "label_ref": "vat_label",
        "for": "pv_system.upfront_capex",
        "rate": {"value": "0.09", "unit": "1"},
        "deductible_ratio": {"value": "1", "unit": "1"},
        "credit_pool": "main_pool",
        "confirmation": {"method": "payment_period"},
    }
    with pytest.raises(FinanceTripletError, match="重复计算同一税基"):
        FinanceProfile.from_dict(p_con_overlap)


def test_600_yaml_roundtrip_lossless():
    """7. 6.0.0 序列化与 YAML 往返无损。"""
    p = _valid_profile_dict()
    profile = FinanceProfile.from_dict(p)
    effective = merge_effective(profile, None)
    text = effective_to_yaml(effective)
    assert 'ies.effective-finance-config' in text
    assert '6.0.0' in text
    assert 'net_cashflows' in text

    restored = effective_from_yaml(text)
    assert restored.to_dict() == effective.to_dict()


def test_600_task_submission_gate_and_period_checks():
    """6 & 8. 任务提交边界跨输入检查与未接通能力门禁。"""
    p = _valid_profile_dict()
    profile = FinanceProfile.from_dict(p)
    effective = merge_effective(profile, None)

    # 1. 维护项不存在
    eval_missing = EvaluationInput(
        evaluation_years=20,
        construction_period=1,
        operation_years=19,
        discount_rate=0.05,
        construction_payments=(),
        maintenance_expenses=(),  # 空维护清单，引用的 (5, 1) 不存在
    )
    with pytest.raises(InvalidRequestError) as exc_info:
        _check_tax_evaluation_refs(eval_missing, effective)
    assert exc_info.value.code == "TASK-REQ-015"
    assert "维护项在本次评价清单中不存在" in str(exc_info.value)

    # 2. 期间错误：换新发生在评价年 8，但折旧首年设为运营年 6（对应评价年 1 + 6 = 7 <= 8）
    eval_period = EvaluationInput(
        evaluation_years=20,
        construction_period=1,
        operation_years=19,
        discount_rate=0.05,
        construction_payments=(),
        maintenance_expenses=(
            MaintenanceExpense(year=8, currency="CNY", amount="50000", terms=()),
        ),
    )
    p_bad_period = _valid_profile_dict()
    # 引用 (8, 1)，但 first_year=6，K=1 -> 计提评价年 7 <= 8 发生年
    p_bad_period["tax_rules"]["cit_rule"]["depreciation"][1]["base_from"]["year"] = 8
    p_bad_period["tax_rules"]["cit_rule"]["depreciation"][1]["first_year"] = 6
    eff_bad_period = merge_effective(FinanceProfile.from_dict(p_bad_period), None)

    with pytest.raises(InvalidRequestError) as exc_info:
        _check_tax_evaluation_refs(eval_period, eff_bad_period)
    assert exc_info.value.code == "TASK-REQ-015"
    assert "换新折旧首年须晚于维护支付发生年" in str(exc_info.value)

    # 3. 扣除冲突：(5, 1) 既声明为资本化折旧来源，又声明 deductible: true
    eval_valid = EvaluationInput(
        evaluation_years=20,
        construction_period=1,
        operation_years=19,
        discount_rate=0.05,
        construction_payments=(),
        maintenance_expenses=(
            MaintenanceExpense(year=5, currency="CNY", amount="50000", terms=()),
        ),
    )
    p_conflict = _valid_profile_dict()
    p_conflict["tax_rules"]["cit_rule"]["deductible_maintenance"]["items"] = [
        {"year": 5, "position": 1, "deductible": True}
    ]
    eff_conflict = merge_effective(FinanceProfile.from_dict(p_conflict), None)

    with pytest.raises(InvalidRequestError) as exc_info:
        _check_tax_evaluation_refs(eval_valid, eff_conflict)
    assert exc_info.value.code == "TASK-REQ-015"
    assert "不得同时声明为当期费用扣除" in str(exc_info.value)

    # 4. 未接通生成器新能力的公开拒收（TASK-REQ-007）
    base_caps = (
        "finance.vat",
        "finance.construction_input_vat",
        "finance.income_tax",
        "finance.credit_pools",
        "finance.financial_facts",
    )
    with pytest.raises(InvalidRequestError) as exc_info:
        _check_tax_capabilities("ies.generator.highs_milp@7.1.0", base_caps, effective)
    assert exc_info.value.code == "TASK-REQ-007"
    missing = exc_info.value.params["missing_capabilities"]
    assert "finance.net_cashflows" in missing
    assert "finance.affine_annual_om" in missing
    assert "finance.maintenance_depreciation" in missing
    assert "finance.ratio_residual" in missing
    assert "finance.subcomponent_vat" in missing
