"""P3 exit: one public two-year network, finance, tax and baseline chain.

Both alternatives are explicitly and independently solved. The candidate
generator only receives the selected frozen baseline facts, never a model to
solve. Expected values below are hand accounts, not a second tax engine.
"""

import json
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_finance_s2_execution import _money
from test_p3_f2_execution import _relations
from test_p3_network_boundaries import _solve
from test_p3_port_network import _inputs
from test_project_model import _finance_payload

from iesplan.application.tasks.baselines import check_comparison_inputs
from iesplan.application.tasks.finance_relations import check_financial_relation_inputs
from iesplan.application.worker.compute_cases import build_financial_evaluation
from iesplan.assembly import validate_assembly_doc
from iesplan.computation import materialize_non_predictive_series
from iesplan.computation.generators.highs_milp import HIGHS_MILP_PROVIDER
from iesplan.finance import FinanceProfile, freeze_comparison, merge_effective, parse_comparison
from iesplan.tasks import parse_evaluation_input


def _case(efficiency, unit_price, comparison=None):
    original, documents = _inputs()
    doc = json.loads(original[0].canonical_text)
    doc["devices"]["route"]["asset_origin"] = "new"
    doc["devices"]["route"]["properties"]["efficiency"] = {"value": efficiency, "unit": "1"}
    doc["planning"]["variables"]["route_capacity"].update(lower_bound="0", upper_bound="10")
    doc["planning"]["objective"] = {"sense": "maximize", "expression": "system.after_tax_npv"}
    doc["finance_binding"]["instances"]["route"] = {
        "type": "costed",
        "finance_type": "same",
        "drivers": {"cap": {"property": "capacity"}},
    }
    doc["system_equations"] = {
        "delivery": {
            "constants": {"start": {"value": 0, "unit": "kWh"}},
            "references": {
                "actual": {"kind": "interface", "instance": "route", "interface": "power_out"},
            },
            "variables": {
                "meter": {"unit": "kW", "valid_range": {"minimum": 0, "maximum": None}},
                "energy": {
                    "unit": "kWh",
                    "valid_range": {"minimum": 0, "maximum": None},
                    "initial": {"property_ref": "start"},
                },
            },
            "relations": [
                {"id": "meter", "expression": "meter[t] = actual[t]"},
                {"id": "energy", "expression": "energy[t] = energy[t-1] + meter[t] * step_duration"},
            ],
        }
    }
    doc["outputs"]["series"] = [
        "source.power_out",
        "source.unused",
        "route.power_in",
        "route.power_out",
        "load.electricity_in",
    ]
    doc["outputs"]["system_series"] = [
        {"block": "delivery", "variable": name} for name in ("meter", "energy")
    ]
    profile = _finance_payload()
    profile["finance_types"] = {
        "same": {"upfront_capex": {"linear": {"cap": {"unit_cost": _money("0", "CNY/kW")}}}},
    }
    _relations(profile)
    # The asset purchase is its own occurrence/payment, not a second capex.
    profile["amounts"]["purchase_quote"] = {
        "unit": "CNY",
        "axis": "scalar",
        "constant": _money("0"),
        "terms": {
            "scale": {
                "coefficient": _money(unit_price, "CNY/kW"),
                "source": {
                    "kind": "driver",
                    "finance_type": "same",
                    "driver": "cap",
                    "scope": {"kind": "instances", "instances": ["route"]},
                    "period": {"kind": "scalar"},
                },
            }
        },
    }
    profile["amounts"]["sale_quote"] = {
        "unit": "CNY",
        "axis": "financial_year",
        "constant": _money("0"),
        "terms": {
            "metered": {
                "coefficient": _money("0.01", "CNY/kWh"),
                "source": {
                    "kind": "system_quantity",
                    "block": "delivery",
                    "variable": "meter",
                    "aggregation": "integrate_positive",
                    "period": {"kind": "current"},
                },
            }
        },
    }
    profile["cash_movements"]["purchase"]["payments"] = [
        {"offset": 0, "ratio": "0.5"},
        {"offset": 3, "ratio": "0.5"},
    ]
    profile["cash_movements"]["sales"]["amount"]["period"] = {"kind": "current"}
    profile["tax_rules"]["income"]["depreciation"][0]["life_years"] = 4
    finance = merge_effective(FinanceProfile.from_dict(profile), None)
    evaluation = parse_evaluation_input(
        {
            "construction_period": 0,
            "operation_years": 2,
            "evaluation_years": 2,
            "discount_rate": 0.1,
            "construction_payments": [{"t": 0, "ratio": 1}],
            "maintenance_expenses": [],
        }
    )
    if comparison is not None:
        evaluation = replace(evaluation, comparison=comparison)
        doc["planning"]["constraints"]["frozen_budget"] = {
            "type": "amount_relation",
            "expression": "quote <= 1.5 * prior_basis",
            "references": {
                "quote": {"kind": "amount", "id": "purchase_quote", "period": {"kind": "scalar"}},
                "prior_basis": {"kind": "baseline", "id": "prior_basis", "period": {"kind": "scalar"}},
            },
        }
    checked = validate_assembly_doc(
        doc,
        device_documents=documents,
        finance_snapshots={"obj-finance": finance},
    )
    assert checked.ok, [(d.location, d.params) for d in checked.diagnostics]
    check_comparison_inputs(
        evaluation,
        checked.artifact,
        finance,
        HIGHS_MILP_PROVIDER.ref,
        HIGHS_MILP_PROVIDER.descriptor.capabilities,
    )
    check_financial_relation_inputs(evaluation, checked.artifact, finance)
    series = materialize_non_predictive_series(checked.artifact, {}, operation_years=2)
    return checked.artifact, original[1], series, finance, original[4], evaluation


def _run(inputs, directory):
    directory.mkdir()
    bundle = HIGHS_MILP_PROVIDER.generate(*inputs)
    result = _solve(bundle, directory)
    assert result.math_status == "solved"
    fin = build_financial_evaluation(
        SimpleNamespace(
            evaluation={
                "construction_period": 0,
                "operation_years": 2,
                "evaluation_years": 2,
                "discount_rate": 0.1,
            }
        ),
        inputs[3],
        result,
    )
    assert fin["status"] == "evaluated"
    return bundle, result, fin


def _check_accounts(fin, *, gross, basis, depreciation, vat_paid, levy, income, cash):
    movement = {(row["id"], row["period"]): row for row in fin["cash_movements"]}
    purchase = movement[("purchase", 0)]
    assert float(purchase["gross"]) == pytest.approx(gross)
    assert float(purchase["deductible_input"]) == pytest.approx(gross - basis)
    assert float(purchase["net_basis"]) == pytest.approx(basis)
    assert [p["at"]["year"] for p in purchase["payments"]] == [0, 3]
    assert [float(p["amount"]) for p in purchase["payments"]] == pytest.approx([gross / 2] * 2)
    assert float(purchase["end_unpaid"]) == pytest.approx(gross / 2)
    assert [float(movement[("sales", y)]["gross"]) for y in (1, 2)] == pytest.approx([385.44] * 2)
    assert [float(movement[("sales", y)]["net_revenue"]) for y in (1, 2)] == pytest.approx([350.4] * 2)
    asset = fin["assets"][0]
    assert asset["id"] == "machine"
    assert float(asset["basis"]) == pytest.approx(basis)
    assert [float(row["value"]) for row in asset["book_values"]] == pytest.approx(
        [basis - depreciation, basis - 2 * depreciation],
    )
    tax = fin["tax_cash"]
    assert [float(row["amount"]) for row in tax["depreciation"]] == pytest.approx([depreciation] * 2)
    vat = {row["month"]: row for row in tax["vat"]}
    assert set(vat) == set(range(1, 25))
    assert [float(vat[m]["paid"]) for m in (12, 24)] == pytest.approx(vat_paid)
    rules = {row["month"]: row for row in tax["vat_rules"]}
    derived = {row["period"]: row for row in tax["derived"]}
    assert set(rules) == set(derived) == set(vat)
    for month in range(1, 25):
        assert float(vat[month]["paid"]) == pytest.approx(vat_paid[month // 12 - 1] if month % 12 == 0 else 0)
        assert float(rules[month]["deductible_input"]) == pytest.approx(gross - basis if month == 1 else 0)
        assert float(rules[month]["output_tax"]) == pytest.approx(35.04 if month % 12 == 0 else 0)
        assert float(derived[month]["assessed"]) == pytest.approx(
            levy[month // 12 - 1] if month % 12 == 0 else 0
        )
        assert derived[month]["payment_period"] == month + 1
    assert [float(row["tax"]) for row in tax["income"]] == pytest.approx(income)
    assert [float(cf) for cf in fin["annual_net_flows"]] == pytest.approx(cash)
    # Named sales/payments are not also hidden in original operating expenses.
    assert [float(row["net"]) for row in fin["annual_operation"]] == [0, 0]


def test_network_and_finance_share_physical_cash_tax_asset_and_frozen_baseline(tmp_path):
    _, baseline, prior = _run(_case(1, "82.5"), tmp_path / "baseline")
    _check_accounts(
        prior,
        gross=330,
        basis=300,
        depreciation=75,
        vat_paid=[5.04, 35.04],
        levy=[0.2016, 1.4016],
        income=[68.7996, 68.4996],
        cash=[-165, 311.6004, 281.6988],
    )
    request = parse_comparison(
        {
            "mode": "difference",
            "baseline": {"kind": "task_result", "task_id": 101, "evidence_id": 201},
            "comparability": {
                "service_basis": "相同两年负荷和端口拓扑，各自完整计税",
                "differences": "传输效率与单位投资不同；两方案独立求解",
            },
            "facts": {
                "prior_basis": {"kind": "asset", "id": "machine", "measure": "basis"},
                "prior_sales": {"kind": "amount", "id": "sale_quote"},
                "prior_cash": {"kind": "cashflow", "measure": "net"},
                "prior_vat": {"kind": "tax", "source": {"kind": "pool", "id": "pool"}, "measure": "paid"},
                "prior_income": {
                    "kind": "tax",
                    "source": {"kind": "rule", "id": "income"},
                    "measure": "assessed",
                },
            },
        },
        years=2,
    )
    comparison = freeze_comparison(
        request,
        years=2,
        currency="CNY",
        financial=prior,
        source={
            "kind": "task_result",
            "task_id": 101,
            "evidence_id": 201,
            "attempt_id": 1,
            "object_id": 301,
        },
    )
    bundle, candidate, fin = _run(_case(0.8, "110", comparison), tmp_path / "candidate")
    _check_accounts(
        fin,
        gross=440,
        basis=400,
        depreciation=100,
        vat_paid=[0, 30.08],
        levy=[0, 1.2032],
        income=[62.6, 62.2992],
        cash=[-220, 322.84, 293.0608],
    )
    for result, supply in ((baseline, 4), (candidate, 5)):
        curves = result.outputs["series"]
        expected = {
            "source.power_out": supply,
            "route.power_in": supply,
            "route.power_out": 4,
            "load.electricity_in": 4,
            "source.unused": 0,
        }
        for key, value in expected.items():
            assert len(curves[key]["values"]) == 17520
            assert list(curves[key]["values"]) == pytest.approx([value] * 17520)
        # Both ideal buses balance, but transfer loss remains between them.
        assert supply * (1 if supply == 4 else 0.8) == pytest.approx(4)
        system = result.outputs["system_series"]
        assert list(system["system:delivery.meter"]["values"]) == pytest.approx([4] * 17520)
        energy = system["system:delivery.energy"]["values"]
        assert [energy[i] for i in (0, 8759, 8760, 17519)] == pytest.approx([4, 35040, 35044, 70080])
    variables = {row["name"]: row["value"] for row in candidate.outputs["solution"]["variables"]}
    assert float(variables["route_capacity"]) == pytest.approx(4)
    comparison = fin["comparison"]
    assert comparison["facts"]["prior_basis"] == {
        "unit": "CNY",
        "axis": "scalar",
        "values": [prior["assets"][0]["basis"]],
    }
    assert comparison["facts"]["prior_vat"]["values"] == [row["paid"] for row in prior["tax_cash"]["vat"]]
    assert [float(v) for v in comparison["facts"]["prior_sales"]["values"]] == pytest.approx(
        [0, 350.4, 350.4]
    )
    assert comparison["baseline_net_flows"] == prior["annual_net_flows"]
    assert [float(v) for v in comparison["evaluation_net_flows"]] == pytest.approx([-55, 11.2396, 11.362])
    expected_npv = Decimal("-55") + Decimal("11.2396") / Decimal("1.1") + Decimal("11.362") / Decimal("1.21")
    assert float(fin["evaluation"]["npv"]) == pytest.approx(float(expected_npv))
    assert float(candidate.outputs["solution"]["objective_value"]) == pytest.approx(float(expected_npv))
    sources = json.loads(bundle.files["input/parameters.json"])["model_sources"]["sources"]
    buses = {tuple(source["inputs"]) for source in sources.values() if source["kind"] == "bus"}
    assert buses == {("route.power_in", "source.power_out"), ("load.electricity_in", "route.power_out")}
    assert (
        "relation:baseline.prior_basis"
        in sources["reference:planning.amount.frozen_budget.prior_basis"]["depends_on"]
    )
