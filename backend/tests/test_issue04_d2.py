"""ISSUE-04 D2: exact natural-month demand accounting with real Docker HiGHS.

All inputs are synthetic. Public assembly validation, real generator, actual
HiGHS primal, paired adapter and worker sealing function; no mock peak values.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from iesplan.assembly import validate_assembly_doc
from iesplan.application.worker.compute_cases import build_financial_evaluation
from iesplan.computation import (
    CalculationConfig, HIGHS_MILP_PROVIDER, materialize_non_predictive_series,
)
from iesplan.computation.generators.highs_milp import GENERATOR_REF
from iesplan.computation.generators.linear import LoweringUnsupportedError
from iesplan.computation.result_adapters.highs import ADAPTER_REF, HIGHS_ADAPTER
from iesplan.core.yamlmini import load as yaml_load
from iesplan.devices import init_registry
from iesplan.devices.parser2 import parse_device_model_v2
from iesplan.finance import (
    ConstructionPayment, EvaluationInput, FinanceProfile, merge_effective,
)
from test_highs_result_adapter import SOLUTION_PATH, _bundle, _receipt
from test_vat_monthly import (
    GRID_YAML, HEATER_YAML, LOAD_YAML, TANK_YAML,
    _contribution, _device_docs, _doc, _finance_payload, _solve_internal,
)

METER_RATE = {"rate": {"value": "12", "unit": "CNY/kW"}}
POWER_KW = 7.0 / 0.9


def _inputs(*, resolution="1h", start_year=2024, rate="12",
            second_rate=None, bounded=True, empty=False, vat=False,
            income=False, basis="tax_inclusive", deductible="1",
            dispatch=False, sales=False, income_deduct_demand=True):
    doc = _doc()
    doc["project_baseline"]["resolution"] = resolution
    doc["project_baseline"]["start_year"] = start_year
    doc["tariff_bindings"] = {}
    doc["planning"]["objective"] = {"sense": "minimize", "expression": "0 CNY"}
    doc["demand_bindings"] = {} if empty else {
        "first": {
            "charge": "charge_a", "instance": "grid1", "interface": "supply",
        },
    }
    if second_rate is not None and not empty:
        doc["demand_bindings"]["second"] = {
            "charge": "charge_b", "instance": "grid1", "interface": "supply",
        }
    profile = _finance_payload()
    profile["credit_pools"] = {}
    profile["tax_rules"] = {}
    profile["demand_charges"] = {
        "charge_a": {"rate": {"value": rate, "unit": "CNY/kW"}},
    }
    if second_rate is not None:
        profile["demand_charges"]["charge_b"] = {
            "rate": {"value": second_rate, "unit": "CNY/kW"},
        }
    profile["profile"]["price_basis"] = basis
    if sales:
        # Revenue from the physically constrained 7 kW heater output.
        doc["tariff_bindings"] = {
            "heat_sales_meter": {
                "price": "heat_sale", "instance": "heater1",
                "interface": "heat_out", "aggregation": "integrate_positive",
            },
        }
        doc["finance_binding"]["instances"]["heater1"] = {"type": "none"}
        doc["finance_binding"]["instances"]["tank1"] = {"type": "none"}
        profile["energy_prices"]["heat_sale"] = {
            "carrier": "heat", "direction": "sale", "kind": "constant",
            "value": {"value": "1", "unit": "CNY/kWh"},
        }
    if dispatch:
        # One real dispatch decision: costly local generator vs cheap utility.
        # Demand cost drives utility consumption down in the same NPV solve.
        doc["devices"]["gen1"] = {
            "definition": {"id": "acme.device.grid"},
            "asset_origin": "existing",
            "properties": {}, "predefined_interfaces": {},
        }
        doc["connections"]["gen_link"] = {
            "from": "gen1.supply", "to": "heater1.electricity_in",
        }
        doc["finance_binding"]["instances"]["gen1"] = {
            "type": "costed", "finance_type": "local_generator",
            "drivers": {
                "throughput": {
                    "interface": "supply", "aggregation": "integrate_positive",
                },
            },
        }
        profile["finance_types"]["local_generator"] = {
            "period_variable_om": {
                "linear": {
                    "throughput": {
                        "unit_cost": {"value": "1", "unit": "CNY/kWh"},
                    },
                },
            },
        }
        profile["energy_prices"]["utility_energy"] = {
            "carrier": "electricity", "direction": "purchase", "kind": "constant",
            "value": {"value": "0.01", "unit": "CNY/kWh"},
        }
        doc["tariff_bindings"] = {
            "utility": {
                "price": "utility_energy", "instance": "grid1",
                "interface": "supply", "aggregation": "integrate_positive",
            },
        }
        doc["planning"]["objective"] = {
            "sense": "maximize", "expression": "system.after_tax_npv",
        }
    if vat or income:
        profile["taxes"]["demand_label"] = {
            "display_name": "Synthetic demand input", "tax_type": "value_added_tax",
            "applies_to": "demand_charges.charge_a",
        }
    if vat:
        profile["credit_pools"] = {
            "pool": {
                "opening_credit": {"value": "0", "unit": "CNY"},
                "receivable_policy": "end_receivable", "refunds": [],
            },
        }
        profile["tax_rules"]["vat_demand"] = {
            "kind": "vat", "label_ref": "demand_label",
            "settlement": "monthly", "in_tax": [],
            "out_tax": ([{
                "price": "heat_sale", "rate": {"value": "0.13", "unit": "1"},
            }] if sales else []),
            "om_input_tax": [], "maintenance_input_tax": [],
            "demand_input_tax": [{
                "charge": "charge_a",
                "rate": {"value": "0.1", "unit": "1"},
                "deductible_ratio": {"value": deductible, "unit": "1"},
            }],
            "credit_pool": "pool", "confirmation": "same_period",
        }
    if income:
        profile["taxes"]["income_label"] = {
            "display_name": "Synthetic income", "tax_type": "income_tax",
            "applies_to": "demand_charges.charge_a",
        }
        profile["tax_rules"]["income_demand"] = {
            "kind": "income_tax", "label_ref": "income_label",
            "rate": {"value": "0.25", "unit": "1"},
            "revenue": ["heat_sale"] if sales else [],
            "energy_cost": [],
            "demand_cost": ["charge_a"] if income_deduct_demand else [],
            "annual_om": [], "variable_om": [],
            "deductible_maintenance": {"mode": "explicit_items", "items": []},
            "depreciation": [], "loss_carryforward_years": 0,
            "opening_losses": [],
        }
    finance = merge_effective(FinanceProfile.from_dict(profile), None)
    docs = _device_docs()
    grid_yaml = GRID_YAML
    if bounded:
        grid_yaml = grid_yaml.replace(
            "valid_range: {minimum: 0, maximum: null}",
            "valid_range: {minimum: 0, maximum: 20}",
        )
        parsed = parse_device_model_v2(yaml_load(grid_yaml), file="synthetic-grid.yaml")
        assert parsed.ok, parsed.diagnostics
        docs["acme.device.grid"] = parsed.document
    models = {
        "heater1": _contribution(HEATER_YAML),
        "tank1": _contribution(TANK_YAML),
        "load1": _contribution(LOAD_YAML),
        "grid1": _contribution(grid_yaml),
    }
    if dispatch:
        models["gen1"] = _contribution(grid_yaml)
    init_registry()
    result = validate_assembly_doc(
        doc, finance_snapshots={"obj-finance": finance}, device_documents=docs,
    )
    assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]
    evaluation = EvaluationInput(
        evaluation_years=2, construction_period=1, operation_years=1,
        discount_rate=0.0,
        construction_payments=(ConstructionPayment(t=0, ratio="1"),),
        maintenance_expenses=(),
    )
    mat = materialize_non_predictive_series(result.artifact, {}, operation_years=1)
    config = CalculationConfig(
        mode="planning", generator=GENERATOR_REF,
        solver="ies.solver.highs@1.7.2", precision="medium",
        time_limit_seconds=600, outputs=["solution"],
    )
    return result.artifact, models, mat, finance, config, evaluation


def _solved(inputs, tmp_path):
    artifact, models, mat, finance, config, evaluation = inputs
    bundle = HIGHS_MILP_PROVIDER.generate(
        artifact, models, mat, finance, config, evaluation,
    )
    _, _, raw = _solve_internal(bundle, tmp_path)
    adapted = HIGHS_ADAPTER.adapt(
        _bundle({
            "input/problem.mps": bundle.files["input/problem.mps"],
            "input/parameters.json": bundle.files["input/parameters.json"],
        }),
        _receipt(result_adapter_ref=ADAPTER_REF), {SOLUTION_PATH: raw},
    )
    sealed = build_financial_evaluation(
        SimpleNamespace(evaluation={
            "evaluation_years": 2, "construction_period": 1,
            "operation_years": 1, "discount_rate": 0.0,
        }),
        finance, adapted,
    )
    assert sealed is not None
    return adapted.outputs["financial_facts"], sealed, bundle


@pytest.mark.parametrize("resolution", ("1h", "30min"))
def test_zero_rate_non_economic_still_real_peak(tmp_path, resolution):
    """No economic push on peak: non-economic constant objective + zero rate."""
    inputs = _inputs(resolution=resolution, rate="0")
    facts, sealed, _ = _solved(inputs, tmp_path)
    rows = facts["monthly_demand"]
    assert len(rows) == 12
    assert sealed["schema_version"] == "2.0.0"
    assert sealed["monthly_demand"] == list(rows)
    assert [(r["calendar_year"], r["calendar_month"]) for r in rows] == [
        (2024, month) for month in range(1, 13)
    ]
    for row in rows:
        assert row["billing_id"] == "first"
        assert row["financial_year"] == 2
        assert row["peak_unit"] == "kW"
        assert float(row["peak_power"]) == pytest.approx(POWER_KW, abs=1e-5)
        assert float(row["charge"]) == pytest.approx(0.0, abs=1e-6)
    assert float(facts["annual_operation"][0]["demand_charge"]) == pytest.approx(0.0)
    assert len(facts["annual_net_flows"]) == 3


def test_two_rates_same_meter_no_energy_tariff(tmp_path):
    """Two user-declared charges on the same physical meter are independent."""
    facts, sealed, _ = _solved(_inputs(second_rate="4"), tmp_path)
    rows = facts["monthly_demand"]
    assert len(rows) == 24
    assert [(row["billing_id"], row["calendar_month"]) for row in rows] == [
        (billing, month)
        for billing in ("first", "second") for month in range(1, 13)
    ]
    first, second = rows[:12], rows[12:]
    for a, b in zip(first, second, strict=True):
        assert float(a["peak_power"]) == pytest.approx(POWER_KW, abs=1e-5)
        assert float(b["peak_power"]) == pytest.approx(POWER_KW, abs=1e-5)
        assert float(a["charge"]) == pytest.approx(POWER_KW * 12, abs=1e-5)
        assert float(b["charge"]) == pytest.approx(POWER_KW * 4, abs=1e-5)
    year = facts["annual_operation"][0]
    assert float(year["demand_charge"]) == pytest.approx(
        12 * POWER_KW * (12 + 4), abs=1e-4,
    )
    assert sealed["tax_cash"]["vat"] == []


def test_unused_demand_catalogue_publishes_empty_months(tmp_path):
    facts, sealed, _ = _solved(_inputs(empty=True), tmp_path)
    assert facts["monthly_demand"] == ()
    assert sealed["monthly_demand"] == []
    assert float(facts["annual_operation"][0]["demand_charge"]) == 0.0


def test_missing_finite_real_power_bound_rejected_at_generator():
    """Business-valid nonnegative but unbounded meter fails only at build."""
    inputs = _inputs(bounded=False)
    with pytest.raises(LoweringUnsupportedError, match="缺实际有限界"):
        HIGHS_MILP_PROVIDER.generate(*inputs)


@pytest.mark.parametrize("basis,deductible", [
    ("tax_inclusive", "1"),
    ("tax_exclusive", "1"),
    ("tax_inclusive", "0"),
    ("tax_exclusive", "0"),
])
def test_demand_vat_gross_cash_and_income_scope(tmp_path, basis, deductible):
    facts, sealed, _ = _solved(
        _inputs(vat=True, income=True, basis=basis, deductible=deductible),
        tmp_path,
    )
    rate = 12.0
    base_month = POWER_KW * rate
    gross = base_month * (1.1 if basis == "tax_exclusive" else 1.0)
    deductible_input = (
        base_month * (0.1 if basis == "tax_exclusive" else 0.1 / 1.1)
        * float(deductible)
    )
    assert len(facts["monthly_demand"]) == 12
    assert all(float(r["charge"]) == pytest.approx(gross, abs=1e-4)
               for r in facts["monthly_demand"])
    demand_total = 12 * gross
    assert float(facts["annual_operation"][0]["demand_charge"]) == pytest.approx(
        demand_total, abs=1e-4,
    )
    # Zero output VAT => input VAT accumulates, no cash disbursement; noncash
    # credits change only income-tax base, never pay the bill a second time.
    assert sum(float(r["paid"]) for r in facts["tax_cash"]["vat"]) == pytest.approx(0.0)
    assert float(facts["tax_cash"]["vat"][-1]["carry"]) == pytest.approx(
        12 * deductible_input, abs=1e-4,
    )
    income_year2 = next(
        row for row in facts["tax_cash"]["income"]
        if row["rule"] == "income_demand" and row["year"] == 2
    )
    assert float(income_year2["taxable_base"]) == pytest.approx(
        -(demand_total - 12 * deductible_input), abs=1e-4,
    )
    assert float(income_year2["tax"]) == pytest.approx(0.0, abs=1e-4)
    assert float(facts["annual_net_flows"][2]) == pytest.approx(
        float(facts["annual_operation"][0]["net"]), abs=1e-4,
    )
    assert float(sealed["evaluation"]["npv"]) == pytest.approx(
        sum(float(x) for x in facts["annual_net_flows"]), abs=1e-4,
    )


def test_dispatch_changes_due_to_monthly_demand_cost(tmp_path):
    """Exactly the same dispatch system; only demand rate changes.

    No fee: cheap utility supplies the 7/0.9 kW load. With a 1000 CNY/kW
    monthly fee, local generation at 1 CNY/kWh is cheaper than exposing
    the utility peak over twelve months, and the optimal dispatch changes.
    """
    baseline, _, _ = _solved(
        _inputs(start_year=2023, rate="0", dispatch=True),
        tmp_path / "no_demand",
    )
    charged, _, _ = _solved(
        _inputs(start_year=2023, rate="1000", dispatch=True),
        tmp_path / "with_demand",
    )
    assert all(
        float(row["peak_power"]) == pytest.approx(POWER_KW, abs=1e-4)
        for row in baseline["monthly_demand"]
    )
    assert all(
        float(row["peak_power"]) == pytest.approx(0.0, abs=1e-4)
        for row in charged["monthly_demand"]
    )
    assert float(baseline["annual_operation"][0]["demand_charge"]) == 0.0
    assert float(charged["annual_operation"][0]["demand_charge"]) == pytest.approx(
        0.0, abs=1e-4,
    )
    # Dispatch changes costs: local variable cost becomes much larger and
    # utility energy payment falls, both from one solved model per scenario.
    assert float(charged["annual_operation"][0]["variable_om"]) > float(
        baseline["annual_operation"][0]["variable_om"]
    ) + 50_000
    assert float(charged["annual_operation"][0]["energy_net"]) > float(
        baseline["annual_operation"][0]["energy_net"]
    )


@pytest.mark.parametrize("deductible", ("0", "1"))
@pytest.mark.parametrize("income_deduct_demand", (False, True))
def test_nonzero_sales_vat_income_and_after_tax_npv_by_hand(
    tmp_path, deductible, income_deduct_demand,
):
    """Positive output VAT, VAT actually paid, income tax and NPV from HiGHS.

    Gross revenue = 7 kW * 8760 h * 1 CNY/kWh. Each demand month
    independently uses the solved 7/0.9 kW grid supply meter.
    """
    facts, sealed, _ = _solved(
        _inputs(
            start_year=2023, vat=True, income=True, sales=True,
            deductible=deductible, income_deduct_demand=income_deduct_demand,
        ),
        tmp_path,
    )
    gross_revenue = 7.0 * 8760
    gross_demand_month = POWER_KW * 12.0
    gross_demand_year = gross_demand_month * 12
    output_vat = gross_revenue * 0.13 / 1.13
    input_vat = gross_demand_year * 0.1 / 1.1 * float(deductible)
    hand_vat_paid = output_vat - input_vat
    hand_taxable = gross_revenue - output_vat
    if income_deduct_demand:
        hand_taxable -= gross_demand_year - input_vat
    hand_income_paid = hand_taxable * 0.25
    hand_net_cash = (
        gross_revenue - gross_demand_year - hand_vat_paid - hand_income_paid
    )

    assert len(facts["monthly_demand"]) == 12
    assert all(
        float(row["charge"]) == pytest.approx(gross_demand_month, abs=1e-4)
        for row in facts["monthly_demand"]
    )
    assert float(facts["annual_operation"][0]["demand_charge"]) == pytest.approx(
        gross_demand_year, abs=1e-4,
    )
    assert float(facts["annual_operation"][0]["energy_net"]) == pytest.approx(
        gross_revenue, abs=1e-4,
    )
    assert float(facts["annual_operation"][0]["variable_om"]) == pytest.approx(0)
    assert float(facts["annual_operation"][0]["fixed_om"]) == pytest.approx(0)
    assert float(facts["construction_total"]) == pytest.approx(0)
    assert sum(float(row["paid"]) for row in facts["tax_cash"]["vat"]) == pytest.approx(
        hand_vat_paid, abs=1e-4,
    )
    assert hand_vat_paid > 0
    assert float(facts["tax_cash"]["vat"][-1]["carry"]) == pytest.approx(0, abs=1e-5)
    year2 = next(
        row for row in facts["tax_cash"]["income"]
        if row["rule"] == "income_demand" and row["year"] == 2
    )
    assert float(year2["taxable_base"]) == pytest.approx(hand_taxable, abs=1e-4)
    assert float(year2["tax"]) == pytest.approx(hand_income_paid, abs=1e-4)
    assert hand_income_paid > 0
    assert float(facts["annual_operation"][0]["net"]) == pytest.approx(
        gross_revenue - gross_demand_year, abs=1e-4,
    )
    # Demand paid once in operation; no second charge in tax or cashflow.
    assert [float(v) for v in facts["annual_net_flows"][:2]] == pytest.approx(
        [0, 0], abs=1e-5,
    )
    assert float(facts["annual_net_flows"][2]) == pytest.approx(
        hand_net_cash, abs=1e-4,
    )
    assert sealed["monthly_demand"] == list(facts["monthly_demand"])
    assert float(sealed["evaluation"]["npv"]) == pytest.approx(
        hand_net_cash, abs=1e-4,
    )
