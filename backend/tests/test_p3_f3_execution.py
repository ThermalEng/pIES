"""One real HiGHS primal for baseline references, constraints and evaluation."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_finance_s2_execution import _income, _money, _vat_labels
from test_issue06_s2 import _builder, _tiny_solve
from test_p3_f1_execution import _adapt, _rebuild
from test_p3_f3_comparison import declared

from iesplan.application.tasks.baselines import check_comparison_inputs
from iesplan.application.worker.compute_cases import build_financial_evaluation
from iesplan.computation.generators.highs_milp import DESCRIPTOR
from iesplan.core.jsonutil import canonical_json
from iesplan.finance import freeze_comparison, parse_comparison
from iesplan.tasks import InvalidRequestError


def _freeze(builder, raw):
    builder.evaluation = replace(
        builder.evaluation,
        comparison=freeze_comparison(parse_comparison(raw, years=2), years=2, currency="CNY"),
    )
    return builder


def _seal(builder, result):
    return build_financial_evaluation(
        SimpleNamespace(
            evaluation={
                "construction_period": 0,
                "operation_years": 2,
                "evaluation_years": 2,
                "discount_rate": 0,
            }
        ),
        builder.finance,
        result,
    )


def test_difference_objective_adapter_and_metrics_select_same_cashflow(tmp_path):
    builder = _freeze(_builder([], fixed_capacity=True), declared())
    _, declaration, model = _tiny_solve(builder, tmp_path, with_model=True)
    result = _adapt(builder, model, declaration, tmp_path)
    fin = _seal(builder, result)
    candidate = [float(value) for value in fin["annual_net_flows"]]
    comparison = fin["comparison"]
    expected = [c - b for c, b in zip(candidate, [-100, 60, 60], strict=True)]
    assert [float(v) for v in comparison["evaluation_net_flows"]] == pytest.approx(expected)
    assert float(fin["evaluation"]["npv"]) == pytest.approx(sum(expected))
    assert float(result.outputs["solution"]["objective_value"]) == pytest.approx(sum(expected))
    assert list(comparison["baseline_net_flows"]) == ["-100", "60", "60"]
    assert comparison["facts"]["limit"]["values"] == ["0", "10", "20"]
    assert any(row["path"] == ["financial_facts", "comparison"] for row in builder.model_sources()["outputs"])


def _budget(profile):
    profile["amounts"] = {
        "investment": {
            "unit": "CNY",
            "axis": "scalar",
            "constant": _money("0"),
            "terms": {
                "cost": {
                    "coefficient": _money("1", "1"),
                    "source": {
                        "kind": "cost",
                        "path": "same.upfront_capex",
                        "basis": "raw",
                        "scope": {"kind": "instances", "instances": ["a"]},
                        "period": {"kind": "scalar"},
                    },
                }
            },
        }
    }


def test_frozen_budget_binds_directly_into_constraint_with_units_and_provenance(tmp_path):
    base = _builder([], finance_patch=_budget)

    def mutate(doc):
        doc["devices"]["b"]["asset_origin"] = "existing"
        doc["planning"]["objective"]["expression"] = "cap_a"
        doc["planning"]["constraints"] = {
            "budget_limit": {
                "type": "amount_relation",
                "expression": "candidate <= budget",
                "references": {
                    "candidate": {"kind": "amount", "id": "investment", "period": {"kind": "scalar"}},
                    "budget": {"kind": "baseline", "id": "limit", "period": {"kind": "scalar"}},
                },
            }
        }

    builder = _rebuild(base, mutate)
    raw = declared("reference")
    raw["baseline"]["facts"]["budget"] = {"axis": "scalar", "unit": "CNY", "values": ["600"]}
    _freeze(builder, raw)
    artifact = SimpleNamespace(canonical_text=canonical_json(builder.doc))
    check_comparison_inputs(builder.evaluation, artifact, builder.finance, "test", DESCRIPTOR.capabilities)
    _, declaration, model = _tiny_solve(builder, tmp_path, with_model=True)
    result = _adapt(builder, model, declaration, tmp_path)
    capacity = next(row for row in result.outputs["solution"]["variables"] if row["name"] == "cap_a")
    assert float(capacity["value"]) == pytest.approx(3)
    assert float(result.outputs["financial_facts"]["amounts"][0]["values"][0]["value"]) == pytest.approx(600)
    assert result.outputs["financial_facts"]["comparison"]["mode"] == "reference"
    provenance = builder.model_sources()["sources"]
    assert (
        provenance["relation:baseline.limit"]["location"]["field_path"] == "evaluation.comparison.facts.limit"
    )
    assert (
        "relation:baseline.limit" in provenance["reference:planning.amount.budget_limit.budget"]["depends_on"]
    )


@pytest.mark.parametrize("candidate,baseline,delta", [("4", "10", 6), ("10", "4", -6)])
def test_difference_subtracts_after_separate_tax_not_tax_of_difference(tmp_path, candidate, baseline, delta):
    def profile_patch(profile):
        for ft in profile["finance_types"].values():
            ft["upfront_capex"]["linear"]["cap"]["unit_cost"] = _money("0", "CNY/kW")
        profile["amounts"] = {
            "expense": {"unit": "CNY", "axis": "scalar", "constant": _money(candidate), "terms": {}}
        }
        profile["cash_movements"] = {
            "operating": {
                "amount": {"kind": "amount", "id": "expense", "period": {"kind": "scalar"}},
                "price_basis": "tax_inclusive",
                "direction": "outflow",
                "recognition": {"kind": "at", "at": {"kind": "financial_year", "year": 1}},
                "payments": [{"offset": 0, "ratio": "1"}],
            }
        }
        _vat_labels(profile)
        profile["tax_rules"] = {
            "cit": {**_income([]), "rate": _money("0.2", "1"), "movement_cost": ["operating"]}
        }

    builder = _builder([], finance_patch=profile_patch)
    raw = declared()
    raw["baseline"]["annual_net_flows"] = ["0", "-" + baseline, "0"]
    _freeze(builder, raw)
    _, declaration, model = _tiny_solve(builder, tmp_path, with_model=True)
    result = _adapt(builder, model, declaration, tmp_path)
    sealed = _seal(builder, result)
    assert [float(row["tax"]) for row in sealed["tax_cash"]["income"]] == [0, 0]
    assert float(sealed["comparison"]["evaluation_net_flows"][1]) == pytest.approx(delta)
    assert float(sealed["evaluation"]["npv"]) == pytest.approx(delta)


def test_capability_gate_and_baseline_period_errors_at_submission():
    builder = _freeze(_builder([]), declared())
    artifact = SimpleNamespace(canonical_text=canonical_json(builder.doc))
    with pytest.raises(InvalidRequestError) as error:
        check_comparison_inputs(builder.evaluation, artifact, builder.finance, "custom", ())
    assert error.value.code == "TASK-REQ-007"
    assert error.value.params["missing_capabilities"] == [
        "finance.cashflow_difference",
        "finance.financial_facts",
        "finance.frozen_baseline",
    ]


def test_period_indexed_baseline_amount_participates_in_cash_without_automatic_subtraction(tmp_path):
    def patch(profile):
        for entry in profile["finance_types"].values():
            entry["upfront_capex"]["linear"]["cap"]["unit_cost"] = _money("0", "CNY/kW")
        profile["amounts"] = {
            "revenue": {
                "unit": "CNY",
                "axis": "financial_year",
                "constant": _money("0"),
                "terms": {
                    "base": {
                        "coefficient": _money("2", "1"),
                        "source": {"kind": "baseline", "id": "limit", "period": {"kind": "current"}},
                    }
                },
            }
        }
        profile["cash_movements"] = {
            "sales": {
                "amount": {"kind": "amount", "id": "revenue", "period": {"kind": "current"}},
                "direction": "inflow",
                "price_basis": "tax_inclusive",
                "recognition": {"kind": "each", "axis": "financial_year", "first": 1, "last": 2},
                "payments": [{"offset": 0, "ratio": "1"}],
            }
        }

    builder = _freeze(_builder([], finance_patch=patch), declared("reference"))
    artifact = SimpleNamespace(canonical_text=canonical_json(builder.doc))
    check_comparison_inputs(builder.evaluation, artifact, builder.finance, "test", DESCRIPTOR.capabilities)
    _, declaration, model = _tiny_solve(builder, tmp_path, with_model=True)
    result = _adapt(builder, model, declaration, tmp_path)
    fin = _seal(builder, result)
    assert [float(value) for value in fin["annual_net_flows"]] == [0, 20, 40]
    assert fin["comparison"]["mode"] == "reference"
    assert "baseline_net_flows" not in fin["comparison"]
    assert float(fin["evaluation"]["npv"]) == pytest.approx(60)
    source = builder.model_sources()["sources"][
        'relation:financial:["amount","revenue","financial_year",1,"value"]'
    ]
    assert "relation:baseline.limit" in source["depends_on"]


def _usd_declared(unit):
    raw = declared("reference")
    raw["baseline"]["currency"] = "USD"
    raw["baseline"]["facts"] = {
        "budget": {"unit": unit, "axis": "scalar", "values": ["100" if unit == "USD/kW" else "600"]},
    }
    raw["facts"] = {"limit": {"kind": "declared_fact", "id": "budget"}}
    return raw


@pytest.mark.parametrize("unit", ["USD", "USD/kW"])
def test_usd_baseline_amount_and_rate_keep_project_currency(tmp_path, unit):
    def patch(profile):
        _budget(profile)
        investment = profile["amounts"]["investment"]
        investment["unit"] = "USD"
        investment["constant"] = _money("0", "USD")
        investment["terms"]["cost"]["coefficient"] = _money("1", "1")
        profile["amounts"]["ceiling"] = {
            "unit": "USD",
            "axis": "scalar",
            "constant": _money("0", "USD"),
            "terms": {
                "rate": {
                    "coefficient": _money("6", "kW") if unit == "USD/kW" else _money("1", "1"),
                    "source": {"kind": "baseline", "id": "limit", "period": {"kind": "scalar"}},
                }
            },
        }

    def mutate(doc):
        doc["devices"]["b"]["asset_origin"] = "existing"
        doc["planning"]["objective"]["expression"] = "cap_a"
        doc["planning"]["constraints"] = {
            "budget_limit": {
                "type": "amount_relation",
                "expression": "candidate <= budget",
                "references": {
                    "candidate": {"kind": "amount", "id": "investment", "period": {"kind": "scalar"}},
                    "budget": {"kind": "amount", "id": "ceiling", "period": {"kind": "scalar"}},
                },
            }
        }

    builder = _rebuild(_builder([], finance_patch=patch, currency="USD"), mutate)
    builder.evaluation = replace(
        builder.evaluation,
        comparison=freeze_comparison(
            parse_comparison(_usd_declared(unit), years=2), years=2, currency="USD"
        ),
    )
    artifact = SimpleNamespace(canonical_text=canonical_json(builder.doc))
    check_comparison_inputs(builder.evaluation, artifact, builder.finance, "test", DESCRIPTOR.capabilities)
    _, declaration, model = _tiny_solve(builder, tmp_path, with_model=True)
    result = _adapt(builder, model, declaration, tmp_path)
    capacity = next(row for row in result.outputs["solution"]["variables"] if row["name"] == "cap_a")
    assert float(capacity["value"]) == pytest.approx(3)


def _tax_month_baseline(first, last, recognition_at):
    def patch(profile):
        for entry in profile["finance_types"].values():
            entry["upfront_capex"]["linear"]["cap"]["unit_cost"] = _money("0", "CNY/kW")
        profile["amounts"] = {
            "revenue": {
                "unit": "CNY",
                "axis": "financial_year",
                "constant": _money("0"),
                "terms": {
                    "base": {
                        "coefficient": _money("1", "1"),
                        "source": {
                            "kind": "baseline",
                            "id": "monthly",
                            "period": {"kind": "year_months", "year": "current"},
                        },
                    }
                },
            }
        }
        profile["cash_movements"] = {
            "sales": {
                "amount": {"kind": "amount", "id": "revenue", "period": {"kind": "current"}},
                "direction": "inflow",
                "price_basis": "tax_inclusive",
                "recognition": recognition_at,
                "payments": [{"offset": 0, "ratio": "1"}],
            }
        }

    raw = declared("reference")
    raw["baseline"]["facts"] = {
        "monthly": {"unit": "CNY", "axis": "tax_month", "values": [str(i) for i in range(1, 25)]}
    }
    raw["facts"] = {"monthly": {"kind": "declared_fact", "id": "monthly"}}
    return _freeze(_builder([], finance_patch=patch), raw)


def test_baseline_current_months_are_checked_for_every_consumed_period():
    each = {"kind": "each", "axis": "financial_year", "first": 1, "last": 2}
    builder = _tax_month_baseline(1, 2, each)
    artifact = SimpleNamespace(canonical_text=canonical_json(builder.doc))
    check_comparison_inputs(builder.evaluation, artifact, builder.finance, "test", DESCRIPTOR.capabilities)

    initial = {"kind": "at", "at": {"kind": "financial_year", "year": 0}}
    builder = _tax_month_baseline(0, 0, initial)
    artifact = SimpleNamespace(canonical_text=canonical_json(builder.doc))
    with pytest.raises(InvalidRequestError) as error:
        check_comparison_inputs(
            builder.evaluation, artifact, builder.finance, "test", DESCRIPTOR.capabilities,
        )
    assert error.value.code == "TASK-REQ-015"
    assert error.value.location["field"] == (
        "effective_finance.amounts.revenue.terms.base.source.period"
    )
