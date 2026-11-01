"""S2 synthetic affine, tax-base, depreciation and decision evidence.

Only public 7.0.0 finance documents are varied; the small model has no
hourly decision columns.  Solver tests use the existing HiGHS CI adapter.
"""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from test_issue06_s2 import _amount, _builder, _expense, _tiny_solve
from iesplan.application.worker.compute_cases import build_financial_evaluation
from iesplan.computation.result_adapters.highs import _recover_financial_facts


def _money(value: str, unit: str = "CNY") -> dict:
    return {"value": value, "unit": unit}


def _vat_labels(profile: dict) -> None:
    profile["taxes"] = {
        "vat_tag": {"display_name": "Synthetic VAT", "tax_type": "vat",
                    "applies_to": "same.upfront_capex"},
        "income_tag": {"display_name": "Synthetic income", "tax_type": "income_tax",
                       "applies_to": "same.upfront_capex"},
    }
    profile["credit_pools"] = {
        "pool": {"opening_credit": _money("0"),
                 "receivable_policy": "end_receivable", "refunds": []},
    }


def _income(depreciation: list[dict], *, annual=False, items=()) -> dict:
    return {
        "kind": "income_tax", "label_ref": "income_tag", "rate": _money("0.25", "1"),
        "revenue": [], "energy_cost": [],
        "annual_om": ["same.annual_fixed_om"] if annual else [], "variable_om": [],
        "deductible_maintenance": {"mode": "explicit_items", "items": list(items)},
        "depreciation": depreciation, "loss_carryforward_years": 5,
        "opening_losses": [],
    }


def _depr(source: dict, life: int, first: int, residual: dict | None = None) -> dict:
    return {"base_from": source, "method": "straight_line", "life_years": life,
            "first_year": first, "residual": residual or _money("0", "1")}


def _construction_and_annual(profile: dict) -> None:
    """Different leaves of the same component have different VAT treatment."""
    capex = profile["finance_types"]["same"]["upfront_capex"]
    capex["fixed"] = _money("500")
    profile["finance_types"]["same"]["annual_fixed_om"] = {
        "fixed": _money("500", "CNY/a"),
        "linear": {"cap": {"unit_cost": _money("25", "CNY/kW/a")}},
    }
    _vat_labels(profile)
    profile["tax_rules"] = {
        "fixed_construction": {
            "kind": "construction_input_vat", "label_ref": "vat_tag",
            "for": "same.upfront_capex.fixed", "rate": _money("0.1", "1"),
            "deductible_ratio": _money("1", "1"), "credit_pool": "pool",
            "confirmation": {"method": "payment_period"},
        },
        "linear_construction": {
            "kind": "construction_input_vat", "label_ref": "vat_tag",
            "for": "same.upfront_capex.linear.cap", "rate": _money("0.2", "1"),
            "deductible_ratio": _money("0.5", "1"), "credit_pool": "pool",
            "confirmation": {"method": "payment_period"},
        },
        "annual_vat": {
            "kind": "vat", "label_ref": "vat_tag", "settlement": "monthly",
            "in_tax": [], "out_tax": [],
            "om_input_tax": [
                {"for": "same.annual_fixed_om.fixed", "rate": _money("0.1", "1"),
                 "deductible_ratio": _money("1", "1")},
                {"for": "same.annual_fixed_om.linear.cap", "rate": _money("0.2", "1"),
                 "deductible_ratio": _money("0", "1")},
            ],
            "maintenance_input_tax": [], "credit_pool": "pool",
            "confirmation": "same_period",
        },
        "income": _income([
            _depr({"kind": "upfront_capex_net", "finance_type": "same", "scope": {"kind": "all_new_instances"}}, 4, 1,
                  _money("0.1", "1")),
        ], annual=True),
    }
    profile["net_cashflows"] = {
        "recovery": {"at": {"kind": "evaluation_end"}, "direction": "inflow",
                     "amount": {"kind": "source_share", "base_from": {
                         "kind": "upfront_capex_net", "finance_type": "same", "scope": {"kind": "all_new_instances"}},
                         "ratio": _money("0.1", "1")}},
    }


def test_annual_rates_subcomponent_net_basis_and_cash_periods(tmp_path):
    builder = _builder([], fixed_capacity=True, finance_patch=_construction_and_annual)
    solved, facts = _tiny_solve(builder, tmp_path)
    assert solved["cap_a"] == pytest.approx(10000.0)
    # a: 10 kW; b: 100 kW.  Two newly built assets, one existing excluded.
    gross = 2 * 500 + 110 * 200
    yearly = 2 * 500 + 110 * 25
    deductible = 2 * 500 * .1 / 1.1 + (110 * 200) * .2 / 1.2 * .5
    net = gross - deductible
    values = solved
    assert _amount(facts["construction_total"], **values) == pytest.approx(gross)
    assert _amount(facts["annual_fixed_om"], **values) == pytest.approx(yearly)
    for row in facts["annual_operation"]:
        assert _amount(row["fixed_om"], **values) == pytest.approx(yearly)
        assert _amount(row["net"], **values) == pytest.approx(-yearly)
    assert len(facts["annual_operation"]) == 2  # not 8760/8784 fee occurrences
    assert _amount(facts["annual_net_flows"][0], **values) == pytest.approx(-gross)
    assert _amount(facts["annual_net_flows"][1], **values) == pytest.approx(-yearly)
    assert _amount(facts["net_cashflows"][0]["amount"], **values) == pytest.approx(net * .1)
    assert _amount(facts["annual_net_flows"][2], **values) == pytest.approx(-yearly + net * .1)
    rows = facts["tax_cash"]["depreciation"]
    assert [row["base_from"] for row in rows] == [
        {"kind": "upfront_capex_net", "finance_type": "same", "scope": {"kind": "all_new_instances"}}
    ] * 2
    assert all(_amount(row["amount"], **values) == pytest.approx(net * .9 / 4)
               for row in rows)
    provenance = builder.model_sources()
    kinds = {source["kind"] for source in provenance["sources"].values()}
    assert {"vat_period", "income_year", "depreciation", "cost"} <= kinds
    for group in provenance["groups"]:
        assert group["source"] in provenance["sources"]
    assert any(group["role"] == "row:vat" and group["axis"] == "month"
               for group in provenance["groups"])
    assert any(group["role"] == "row:income" and group["axis"] == "year"
               for group in provenance["groups"])
    assert any(output["path"][:3] == ["financial_facts", "tax_cash", "depreciation"]
               for output in provenance["outputs"])
    # VAT credit comes from each covered leaf; the non-deductible annual
    # linear item never disappears from income tax's annual cost basis.
    taxable = facts["tax_cash"]["income"]
    expected_annual_basis = yearly - 2 * 500 * .1 / 1.1
    assert _amount(taxable[1]["taxable_base"], **values) == pytest.approx(
        -expected_annual_basis - net * .9 / 4
    )
    # Real monthly VAT ledger must credit the leaf-scoped annual fixed O&M.
    # The adjacent linear leaf has deductible_ratio=0 and contributes no credit.
    vat_by_month = {row["month"]: row for row in facts["tax_cash"]["vat"]}
    assert len(vat_by_month) == 24
    for month in (12, 24):
        delta = (_amount(vat_by_month[month]["carry"], **solved)
                 - _amount(vat_by_month[month - 1]["carry"], **solved))
        assert delta == pytest.approx(2 * 500 * .1 / 1.1)


def _replacement_profile(profile: dict) -> None:
    _vat_labels(profile)
    profile["tax_rules"] = {
        "vat": {
            "kind": "vat", "label_ref": "vat_tag", "settlement": "monthly",
            "in_tax": [], "out_tax": [], "om_input_tax": [],
            "maintenance_input_tax": [
                {"year": 2, "position": 1, "rate": _money("0.1", "1"),
                 "deductible_ratio": _money("1", "1")},
                {"year": 2, "position": 2, "rate": _money("0.1", "1"),
                 "deductible_ratio": _money("0", "1")},
            ], "credit_pool": "pool", "confirmation": "same_period",
        },
        "income": _income([
            _depr({"kind": "upfront_capex_net", "finance_type": "same", "scope": {"kind": "all_new_instances"}}, 4, 1),
            _depr({"kind": "maintenance_expense_net", "year": 2, "position": 1}, 2, 2),
            _depr({"kind": "maintenance_expense_net", "year": 2, "position": 2},
                  2, 2, _money("50")),
        ], items=[{"year": 2, "position": 1, "deductible": False},
                  {"year": 2, "position": 2, "deductible": False}]),
    }


def test_initial_and_two_same_year_replacements_have_separate_depreciation(tmp_path):
    expenses = [dict(_expense("300"), year=2), dict(_expense("500"), year=2)]
    builder = _builder(expenses, fixed_capacity=True,
                       finance_patch=_replacement_profile,
                       evaluation_patch=lambda e: e.update({
                           "construction_period": 1, "operation_years": 3,
                           "evaluation_years": 4, "construction_payments": [{"t": 1, "ratio": 1}],
                       }))
    solved, facts = _tiny_solve(builder, tmp_path)
    assert solved["cap_a"] == pytest.approx(10000.0)
    values = solved
    depreciation = facts["tax_cash"]["depreciation"]
    identities = [(r["base_from"]["kind"], r["base_from"].get("position"), r["year"])
                  for r in depreciation]
    assert identities.count(("upfront_capex_net", None, 2)) == 1
    for pos in (1, 2):
        assert ("maintenance_expense_net", pos, 2) not in identities
        assert identities.count(("maintenance_expense_net", pos, 3)) == 1
        assert identities.count(("maintenance_expense_net", pos, 4)) == 1
    by_key = {(r["base_from"]["kind"], r["base_from"].get("position"), r["year"]): r
              for r in depreciation}
    assert _amount(by_key[("maintenance_expense_net", 1, 3)]["amount"], **values) == pytest.approx(
        (300 - 300 * .1 / 1.1) / 2
    )
    assert _amount(by_key[("maintenance_expense_net", 2, 3)]["amount"], **values) == pytest.approx(225)
    assert _amount(facts["annual_net_flows"][2], **values) == pytest.approx(-800)
    assert _amount(facts["annual_net_flows"][3], **values) == pytest.approx(0)


def _decision_profile(recovery: str, annual_rate: str):
    def patch(profile: dict) -> None:
        profile["finance_types"]["same"]["annual_fixed_om"] = {
            "linear": {"cap": {"unit_cost": _money(annual_rate, "CNY/kW/a")}}
        }
        if recovery != "absent":
            profile["net_cashflows"] = {
                "recovery": {"at": {"kind": "evaluation_end"}, "direction": "inflow",
                             "amount": {"kind": "source_share", "base_from": {
                                 "kind": "upfront_capex_net", "finance_type": "same", "scope": {"kind": "all_new_instances"}},
                                 "ratio": _money(recovery, "1")}},
            }
    return patch


@pytest.mark.parametrize("recovery,annual_rate,capacity", [
    ("absent", "0", 0.0),
    ("0", "0", 0.0),
    ("1.5", "30", 10000.0),
    ("1.5", "80", 0.0),
])
def test_recovery_and_annual_fee_switch_main_solution(tmp_path, recovery, annual_rate, capacity):
    builder = _builder([], finance_patch=_decision_profile(recovery, annual_rate))
    solved, facts, model = _tiny_solve(builder, tmp_path, with_model=True)
    assert solved["cap_a"] == pytest.approx(capacity, abs=1e-6)
    assert len(facts["net_cashflows"]) == (0 if recovery == "absent" else 1)
    if recovery == "absent":
        assert _amount(facts["annual_net_flows"][2], **solved) == pytest.approx(0)
    else:
        assert _amount(facts["net_cashflows"][0]["amount"], **solved) == pytest.approx(
            (20000 + .2 * capacity) * float(recovery)
        )
    # Adapter's actual affine recovery path, on the very same MPS primal vector;
    # Worker projects it and evaluates, without reading the draft or rebuilding tax.
    columns = list(solved)
    restored = _recover_financial_facts(
        facts, {name: index for index, name in enumerate(columns)},
        [solved[name] for name in columns],
    )
    sealed = build_financial_evaluation(
        SimpleNamespace(evaluation={
            "construction_period": 0, "operation_years": 2,
            "evaluation_years": 2, "discount_rate": 0.0,
        }),
        builder.finance,
        SimpleNamespace(math_status="solved", outputs={
            "solution": {}, "financial_facts": restored,
        }),
    )
    assert sealed["schema_version"] == "1.0.0"
    assert sealed["net_cashflows"] == restored["net_cashflows"]
    assert sealed["annual_net_flows"] == restored["annual_net_flows"]
    objective = model.objective.constant + sum(
        coef * solved[name] for (name, _), coef in model.objective.terms
    )
    assert sum(float(cf) for cf in sealed["annual_net_flows"]) == pytest.approx(objective)
    assert float(sealed["evaluation"]["npv"]) == pytest.approx(objective)


@pytest.mark.parametrize("currency", ("CNY", "USD"))
def test_same_currency_annual_fixed_and_linear_end_to_end(tmp_path, currency):
    """One genuine HiGHS solution → paired 6.0.0 adapter → year/NPV/components.

    Currency is retained, never converted: a=10kW, b=100kW new, old
    existing excluded.  Annual: 2*500 + 25*(10+100) = 3750;
    upfront: 200*(10+100) = 22000; two operation years, zero discount.
    """
    import json

    from iesplan.computation.generators.mps import write_mps
    from iesplan.computation.result_adapters.highs import HIGHS_ADAPTER
    from test_highs_result_adapter import (
        PARAMETERS_PATH, PROBLEM_PATH, SOLUTION_PATH, _bundle,
        _parameters, _receipt,
    )

    def annual(profile):
        profile["finance_types"]["same"]["annual_fixed_om"] = {
            "fixed": _money("500", f"{currency}/a"),
            "linear": {"cap": {"unit_cost": _money("25", f"{currency}/kW/a")}},
        }

    builder = _builder([], currency=currency, fixed_capacity=True, finance_patch=annual)
    solved, facts, model = _tiny_solve(builder, tmp_path, with_model=True)
    mps_text, offset = write_mps(model)
    doc = json.loads(_parameters(offset=offset, objective_unit=currency).decode("utf-8"))
    doc["unit_map"] = dict(builder.unit_map)
    doc["financial_facts"] = dict(facts)
    doc["components"] = {name: dict(value) for name, value in builder.finance_components().items()}
    bundle = _bundle({
        PROBLEM_PATH: mps_text.encode("utf-8"),
        PARAMETERS_PATH: (json.dumps(doc, sort_keys=True) + "\n").encode("utf-8"),
    })
    adapted = HIGHS_ADAPTER.adapt(
        bundle, _receipt(), {SOLUTION_PATH: (tmp_path / "output" / "solution.sol").read_bytes()},
    )
    assert adapted.math_status == "solved"
    solution = adapted.outputs["solution"]
    assert solution["objective_unit"] == currency
    assert solution["objective_value"] == pytest.approx(-29500.0)
    components = solution["components"]
    assert components["a__annual_fixed_om"]["unit"] == f"{currency}/a"
    assert components["b__annual_fixed_om"]["unit"] == f"{currency}/a"
    assert components["a__annual_fixed_om"]["value"] == pytest.approx(750.0)
    assert components["b__annual_fixed_om"]["value"] == pytest.approx(3000.0)
    restored = adapted.outputs["financial_facts"]
    assert restored["currency"] == currency
    assert float(restored["annual_fixed_om"]) == pytest.approx(3750.0)
    assert float(restored["construction_total"]) == pytest.approx(22000.0)
    assert [float(row["fixed_om"]) for row in restored["annual_operation"]] == pytest.approx([3750, 3750])
    assert [float(cf) for cf in restored["annual_net_flows"]] == pytest.approx([-22000, -3750, -3750])
    sealed = build_financial_evaluation(
        SimpleNamespace(evaluation={
            "construction_period": 0, "operation_years": 2,
            "evaluation_years": 2, "discount_rate": 0.0,
        }), builder.finance, adapted,
    )
    assert sealed["currency"] == currency
    assert float(sealed["evaluation"]["npv"]) == pytest.approx(solution["objective_value"])
    assert sealed["annual_net_flows"] == list(restored["annual_net_flows"])
    assert solved["cap_a"] == pytest.approx(10000)
