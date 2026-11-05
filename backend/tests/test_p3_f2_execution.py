"""F2 uses one real HiGHS primal for payments, assets, taxes and provenance."""

import json
from collections.abc import Mapping
from types import SimpleNamespace

import pytest
from test_finance_s2_execution import _income, _money, _vat_labels
from test_issue06_s2 import _builder, _tiny_solve
from test_p3_f1_contract import _amount
from test_p3_f1_execution import _adapt

from iesplan.application.worker.compute_cases import build_financial_evaluation
from iesplan.computation.generators.highs_milp import DESCRIPTOR as HIGHS_DESCRIPTOR


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _relations(profile):
    for entry in profile["finance_types"].values():
        entry["upfront_capex"]["linear"]["cap"]["unit_cost"] = _money("0", "CNY/kW")
    profile["amounts"] = {"purchase_quote": _amount(), "sale_quote": _amount()}
    profile["amounts"]["purchase_quote"]["constant"] = _money("110")
    profile["amounts"]["sale_quote"]["constant"] = _money("200")
    profile["cash_movements"] = {
        "purchase": {
            "amount": {"kind": "amount", "id": "purchase_quote", "period": {"kind": "scalar"}},
            "price_basis": "tax_inclusive", "direction": "outflow",
            "recognition": {"kind": "at", "at": {"kind": "financial_year", "year": 0}},
            "payments": [{"offset": 0, "ratio": "0.4"}, {"offset": 3, "ratio": "0.6"}],
        },
        "sales": {
            "amount": {"kind": "amount", "id": "sale_quote", "period": {"kind": "scalar"}},
            "price_basis": "tax_exclusive", "direction": "inflow",
            "recognition": {"kind": "each", "axis": "financial_year", "first": 1, "last": 2},
            "payments": [{"offset": 0, "ratio": "1"}],
        },
    }


    profile["assets"] = {"machine": {
        "basis_from": {"kind": "movement", "id": "purchase", "basis": "net_basis",
                       "period": {"kind": "financial_year", "year": 0}},
        "capitalized_at": {"kind": "financial_year", "year": 0}, "available_from": 1,
    }}
    _vat_labels(profile)
    profile["tax_rules"] = {
        "vat": {
            "kind": "vat", "label_ref": "vat_tag", "settlement": "monthly",
            "in_tax": [], "out_tax": [], "om_input_tax": [], "maintenance_input_tax": [],
            "credit_pool": "pool", "confirmation": "same_period",
            "movement_input_tax": [{
                "movement": "purchase", "rate": _money("0.1", "1"),
                "deductible_ratio": _money("1", "1"),
                "confirmation": {"method": "explicit_schedule", "occurrences": [{
                    "at": {"kind": "financial_year", "year": 0},
                    "schedule": [{"period": 1, "ratio": "1"}],
                }]},
            }],
            "movement_output_tax": [{
                "movement": "sales", "rate": _money("0.1", "1"),
                "confirmation": {"method": "recognition_period"},
            }],
        },
        "income": {
            **_income([{
                "base_from": {"kind": "asset", "id": "machine"},
                "method": "straight_line", "life_years": 5, "first_year": 1,
                "residual": _money("0", "1"),
            }]),
            "movement_revenue": ["sales"], "derived_tax_cost": ["levy"],
        },
        "levy": {
            "kind": "derived_tax", "label_ref": "vat_tag", "axis": "tax_month",
            "base_from": {"kind": "tax", "source": {"kind": "pool", "id": "pool"},
                          "measure": "paid", "period": {"kind": "current"}},
            "rate": _money("0.04", "1"), "payment_lag": 1,
        },
    }


def test_builtin_highs_descriptor_declares_completed_f2_capabilities():
    required = {
        "finance.cash_movements",
        "finance.assets",
        "finance.derived_tax",
        "finance.financial_facts",
    }
    assert required.issubset(HIGHS_DESCRIPTOR.capabilities)


def test_f2_payments_asset_and_lagged_tax_match_one_primal_and_frozen_outputs(tmp_path):
    builder = _builder([], fixed_capacity=True, finance_patch=_relations)
    _, declaration, model = _tiny_solve(builder, tmp_path, with_model=True)
    result = _adapt(builder, model, declaration, tmp_path)
    facts = result.outputs["financial_facts"]
    movements = {(row["id"], row["period"]): row for row in facts["cash_movements"]}
    assert set(movements) == {("purchase", 0), ("sales", 1), ("sales", 2)}
    purchase = movements[("purchase", 0)]
    assert float(purchase["gross"]) == pytest.approx(110)
    assert float(purchase["deductible_input"]) == pytest.approx(10)
    assert float(purchase["net_basis"]) == pytest.approx(100)
    assert [payment["at"]["year"] for payment in purchase["payments"]] == [0, 3]
    assert [float(payment["amount"]) for payment in purchase["payments"]] == pytest.approx([44, 66])
    assert float(purchase["end_unpaid"]) == pytest.approx(66)
    assert float(movements[("sales", 1)]["gross"]) == pytest.approx(220)
    asset = facts["assets"][0]
    assert asset["id"] == "machine"
    assert float(asset["basis"]) == pytest.approx(100)
    assert [float(row["value"]) for row in asset["book_values"]] == pytest.approx([80, 60])
    vat_rules = {row["month"]: row for row in facts["tax_cash"]["vat_rules"]}
    assert len(vat_rules) == 24
    assert float(vat_rules[1]["deductible_input"]) == pytest.approx(10)
    assert [float(vat_rules[month]["output_tax"]) for month in (12, 24)] == pytest.approx([20, 20])
    derived = {row["period"]: row for row in facts["tax_cash"]["derived"]}
    assert float(derived[12]["assessed"]) == pytest.approx(0.4)
    assert derived[12]["payment_period"] == 13
    assert float(derived[24]["paid"]) == pytest.approx(0.8)
    assert derived[24]["payment_period"] == 25
    assert [float(row["tax"]) for row in facts["tax_cash"]["income"]] == pytest.approx([44.9, 44.8])
    assert [float(value) for value in facts["annual_net_flows"]] == pytest.approx([-44, 165.1, 154.8])
    sealed = build_financial_evaluation(SimpleNamespace(evaluation={
        "construction_period": 0, "operation_years": 2, "evaluation_years": 2, "discount_rate": 0,
    }), builder.finance, result)
    for field in ("cash_movements", "assets", "tax_cash", "annual_net_flows"):
        assert _plain(sealed[field]) == _plain(facts[field])

    provenance = builder.model_sources()
    sources = provenance["sources"]
    paths = {tuple(output["path"]): output["sources"] for output in provenance["outputs"]}
    for output in provenance["outputs"]:
        assert set(output["sources"]) <= sources.keys(), output
    for field, rows in (("cash_movements", facts["cash_movements"]), ("assets", facts["assets"])):
        for index in range(len(rows)):
            assert paths[("financial_facts", field, index)]
    for field in ("vat_rules", "derived"):
        for index in range(len(facts["tax_cash"][field])):
            assert paths[("financial_facts", "tax_cash", field, index)]
    assert "relation:depreciation.income.0" in paths[("financial_facts", "assets", 0)]
    movement_source = paths[("financial_facts", "cash_movements", 0)][0]
    assert sources[movement_source]["location"]["field_path"] == "finance.cash_movements.purchase.amount"
    assert any(json.loads(identity.removeprefix("relation:financial:")) ==
               ["tax_rule", "vat", "tax_month", 1, "deductible_input"]
               for identity in paths[("financial_facts", "tax_cash", "vat_rules", 0)])
