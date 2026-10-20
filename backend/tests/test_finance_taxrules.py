"""3.0.0 税则/池规则契约测试：正式示例与合成三件套的真实解析/合并/往返。

全部经生产 API（FinanceProfile.from_dict / FinanceOverrides.from_dict /
merge_effective / effective_to_yaml / effective_from_yaml），不用
yaml.safe_load 自检代替。预期独立手算。纯函数契约测试，无数据库/HTTP。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from iesplan.application.tasks.submissions import _reject_unsupported_tax_rules
from iesplan.finance import (
    FinanceOverrides,
    FinanceProfile,
    FinanceTripletError,
    merge_effective,
)
from iesplan.finance.yaml_io import effective_from_yaml, effective_to_yaml
from iesplan.tasks.contracts import InvalidRequestError

# ---------------------------------------------------------------------------
# 合成基座（与 finance-yaml.md 完整示例对齐的子集）
# ---------------------------------------------------------------------------


def _base_profile() -> dict:
    return {
        "schema": "ies.finance-profile",
        "schema_version": "3.0.0",
        "profile": {
            "id": "cn-north-demo",
            "region": "CN-North",
            "currency": "CNY",
            "base_year": 2025,
            "price_basis": "tax_inclusive",
            "cost_method": "fixed_plus_linear",
        },
        "finance_types": {
            "pv_system": {
                "upfront_capex": {
                    "fixed": {"value": "0", "unit": "CNY"},
                    "linear": {"capacity_kw": {"unit_cost": {"value": "3500", "unit": "CNY/kW"}}},
                },
                "annual_fixed_om": {
                    "linear": {"capacity_kw": {"unit_cost": {"value": "35", "unit": "CNY/kW/a"}}},
                },
                "period_variable_om": {
                    "linear": {"generated_kwh": {"unit_cost": {"value": "0.01", "unit": "CNY/kWh"}}},
                },
            },
        },
        "energy_prices": {
            "grid_import": {
                "carrier": "electricity",
                "direction": "purchase",
                "kind": "constant",
                "value": {"value": "0.7", "unit": "CNY/kWh"},
            },
            "pv_export": {
                "carrier": "electricity",
                "direction": "sale",
                "kind": "constant",
                "value": {"value": "0.4", "unit": "CNY/kWh"},
            },
        },
        "taxes": {
            "vat_label": {
                "display_name": "合成增值税",
                "tax_type": "value_added_tax",
                "applies_to": "grid_import",
            },
            "income_label": {
                "display_name": "合成所得税",
                "tax_type": "income_tax",
                "applies_to": "pv_export",
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
            "operating_vat": {
                "kind": "vat",
                "label_ref": "vat_label",
                "settlement": "monthly",
                "in_tax": [
                    {
                        "price": "grid_import",
                        "rate": {"value": "0.13", "unit": "1"},
                        "deductible_ratio": {"value": "1", "unit": "1"},
                    },
                ],
                "out_tax": [
                    {"price": "pv_export", "rate": {"value": "0.13", "unit": "1"}},
                ],
                "om_input_tax": [
                    {
                        "for": "pv_system.annual_fixed_om",
                        "rate": {"value": "0.06", "unit": "1"},
                        "deductible_ratio": {"value": "1", "unit": "1"},
                    },
                    {
                        "for": "pv_system.period_variable_om",
                        "rate": {"value": "0.06", "unit": "1"},
                        "deductible_ratio": {"value": "0.5", "unit": "1"},
                    },
                ],
                "maintenance_input_tax": [],
                "credit_pool": "main_pool",
                "confirmation": "same_period",
            },
            "building_vat": {
                "kind": "construction_input_vat",
                "label_ref": "vat_label",
                "for": "pv_system.upfront_capex",
                "rate": {"value": "0.09", "unit": "1"},
                "deductible_ratio": {"value": "0.8", "unit": "1"},
                "credit_pool": "main_pool",
                "confirmation": {"method": "payment_period"},
            },
            "income": {
                "kind": "income_tax",
                "label_ref": "income_label",
                "rate": {"value": "0.25", "unit": "1"},
                "revenue": ["pv_export"],
                "energy_cost": ["grid_import"],
                "annual_om": ["pv_system.annual_fixed_om"],
                "variable_om": ["pv_system.period_variable_om"],
                "deductible_maintenance": {"mode": "explicit_items", "items": []},
                "depreciation": [
                    {
                        "asset": "pv_system",
                        "base_from": "pv_system.upfront_capex_net",
                        "method": "straight_line",
                        "life_years": 20,
                        "residual": {"value": "0", "unit": "CNY"},
                        "first_year": 1,
                    },
                ],
                "loss_carryforward_years": 5,
                "opening_losses": [],
            },
        },
    }


def _base_overrides(profile: FinanceProfile) -> dict:
    return {
        "schema": "ies.finance-overrides",
        "schema_version": "3.0.0",
        "profile_ref": {"id": profile.profile_id},
        "tax_rules": {
            "operating_vat": {
                "in_tax": {"grid_import": {"rate": {"value": "0.15", "unit": "1"}}},
                "maintenance_input_tax": [
                    {
                        "year": 2,
                        "position": 1,
                        "rate": {"value": "0.13", "unit": "1"},
                        "deductible_ratio": {"value": "0", "unit": "1"},
                    },
                    {
                        "year": 2,
                        "position": 2,
                        "rate": {"value": "0.13", "unit": "1"},
                        "deductible_ratio": {"value": "1", "unit": "1"},
                    },
                ],
            },
            "income": {
                "deductible_maintenance": {
                    "mode": "explicit_items",
                    "items": [
                        {"year": 2, "position": 1, "deductible": True},
                        {"year": 2, "position": 2, "deductible": False},
                    ],
                },
                "opening_losses": [
                    {"amount": {"value": "30000", "unit": "CNY"}, "last_usable_year": 6},
                ],
            },
        },
        "credit_pools": {
            "main_pool": {
                "opening_credit": {"value": "90000", "unit": "CNY"},
                "refunds": [
                    {"period": 14, "amount": {"value": "1500", "unit": "CNY"}, "arrival_period": 15},
                ],
            },
        },
    }


# ---------------------------------------------------------------------------
# ① 正式示例与合成三件套解析/合并/往返
# ---------------------------------------------------------------------------


def test_profile_with_full_rules_parses() -> None:
    profile = FinanceProfile.from_dict(_base_profile())
    assert set(profile.tax_rules) == {"operating_vat", "building_vat", "income"}
    assert set(profile.credit_pools) == {"main_pool"}
    vat = profile.tax_rules["operating_vat"]
    assert vat.in_tax[0].rate.value == Decimal("0.13")  # type: ignore[union-attr]
    assert profile.tax_rules["building_vat"].deductible_ratio.value == Decimal("0.8")  # type: ignore[union-attr]


def test_merge_applies_sparse_overrides_and_roundtrips() -> None:
    profile = FinanceProfile.from_dict(_base_profile())
    overrides = FinanceOverrides.from_dict(_base_overrides(profile), profile=profile)
    effective = merge_effective(profile, overrides)
    vat = effective.tax_rules["operating_vat"]
    assert vat.in_tax[0].rate.value == Decimal("0.15")  # type: ignore[union-attr]
    assert vat.in_tax[0].deductible_ratio.value == Decimal("1")  # type: ignore[union-attr]
    pool = effective.credit_pools["main_pool"]
    assert pool.opening_credit.value == Decimal("90000")
    assert [(r.period, r.amount.value, r.arrival_period) for r in pool.refunds] == [(14, Decimal("1500"), 15)]
    income = effective.tax_rules["income"]
    assert [
        (i.year, i.position, i.deductible)  # type: ignore[union-attr]
        for i in income.deductible_maintenance.items
    ] == [(2, 1, True), (2, 2, False)]
    assert [
        (o.amount.value, o.last_usable_year)  # type: ignore[union-attr]
        for o in income.opening_losses
    ] == [(Decimal("30000"), 6)]
    # 税目/未覆盖原样保留
    assert set(effective.taxes) == {"vat_label", "income_label"}
    # YAML 序列化往返相等
    text = effective_to_yaml(effective)
    assert text.endswith("\n")
    assert effective_from_yaml(text).to_dict() == effective.to_dict()


def test_merge_does_not_mutate_inputs() -> None:
    profile = FinanceProfile.from_dict(_base_profile())
    overrides = FinanceOverrides.from_dict(_base_overrides(profile), profile=profile)
    before_profile = profile.to_dict()
    before_overrides = overrides.to_dict()
    merge_effective(profile, overrides)
    assert profile.to_dict() == before_profile
    assert overrides.to_dict() == before_overrides


def test_empty_tables_explicit_and_merge_passthrough() -> None:
    payload = _base_profile()
    payload["credit_pools"] = {}
    payload["tax_rules"] = {}
    profile = FinanceProfile.from_dict(payload)
    assert profile.credit_pools == {}
    assert profile.tax_rules == {}
    effective = merge_effective(profile, None)
    assert effective.credit_pools == {}
    assert effective.tax_rules == {}


# ---------------------------------------------------------------------------
# ③ 同池多率/部分抵扣/两方法/单一所有者/维护独立
# ---------------------------------------------------------------------------


def test_multirate_same_pool_and_partial_construction() -> None:
    payload = _base_profile()
    payload["energy_prices"]["heat_supply"] = {
        "carrier": "heat",
        "direction": "purchase",
        "kind": "constant",
        "value": {"value": "0.35", "unit": "CNY/kWh"},
    }
    payload["tax_rules"]["operating_vat"]["in_tax"].append(
        {
            "price": "heat_supply",
            "rate": {"value": "0.09", "unit": "1"},
            "deductible_ratio": {"value": "1", "unit": "1"},
        }
    )
    profile = FinanceProfile.from_dict(payload)
    vat = profile.tax_rules["operating_vat"]
    assert {e.price: e.rate.value for e in vat.in_tax} == {  # type: ignore[union-attr]
        "grid_import": Decimal("0.13"),
        "heat_supply": Decimal("0.09"),
    }
    assert all(
        e.credit_pool == "main_pool"  # type: ignore[union-attr]
        for e in (vat, profile.tax_rules["building_vat"])
    )


def test_explicit_schedule_method_and_replace() -> None:
    payload = _base_profile()
    payload["tax_rules"]["building_vat"]["confirmation"] = {
        "method": "explicit_schedule",
        "schedule": [{"period": 1, "ratio": "0.6"}, {"period": 13, "ratio": "0.4"}],
    }
    profile = FinanceProfile.from_dict(payload)
    confirm = profile.tax_rules["building_vat"].confirmation  # type: ignore[union-attr]
    assert confirm.method == "explicit_schedule"
    assert [(e.period, e.ratio) for e in confirm.schedule] == [(1, Decimal("0.6")), (13, Decimal("0.4"))]
    overrides = FinanceOverrides.from_dict(
        {
            "schema": "ies.finance-overrides",
            "schema_version": "3.0.0",
            "profile_ref": {"id": profile.profile_id},
            "tax_rules": {"building_vat": {"confirmation": {"schedule": [{"period": 2, "ratio": "1"}]}}},
        },
        profile=profile,
    )
    effective = merge_effective(profile, overrides)
    confirm = effective.tax_rules["building_vat"].confirmation  # type: ignore[union-attr]
    assert confirm.method == "explicit_schedule"
    assert [(e.period, e.ratio) for e in confirm.schedule] == [(2, Decimal("1"))]


def test_rule_may_not_carry_pool_facts() -> None:
    payload = _base_profile()
    payload["tax_rules"]["operating_vat"]["opening_credit"] = {"value": "1", "unit": "CNY"}
    with pytest.raises(FinanceTripletError):
        FinanceProfile.from_dict(payload)


def test_same_year_maintenance_dimensions_independent() -> None:
    profile = FinanceProfile.from_dict(_base_profile())
    effective = merge_effective(
        profile, FinanceOverrides.from_dict(_base_overrides(profile), profile=profile)
    )
    vat_items = effective.tax_rules["operating_vat"].maintenance_input_tax  # type: ignore[union-attr]
    income_items = effective.tax_rules["income"].deductible_maintenance.items  # type: ignore[union-attr]
    # 第 1 笔所得税可扣但 VAT 比例 0；第 2 笔所得税不可扣但 VAT 全额可抵。
    assert [(i.deductible_ratio.value) for i in vat_items] == [Decimal("0"), Decimal("1")]
    assert [(i.deductible) for i in income_items] == [True, False]


# ---------------------------------------------------------------------------
# ⑤ 越权/未知/旧版/不完整拒绝
# ---------------------------------------------------------------------------


def test_old_versions_rejected() -> None:
    for version in ("1.0.0", "2.0.0"):
        payload = _base_profile()
        payload["schema_version"] = version
        with pytest.raises(FinanceTripletError):
            FinanceProfile.from_dict(payload)


def test_taxes_with_rate_rejected() -> None:
    payload = _base_profile()
    payload["taxes"]["vat_label"]["rate"] = {"value": "0.13", "unit": "1"}
    with pytest.raises(FinanceTripletError):
        FinanceProfile.from_dict(payload)


def test_missing_required_tax_keys_rejected() -> None:
    payload = _base_profile()
    del payload["credit_pools"]
    with pytest.raises(FinanceTripletError):
        FinanceProfile.from_dict(payload)


def test_override_method_scope_pool_changes_rejected() -> None:
    profile = FinanceProfile.from_dict(_base_profile())
    base = {
        "schema": "ies.finance-overrides",
        "schema_version": "3.0.0",
        "profile_ref": {"id": profile.profile_id},
    }
    cases = [
        {"tax_rules": {"building_vat": {"confirmation": {"method": "payment_period"}}}},
        {"tax_rules": {"building_vat": {"for": "pv_system.annual_fixed_om"}}},
        {"tax_rules": {"building_vat": {"credit_pool": "main_pool", "kind": "vat"}}},
        {"tax_rules": {"new_rule": {"kind": "vat"}}},
        {"tax_rules": {"income": {"revenue": ["pv_export"]}}},
        {"tax_rules": {"income": {"depreciation": {"pv_system": {"method": "straight_line"}}}}},
        {"credit_pools": {"new_pool": {"opening_credit": {"value": "1", "unit": "CNY"}}}},
        {"credit_pools": {"main_pool": {"receivable_policy": "end_receivable"}}},
    ]
    for patch in cases:
        payload = dict(base)
        payload.update(patch)
        with pytest.raises(FinanceTripletError):
            FinanceOverrides.from_dict(payload, profile=profile)


def test_unknown_references_rejected() -> None:
    payload = _base_profile()
    payload["tax_rules"]["operating_vat"]["in_tax"][0]["price"] = "ghost_price"
    with pytest.raises(FinanceTripletError):
        FinanceProfile.from_dict(payload)
    payload = _base_profile()
    payload["tax_rules"]["operating_vat"]["label_ref"] = "ghost_label"
    with pytest.raises(FinanceTripletError):
        FinanceProfile.from_dict(payload)
    payload = _base_profile()
    payload["tax_rules"]["operating_vat"]["credit_pool"] = "ghost_pool"
    with pytest.raises(FinanceTripletError):
        FinanceProfile.from_dict(payload)


def test_duplicate_tax_base_rejected() -> None:
    payload = _base_profile()
    payload["tax_rules"]["second_vat"] = {
        "kind": "vat",
        "label_ref": "vat_label",
        "settlement": "monthly",
        "in_tax": [
            {
                "price": "grid_import",
                "rate": {"value": "0.13", "unit": "1"},
                "deductible_ratio": {"value": "1", "unit": "1"},
            }
        ],
        "out_tax": [],
        "om_input_tax": [],
        "maintenance_input_tax": [],
        "credit_pool": "main_pool",
        "confirmation": "same_period",
    }
    with pytest.raises(FinanceTripletError):
        FinanceProfile.from_dict(payload)


def test_bad_schedule_and_incomplete_atoms_rejected() -> None:
    payload = _base_profile()
    payload["tax_rules"]["building_vat"]["confirmation"] = {
        "method": "explicit_schedule",
        "schedule": [{"period": 1, "ratio": "0.6"}, {"period": 13, "ratio": "0.3"}],
    }
    with pytest.raises(FinanceTripletError):
        FinanceProfile.from_dict(payload)
    payload = _base_profile()
    payload["tax_rules"]["operating_vat"]["in_tax"][0]["rate"] = {"value": "0.13"}
    with pytest.raises(FinanceTripletError):
        FinanceProfile.from_dict(payload)


# ---------------------------------------------------------------------------
# ⑦ 提交边界：非空规则拒绝，空规则放行
# ---------------------------------------------------------------------------


def test_submission_gate_rejects_nonempty_rules() -> None:
    profile = FinanceProfile.from_dict(_base_profile())
    effective = merge_effective(profile, None)
    with pytest.raises(InvalidRequestError) as caught:
        _reject_unsupported_tax_rules("acme.generator@1.0.0", effective)
    assert caught.value.code == "TASK-REQ-007"
    assert sorted(caught.value.params["tax_rules"]) == ["building_vat", "income", "operating_vat"]


def test_submission_gate_passes_empty_rules() -> None:
    payload = _base_profile()
    payload["credit_pools"] = {}
    payload["tax_rules"] = {}
    effective = merge_effective(FinanceProfile.from_dict(payload), None)
    assert _reject_unsupported_tax_rules("acme.generator@1.0.0", effective) is None
