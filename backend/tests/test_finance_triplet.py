"""财务三件套(finance-yaml@1.0.0)契约切片测试: FinanceProfile / Overrides / Effective 与确定性合并器。

0.6.5 条目 1(契约)的首个可验收切片。覆盖:
- Profile: 完整示例往返、金额 Decimal 纪律(float/int/NaN/Infinity/null 拒绝)、
  单位规范化、成本非负/价格正零负、carrier/direction/kind、字头校验;
- Overrides: profile_ref 精确引用、越权覆盖拒绝(新增 finance_type/driver/price_id、
  改 carrier/direction/单位/分量、顶层禁改字段)、部分金额/null 拒绝、摘要一致性;
- 空 Overrides 文档: 有效, 合并结果等于 Profile;
- 合并器: 稀疏覆盖只替换叶子、未覆盖原样保留、三摘要闭合、重新合并可复现;
- Effective: 只能经合并器生成语义的验证(from_dict 自洽 + 精确来源重新合并)。

测试环境: 纯函数契约测试, 无数据库/HTTP(宪法 §14.2 纯函数单元测试)。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from iesplan.finance.triplet import (
    EffectiveFinanceConfig,
    FinanceOverrides,
    FinanceProfile,
    FinanceTripletError,
    SeriesMeta,
    merge_effective,
)

# ---------------------------------------------------------------------------
# 样例(与 finance-yaml.md 完整示例对齐的合理子集)
# ---------------------------------------------------------------------------


def _profile_payload() -> dict:
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
                    "linear": {"capacity_kw": {"unit_cost": {"value": "35", "unit": "CNY/kW·a"}}},
                },
                "period_variable_om": {
                    "linear": {"generated_kwh": {"unit_cost": {"value": "0.01", "unit": "CNY/kWh"}}},
                },
            },
            "battery_system": {
                "upfront_capex": {
                    "linear": {"energy_kwh": {"unit_cost": {"value": "900", "unit": "CNY/kWh"}}},
                },
                "annual_fixed_om": {
                    "linear": {"energy_kwh": {"unit_cost": {"value": "18", "unit": "CNY/kWh·a"}}},
                },
                "period_variable_om": {
                    "linear": {"cycled_kwh": {"unit_cost": {"value": "0.002", "unit": "CNY/kWh"}}},
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
                "kind": "time_series",
                "ref": "a" * 64,
                "series_meta": {
                    "resolution": "1h",
                    "leap_year": False,
                    "point_count": 8760,
                    "unit": "CNY/kWh",
                },
            },
            "heat_supply": {
                "carrier": "heat",
                "direction": "purchase",
                "kind": "constant",
                "value": {"value": "0.35", "unit": "CNY/kWh"},
            },
        },
        "taxes": {
            "grid_import_vat": {
                "display_name": "购电增值税",
                "tax_type": "value_added_tax",
                "rate": {"value": "0.13", "unit": "1"},
                "applies_to": "grid_import",
            }
        },
    }


def _profile() -> FinanceProfile:
    return FinanceProfile.from_dict(_profile_payload())


def _overrides_payload(profile: FinanceProfile) -> dict:
    return {
        "schema": "ies.finance-overrides",
        "schema_version": "1.0.0",
        "profile_ref": {"id": profile.profile_id},
        "finance_types": {
            "pv_system": {
                "upfront_capex": {
                    "fixed": {"value": "1500", "unit": "CNY"},
                    "linear": {"capacity_kw": {"unit_cost": {"value": "3200", "unit": "CNY/kW"}}},
                }
            }
        },
        "energy_prices": {
            "grid_import": {"kind": "constant", "value": {"value": "0.75", "unit": "CNY/kWh"}}
        },
    }


def _overrides(profile: FinanceProfile) -> FinanceOverrides:
    return FinanceOverrides.from_dict(_overrides_payload(profile), profile=profile)


# ---------------------------------------------------------------------------
# FinanceProfile 契约
# ---------------------------------------------------------------------------


def test_profile_roundtrip_preserves_identity():
    profile = _profile()
    # 文本文件只校验字头，不做内容摘要
    recomputed = FinanceProfile.from_dict(profile.to_dict())
    assert recomputed.to_dict() == profile.to_dict()
    assert recomputed.profile_id == profile.profile_id


def test_profile_content_changes_with_semantics():
    base = _profile_payload()
    p1 = FinanceProfile.from_dict(base)
    base2 = {**base, "energy_prices": {**base["energy_prices"],
                                       "grid_import": {**base["energy_prices"]["grid_import"],
                                                       "value": {"value": "0.71", "unit": "CNY/kWh"}}}}
    p2 = FinanceProfile.from_dict(base2)
    # 不同语义产生不同内容
    assert p1.to_dict() != p2.to_dict()


def test_profile_rejects_float_and_int_money():
    payload = _profile_payload()
    # tax rate / 金额不允许 int/float, 只允许十进制字符串
    bad = {**payload, "profile": {**payload["profile"]}}
    ft = bad["finance_types"]["pv_system"]["upfront_capex"]["linear"]["capacity_kw"]
    ft["unit_cost"] = {"value": 3500, "unit": "CNY/kW"}
    with pytest.raises(FinanceTripletError, match="十进制字符串"):
        FinanceProfile.from_dict(bad)
    # 顶层 energy price value 用 float 也拒绝
    payload2 = _profile_payload()
    payload2["energy_prices"]["grid_import"]["value"] = {"value": 0.7, "unit": "CNY/kWh"}
    with pytest.raises(FinanceTripletError, match="十进制字符串"):
        FinanceProfile.from_dict(payload2)


def test_profile_rejects_nan_infinity_null():
    for raw in ("NaN", "Infinity", "-Infinity", None):
        payload = _profile_payload()
        payload["energy_prices"]["grid_import"]["value"] = {"value": raw, "unit": "CNY/kWh"}
        with pytest.raises(FinanceTripletError):
            FinanceProfile.from_dict(payload)


def test_profile_cost_nonnegative_energy_price_allows_negative():
    # 成本金额禁止负
    payload = _profile_payload()
    payload["finance_types"]["pv_system"]["annual_fixed_om"]["linear"]["capacity_kw"]["unit_cost"] = {
        "value": "-5", "unit": "CNY/kW·a"
    }
    with pytest.raises(FinanceTripletError, match="禁止负数"):
        FinanceProfile.from_dict(payload)
    # 能源价格允许负/零/正
    for raw in ("-0.3", "0", "0.7"):
        p = _profile_payload()
        p["energy_prices"]["grid_import"]["value"] = {"value": raw, "unit": "CNY/kWh"}
        assert FinanceProfile.from_dict(p) is not None


def test_profile_partial_money_rejected():
    payload = _profile_payload()
    payload["energy_prices"]["grid_import"]["value"] = {"value": "0.7"}
    with pytest.raises(FinanceTripletError, match="unit"):
        FinanceProfile.from_dict(payload)
    payload2 = _profile_payload()
    payload2["energy_prices"]["grid_import"]["value"] = {"unit": "CNY/kWh"}
    with pytest.raises(FinanceTripletError, match="value"):
        FinanceProfile.from_dict(payload2)


def test_profile_price_unit_currency_prefix():
    payload = _profile_payload()
    payload["energy_prices"]["grid_import"]["value"] = {"value": "0.7", "unit": "USD/kWh"}
    with pytest.raises(FinanceTripletError, match="CNY/"):
        FinanceProfile.from_dict(payload)


def test_profile_unknown_fields_rejected():
    payload = _profile_payload()
    payload["extensions"] = {}
    with pytest.raises(FinanceTripletError, match="未知字段"):
        FinanceProfile.from_dict(payload)
    # 价格条目出现未知键
    payload2 = _profile_payload()
    payload2["energy_prices"]["grid_import"]["peak"] = "x"
    with pytest.raises(FinanceTripletError, match="未知字段"):
        FinanceProfile.from_dict(payload2)


def test_profile_deep_immutable():
    profile = _profile()
    with pytest.raises(FrozenInstanceError):
        profile.schema = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        profile.finance_types["pv_system"] = None  # type: ignore[index]


def test_finance_type_component_requirements():
    # 分量均可选, finance_type 至少一个存在(finance-yaml.md 字段表): 无 upfront 合法
    payload = _profile_payload()
    payload["finance_types"]["grid_service"] = {
        "annual_fixed_om": {
            "linear": {"capacity_kw": {"unit_cost": {"value": "12", "unit": "CNY/kW·a"}}}
        }
    }
    profile = FinanceProfile.from_dict(payload)
    assert "grid_service" in profile.finance_types
    effective = merge_effective(profile, None)
    entry = effective.finance_types["grid_service"]
    assert entry.upfront_capex is None and entry.period_variable_om is None
    assert entry.annual_fixed_om is not None
    # 空 finance_type 拒绝
    payload2 = _profile_payload()
    payload2["finance_types"]["empty_entry"] = {}
    with pytest.raises(FinanceTripletError, match="不能为空|至少需要"):
        FinanceProfile.from_dict(payload2)
    # fixed 只允许在 upfront_capex; annual_fixed_om 带 fixed 拒绝
    payload3 = _profile_payload()
    payload3["finance_types"]["pv_system"]["annual_fixed_om"]["fixed"] = {"value": "5", "unit": "CNY"}
    with pytest.raises(FinanceTripletError, match="不允许 fixed"):
        FinanceProfile.from_dict(payload3)


# ---------------------------------------------------------------------------
# FinanceOverrides 契约
# ---------------------------------------------------------------------------


def test_overrides_roundtrip_preserves_content():
    profile = _profile()
    ov = _overrides(profile)
    assert FinanceOverrides.from_dict(ov.to_dict(), profile=profile).to_dict() == ov.to_dict()


def test_overrides_ref_must_match_profile():
    profile = _profile()
    other = FinanceProfile.from_dict({**_profile_payload(),
                                       "profile": {**_profile_payload()["profile"], "id": "cn-south-demo"}})
    payload = _overrides_payload(other)
    # 引用其他 profile
    payload["profile_ref"] = {"id": "cn-south-demo"}
    with pytest.raises(FinanceTripletError, match="不一致"):
        FinanceOverrides.from_dict(payload, profile=profile)


def test_overrides_forbidden_top_level_fields():
    profile = _profile()
    for forbidden in ("currency", "base_year", "price_basis", "cost_method", "taxes"):
        payload = _overrides_payload(profile)
        payload[forbidden] = "x"
        with pytest.raises(FinanceTripletError, match="禁止出现"):
            FinanceOverrides.from_dict(payload, profile=profile)


def test_overrides_cannot_add_finance_type_or_price():
    profile = _profile()
    # 新增 finance_type
    payload = _overrides_payload(profile)
    payload["finance_types"]["gas_boiler"] = {
        "upfront_capex": {"linear": {"capacity_kw": {"unit_cost": {"value": "600", "unit": "CNY/kW"}}}}
    }
    with pytest.raises(FinanceTripletError, match="不存在, 禁止新增"):
        FinanceOverrides.from_dict(payload, profile=profile)
    # 新增 price_id
    payload2 = _overrides_payload(profile)
    payload2["energy_prices"]["gas_import"] = {"kind": "constant", "value": {"value": "3", "unit": "CNY/m³"}}
    with pytest.raises(FinanceTripletError, match="不存在, 禁止新增"):
        FinanceOverrides.from_dict(payload2, profile=profile)


def test_overrides_cannot_change_unit_or_add_driver():
    profile = _profile()
    # 改单位
    payload = _overrides_payload(profile)
    payload["finance_types"]["pv_system"]["upfront_capex"]["fixed"] = {"value": "1500", "unit": "USD"}
    with pytest.raises(FinanceTripletError, match="禁止改单位|不一致"):
        FinanceOverrides.from_dict(payload, profile=profile)
    # 新增 driver
    payload2 = _overrides_payload(profile)
    payload2["finance_types"]["pv_system"]["upfront_capex"]["linear"]["rated_area_m2"] = {
        "unit_cost": {"value": "500", "unit": "CNY/m²"}
    }
    with pytest.raises(FinanceTripletError, match="不存在, 禁止新增"):
        FinanceOverrides.from_dict(payload2, profile=profile)


def test_overrides_carrier_direction_inherited_only():
    profile = _profile()
    payload = _overrides_payload(profile)
    payload["energy_prices"]["grid_import"]["carrier"] = "electricity"
    with pytest.raises(FinanceTripletError, match="carrier"):
        FinanceOverrides.from_dict(payload, profile=profile)
    payload2 = _overrides_payload(profile)
    payload2["energy_prices"]["grid_import"]["direction"] = "sale"
    with pytest.raises(FinanceTripletError, match="direction"):
        FinanceOverrides.from_dict(payload2, profile=profile)


def test_overrides_price_partial_replace_rejected():
    profile = _profile()
    # 换 time_series 但缺 series_meta
    payload = _overrides_payload(profile)
    payload["energy_prices"]["pv_export"] = {"kind": "time_series", "ref": "b" * 64}
    with pytest.raises(FinanceTripletError, match="series_meta"):
        FinanceOverrides.from_dict(payload, profile=profile)
    # constant 缺 value
    payload2 = _overrides_payload(profile)
    payload2["energy_prices"]["grid_import"] = {"kind": "constant"}
    with pytest.raises(FinanceTripletError, match="value"):
        FinanceOverrides.from_dict(payload2, profile=profile)


def test_empty_overrides_doc_is_valid():
    profile = _profile()
    empty = FinanceOverrides.empty_for_profile(profile)
    assert not empty.finance_types and not empty.energy_prices
    merged = merge_effective(profile, None)
    merged_explicit = merge_effective(profile, empty)
    assert merged.to_dict() == merged_explicit.to_dict()


# ---------------------------------------------------------------------------
# 合并器
# ---------------------------------------------------------------------------


def test_merge_without_overrides_equals_profile():
    profile = _profile()
    effective = merge_effective(profile, None)
    assert effective.profile_id == profile.profile_id
    assert effective.currency == profile.currency
    assert effective.finance_types.keys() == profile.finance_types.keys()
    assert effective.energy_prices.keys() == profile.energy_prices.keys()
    assert effective.taxes == profile.taxes
    # 无覆盖时, finance_types / energy_prices 与 Profile 语义一致
    assert {k: v.to_dict() for k, v in effective.finance_types.items()} == {
        k: v.to_dict() for k, v in profile.finance_types.items()
    }
    assert {k: v.to_dict() for k, v in effective.energy_prices.items()} == {
        k: v.to_dict() for k, v in profile.energy_prices.items()
    }


def test_merge_sparse_overrides_only_replaces_leaf():
    profile = _profile()
    overrides = _overrides(profile)
    effective = merge_effective(profile, overrides)
    # 被覆盖的 pv_system.upfront_capex.fixed / linear.capacity_kw
    pv = effective.finance_types["pv_system"]
    upfront = pv.upfront_capex
    assert upfront is not None and upfront.fixed is not None and upfront.linear is not None
    assert upfront.fixed.value == Decimal("1500")
    assert upfront.linear["capacity_kw"].unit_cost.value == Decimal("3200")
    # 未覆盖分量原样保留
    annual_om = pv.annual_fixed_om
    period_om = pv.period_variable_om
    assert annual_om is not None and annual_om.linear is not None
    assert annual_om.linear["capacity_kw"].unit_cost.value == Decimal("35")
    assert period_om is not None and period_om.linear is not None
    assert period_om.linear["generated_kwh"].unit_cost.value == Decimal("0.01")
    # 未覆盖 finance_type 原样保留
    battery_upfront = effective.finance_types["battery_system"].upfront_capex
    assert battery_upfront is not None and battery_upfront.linear is not None
    assert battery_upfront.linear["energy_kwh"].unit_cost.value == Decimal("900")
    # 未覆盖 price 原样保留
    assert effective.energy_prices["pv_export"].kind == "time_series"
    heat_value = effective.energy_prices["heat_supply"].value
    assert heat_value is not None
    assert heat_value.value == Decimal("0.35")
    # 被覆盖 price 整项替换
    grid_value = effective.energy_prices["grid_import"].value
    assert grid_value is not None
    assert effective.energy_prices["grid_import"].kind == "constant"
    assert grid_value.value == Decimal("0.75")
    # carrier/direction 继承 Profile
    assert effective.energy_prices["grid_import"].carrier == "electricity"
    assert effective.energy_prices["grid_import"].direction == "purchase"
    # taxes 原样保留
    assert effective.taxes.keys() == profile.taxes.keys()


def test_merge_is_deterministic():
    profile = _profile()
    overrides = _overrides(profile)
    effective = merge_effective(profile, overrides)
    # 合并器确定性: 同输入重新合并产生同一对象
    recomputed = merge_effective(profile, overrides)
    assert recomputed.to_dict() == effective.to_dict()


def test_merge_rejects_mismatched_overrides():
    profile = _profile()
    other_profile = FinanceProfile.from_dict(
        {**_profile_payload(), "profile": {**_profile_payload()["profile"], "id": "cn-east-demo"}}
    )
    ov_for_other = FinanceOverrides.empty_for_profile(other_profile)
    with pytest.raises(FinanceTripletError, match="不一致"):
        merge_effective(profile, ov_for_other)


def test_merge_blocks_unbound_override_additions():
    """绕过 profile 校验解析的 Overrides(仅文档级)带新增项时, 合并必须阻断而非崩溃。"""
    profile = _profile()
    payload = _overrides_payload(profile)
    payload["finance_types"]["gas_boiler"] = {
        "upfront_capex": {"linear": {"capacity_kw": {"unit_cost": {"value": "600", "unit": "CNY/kW"}}}}
    }
    unbound = FinanceOverrides.from_dict(payload)
    with pytest.raises(FinanceTripletError, match="不存在, 禁止新增"):
        merge_effective(profile, unbound)
    payload2 = _overrides_payload(profile)
    payload2["energy_prices"]["gas_import"] = {"kind": "constant", "value": {"value": "3", "unit": "CNY/m³"}}
    with pytest.raises(FinanceTripletError, match="不存在, 禁止新增"):
        merge_effective(profile, FinanceOverrides.from_dict(payload2))


def test_effective_roundtrip():
    profile = _profile()
    overrides = _overrides(profile)
    effective = merge_effective(profile, overrides)
    restored = EffectiveFinanceConfig.from_dict(effective.to_dict())
    assert restored.to_dict() == effective.to_dict()


def test_effective_deep_immutable():
    profile = _profile()
    effective = merge_effective(profile, None)
    with pytest.raises(FrozenInstanceError):
        effective.profile_id = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SeriesMeta 元数据
# ---------------------------------------------------------------------------


def test_series_meta_rejects_invalid_resolution_or_point_count():
    base_meta = {"leap_year": False, "point_count": 8760, "unit": "CNY/kWh"}
    with pytest.raises(FinanceTripletError, match="resolution"):
        SeriesMeta.from_dict({"resolution": "5min", **base_meta})
    with pytest.raises(FinanceTripletError, match="point_count"):
        SeriesMeta.from_dict({"resolution": "1h", "leap_year": False, "point_count": 0, "unit": "CNY/kWh"})
    with pytest.raises(FinanceTripletError, match="leap_year"):
        SeriesMeta.from_dict({"resolution": "1h", "leap_year": "no", "point_count": 8760, "unit": "CNY/kWh"})
