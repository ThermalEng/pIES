"""F1 public input semantics: named leaves, typed references and atomic overrides."""

from copy import deepcopy
from decimal import Decimal

import pytest
from test_finance_s1_triplet_contract import _valid_profile_dict

from iesplan.finance import (
    AmountRefCashflowAmount,
    EffectiveFinanceConfig,
    FinanceOverrides,
    FinanceProfile,
    FinanceTripletError,
    amount_dependency_closure,
    cost_target_leaves,
    effective_from_yaml,
    effective_to_yaml,
    merge_effective,
    parse_amount_reference,
)


def _money(value, unit="CNY"):
    return {"value": str(value), "unit": unit}


def _amount(source=None, *, unit="CNY", axis="scalar", coefficient=None):
    return {
        "unit": unit,
        "axis": axis,
        "constant": _money("0", unit),
        "terms": {}
        if source is None
        else {
            "one": {"coefficient": coefficient or _money("1", "1"), "source": source},
        },
    }


def _profile_data():
    data = _valid_profile_dict()
    data["tax_rules"] = {}
    data["net_cashflows"] = {}
    data["finance_types"]["pv_system"]["annual_fixed_om"] = {
        "items": {
            "materials": {"linear": {"capacity_kw": {"unit_cost": _money("2", "CNY/kW/a")}}},
            "service": {
                "fixed": _money("10", "CNY/a"),
                "linear": {"capacity_kw": {"unit_cost": _money("5", "CNY/kW/a")}},
            },
        }
    }
    data["amounts"] = {
        "year_fee": _amount(
            {
                "kind": "cost",
                "path": "pv_system.annual_fixed_om",
                "basis": "raw",
                "scope": {"kind": "instances", "instances": ["pv2", "pv1"]},
                "period": {"kind": "financial_year", "year": 2},
            }
        ),
        "fee_copy": _amount({"kind": "amount", "id": "year_fee", "period": {"kind": "scalar"}}),
    }
    return data


def _patch(profile, **values):
    return {
        "schema": "ies.finance-overrides",
        "schema_version": "1.0.0",
        "profile_ref": {"id": profile.profile_id},
        **values,
    }


def test_named_cost_items_roundtrip_actual_leaves_and_shared_driver():
    data = _profile_data()
    profile = FinanceProfile.from_dict(data)
    cost = profile.finance_types["pv_system"].annual_fixed_om
    assert set(cost.items) == {"materials", "service"}
    assert cost.items["materials"].linear["capacity_kw"].unit_cost.value == Decimal("2")
    assert cost.items["service"].linear["capacity_kw"].unit_cost.value == Decimal("5")
    assert cost_target_leaves("pv_system.annual_fixed_om", profile.finance_types) == (
        "pv_system.annual_fixed_om.items.materials.linear.capacity_kw",
        "pv_system.annual_fixed_om.items.service.fixed",
        "pv_system.annual_fixed_om.items.service.linear.capacity_kw",
    )
    assert cost_target_leaves("pv_system.annual_fixed_om.items.service", profile.finance_types) == (
        "pv_system.annual_fixed_om.items.service.fixed",
        "pv_system.annual_fixed_om.items.service.linear.capacity_kw",
    )
    assert profile.amounts["year_fee"].terms["one"].source.scope.instances == ("pv1", "pv2")
    effective = merge_effective(profile, None)
    assert effective_from_yaml(effective_to_yaml(effective)).to_dict() == effective.to_dict()
    assert EffectiveFinanceConfig.from_dict(effective.to_dict()).to_dict() == effective.to_dict()
    with pytest.raises(TypeError):
        cost.items["new"] = cost.items["service"]
    data["finance_types"]["pv_system"]["annual_fixed_om"]["items"]["service"]["fixed"]["value"] = "99"
    assert cost.items["service"].fixed.value == Decimal("10")


@pytest.mark.parametrize(
    "replacement",
    [
        {"items": {"service": {}}},
        {"items": {"service": {"fixed": None}}},
        {"items": {"service": {"items": {}}}},
        {"items": {"service": {"fixed": _money("-1", "CNY/a")}}},
        {"items": {"service": {"fixed": _money("1", "CNY")}}},
    ],
)
def test_named_items_reject_invalid_amounts_or_time_units(replacement):
    data = _profile_data()
    data["finance_types"]["pv_system"]["annual_fixed_om"] = replacement
    with pytest.raises(FinanceTripletError):
        FinanceProfile.from_dict(data)


def test_named_variable_om_still_rejects_fixed():
    data = _profile_data()
    data["finance_types"]["pv_system"]["period_variable_om"] = {"items": {"bad": {"fixed": _money("1")}}}
    with pytest.raises(FinanceTripletError, match="不允许 fixed"):
        FinanceProfile.from_dict(data)


def test_vat_named_item_scope_and_whole_scope_overlap():
    data = _profile_data()
    data["tax_rules"] = _valid_profile_dict()["tax_rules"]
    vat = data["tax_rules"]["op_vat"]
    vat["om_input_tax"] = [
        {
            "for": "pv_system.annual_fixed_om.items.materials",
            "rate": _money("0.1", "1"),
            "deductible_ratio": _money("1", "1"),
        },
        {
            "for": "pv_system.annual_fixed_om.items.service.fixed",
            "rate": _money("0.2", "1"),
            "deductible_ratio": _money("1", "1"),
        },
    ]
    FinanceProfile.from_dict(data)
    vat["om_input_tax"].append(
        {"for": "pv_system.annual_fixed_om", "rate": _money("0.1", "1"), "deductible_ratio": _money("1", "1")}
    )
    with pytest.raises(FinanceTripletError, match="重复计算同一税基"):
        FinanceProfile.from_dict(data)


def test_amount_dag_reordering_preserves_dependency_and_signed_values():
    data = _profile_data()
    data["amounts"]["year_fee"]["constant"] = _money("-3")
    left = FinanceProfile.from_dict(data)
    data["amounts"] = dict(reversed(list(data["amounts"].items())))
    right = FinanceProfile.from_dict(data)
    assert left.to_dict() == right.to_dict()
    assert amount_dependency_closure(left.amounts, ["fee_copy"]) == ("year_fee", "fee_copy")
    assert left.amounts["year_fee"].constant.value == Decimal("-3")
    with pytest.raises(TypeError):
        left.amounts["fee_copy"].terms["new"] = left.amounts["fee_copy"].terms["one"]


@pytest.mark.parametrize("kind", ["cycle", "missing", "invalid_unit", "scalar_current", "unknown_field"])
def test_amounts_reject_file_reference_errors_even_unused(kind):
    data = _profile_data()
    term = data["amounts"]["year_fee"]["terms"]["one"]
    if kind == "cycle":
        term["source"] = {"kind": "amount", "id": "fee_copy", "period": {"kind": "scalar"}}
    elif kind == "missing":
        term["source"] = {"kind": "amount", "id": "missing", "period": {"kind": "scalar"}}
    elif kind == "invalid_unit":
        term["coefficient"] = _money("1", "kW")
    elif kind == "scalar_current":
        term["source"]["period"] = {"kind": "current"}
    else:
        term["source"]["id"] = "unexpected"
    with pytest.raises(FinanceTripletError):
        FinanceProfile.from_dict(data)


def test_amount_annual_rate_and_explicit_year_conversion():
    data = _profile_data()
    source = deepcopy(data["amounts"]["year_fee"]["terms"]["one"]["source"])
    source["period"] = {"kind": "scalar"}
    data["amounts"] = {
        "annual_rate": _amount(source, unit="CNY/a"),
        "annual_amount": _amount(
            {"kind": "amount", "id": "annual_rate", "period": {"kind": "scalar"}},
            coefficient=_money("1", "year"),
            axis="financial_year",
        ),
    }
    profile = FinanceProfile.from_dict(data)
    assert profile.amounts["annual_rate"].unit == "CNY/a"
    data["amounts"]["annual_amount"]["terms"]["one"]["coefficient"] = _money("1", "1")
    with pytest.raises(FinanceTripletError, match="单位不等于"):
        FinanceProfile.from_dict(data)


def test_atomic_named_item_and_amount_parameter_overrides():
    profile = FinanceProfile.from_dict(_profile_data())
    payload = _patch(
        profile,
        finance_types={
            "pv_system": {
                "annual_fixed_om": {
                    "items": {
                        "service": {"fixed": _money("12", "CNY/a")},
                    }
                }
            }
        },
        amounts={
            "year_fee": {
                "constant": _money("-1"),
                "terms": {
                    "one": {
                        "coefficient": _money("2", "1"),
                        "source": {
                            "scope": {"kind": "instances", "instances": ["pv2"]},
                            "period": {"kind": "financial_year", "year": 3},
                        },
                    }
                },
            }
        },
    )
    override = FinanceOverrides.from_dict(payload, profile=profile)
    payload["amounts"]["year_fee"]["constant"]["value"] = "99"
    with pytest.raises(TypeError):
        override.amounts["year_fee"]["constant"]["value"] = "99"
    effective = merge_effective(profile, override)
    assert effective.finance_types["pv_system"].annual_fixed_om.items["service"].fixed.value == Decimal("12")
    assert effective.finance_types["pv_system"].annual_fixed_om.items["service"].linear[
        "capacity_kw"
    ].unit_cost.value == Decimal("5")
    assert effective.amounts["year_fee"].constant.value == Decimal("-1")
    assert effective.amounts["year_fee"].terms["one"].source.period.year == 3
    assert effective.amounts["year_fee"].terms["one"].source.scope.instances == ("pv2",)
    assert effective.amounts["fee_copy"] is profile.amounts["fee_copy"]


@pytest.mark.parametrize(
    "patch",
    [
        {"unknown": {"constant": _money("1")}},
        {"year_fee": {"unit": "USD"}},
        {"year_fee": {"constant": {"value": "1"}}},
        {"year_fee": {"terms": {"extra": {"coefficient": _money("1", "1")}}}},
        {"year_fee": {"terms": {"one": {"coefficient": _money("1", "CNY")}}}},
        {"year_fee": {"terms": {"one": {"source": {"path": "pv_system.upfront_capex"}}}}},
        {"year_fee": {"terms": {"one": {"source": {"scope": {"kind": "all_new_instances"}}}}}},
        {"year_fee": {"terms": {"one": {"source": {"period": {"kind": "scalar"}}}}}},
    ],
)
def test_amount_override_cannot_change_identity_shape_or_unit(patch):
    profile = FinanceProfile.from_dict(_profile_data())
    raw = _patch(profile, amounts=patch)
    with pytest.raises(FinanceTripletError):
        FinanceOverrides.from_dict(raw, profile=profile)
    unbound = None
    try:
        unbound = FinanceOverrides.from_dict(raw)
    except FinanceTripletError:
        pass
    if unbound is not None:
        with pytest.raises(FinanceTripletError):
            merge_effective(profile, unbound)


def test_net_cashflow_amount_ref_has_scalar_identity_and_no_source_override():
    data = _profile_data()
    data["net_cashflows"] = {
        "refund": {
            "at": {"kind": "evaluation_end"},
            "direction": "inflow",
            "amount": {"kind": "amount_ref", "id": "fee_copy"},
        }
    }
    profile = FinanceProfile.from_dict(data)
    assert isinstance(profile.net_cashflows["refund"].amount, AmountRefCashflowAmount)
    assert profile.net_cashflows["refund"].amount.id == "fee_copy"
    with pytest.raises(FinanceTripletError, match="身份不允许覆盖"):
        FinanceOverrides.from_dict(
            _patch(profile, net_cashflows={"refund": {"amount": {"id": "year_fee"}}}), profile=profile
        )
    data["amounts"]["fee_copy"]["axis"] = "financial_year"
    with pytest.raises(FinanceTripletError, match="scalar 本币金额"):
        FinanceProfile.from_dict(data)


@pytest.mark.parametrize(
    "source",
    [
        {
            "kind": "tariff",
            "price": "pv_sale",
            "bindings": ["meter"],
            "basis": "net_basis",
            "period": {"kind": "tax_month", "month": 1},
        },
        {
            "kind": "demand",
            "charge": "main_demand",
            "bindings": ["m", "m"],
            "basis": "raw",
            "period": {"kind": "financial_year", "year": 1},
        },
        {"kind": "construction_unit", "unit_ref": "plant", "period": {"kind": "financial_year", "year": 1}},
        {
            "kind": "cost",
            "path": "pv_system.upfront_capex",
            "basis": "raw",
            "scope": {"kind": "all_new_instances"},
            "period": {"kind": "tax_month", "month": 1},
        },
        {
            "kind": "driver",
            "finance_type": "pv_system",
            "driver": "invented",
            "scope": {"kind": "all_new_instances"},
            "period": {"kind": "scalar"},
        },
        {
            "kind": "movement",
            "id": "missing",
            "basis": "raw",
            "period": {"kind": "financial_year", "year": 0},
        },
    ],
)
def test_source_scope_direction_identity_and_period_rules(source):
    data = _profile_data()
    data["amounts"] = {"value": _amount(source)}
    with pytest.raises(FinanceTripletError):
        FinanceProfile.from_dict(data)


def test_future_reference_shape_is_explicit_not_a_string_alias():
    ref = parse_amount_reference({"kind": "baseline", "id": "baseline_npv", "period": {"kind": "scalar"}})
    assert ref.to_dict() == {"kind": "baseline", "id": "baseline_npv", "period": {"kind": "scalar"}}
    with pytest.raises(FinanceTripletError):
        parse_amount_reference(
            {"kind": "baseline", "id": "baseline_npv", "period": {"kind": "scalar"}, "task_id": "current"}
        )


def test_bound_override_rebinds_same_id_new_profile_without_restoring_old_parameters():
    first = FinanceProfile.from_dict(_profile_data())
    override = FinanceOverrides.from_dict(
        _patch(
            first,
            finance_types={
                "pv_system": {
                    "annual_fixed_om": {
                        "items": {
                            "service": {"fixed": _money("12", "CNY/a")},
                        }
                    }
                }
            },
            amounts={"year_fee": {"terms": {"one": {"coefficient": _money("2", "1")}}}},
        ),
        profile=first,
    )
    updated = first.to_dict()
    updated["finance_types"]["pv_system"]["annual_fixed_om"]["items"]["materials"]["linear"]["capacity_kw"][
        "unit_cost"
    ]["value"] = "200"
    updated["amounts"]["year_fee"]["constant"]["value"] = "5"
    second = FinanceProfile.from_dict(updated)
    assert second.profile_id == first.profile_id and second is not first
    effective = merge_effective(second, override)
    material = effective.finance_types["pv_system"].annual_fixed_om.items["materials"]
    assert material.linear["capacity_kw"].unit_cost.value == Decimal("200")
    assert effective.finance_types["pv_system"].annual_fixed_om.items["service"].fixed.value == Decimal("12")
    assert effective.amounts["year_fee"].constant.value == Decimal("5")
    assert effective.amounts["year_fee"].terms["one"].coefficient.value == Decimal("2")
    # Serialising a sparse override never serialises its private owner object.
    assert FinanceOverrides.from_dict(override.to_dict()).to_dict() == override.to_dict()


def test_same_id_profile_rebinding_checks_changed_override_permission():
    first = FinanceProfile.from_dict(_profile_data())
    override = FinanceOverrides.from_dict(
        _patch(
            first,
            finance_types={
                "pv_system": {
                    "annual_fixed_om": {
                        "items": {
                            "service": {"fixed": _money("12", "CNY/a")},
                        }
                    }
                }
            },
        ),
        profile=first,
    )
    updated = first.to_dict()
    del updated["finance_types"]["pv_system"]["annual_fixed_om"]["items"]["service"]["fixed"]
    second = FinanceProfile.from_dict(updated)
    with pytest.raises(FinanceTripletError, match="禁止新增"):
        merge_effective(second, override)


def test_same_profile_owner_reuses_typed_override_without_reparsing(monkeypatch):
    from iesplan.finance import Money

    profile = FinanceProfile.from_dict(_profile_data())
    override = FinanceOverrides.from_dict(
        _patch(
            profile,
            finance_types={
                "pv_system": {
                    "annual_fixed_om": {
                        "items": {
                            "service": {"fixed": _money("12", "CNY/a")},
                        }
                    }
                }
            },
            amounts={"year_fee": {"constant": _money("7")}},
        ),
        profile=profile,
    )

    def unexpected_parse(*args, **kwargs):
        raise AssertionError("already bound immutable money must not be reparsed")

    monkeypatch.setattr(Money, "from_dict", unexpected_parse)
    effective = merge_effective(profile, override)
    assert effective.amounts["year_fee"].constant.value == Decimal("7")
    assert effective.finance_types["pv_system"].annual_fixed_om.items["service"].fixed.value == Decimal("12")


def test_system_quantity_accepts_foreign_n3_identity_with_double_underscore():
    source = {
        "kind": "system_quantity",
        "block": "dc__account",
        "variable": "dc__available",
        "aggregation": "integrate_positive",
        "period": {"kind": "financial_year", "year": 1},
    }
    assert parse_amount_reference(source).to_dict() == source
    payload = _profile_data()
    payload["amounts"] = {"system_cost": _amount(source, coefficient=_money("1", "CNY/kWh"))}
    profile = FinanceProfile.from_dict(payload)
    assert profile.amounts["system_cost"].terms["one"].source.variable == "dc__available"
    assert merge_effective(profile, None).amounts["system_cost"].terms["one"].source.block == "dc__account"


@pytest.mark.parametrize("key,value", [("block", ""), ("variable", None), ("variable", 2)])
def test_system_quantity_still_requires_explicit_string_identities(key, value):
    source = {
        "kind": "system_quantity",
        "block": "account",
        "variable": "available",
        "aggregation": "integrate_positive",
        "period": {"kind": "financial_year", "year": 1},
        key: value,
    }
    with pytest.raises(FinanceTripletError, match="系统量标识"):
        parse_amount_reference(source)
