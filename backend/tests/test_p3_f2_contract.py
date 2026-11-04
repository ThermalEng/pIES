"""F2 domain input: occurrence identities, tax/asset references and sparse parameters."""

from copy import deepcopy
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest
from test_finance_s1_triplet_contract import _valid_profile_dict
from test_p3_f1_contract import _amount, _money, _patch, _profile_data

from iesplan.finance import (
    AmountPeriod,
    AssetBaseFrom,
    DerivedTaxRule,
    EffectiveFinanceConfig,
    FinanceOverrides,
    FinanceProfile,
    FinanceTripletError,
    Money,
    asset_basis_sources,
    cash_movement_occurrences,
    effective_from_yaml,
    effective_to_yaml,
    expand_amount_period,
    merge_effective,
)


def _fixture():
    data = _profile_data()
    data["amounts"] = {"purchase_quote": _amount(), "service_quote": _amount()}
    for definition in data["amounts"].values():
        definition["constant"] = _money("100")
    data["cash_movements"] = {
        "purchase_invoice": {
            "amount": {"kind": "amount", "id": "purchase_quote", "period": {"kind": "scalar"}},
            "price_basis": "tax_inclusive",
            "direction": "outflow",
            "recognition": {"kind": "at", "at": {"kind": "financial_year", "year": 0}},
            "payments": [{"offset": 0, "ratio": "0.4"}, {"offset": 2, "ratio": "0.6"}],
        },
        "service": {
            "amount": {"kind": "amount", "id": "service_quote", "period": {"kind": "scalar"}},
            "price_basis": "tax_exclusive",
            "direction": "outflow",
            "recognition": {"kind": "each", "axis": "financial_year", "first": 1, "last": 2},
            "payments": [{"offset": 0, "ratio": "1"}],
        },
    }
    data["assets"] = {
        "asset_a": {
            "basis_from": {
                "kind": "movement",
                "id": "purchase_invoice",
                "basis": "net_basis",
                "period": {"kind": "financial_year", "year": 0},
            },
            "capitalized_at": {"kind": "financial_year", "year": 1},
            "available_from": 1,
        }
    }
    data["tax_rules"] = deepcopy(_valid_profile_dict()["tax_rules"])
    vat = data["tax_rules"]["op_vat"]
    for field in ("in_tax", "out_tax", "om_input_tax", "maintenance_input_tax"):
        vat[field] = []
    vat["movement_input_tax"] = [
        {
            "movement": "service",
            "rate": _money("0.1", "1"),
            "deductible_ratio": _money("1", "1"),
            "confirmation": {
                "method": "explicit_schedule",
                "occurrences": [
                    {"at": {"kind": "financial_year", "year": 2}, "schedule": [{"period": 25, "ratio": "1"}]},
                    {"at": {"kind": "financial_year", "year": 1}, "schedule": [{"period": 13, "ratio": "1"}]},
                ],
            },
        }
    ]
    income = data["tax_rules"]["cit_rule"]
    income["depreciation"] = [
        {
            "base_from": {"kind": "asset", "id": "asset_a"},
            "method": "straight_line",
            "life_years": 5,
            "residual": _money("0"),
            "first_year": 1,
        }
    ]
    income["movement_cost"] = ["service"]
    income["derived_tax_cost"] = ["surcharge"]
    data["tax_rules"]["surcharge"] = {
        "kind": "derived_tax",
        "label_ref": "vat_label",
        "axis": "tax_month",
        "base_from": {
            "kind": "tax",
            "source": {"kind": "pool", "id": "main_pool"},
            "measure": "paid",
            "period": {"kind": "current"},
        },
        "rate": _money("0.04", "1"),
        "payment_lag": 1,
    }
    return data


def test_f2_complete_input_roundtrip_and_owned_occurrence_ids():
    raw = _fixture()
    profile = FinanceProfile.from_dict(raw)
    service = profile.cash_movements["service"]
    assert [(at.axis, at.period) for at in cash_movement_occurrences(service)] == [
        ("financial_year", 1),
        ("financial_year", 2),
    ]
    purchase = profile.cash_movements["purchase_invoice"]
    assert purchase.payments[1].ratio == Decimal("0.6")
    assert purchase.recognition.at.period == 0
    assert profile.assets["asset_a"].capitalized_at.year == 1
    assert isinstance(profile.tax_rules["cit_rule"].depreciation[0].base_from, AssetBaseFrom)
    assert isinstance(profile.tax_rules["surcharge"], DerivedTaxRule)
    occurrences = profile.tax_rules["op_vat"].movement_input_tax[0].confirmation.occurrences
    assert [(item.at.period, item.schedule[0].period) for item in occurrences] == [(1, 13), (2, 25)]
    effective = merge_effective(profile, None)
    assert EffectiveFinanceConfig.from_dict(effective.to_dict()).to_dict() == effective.to_dict()
    assert effective_from_yaml(effective_to_yaml(effective)).to_dict() == effective.to_dict()
    raw["cash_movements"]["purchase_invoice"]["payments"][0]["ratio"] = "1"
    assert purchase.payments[0].ratio == Decimal("0.4")
    with pytest.raises(TypeError):
        profile.assets["other"] = profile.assets["asset_a"]
    with pytest.raises(FrozenInstanceError):
        purchase.payments = ()


def test_prepaid_occurrence_remains_an_independent_signed_transaction():
    raw = _fixture()
    raw["amounts"]["service_quote"]["constant"] = _money("-100")
    raw["cash_movements"]["service"]["payments"] = [{"offset": -1, "ratio": "1"}]
    profile = FinanceProfile.from_dict(raw)
    assert profile.amounts["service_quote"].constant.value == Decimal("-100")
    assert profile.cash_movements["service"].payments[0].offset == -1


@pytest.mark.parametrize(
    "patch",
    [
        {"payments": []},
        {"payments": [{"offset": 0, "ratio": "0.9"}]},
        {"payments": [{"offset": True, "ratio": "1"}]},
        {"payments": [{"offset": 0, "ratio": "-1"}, {"offset": 1, "ratio": "2"}]},
        {"recognition": {"kind": "each", "axis": "financial_year", "first": 0, "last": 2}},
        {"recognition": {"kind": "at", "at": {"kind": "tax_month", "month": 0}}},
        {"amount": {"kind": "amount", "id": "missing", "period": {"kind": "scalar"}}},
        {"amount": {"kind": "amount", "id": "service_quote", "period": {"kind": "current"}}},
        {"price_basis": "guess"},
        {"direction": "unknown"},
    ],
)
def test_movement_rejects_invalid_shapes_parameters_or_quotes(patch):
    raw = _fixture()
    raw["cash_movements"]["service"].update(patch)
    with pytest.raises(FinanceTripletError):
        FinanceProfile.from_dict(raw)


def test_asset_combinations_keep_original_sources_periods_and_exact_shares():
    raw = _fixture()
    raw["amounts"]["split_basis"] = _amount()
    raw["amounts"]["split_basis"]["terms"] = {
        "purchase": {
            "coefficient": _money("0.6", "1"),
            "source": {
                "kind": "movement",
                "id": "purchase_invoice",
                "basis": "net_basis",
                "period": {"kind": "financial_year", "year": 0},
            },
        },
        "initial": {
            "coefficient": _money("0.4", "1"),
            "source": {
                "kind": "cost",
                "path": "pv_system.upfront_capex",
                "scope": {"kind": "instances", "instances": ["pv2", "pv1"]},
                "basis": "net_basis",
                "period": {"kind": "scalar"},
            },
        },
    }
    raw["assets"]["asset_a"]["basis_from"] = {
        "kind": "amount",
        "id": "split_basis",
        "period": {"kind": "scalar"},
    }
    profile = FinanceProfile.from_dict(raw)
    leaves = asset_basis_sources(profile, profile.assets["asset_a"].basis_from)
    assert [(leaf.source.kind, leaf.share) for leaf in leaves] == [
        ("cost", Decimal("0.4")),
        ("movement", Decimal("0.6")),
    ]
    assert leaves[1].source.period.year == 0
    assert leaves[0].source.scope.instances == ("pv1", "pv2")


@pytest.mark.parametrize(
    "basis",
    [
        {
            "kind": "movement",
            "id": "service",
            "basis": "gross",
            "period": {"kind": "financial_year", "year": 1},
        },
        {
            "kind": "movement",
            "id": "unknown",
            "basis": "net_basis",
            "period": {"kind": "financial_year", "year": 1},
        },
        {"kind": "movement", "id": "service", "basis": "net_basis", "period": {"kind": "current"}},
        {
            "kind": "cost",
            "path": "pv_system.annual_fixed_om",
            "basis": "net_basis",
            "scope": {"kind": "all_new_instances"},
            "period": {"kind": "scalar"},
        },
        {
            "kind": "tax",
            "source": {"kind": "pool", "id": "main_pool"},
            "measure": "credit_balance",
            "period": {"kind": "tax_month", "month": 1},
        },
        {"kind": "amount", "id": "purchase_quote", "period": {"kind": "scalar"}},
    ],
)
def test_asset_basis_rejects_noncapital_roots_hand_filled_constants_or_current(basis):
    raw = _fixture()
    raw["assets"]["asset_a"]["basis_from"] = basis
    with pytest.raises(FinanceTripletError):
        FinanceProfile.from_dict(raw)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data["tax_rules"]["op_vat"]["movement_input_tax"].append(
            deepcopy(data["tax_rules"]["op_vat"]["movement_input_tax"][0])
        ),
        lambda data: data["cash_movements"]["service"].update(direction="inflow"),
        lambda data: data["tax_rules"]["cit_rule"]["movement_cost"].append("purchase_invoice"),
        lambda data: data["tax_rules"]["cit_rule"]["derived_tax_cost"].append("unknown"),
        lambda data: data["tax_rules"]["cit_rule"]["depreciation"][0]["base_from"].update(id="unknown"),
        lambda data: data["assets"]["asset_a"].update(available_from=2),
    ],
)
def test_tax_rules_reject_wrong_direction_repeated_claims_and_expense_capital_conflict(mutate):
    raw = _fixture()
    mutate(raw)
    with pytest.raises(FinanceTripletError):
        FinanceProfile.from_dict(raw)


@pytest.mark.parametrize(
    "occurrences",
    [
        [{"at": {"kind": "financial_year", "year": 1}, "schedule": [{"period": 13, "ratio": "0.5"}]}],
        [
            {
                "at": {"kind": "financial_year", "year": 1},
                "schedule": [{"period": 13, "ratio": "0.5"}, {"period": 13, "ratio": "0.5"}],
            }
        ],
        [{"at": {"kind": "financial_year", "year": 1}, "schedule": [{"period": 13, "ratio": "1"}]}] * 2,
    ],
)
def test_each_vat_schedule_is_per_occurrence_not_a_shared_sum(occurrences):
    raw = _fixture()
    raw["tax_rules"]["op_vat"]["movement_input_tax"][0]["confirmation"]["occurrences"] = occurrences
    with pytest.raises(FinanceTripletError):
        FinanceProfile.from_dict(raw)


def test_derived_rate_is_nonnegative_not_a_tax_name_branch_or_bounded_to_one():
    raw = _fixture()
    raw["tax_rules"]["surcharge"]["rate"] = _money("1.2", "1")
    profile = FinanceProfile.from_dict(raw)
    assert profile.tax_rules["surcharge"].rate.value == Decimal("1.2")
    raw["tax_rules"]["surcharge"]["base_from"]["measure"] = "inferred_tax"
    with pytest.raises(FinanceTripletError):
        FinanceProfile.from_dict(raw)


def test_f2_overrides_replace_only_declared_parameters_without_changing_identities(monkeypatch):
    profile = FinanceProfile.from_dict(_fixture())
    patch = _patch(
        profile,
        cash_movements={"purchase_invoice": {"payments": [{"offset": 0, "ratio": "1"}]}},
        assets={"asset_a": {"capitalized_at": {"kind": "tax_month", "month": 12}}},
        tax_rules={
            "surcharge": {"rate": _money("0.05", "1"), "payment_lag": 2},
            "op_vat": {"movement_input_tax": {"service": {"rate": _money("0.2", "1")}}},
        },
    )
    overrides = FinanceOverrides.from_dict(patch, profile=profile)
    patch["cash_movements"]["purchase_invoice"]["payments"][0]["ratio"] = "0"

    def unexpected(*args, **kwargs):
        raise AssertionError("trusted typed parameters must not be reparsed")

    monkeypatch.setattr(Money, "from_dict", unexpected)
    effective = merge_effective(profile, overrides)
    assert effective.cash_movements["purchase_invoice"].payments[0].ratio == Decimal("1")
    assert (
        effective.cash_movements["purchase_invoice"].amount
        is profile.cash_movements["purchase_invoice"].amount
    )
    assert effective.assets["asset_a"].capitalized_at.month == 12
    assert effective.tax_rules["surcharge"].rate.value == Decimal("0.05")
    assert effective.tax_rules["surcharge"].payment_lag == 2
    assert effective.tax_rules["op_vat"].movement_input_tax[0].rate.value == Decimal("0.2")
    assert effective.tax_rules["cit_rule"].movement_cost == ("service",)
    assert effective.tax_rules["cit_rule"].derived_tax_cost == ("surcharge",)


@pytest.mark.parametrize(
    "patch",
    [
        {"cash_movements": {"unknown": {"payments": [{"offset": 0, "ratio": "1"}]}}},
        {"cash_movements": {"service": {"direction": "inflow"}}},
        {
            "cash_movements": {
                "service": {
                    "amount": {"kind": "amount", "id": "purchase_quote", "period": {"kind": "scalar"}}
                }
            }
        },
        {"assets": {"asset_a": {"basis_from": {"id": "service"}}}},
        {"tax_rules": {"surcharge": {"axis": "financial_year"}}},
        {"tax_rules": {"cit_rule": {"movement_cost": []}}},
        {"tax_rules": {"op_vat": {"movement_input_tax": {"unknown": {"rate": _money("0.1", "1")}}}}},
        {
            "tax_rules": {
                "op_vat": {
                    "movement_input_tax": {"service": {"confirmation": {"method": "recognition_period"}}}
                }
            }
        },
    ],
)
def test_f2_sparse_overrides_do_not_add_identities_change_scope_or_method(patch):
    profile = FinanceProfile.from_dict(_fixture())
    with pytest.raises(FinanceTripletError):
        FinanceOverrides.from_dict(_patch(profile, **patch), profile=profile)
    try:
        unbound = FinanceOverrides.from_dict(_patch(profile, **patch))
    except FinanceTripletError:
        return
    with pytest.raises(FinanceTripletError):
        merge_effective(profile, unbound)


def test_vat_override_replaces_complete_occurrence_schedules_by_movement_identity():
    profile = FinanceProfile.from_dict(_fixture())
    rows = [
        {
            "at": {"kind": "financial_year", "year": year},
            "schedule": [{"period": year * 12 + 2, "ratio": "1"}],
        }
        for year in (1, 2)
    ]
    overrides = FinanceOverrides.from_dict(
        _patch(
            profile,
            tax_rules={
                "op_vat": {
                    "movement_input_tax": {"service": {"confirmation": {"occurrences": rows}}},
                }
            },
        ),
        profile=profile,
    )
    effective = merge_effective(profile, overrides)
    occurrences = effective.tax_rules["op_vat"].movement_input_tax[0].confirmation.occurrences
    assert [occ.schedule[0].period for occ in occurrences] == [14, 26]
    assert occurrences[0].at.period == 1


def test_entity_cycles_are_not_mistaken_for_same_period_dependencies():
    raw = _fixture()
    raw["amounts"]["tax_followup"] = _amount(
        {
            "kind": "tax",
            "source": {"kind": "rule", "id": "cit_rule"},
            "measure": "paid",
            "period": {"kind": "financial_year", "year": 1},
        }
    )
    raw["cash_movements"]["followup"] = {
        "amount": {"kind": "amount", "id": "tax_followup", "period": {"kind": "scalar"}},
        "direction": "outflow",
        "price_basis": "tax_inclusive",
        "recognition": {"kind": "at", "at": {"kind": "financial_year", "year": 2}},
        "payments": [{"offset": 0, "ratio": "1"}],
    }
    raw["tax_rules"]["cit_rule"]["movement_cost"].append("followup")
    profile = FinanceProfile.from_dict(raw)
    assert profile.cash_movements["followup"].recognition.at.year == 2


def test_source_share_does_not_implicitly_expand_to_named_assets():
    raw = _fixture()
    raw["net_cashflows"] = {
        "recovery": {
            "at": {"kind": "evaluation_end"},
            "direction": "inflow",
            "amount": {
                "kind": "source_share",
                "base_from": {"kind": "asset", "id": "asset_a"},
                "ratio": _money("0.1", "1"),
            },
        }
    }
    with pytest.raises(FinanceTripletError, match="资产使用amount_ref"):
        FinanceProfile.from_dict(raw)


def test_period_helper_preserves_current_axis_and_explicit_summation():
    assert expand_amount_period(AmountPeriod("current"), "financial_year", 2) == ("financial_year", (2,))
    assert expand_amount_period(AmountPeriod("year_months", year="current"), "financial_year", 2) == (
        "tax_month",
        tuple(range(13, 25)),
    )
    assert expand_amount_period(AmountPeriod("range", axis="tax_month", first=2, last=3), "scalar", 0) == (
        "tax_month",
        (2, 3),
    )


@pytest.mark.parametrize("target", ["payments", "confirmation"])
def test_proportion_sum_is_exact_beyond_default_decimal_context(target):
    raw = _fixture()
    almost_zero = "0.00000000000000000000000000001"
    if target == "payments":
        raw["cash_movements"]["service"]["payments"] = [
            {"offset": 0, "ratio": "1"},
            {"offset": 1, "ratio": almost_zero},
        ]
    else:
        raw["tax_rules"]["op_vat"]["movement_input_tax"][0]["confirmation"]["occurrences"][0]["schedule"] = [
            {"period": 25, "ratio": "1"},
            {"period": 26, "ratio": almost_zero},
        ]
    with pytest.raises(FinanceTripletError, match="精确"):
        FinanceProfile.from_dict(raw)


def test_new_same_id_profile_rebinds_f2_patches_to_new_uncovered_sources():
    first = FinanceProfile.from_dict(_fixture())
    overrides = FinanceOverrides.from_dict(
        _patch(
            first,
            cash_movements={"purchase_invoice": {"payments": [{"offset": 0, "ratio": "1"}]}},
            assets={"asset_a": {"capitalized_at": {"kind": "tax_month", "month": 12}}},
        ),
        profile=first,
    )
    new_data = first.to_dict()
    new_data["amounts"]["replacement_quote"] = _amount()
    new_data["amounts"]["replacement_quote"]["constant"] = _money("200")
    new_data["cash_movements"]["purchase_invoice"]["amount"]["id"] = "replacement_quote"
    new_data["assets"]["asset_a"]["basis_from"] = {
        "kind": "cost",
        "path": "pv_system.upfront_capex",
        "basis": "net_basis",
        "scope": {"kind": "instances", "instances": ["pv1"]},
        "period": {"kind": "scalar"},
    }
    second = FinanceProfile.from_dict(new_data)
    effective = merge_effective(second, overrides)
    assert effective.cash_movements["purchase_invoice"].amount.id == "replacement_quote"
    assert effective.cash_movements["purchase_invoice"].payments[0].ratio == Decimal("1")
    assert effective.assets["asset_a"].basis_from.kind == "cost"
    assert effective.assets["asset_a"].capitalized_at.month == 12


def test_asset_upfront_cost_cannot_use_an_annual_period():
    raw = _fixture()
    raw["assets"]["asset_a"]["basis_from"] = {
        "kind": "cost",
        "path": "pv_system.upfront_capex",
        "basis": "net_basis",
        "scope": {"kind": "all_new_instances"},
        "period": {"kind": "financial_year", "year": 1},
    }
    with pytest.raises(FinanceTripletError, match="期间|来源轴"):
        FinanceProfile.from_dict(raw)
