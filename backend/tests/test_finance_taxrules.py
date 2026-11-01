"""3.0.0 税则/池规则契约测试：正式示例与合成三件套的真实解析/合并/往返。

全部经生产 API（FinanceProfile.from_dict / FinanceOverrides.from_dict /
merge_effective / effective_to_yaml / effective_from_yaml），不用
yaml.safe_load 自检代替。预期独立手算。纯函数契约测试，无数据库/HTTP。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from iesplan.application.tasks.submissions import _check_tax_capabilities
from iesplan.finance import (
    EffectiveFinanceConfig,
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
        "schema_version": "1.0.0",
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
        "demand_charges": {},
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
                        "base_from": {"kind": "upfront_capex_net", "finance_type": "pv_system", "scope": {"kind": "all_new_instances"}},
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
        "net_cashflows": {},
    }


def _base_overrides(profile: FinanceProfile) -> dict:
    return {
        "schema": "ies.finance-overrides",
        "schema_version": "1.0.0",
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
            "schema_version": "1.0.0",
            "profile_ref": {"id": profile.profile_id},
            "tax_rules": {"building_vat": {"confirmation": {"schedule": [{"period": 2, "ratio": "1"}]}}},
        },
        profile=profile,
    )
    effective = merge_effective(profile, overrides)
    confirm = effective.tax_rules["building_vat"].confirmation  # type: ignore[union-attr]
    assert confirm.method == "explicit_schedule"
    assert [(e.period, e.ratio) for e in confirm.schedule] == [(2, Decimal("1"))]


def test_payment_period_schedule_override_rejected() -> None:
    """payment_period 规则出现 confirmation.schedule 即拒收，不静默丢弃。"""
    profile = FinanceProfile.from_dict(_base_profile())
    confirm = profile.tax_rules["building_vat"].confirmation  # type: ignore[union-attr]
    assert confirm.method == "payment_period"
    with pytest.raises(FinanceTripletError, match="schedule"):
        FinanceOverrides.from_dict(
            {
                "schema": "ies.finance-overrides",
                "schema_version": "1.0.0",
                "profile_ref": {"id": profile.profile_id},
                "tax_rules": {"building_vat": {"confirmation": {"schedule": [{"period": 2, "ratio": "1"}]}}},
            },
            profile=profile,
        )


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
    for version in ("0.0.0", "9.9.9"):
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
        "schema_version": "1.0.0",
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


def test_submission_gate_rejects_without_capabilities() -> None:
    """无税务能力的生成器 + 非空规则/池 → 007，列明缺失能力与字段。"""
    profile = FinanceProfile.from_dict(_base_profile())
    effective = merge_effective(profile, None)
    with pytest.raises(InvalidRequestError) as caught:
        _check_tax_capabilities("acme.generator@1.0.0", (), effective)
    assert caught.value.code == "TASK-REQ-007"
    assert caught.value.params["missing_capabilities"] == [
        "finance.construction_input_vat",
        "finance.credit_pools",
        "finance.financial_facts",
        "finance.income_tax",
        "finance.vat",
    ]
    assert caught.value.params["generator"] == "acme.generator@1.0.0"
    assert caught.value.params["fields"]


def test_submission_gate_passes_with_highs_capabilities() -> None:
    """HiGHS 3.4.0 描述符能力 + 非空规则/池 → 放行（不同名猜测）。"""
    from iesplan.computation.generators.highs_milp import DESCRIPTOR as HIGHS_DESCRIPTOR

    profile = FinanceProfile.from_dict(_base_profile())
    effective = merge_effective(profile, None)
    assert (
        _check_tax_capabilities(
            HIGHS_DESCRIPTOR.generator_id + "@" + HIGHS_DESCRIPTOR.version,
            tuple(HIGHS_DESCRIPTOR.capabilities),
            effective,
        )
        is None
    )


def test_submission_gate_passes_empty_rules() -> None:
    payload = _base_profile()
    payload["credit_pools"] = {}
    payload["tax_rules"] = {}
    effective = merge_effective(FinanceProfile.from_dict(payload), None)
    assert _check_tax_capabilities("acme.generator@1.0.0", (), effective) is None


# ---------------------------------------------------------------------------
# 正式 YAML 示例直读（生产解析/合并；手抄子集不能代替）
# ---------------------------------------------------------------------------


_MANUAL_PATH = Path("/manual/developer-guide/zh-CN/formats/finance-yaml.md")


def _manual_yaml_blocks() -> list[str]:
    assert _MANUAL_PATH.is_file(), "正式手册缺失：测试命令须把当前工作树 manual 只读挂载到 /manual，不降门禁"
    text = _MANUAL_PATH.read_text(encoding="utf-8")
    return [part.split("```", 1)[0] for part in text.split("```yaml")[1:]]


def test_manual_examples_parse_and_merge() -> None:
    """正式 finance-yaml.md 的 Profile 基座＋税则节＋逐项维护覆盖直读合并。

    覆盖侧 deductible_maintenance 仅 items（mode 继承），拒收即回归。
    """
    from iesplan.core.yamlmini import load as yaml_load

    blocks = _manual_yaml_blocks()
    profile_block = next(b for b in blocks if "schema: ies.finance-profile" in b)
    tax_block = next(b for b in blocks if b.lstrip().startswith("taxes:") and "main_pool" in b)
    maint_block = next(
        b for b in blocks if "schema: ies.finance-overrides" in b and "deductible_maintenance" in b
    )
    profile_doc = yaml_load(profile_block)
    assert isinstance(profile_doc, dict)
    tax_sections = yaml_load(tax_block)
    assert isinstance(tax_sections, dict)
    for key in ("taxes", "credit_pools", "tax_rules"):
        profile_doc[key] = tax_sections[key]
    profile = FinanceProfile.from_dict(profile_doc)
    assert set(profile.tax_rules) == {"operating_vat", "building_vat", "income"}
    overrides_doc = yaml_load(maint_block)
    assert isinstance(overrides_doc, dict)
    overrides = FinanceOverrides.from_dict(overrides_doc, profile=profile)
    effective = merge_effective(profile, overrides)
    income = effective.tax_rules["income"]
    assert income.deductible_maintenance.mode == "explicit_items"  # type: ignore[union-attr]
    assert [
        (i.year, i.position, i.deductible)  # type: ignore[union-attr]
        for i in income.deductible_maintenance.items
    ] == [(2, 1, True), (2, 2, False)]
    vat_items = effective.tax_rules["operating_vat"].maintenance_input_tax  # type: ignore[union-attr]
    assert [(i.year, i.position, i.deductible_ratio.value) for i in vat_items] == [
        (2, 1, Decimal("0")),
        (2, 2, Decimal("1")),
    ]


# ---------------------------------------------------------------------------
# 无 Profile 解析 → 合并：合法放行，禁改范围拦截
# ---------------------------------------------------------------------------


def test_unbound_overrides_legal_path_merges() -> None:
    profile = FinanceProfile.from_dict(_base_profile())
    payload = _base_overrides(profile)
    standalone = FinanceOverrides.from_dict(payload)
    assert standalone._bound_profile == ""
    effective = merge_effective(profile, standalone)
    assert effective.tax_rules["operating_vat"].in_tax[0].rate.value == Decimal("0.15")  # type: ignore[union-attr]


def test_unbound_overrides_forbidden_scope_rejected_at_merge() -> None:
    profile = FinanceProfile.from_dict(_base_profile())
    payload = _base_overrides(profile)
    payload["tax_rules"]["income"]["revenue"] = []
    standalone = FinanceOverrides.from_dict(payload)
    with pytest.raises(FinanceTripletError):
        merge_effective(profile, standalone)


# ---------------------------------------------------------------------------
# 不可变性：输入别名切断，to_dict 回写无效
# ---------------------------------------------------------------------------


def test_overrides_deeply_immutable() -> None:
    profile = FinanceProfile.from_dict(_base_profile())
    payload = _base_overrides(profile)
    overrides = FinanceOverrides.from_dict(payload, profile=profile)
    from collections.abc import Mapping

    rule_view = overrides.tax_rules["income"]
    assert isinstance(rule_view, Mapping) and not isinstance(rule_view, dict)
    with pytest.raises(TypeError):
        overrides.tax_rules["income"] = {}  # type: ignore[index]
    with pytest.raises(AttributeError):
        overrides.credit_pools["main_pool"]["refunds"].append({})  # type: ignore[attr-defined]
    # 调用方改输入：对象不受影响
    payload["tax_rules"]["income"]["rate"] = {"value": "0.99", "unit": "1"}
    payload["credit_pools"]["main_pool"]["refunds"].append(
        {"period": 99, "amount": {"value": "1", "unit": "CNY"}, "arrival_period": 99}
    )
    assert overrides.to_dict()["tax_rules"]["income"].get("rate") is None
    assert len(overrides.to_dict()["credit_pools"]["main_pool"]["refunds"]) == 1
    # 改 to_dict 副本：对象不受影响
    snapshot = overrides.to_dict()
    snapshot["tax_rules"]["income"]["rate"] = {"value": "0.99", "unit": "1"}
    assert overrides.to_dict()["tax_rules"]["income"].get("rate") is None


# ---------------------------------------------------------------------------
# 显式 null 非法，缺省合法
# ---------------------------------------------------------------------------


def test_explicit_null_tax_sections_rejected() -> None:
    payload = _base_profile()
    payload["tax_rules"] = None  # type: ignore[assignment]
    with pytest.raises(FinanceTripletError):
        FinanceProfile.from_dict(payload)
    payload = _base_profile()
    payload["credit_pools"] = None  # type: ignore[assignment]
    with pytest.raises(FinanceTripletError):
        FinanceProfile.from_dict(payload)
    profile = FinanceProfile.from_dict(_base_profile())
    bad = _base_overrides(profile)
    bad["tax_rules"] = None  # type: ignore[assignment]
    with pytest.raises(FinanceTripletError):
        FinanceOverrides.from_dict(bad, profile=profile)
    effective_doc = merge_effective(profile, None).to_dict()
    effective_doc["tax_rules"] = None
    with pytest.raises(FinanceTripletError):
        EffectiveFinanceConfig.from_dict(effective_doc)


def test_profile_missing_each_tax_section_rejected() -> None:
    """Profile 的 tax_rules/credit_pools 必填（空表必须显式 {}）。

    其余节置空，拒绝必须来自缺节本身，不能是未知池引用的误报。
    """
    for key in ("tax_rules", "credit_pools"):
        payload = _base_profile()
        payload["taxes"] = {}
        payload["credit_pools"] = {}
        payload["tax_rules"] = {}
        del payload[key]
        with pytest.raises(FinanceTripletError, match=key):
            FinanceProfile.from_dict(payload)


def test_profile_explicit_empty_tax_sections_accepted() -> None:
    payload = _base_profile()
    payload["taxes"] = {}
    payload["credit_pools"] = {}
    payload["tax_rules"] = {}
    profile = FinanceProfile.from_dict(payload)
    assert profile.tax_rules == {}
    assert profile.credit_pools == {}


def test_effective_missing_each_tax_section_rejected() -> None:
    base = _base_profile()
    base["taxes"] = {}
    base["credit_pools"] = {}
    base["tax_rules"] = {}
    profile = FinanceProfile.from_dict(base)
    doc = merge_effective(profile, None).to_dict()
    for key in ("tax_rules", "credit_pools"):
        bad = dict(doc)
        del bad[key]
        with pytest.raises(FinanceTripletError, match=key):
            EffectiveFinanceConfig.from_dict(bad)


# ---------------------------------------------------------------------------
# 同类重复扣计与引用唯一性最小反例
# ---------------------------------------------------------------------------


def test_duplicate_annual_om_rejected() -> None:
    payload = _base_profile()
    payload["tax_rules"]["income"]["annual_om"] = ["pv_system.annual_fixed_om", "pv_system.annual_fixed_om"]
    with pytest.raises(FinanceTripletError):
        FinanceProfile.from_dict(payload)


def test_duplicate_maint_and_asset_references_rejected() -> None:
    payload = _base_profile()
    payload["tax_rules"]["income"]["deductible_maintenance"] = {
        "mode": "explicit_items",
        "items": [
            {"year": 2, "position": 1, "deductible": True},
            {"year": 2, "position": 1, "deductible": False},
        ],
    }
    with pytest.raises(FinanceTripletError):
        FinanceProfile.from_dict(payload)
    payload = _base_profile()
    payload["tax_rules"]["income"]["depreciation"] = [
        {
            "base_from": {"kind": "upfront_capex_net", "finance_type": "pv_system", "scope": {"kind": "all_new_instances"}},
            "method": "straight_line",
            "life_years": 20,
            "residual": {"value": "0", "unit": "CNY"},
            "first_year": 1,
        },
        {
            "base_from": {"kind": "upfront_capex_net", "finance_type": "pv_system", "scope": {"kind": "all_new_instances"}},
            "method": "straight_line",
            "life_years": 10,
            "residual": {"value": "0", "unit": "CNY"},
            "first_year": 1,
        },
    ]
    with pytest.raises(FinanceTripletError, match="来源身份重复"):
        FinanceProfile.from_dict(payload)
    payload = _base_profile()
    payload["tax_rules"]["operating_vat"]["maintenance_input_tax"] = [
        {
            "year": 2,
            "position": 1,
            "rate": {"value": "0.13", "unit": "1"},
            "deductible_ratio": {"value": "1", "unit": "1"},
        },
        {
            "year": 2,
            "position": 1,
            "rate": {"value": "0.13", "unit": "1"},
            "deductible_ratio": {"value": "0", "unit": "1"},
        },
    ]
    with pytest.raises(FinanceTripletError):
        FinanceProfile.from_dict(payload)
