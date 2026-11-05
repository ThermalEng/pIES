"""F3 public comparison input and stable sealed-fact selectors."""

from copy import deepcopy

import pytest

from iesplan.finance import ComparisonError, freeze_comparison, parse_comparison


def declared(mode="difference"):
    return {
        "mode": mode,
        "baseline": {
            "kind": "declared",
            "currency": "CNY",
            "evaluation_years": 2,
            "annual_net_flows": ["-100", "60", "60"] if mode == "difference" else None,
            "facts": {"budget": {"unit": "CNY", "axis": "financial_year", "values": ["0", "10", "20"]}},
        },
        "facts": {"limit": {"kind": "declared_fact", "id": "budget"}},
        "comparability": {"service_basis": "same demand", "differences": "different investment"},
    }


def test_declared_input_freezes_values_without_retaining_mutable_request():
    raw = declared()
    parsed = parse_comparison(raw, years=2)
    frozen = freeze_comparison(parsed, years=2, currency="CNY")
    raw["baseline"]["facts"]["budget"]["values"][1] = "999"
    assert frozen.facts["limit"].values == ("0", "10", "20")
    assert frozen.baseline_net_flows == ("-100", "60", "60")
    with pytest.raises(TypeError):
        frozen.facts["other"] = frozen.facts["limit"]


@pytest.mark.parametrize(
    "mutate,field",
    [
        (lambda x: x.update(unknown=1), "comparison"),
        (lambda x: x["baseline"].update(annual_net_flows=["0"]), "annual_net_flows"),
        (lambda x: x["baseline"]["facts"]["budget"].update(values=[0, 1, 2]), "values"),
        (lambda x: x["comparability"].update(differences=" "), "differences"),
        (lambda x: x["facts"]["limit"].update(id="missing"), "limit"),
    ],
)
def test_invalid_comparison_is_locatable(mutate, field):
    raw = declared()
    mutate(raw)
    with pytest.raises(ComparisonError) as error:
        freeze_comparison(parse_comparison(raw, years=2), years=2, currency="CNY")
    assert field in error.value.field


def test_reference_does_not_create_zero_cashflow_or_exchange_currency():
    raw = declared("reference")
    frozen = freeze_comparison(parse_comparison(raw, years=2), years=2, currency="CNY")
    assert frozen.baseline_net_flows is None
    raw["baseline"]["annual_net_flows"] = ["0"] * 3
    with pytest.raises(ComparisonError, match="null"):
        parse_comparison(raw, years=2)
    raw = declared()
    raw["baseline"]["currency"] = "USD"
    with pytest.raises(ComparisonError, match="currency"):
        freeze_comparison(parse_comparison(raw, years=2), years=2, currency="CNY")


def task_request(selectors):
    return {
        "mode": "reference",
        "baseline": {"kind": "task_result", "task_id": 1, "evidence_id": 2},
        "facts": selectors,
        "comparability": {"service_basis": "same", "differences": "none"},
    }


def select(financial, selectors):
    return freeze_comparison(
        parse_comparison(task_request(selectors), years=2),
        years=2,
        currency="CNY",
        financial=financial,
        source={"kind": "task_result", "task_id": 1, "evidence_id": 2, "attempt_id": 3, "object_id": 4},
    )


def test_tax_selectors_use_rule_identity_and_actual_payment_period_not_shared_pool_or_accrual():
    fin = {
        "currency": "CNY",
        "evaluation_years": 2,
        "tax_cash": {
            "vat_rules": [
                {"rule": "sale", "month": m, "output_tax": "7", "deductible_input": "0"} for m in range(1, 25)
            ],
            "vat": [
                {
                    "pool": "p",
                    "month": m,
                    "paid": "3",
                    "refund_claimed": "0",
                    "refund_arrived": "0",
                    "carry": "4",
                    "receivable": "2",
                }
                for m in range(1, 25)
            ],
            "income": [
                {
                    "rule": "cit",
                    "year": y,
                    "taxable_base": "20",
                    "loss_used": "0",
                    "assessed": "5",
                    "tax": "5",
                }
                for y in (1, 2)
            ],
            "derived": [
                {
                    "rule": "levy",
                    "axis": "financial_year",
                    "period": y,
                    "payment_period": y + 1,
                    "assessed": "2",
                    "paid": "2",
                }
                for y in (1, 2)
            ],
        },
    }
    selectors = {
        "output": {"kind": "tax", "source": {"kind": "rule", "id": "sale"}, "measure": "output_tax"},
        "pool": {"kind": "tax", "source": {"kind": "pool", "id": "p"}, "measure": "paid"},
        "credit": {"kind": "tax", "source": {"kind": "pool", "id": "p"}, "measure": "receivable"},
        "cit": {"kind": "tax", "source": {"kind": "rule", "id": "cit"}, "measure": "assessed"},
        "levy": {"kind": "tax", "source": {"kind": "rule", "id": "levy"}, "measure": "paid"},
    }
    facts = select(fin, selectors).facts
    assert facts["output"].values == ("7",) * 24
    assert facts["pool"].values == ("3",) * 24
    assert facts["credit"].values == ("2",) * 24
    assert facts["cit"].values == ("0", "5", "5")
    assert facts["levy"].values == ("0", "0", "2")
    missing = deepcopy(fin)
    del missing["tax_cash"]["vat_rules"][3]["output_tax"]
    with pytest.raises(ComparisonError) as error:
        select(missing, selectors)
    assert "facts.output" in error.value.field
    assert "output_tax" in str(error.value)


def test_missing_amount_period_and_unknown_tax_are_not_filled_from_current_configuration():
    fin = {
        "currency": "CNY",
        "evaluation_years": 2,
        "amounts": [
            {
                "id": "budget",
                "axis": "financial_year",
                "unit": "CNY",
                "values": [{"period": 1, "value": "10"}],
            }
        ],
        "tax_cash": {"vat_rules": [], "vat": [], "income": [], "derived": []},
    }
    for selector in (
        {"kind": "amount", "id": "budget"},
        {"kind": "tax", "source": {"kind": "rule", "id": "missing"}, "measure": "paid"},
    ):
        with pytest.raises(ComparisonError):
            select(fin, {"missing": selector})


def test_all_non_tax_selector_kinds_preserve_occurrence_payment_and_asset_identity():
    financial = {
        "currency": "CNY",
        "evaluation_years": 2,
        "construction_period": 1,
        "annual_net_flows": ["-100", "-10", "30"],
        "annual_operation": [{"financial_year": 2, "energy_net": "30"}],
        "amounts": [
            {"id": "budget", "axis": "scalar", "unit": "CNY/a", "values": [{"period": 0, "value": "7"}]}
        ],
        "cash_movements": [
            {
                "id": "purchase",
                "axis": "financial_year",
                "period": 0,
                "direction": "outflow",
                "gross": "100",
                "net_basis": "90",
                "payments": [
                    {"at": {"kind": "financial_year", "year": 1}, "amount": "40"},
                    {"at": {"kind": "financial_year", "year": 3}, "amount": "60"},
                ],
            }
        ],
        "assets": [
            {
                "id": "machine",
                "basis": "90",
                "book_values": [{"year": 1, "value": "90"}, {"year": 2, "value": "81"}],
            }
        ],
        "tax_cash": {
            "depreciation": [{"base_from": {"kind": "asset", "id": "machine"}, "year": 2, "amount": "9"}]
        },
    }
    selectors = {
        "energy": {"kind": "operation", "measure": "energy_net"},
        "net": {"kind": "cashflow", "measure": "net"},
        "rate": {"kind": "amount", "id": "budget"},
        "gross": {"kind": "movement", "id": "purchase", "measure": "gross"},
        "paid": {"kind": "movement", "id": "purchase", "measure": "paid"},
        "basis": {"kind": "asset", "id": "machine", "measure": "basis"},
        "depreciation": {"kind": "asset", "id": "machine", "measure": "depreciation"},
        "book": {"kind": "asset", "id": "machine", "measure": "book_value"},
    }
    facts = select(financial, selectors).facts
    assert facts["energy"].values == ("0", "0", "30")
    assert facts["net"].values == ("-100", "-10", "30")
    assert facts["rate"].unit == "CNY/a" and facts["rate"].values == ("7",)
    assert facts["gross"].values == ("100", "0", "0")
    assert facts["paid"].values == ("0", "40", "0")
    assert facts["basis"].axis == "scalar" and facts["basis"].values == ("90",)
    assert facts["depreciation"].values == ("0", "0", "9")
    assert facts["book"].values == ("0", "90", "81")
