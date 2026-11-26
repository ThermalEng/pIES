"""F3 tests: pure forward financial evaluation & full 1.1.5 reconciliation."""

from __future__ import annotations

import json
import math
import subprocess
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_f2_solver_restoration import (
    HIGHS_EXECUTABLE,
    _succeeded_receipt,
    _target_campus_config,
    _v1_sol_to_primal_json,
)
from test_highs_milp_provider import _campus_inputs

from iesplan.application.worker.compute_cases import build_financial_evaluation
from iesplan.assembly import validate_assembly_text
from iesplan.computation import HIGHS_ADAPTER, HIGHS_MILP_PROVIDER, ExecutionReceipt
from iesplan.computation.contracts import PRIMAL_SOLUTION_OUTPUT_PATH
from iesplan.computation.generators.highs_milp import DESCRIPTOR as HIGHS_GEN_DESCRIPTOR
from iesplan.computation.generators.target_bundle import EPIGRAPHS_PATH, generate_target_highs_bundle
from iesplan.computation.result_adapters.highs import ADAPTER_REF as HIGHS_ADAPTER_REF
from iesplan.computation.result_adapters.target_adapter import TargetResultAdapter
from iesplan.computation.sequences import (
    MaterializedPriceSeries,
    MaterializedSeries,
    MaterializedSet,
)
from iesplan.core.content import read_content
from iesplan.devices import init_registry, list_devices
from iesplan.finance import (
    FINANCIAL_EVALUATION_SCHEMA,
    FINANCIAL_EVALUATION_VERSION,
    ConstructionPayment,
    CostComponentShareTerm,
    EffectiveFinanceConfig,
    EvaluationInput,
    FinanceOverrides,
    FinanceProfile,
    MaintenanceExpense,
    MaintenanceUnitCost,
    ScaleUnitCostTerm,
    evaluate_forward_finance,
    freeze_comparison,
    merge_effective,
    parse_comparison,
)
from iesplan.modeling import build_math_contribution, contribution_to_dict

_MONEY_TOL = 1e-6


def _assert_eval_close(actual: Any, expected: Any, *, path: str = "root") -> None:
    """Recursively assert exact literals and numerically equal Decimal money strings."""
    if path.startswith("root.payments_applied[") and path.endswith("].ratio"):
        # F3 preserves canonical string payment.ratio ("1", "0.4") while 1.1.5 emits JSON float.
        assert Decimal(str(actual)) == Decimal(str(expected)), f"{path}: {actual!r} != {expected!r}"
        return
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected dict, got {type(actual).__name__}"
        assert set(actual.keys()) == set(expected.keys()), (
            f"{path}: keys differ: {sorted(actual.keys())} != {sorted(expected.keys())}"
        )
        for key in expected:
            _assert_eval_close(actual[key], expected[key], path=f"{path}.{key}")
        return
    if isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: expected list, got {type(actual).__name__}"
        assert len(actual) == len(expected), f"{path}: len {len(actual)} != {len(expected)}"
        for idx, (a_item, e_item) in enumerate(zip(actual, expected, strict=True)):
            _assert_eval_close(a_item, e_item, path=f"{path}[{idx}]")
        return
    if isinstance(expected, bool):
        assert actual is expected, f"{path}: {actual!r} is not {expected!r}"
        return
    if isinstance(expected, int):
        assert isinstance(actual, int) and not isinstance(actual, bool), f"{path}: {actual!r}"
        assert actual == expected, f"{path}: {actual} != {expected}"
        return
    if isinstance(expected, float):
        assert isinstance(actual, (int, float)) and not isinstance(actual, bool)
        if not math.isfinite(expected):
            assert actual == expected
        else:
            assert float(actual) == pytest.approx(expected, rel=1e-7, abs=_MONEY_TOL), (
                f"{path}: {actual} != {expected}"
            )
        return
    if isinstance(expected, str):
        try:
            e_dec = Decimal(expected)
        except Exception:
            assert actual == expected, f"{path}: {actual!r} != {expected!r}"
            return
        assert isinstance(actual, str), f"{path}: expected decimal string, got {actual!r}"
        a_dec = Decimal(actual)
        assert float(a_dec) == pytest.approx(float(e_dec), rel=1e-7, abs=_MONEY_TOL), (
            f"{path}: {actual} != {expected}"
        )
        return
    assert actual == expected, f"{path}: {actual!r} != {expected!r}"


def _solve_target_bundle_to_solution(bundle: Any, tmp_path: Path) -> dict[str, Any]:
    run_dir = tmp_path / "target_run"
    for rel, content in bundle.files.items():
        dest = run_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(read_content(content))
    (run_dir / "output").mkdir(parents=True, exist_ok=True)

    args = [
        str(HIGHS_EXECUTABLE),
        "input/problem.mps",
        "--solution_file",
        "output/solution.sol",
        "--time_limit",
        "60",
        "--random_seed",
        "7",
        "--report_model_columns",
        "true",
    ]
    if "input/acceleration.json" in bundle.files:
        args.extend(
            ["--acceleration_mode", "on_demand_mutex", "--acceleration_file", "input/acceleration.json"]
        )
    if EPIGRAPHS_PATH in bundle.files:
        args.extend(["--epigraph_file", EPIGRAPHS_PATH])

    proc = subprocess.run(args, cwd=run_dir, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    primal_payload = _v1_sol_to_primal_json((run_dir / "output" / "solution.sol").read_text(encoding="utf-8"))
    sol_out = tmp_path / "engineering_solution.json"
    outcome = TargetResultAdapter().restore_with_outcome(
        bundle,
        _succeeded_receipt(),
        {PRIMAL_SOLUTION_OUTPUT_PATH: json.dumps(primal_payload).encode("utf-8")},
        solution_output_path=sol_out,
    )
    return outcome.load_engineering_solution()


def _run_legacy_115_evaluation(
    artifact: Any,
    models: Any,
    materialized: Any,
    finance: EffectiveFinanceConfig,
    config: Any,
    evaluation: EvaluationInput,
    tmp_path: Path,
) -> dict[str, Any]:
    bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
    run_dir = tmp_path / "legacy_run"
    for rel, content in bundle.files.items():
        dest = run_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(read_content(content))
    (run_dir / "output").mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            str(HIGHS_EXECUTABLE),
            "input/problem.mps",
            "--solution_file",
            "output/solution.sol",
            "--time_limit",
            "60",
            "--random_seed",
            "7",
        ],
        cwd=run_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    receipt = ExecutionReceipt(
        receipt_id="rcpt-115",
        bundle_id=str(bundle.manifest.bundle.get("id", "bundle-115")),
        assembly_id="asm-115",
        calculation_config_id="cfg-115",
        attempt_id="att-115",
        generator_ref=HIGHS_GEN_DESCRIPTOR.ref,
        solver_ref="ies.solver.highs@1.7.2",
        executor_ref="ies.executor.local_process@1.0.0",
        executable_ref="ies.solver.highs@1.7.2",
        result_adapter_ref=HIGHS_ADAPTER_REF,
        status="succeeded",
        exit_code=0,
        termination_reason="exited",
        started_at="2026-11-25T10:00:00Z",
        ended_at="2026-11-25T10:00:01Z",
    )
    result = HIGHS_ADAPTER.adapt(
        bundle,
        receipt,
        {"output/solution.sol": (run_dir / "output" / "solution.sol").read_bytes()},
    )
    snapshot = SimpleNamespace(
        evaluation={
            "construction_period": evaluation.construction_period,
            "operation_years": evaluation.operation_years,
            "evaluation_years": evaluation.evaluation_years,
            "discount_rate": evaluation.discount_rate,
        }
    )
    sealed = build_financial_evaluation(snapshot, finance, result)
    assert sealed is not None
    return sealed


def test_campus_forward_finance_reconciles_with_highs_evaluation(tmp_path: Path) -> None:
    """Campus end-to-end: F2 restored engineering-solution -> forward finance matches 1.1.5."""
    from test_highs_milp_provider import SAMPLES

    init_registry()
    raw_text = (SAMPLES / "valid" / "campus.assembly.yaml").read_text(encoding="utf-8")
    unit_block = (
        "  construction_units:\n"
        "    u_pv1:\n"
        "      device_ref: pv1\n"
        "      mode: optional\n"
        "      scales:\n"
        "        pv_cap:\n"
        "          active_lower_bound: \"0\"\n"
        "          active_upper_bound: null\n"
    )
    campus_res = validate_assembly_text(
        raw_text.replace("  constraints:\n", unit_block + "  constraints:\n", 1),
        package_dir=SAMPLES,
    )
    assert campus_res.artifact is not None, campus_res.diagnostics
    artifact = campus_res.artifact
    _, models, materialized, finance, config, evaluation = _campus_inputs()

    target_bundle = generate_target_highs_bundle(
        artifact,
        models,
        materialized,
        finance,
        _target_campus_config(),
        evaluation,
    )
    solution = _solve_target_bundle_to_solution(target_bundle, tmp_path)
    expected = _run_legacy_115_evaluation(
        artifact, models, materialized, finance, config, evaluation, tmp_path
    )

    actual_direct = evaluate_forward_finance(
        solution,
        artifact=artifact,
        finance=finance,
        evaluation=evaluation,
        materialized=materialized,
        device_models=models,
    )
    assert actual_direct["schema"] == FINANCIAL_EVALUATION_SCHEMA
    assert actual_direct["schema_version"] == FINANCIAL_EVALUATION_VERSION
    assert actual_direct["status"] == "evaluated"
    _assert_eval_close(actual_direct, expected)

    # Also verify frozen dict inputs + split prices/device_series work identically
    frozen_eval = {
        "evaluation_years": evaluation.evaluation_years,
        "construction_period": evaluation.construction_period,
        "operation_years": evaluation.operation_years,
        "discount_rate": evaluation.discount_rate,
        "construction_payments": [
            {"t": p.t, "ratio": p.ratio} for p in evaluation.construction_payments
        ],
        "maintenance_expenses": [],
        "comparison": {"mode": "absolute"},
    }
    actual_frozen = evaluate_forward_finance(
        solution,
        artifact={"canonical_text": artifact.canonical_text},
        finance=finance.to_dict(),
        evaluation=frozen_eval,
        prices=[p.to_dict() for p in materialized.prices],
        device_series=[s.to_dict() for s in materialized.series],
        device_models={inst: contribution_to_dict(c) for inst, c in models.items()},
    )
    _assert_eval_close(actual_frozen, expected)


def _money(value: str, unit: str = "CNY") -> dict[str, Any]:
    return {"value": value, "unit": unit}


def _empty_overrides(profile_id: str) -> FinanceOverrides:
    return FinanceOverrides.from_dict(
        {"schema": "ies.finance-overrides", "schema_version": "1.0.0", "profile_ref": {"id": profile_id}}
    )


def _complex_finance_case(
    *, optional_built: bool, comparison_mode: str = "difference"
) -> tuple[Any, Any, MaterializedSet, EffectiveFinanceConfig, EvaluationInput]:
    """Multi-year scenario exercising VAT, derived/income tax, assets, demand, and comparison."""
    init_registry()
    device_contribs = {}
    for dev_doc in list_devices():
        built_dev = build_math_contribution(dev_doc)
        assert built_dev.ok, (dev_doc.device.id, built_dev.diagnostics)
        device_contribs[dev_doc.device.id] = built_dev.contribution

    profile_payload: dict[str, Any] = {
        "schema": "ies.finance-profile",
        "schema_version": "1.0.0",
        "profile": {
            "id": "complex-f3",
            "region": "CN-North",
            "currency": "CNY",
            "base_year": 2025,
            "price_basis": "tax_exclusive",
            "cost_method": "fixed_plus_linear",
        },
        "finance_types": {
            "grid_ft": {
                "upfront_capex": {
                    "fixed": _money("100"),
                    "linear": {"cap": {"unit_cost": _money("50", "CNY/kW")}},
                },
                "annual_fixed_om": {
                    "items": {
                        "materials": {"linear": {"cap": {"unit_cost": _money("2", "CNY/kW/a")}}},
                        "service": {
                            "fixed": _money("10", "CNY/a"),
                            "linear": {"cap": {"unit_cost": _money("3", "CNY/kW/a")}},
                        },
                    }
                },
                "period_variable_om": {
                    "items": {
                        "chem": {"linear": {"energy": {"unit_cost": _money("0.05", "CNY/kWh")}}},
                        "misc": {"linear": {"energy": {"unit_cost": _money("0.02", "CNY/kWh")}}},
                    }
                },
            },
        },
        "energy_prices": {
            "elec_buy": {
                "carrier": "electricity",
                "direction": "purchase",
                "kind": "time_series",
                "series_meta": {"resolution": "1h", "point_count": 17520, "unit": "CNY/kWh"},
                "ref": "price:elec_buy",
            },
        },
        "demand_charges": {
            "cap_charge": {"rate": _money("10", "CNY/kW")},
        },
        "taxes": {
            "vat_tag": {
                "display_name": "VAT",
                "tax_type": "vat",
                "applies_to": "grid_ft.upfront_capex",
            },
            "income_tag": {
                "display_name": "CIT",
                "tax_type": "income_tax",
                "applies_to": "grid_ft.upfront_capex",
            },
            "surtax_tag": {
                "display_name": "Surtax",
                "tax_type": "vat_surcharge",
                "applies_to": "grid_ft.upfront_capex",
            },
        },
        "credit_pools": {
            "pool": {
                "opening_credit": _money("50"),
                "receivable_policy": "end_receivable",
                "refunds": [{"period": 1, "amount": _money("40"), "arrival_period": 13}],
            }
        },
        "amounts": {
            "capex_gross": {
                "unit": "CNY",
                "axis": "scalar",
                "constant": _money("0"),
                "terms": {
                    "c": {
                        "coefficient": _money("1", "1"),
                        "source": {
                            "kind": "cost",
                            "path": "grid_ft.upfront_capex",
                            "basis": "gross",
                            "scope": {"kind": "instances", "instances": ["grid"]},
                            "period": {"kind": "scalar"},
                        },
                    }
                },
            },
            "grant_base": {"unit": "CNY", "axis": "scalar", "constant": _money("300"), "terms": {}},
            "end_bonus": {
                "unit": "CNY",
                "axis": "scalar",
                "constant": _money("25"),
                "terms": {
                    "u": {
                        "coefficient": _money("10", "CNY"),
                        "source": {
                            "kind": "construction_unit",
                            "unit_ref": "u_grid",
                            "period": {"kind": "scalar"},
                        },
                    }
                },
            },
            "asset_salvage": {
                "unit": "CNY",
                "axis": "scalar",
                "constant": _money("0"),
                "terms": {
                    "a": {
                        "coefficient": _money("0.1", "1"),
                        "source": {
                            "kind": "asset",
                            "id": "plant_asset",
                            "measure": "basis",
                            "period": {"kind": "scalar"},
                        },
                    }
                },
            },
        },
        "cash_movements": {
            "equip_pay": {
                "amount": {"kind": "amount", "id": "capex_gross", "period": {"kind": "scalar"}},
                "price_basis": "tax_inclusive",
                "direction": "outflow",
                "recognition": {"kind": "at", "at": {"kind": "financial_year", "year": 1}},
                "payments": [{"offset": 0, "ratio": "0.5"}, {"offset": 3, "ratio": "0.5"}],
            },
            "service_rev": {
                "amount": {"kind": "amount", "id": "grant_base", "period": {"kind": "scalar"}},
                "price_basis": "tax_exclusive",
                "direction": "inflow",
                "recognition": {"kind": "each", "axis": "financial_year", "first": 2, "last": 3},
                "payments": [{"offset": 0, "ratio": "1"}],
            },
        },
        "assets": {
            "plant_asset": {
                "basis_from": {
                    "kind": "movement",
                    "id": "equip_pay",
                    "basis": "net_basis",
                    "period": {"kind": "financial_year", "year": 1},
                },
                "capitalized_at": {"kind": "financial_year", "year": 1},
                "available_from": 1,
            }
        },
        "tax_rules": {
            "capex_vat": {
                "kind": "construction_input_vat",
                "label_ref": "vat_tag",
                "for": "grid_ft.upfront_capex",
                "rate": _money("0.1", "1"),
                "deductible_ratio": _money("1", "1"),
                "credit_pool": "pool",
                "confirmation": {"method": "payment_period"},
            },
            "op_vat": {
                "kind": "vat",
                "label_ref": "vat_tag",
                "settlement": "monthly",
                "in_tax": [
                    {"price": "elec_buy", "rate": _money("0.13", "1"), "deductible_ratio": _money("1", "1")}
                ],
                "out_tax": [],
                "om_input_tax": [
                    {
                        "for": "grid_ft.annual_fixed_om.items.materials",
                        "rate": _money("0.13", "1"),
                        "deductible_ratio": _money("1", "1"),
                    },
                    {
                        "for": "grid_ft.period_variable_om.items.chem",
                        "rate": _money("0.13", "1"),
                        "deductible_ratio": _money("0.5", "1"),
                    },
                ],
                "maintenance_input_tax": [
                    {
                        "year": 2,
                        "position": 1,
                        "rate": _money("0.06", "1"),
                        "deductible_ratio": _money("1", "1"),
                    }
                ],
                "demand_input_tax": [
                    {
                        "charge": "cap_charge",
                        "rate": _money("0.06", "1"),
                        "deductible_ratio": _money("1", "1"),
                    }
                ],
                "movement_input_tax": [
                    {
                        "movement": "equip_pay",
                        "rate": _money("0.05", "1"),
                        "deductible_ratio": _money("1", "1"),
                        "confirmation": {
                            "method": "explicit_schedule",
                            "occurrences": [
                                {
                                    "at": {"kind": "financial_year", "year": 1},
                                    "schedule": [{"period": 1, "ratio": "1"}],
                                }
                            ],
                        },
                    }
                ],
                "movement_output_tax": [
                    {
                        "movement": "service_rev",
                        "rate": _money("0.06", "1"),
                        "confirmation": {"method": "recognition_period"},
                    }
                ],
                "credit_pool": "pool",
                "confirmation": "same_period",
            },
            "levy": {
                "kind": "derived_tax",
                "label_ref": "surtax_tag",
                "axis": "tax_month",
                "base_from": {
                    "kind": "tax",
                    "source": {"kind": "pool", "id": "pool"},
                    "measure": "paid",
                    "period": {"kind": "current"},
                },
                "rate": _money("0.07", "1"),
                "payment_lag": 1,
            },
            "cit": {
                "kind": "income_tax",
                "label_ref": "income_tag",
                "rate": _money("0.25", "1"),
                "revenue": [],
                "energy_cost": ["elec_buy"],
                "annual_om": ["grid_ft.annual_fixed_om"],
                "variable_om": ["grid_ft.period_variable_om"],
                "demand_cost": ["cap_charge"],
                "deductible_maintenance": {
                    "mode": "explicit_items",
                    "items": [{"year": 2, "position": 1, "deductible": True}],
                },
                "depreciation": [
                    {
                        "base_from": {"kind": "asset", "id": "plant_asset"},
                        "method": "straight_line",
                        "life_years": 4,
                        "first_year": 1,
                        "residual": _money("100"),
                    },
                    {
                        "base_from": {"kind": "maintenance_expense_net", "year": 2, "position": 1},
                        "method": "straight_line",
                        "life_years": 2,
                        "first_year": 1,
                        "residual": _money("0", "1"),
                    },
                ],
                "loss_carryforward_years": 1,
                "opening_losses": [{"amount": _money("80"), "last_usable_year": 1}],
                "movement_revenue": ["service_rev"],
                "movement_cost": [],
                "derived_tax_cost": ["levy"],
            },
        },
        "net_cashflows": {
            "residual_end": {
                "at": {"kind": "evaluation_end"},
                "direction": "inflow",
                "amount": {"kind": "amount_ref", "id": "end_bonus"},
            },
            "salvage_share": {
                "at": {"kind": "financial_year", "year": 3},
                "direction": "inflow",
                "amount": {"kind": "amount_ref", "id": "asset_salvage"},
            },
            "capex_share": {
                "at": {"kind": "financial_year", "year": 3},
                "direction": "inflow",
                "amount": {
                    "kind": "source_share",
                    "base_from": {
                        "kind": "upfront_capex_net",
                        "finance_type": "grid_ft",
                        "scope": {"kind": "instances", "instances": ["grid"]},
                    },
                    "ratio": _money("0.05", "1"),
                },
            },
            "fixed_grant": {
                "at": {"kind": "financial_year", "year": 2},
                "direction": "inflow",
                "amount": {"kind": "fixed", "value": _money("50")},
            },
        },
    }
    finance = merge_effective(FinanceProfile.from_dict(profile_payload), _empty_overrides("complex-f3"))

    load_demand_val = 10.0 if optional_built else 0.0
    assembly_doc = {
        "schema": "ies.assembly",
        "schema_version": "1.0.0",
        "assembly": {"id": "complex_f3", "name": "Complex F3"},
        "project_baseline": {
            "resolution": "1h",
            "start_year": 2025,
            "scenario_mode": "single",
        },
        "devices": {
            "grid": {
                "definition": {"id": "ies.device.grid_connection"},
                "asset_origin": "new",
                "properties": {
                    "max_import_power_kw": {"value": 100.0, "unit": "kW"},
                    "max_export_power_kw": {"value": 0.0, "unit": "kW"},
                },
                "predefined_interfaces": {},
            },
            "load": {
                "definition": {"id": "ies.device.electric_load"},
                "asset_origin": "existing",
                "properties": {"peak_power_kw": {"value": 100.0, "unit": "kW"}},
                "predefined_interfaces": {
                    "electricity_demand": {"mode": "constant", "value": load_demand_val},
                },
            },
        },
        "connections": {
            "e_grid_load": {"from": "grid.electricity_import", "to": "load.electricity_in"},
        },
        "finance": {
            "ref": {"kind": "object", "object_id": "fin:complex"},
            "profile_id": "complex-f3",
        },
        "finance_binding": {
            "instances": {
                "grid": {
                    "type": "costed",
                    "finance_type": "grid_ft",
                    "drivers": {
                        "cap": {"property": "max_import_power_kw"},
                        "energy": {
                            "interface": "electricity_import",
                            "aggregation": "integrate_positive",
                        },
                    },
                    "cost_occurrence": {
                        "upfront_capex": {"kind": "construction_unit", "unit_ref": "u_grid"},
                        "annual_fixed_om.items.service": {
                            "kind": "construction_unit",
                            "unit_ref": "u_grid",
                        },
                    },
                },
                "load": {"type": "none"},
            }
        },
        "tariff_bindings": {
            "buy_e": {
                "instance": "grid",
                "interface": "electricity_import",
                "aggregation": "integrate_positive",
                "price": "elec_buy",
            }
        },
        "demand_bindings": {
            "grid_demand": {
                "instance": "grid",
                "interface": "electricity_import",
                "charge": "cap_charge",
            }
        },
        "planning": {
            "variables": {
                "v_cap": {
                    "device_ref": "grid",
                    "parameter": "max_import_power_kw",
                    "unit": "kW",
                    "lower_bound": "20" if optional_built else "0",
                    "upper_bound": "20" if optional_built else "0",
                }
            },
            "construction_units": {
                "u_grid": {
                    "device_ref": "grid",
                    "mode": "optional",
                    "scales": {
                        "v_cap": {
                            "active_lower_bound": "20" if optional_built else "0",
                            "active_upper_bound": None,
                        }
                    },
                }
            },
            "constraints": {},
            "objective": {"sense": "maximize", "expression": "system.after_tax_npv"},
        },
        "outputs": {
            "series": ["grid.electricity_import"],
            "metrics": [],
        },
        "extensions": {},
    }
    def _dump_yaml(val: Any, indent: int = 0) -> str:
        pad = " " * indent
        if isinstance(val, dict):
            if not val:
                return "{}"
            lines = []
            for k, v in val.items():
                if isinstance(v, (dict, list)) and v:
                    lines.append(f"{pad}{k}:\n{_dump_yaml(v, indent + 2)}")
                else:
                    lines.append(f"{pad}{k}: {_dump_yaml(v, 0)}")
            return "\n".join(lines)
        if isinstance(val, list):
            if not val:
                return "[]"
            return "\n".join(f"{pad}- {_dump_yaml(item, 0)}" for item in val)
        return json.dumps(val, ensure_ascii=False)

    yaml_text = _dump_yaml(assembly_doc) + "\n"
    load_vals = tuple([load_demand_val] * 17520)
    raw_artifact = validate_assembly_text(
        yaml_text,
        finance_snapshots={"fin:complex": finance},
    )
    assert raw_artifact.artifact is not None, raw_artifact.diagnostics

    raw_comp = {
        "mode": comparison_mode,
        "baseline": {
            "kind": "declared",
            "currency": "CNY",
            "evaluation_years": 3,
            "annual_net_flows": (
                ["-500", "100", "100", "100"] if comparison_mode == "difference" else None
            ),
            "facts": {},
        },
        "facts": {},
        "comparability": {"service_basis": "same", "differences": "test"},
    }
    comp = freeze_comparison(parse_comparison(raw_comp, years=3), years=3, currency="CNY")

    evaluation = EvaluationInput(
        evaluation_years=3,
        construction_period=1,
        operation_years=2,
        discount_rate=0.08,
        construction_payments=(
            ConstructionPayment(t=0, ratio="0.4"),
            ConstructionPayment(t=1, ratio="0.6"),
        ),
        maintenance_expenses=(
            MaintenanceExpense(
                year=2,
                amount="50",
                currency="CNY",
                terms=(
                    ScaleUnitCostTerm(
                        kind="scale_unit_cost",
                        instance_id="grid",
                        property="max_import_power_kw",
                        unit_cost=MaintenanceUnitCost(value="1", unit="CNY/kW"),
                    ),
                    CostComponentShareTerm(
                        kind="cost_component_share",
                        instance_id="grid",
                        component="upfront_capex",
                        ratio=0.05,
                    ),
                ),
            ),
        ),
        comparison=comp,
    )

    price_vals = tuple([0.4 + 0.1 * ((i % 24) // 12) for i in range(17520)])
    materialized = MaterializedSet(
        operation_years=2,
        series=(
            MaterializedSeries(
                device="load",
                interface="electricity_demand",
                mode="constant",
                resolution="1h",
                point_count=17520,
                values=load_vals,
                source={"kind": "constant"},
            ),
        ),
        prices=(
            MaterializedPriceSeries(
                price_id="elec_buy",
                point_count=17520,
                resolution="1h",
                unit="CNY/kWh",
                values=price_vals,
                source={"kind": "price_series", "object_id": "price:elec_buy"},
            ),
        ),
    )
    models = {
        "grid": device_contribs["ies.device.grid_connection"],
        "load": device_contribs["ies.device.electric_load"],
    }
    return raw_artifact.artifact, models, materialized, finance, evaluation


@pytest.mark.parametrize("optional_built", [True, False])
def test_forward_finance_reconciles_complex_tax_demand_and_comparison(
    tmp_path: Path, optional_built: bool
) -> None:
    """Complex multi-year VAT/income/derived/assets/movements/demand/maintenance/comparison matches 1.1.5."""
    artifact, models, materialized, finance, evaluation = _complex_finance_case(
        optional_built=optional_built, comparison_mode="difference"
    )
    legacy_cfg = _campus_inputs()[4]
    target_cfg = _target_campus_config()

    target_bundle = generate_target_highs_bundle(
        artifact, models, materialized, finance, target_cfg, evaluation
    )
    solution = _solve_target_bundle_to_solution(target_bundle, tmp_path / f"b_{optional_built}")
    expected = _run_legacy_115_evaluation(
        artifact, models, materialized, finance, legacy_cfg, evaluation, tmp_path / f"l_{optional_built}"
    )

    actual = evaluate_forward_finance(
        solution,
        artifact=artifact,
        finance=finance,
        evaluation=evaluation,
        materialized=materialized,
        device_models=models,
    )
    _assert_eval_close(actual, expected)
    assert actual["comparison"]["mode"] == "difference"
    assert actual["construction_units"][0]["built"] is optional_built

    # Serialized device_models (valid_range dicts from contribution_to_dict) must match dataclasses
    serialized_models = {inst: contribution_to_dict(model) for inst, model in models.items()}
    actual_serialized = evaluate_forward_finance(
        solution,
        artifact=artifact,
        finance=finance,
        evaluation=evaluation,
        materialized=materialized,
        device_models=serialized_models,
    )
    assert actual_serialized == actual


def test_pure_forward_finance_synthetic_solution_without_highs() -> None:
    """Pure forward evaluation from synthetic engineering-solution@1.0.0 without HiGHS/bundle."""
    profile_payload = {
        "schema": "ies.finance-profile",
        "schema_version": "1.0.0",
        "profile": {
            "id": "pure-fwd",
            "region": "CN-North",
            "currency": "CNY",
            "base_year": 2025,
            "price_basis": "tax_inclusive",
            "cost_method": "fixed_plus_linear",
        },
        "finance_types": {
            "gen": {
                "upfront_capex": {
                    "fixed": _money("200"),
                    "linear": {"cap": {"unit_cost": _money("10", "CNY/kW")}},
                },
                "annual_fixed_om": {"fixed": _money("30", "CNY/a")},
                "period_variable_om": {
                    "linear": {"energy": {"unit_cost": _money("0.1", "CNY/kWh")}}
                },
            }
        },
        "energy_prices": {
            "grid_sell": {
                "carrier": "electricity",
                "direction": "sale",
                "kind": "constant",
                "value": _money("1.0", "CNY/kWh"),
            }
        },
        "demand_charges": {
            "dc": {"rate": _money("2", "CNY/kW")},
        },
        "taxes": {
            "income_tag": {
                "display_name": "CIT",
                "tax_type": "income_tax",
                "applies_to": "gen.upfront_capex",
            },
        },
        "credit_pools": {},
        "tax_rules": {
            "cit": {
                "kind": "income_tax",
                "label_ref": "income_tag",
                "rate": _money("0.25", "1"),
                "revenue": ["grid_sell"],
                "energy_cost": [],
                "annual_om": ["gen.annual_fixed_om"],
                "variable_om": ["gen.period_variable_om"],
                "demand_cost": ["dc"],
                "deductible_maintenance": {"mode": "explicit_items", "items": []},
                "depreciation": [
                    {
                        "base_from": {
                            "kind": "upfront_capex_net",
                            "finance_type": "gen",
                            "scope": {"kind": "all_new_instances"},
                        },
                        "method": "straight_line",
                        "life_years": 2,
                        "first_year": 1,
                        "residual": _money("0", "1"),
                    }
                ],
                "loss_carryforward_years": 2,
                "opening_losses": [{"amount": _money("100"), "last_usable_year": 1}],
            }
        },
        "net_cashflows": {
            "end_bonus": {
                "at": {"kind": "evaluation_end"},
                "direction": "inflow",
                "amount": {"kind": "fixed", "value": _money("50")},
            }
        },
    }
    finance = merge_effective(FinanceProfile.from_dict(profile_payload), _empty_overrides("pure-fwd"))

    assembly_doc = {
        "schema": "ies.assembly",
        "schema_version": "3.0.0",
        "assembly": {"id": "pure_synth", "name": "Pure Synthetic"},
        "project_baseline": {
            "resolution": "1h",
            "start_year": 2025,
            "scenario_mode": "single_year",
        },
        "devices": {
            "g": {"asset_origin": "new", "properties": {"cap_kw": {"value": 10.0, "unit": "kW"}}},
        },
        "finance_binding": {
            "instances": {
                "g": {
                    "type": "costed",
                    "finance_type": "gen",
                    "drivers": {
                        "cap": {"property": "cap_kw"},
                        "energy": {"interface": "out_e", "aggregation": "integrate_positive"},
                    },
                }
            }
        },
        "tariff_bindings": {
            "sell": {
                "instance": "g",
                "interface": "out_e",
                "aggregation": "integrate_positive",
                "price": "grid_sell",
            }
        },
        "demand_bindings": {
            "dem": {
                "instance": "g",
                "interface": "out_e",
                "charge": "dc",
            }
        },
        "planning": {
            "variables": {
                "cap_v": {"device_ref": "g", "parameter": "cap_kw", "unit": "kW"},
            },
            "construction_units": {
                "u_g": {"device_ref": "g", "mode": "mandatory", "scales": {"cap_v": {}}},
            },
            "constraints": {},
        },
    }

    # 1 operating year (8760 steps @ 1h): constant 1 kW -> 8760 kWh/yr, peak 1 kW * 12 mo = 24 CNY
    solution = {
        "schema": "ies.engineering-solution",
        "schema_version": "1.0.0",
        "solver_summary": {
            "termination_reason": "optimal",
            "math_status": "solved",
            "has_feasible_solution": True,
            "objective_value": 0.0,
            "objective_unit": "CNY",
            "best_bound": 0.0,
            "relative_gap": 0.0,
        },
        "construction_units": [
            {
                "unit_id": "u_g",
                "instance_id": "g",
                "mode": "mandatory",
                "built": True,
                "scales": [{"planning_variable_id": "cap_v", "value": 80.0, "unit": "kW"}],
            }
        ],
        "series": {
            "g.out_e": {
                "instance_id": "g",
                "variable_id": "out_e",
                "unit": "kW",
                "values": [1.0] * 8760,
            }
        },
        "system_series": {},
    }

    # Use canonical frozen evaluation dict with string ratio via parse_evaluation_input
    from iesplan.tasks.contracts import parse_evaluation_input

    parsed_eval = parse_evaluation_input(
        {
            "evaluation_years": 1,
            "construction_period": 0,
            "operation_years": 1,
            "discount_rate": 0.0,
            "construction_payments": [{"t": 0, "ratio": 1}],
            "maintenance_expenses": [],
        }
    )
    assert parsed_eval.construction_payments[0].ratio == "1"

    out = evaluate_forward_finance(
        solution,
        artifact=assembly_doc,
        finance=finance,
        evaluation=parsed_eval,
    )
    # Construction = 200 + 80 * 10 = 1000
    assert Decimal(out["construction_total"]) == Decimal("1000")
    assert out["payments_applied"] == [{"t": 0, "ratio": "1", "amount": "1000.0"}]
    assert Decimal(out["annual_fixed_om"]) == Decimal("30")
    # Energy = 8760 * 1 = 8760; Variable OM = 876; Fixed OM = 30; Demand = 12 * 2 = 24
    op0 = out["annual_operation"][0]
    assert Decimal(op0["energy_net"]) == Decimal("8760")
    assert Decimal(op0["variable_om"]) == Decimal("876")
    assert Decimal(op0["fixed_om"]) == Decimal("30")
    assert Decimal(op0["demand_charge"]) == Decimal("24")
    assert Decimal(op0["net"]) == Decimal("7830")
    # Depreciation = 1000 / 2 = 500; Base = 7330; Opening loss used = 100; Tax = 0.25 * 7230 = 1807.5
    inc0 = out["tax_cash"]["income"][0]
    assert Decimal(inc0["taxable_base"]) == Decimal("7330")
    assert Decimal(inc0["loss_used"]) == Decimal("100")
    assert Decimal(inc0["assessed"]) == Decimal("1807.5")
    # CF[0] = -1000, CF[1] = 7830 - 1807.5 + 50 (net_cashflow) = 6072.5
    assert [Decimal(v) for v in out["annual_net_flows"]] == [Decimal("-1000"), Decimal("6072.5")]
    assert Decimal(out["evaluation"]["npv"]) == Decimal("5072.5")
    assert len(out["monthly_demand"]) == 12
    assert all(Decimal(row["peak_power"]) == Decimal("1") for row in out["monthly_demand"])

    # Also verify non-validating frozen dict reconstruction works with string ratio
    frozen_eval = {
        "evaluation_years": 1,
        "construction_period": 0,
        "operation_years": 1,
        "discount_rate": 0.0,
        "construction_payments": [{"t": 0, "ratio": "1"}],
        "maintenance_expenses": [],
    }
    out_frozen = evaluate_forward_finance(
        solution,
        artifact=assembly_doc,
        finance=finance.to_dict(),
        evaluation=frozen_eval,
    )
    assert out_frozen["payments_applied"] == out["payments_applied"]
    assert out_frozen["annual_net_flows"] == out["annual_net_flows"]

    # Omission of planned variable / construction unit must raise, never fall back to property default
    missing_scale_sol = deepcopy(solution)
    missing_scale_sol["construction_units"][0]["scales"] = []
    with pytest.raises(KeyError, match="cap_v"):
        evaluate_forward_finance(
            missing_scale_sol, artifact=assembly_doc, finance=finance, evaluation=parsed_eval
        )

    missing_unit_sol = deepcopy(solution)
    missing_unit_sol["construction_units"] = []
    with pytest.raises(KeyError):
        evaluate_forward_finance(
            missing_unit_sol, artifact=assembly_doc, finance=finance, evaluation=parsed_eval
        )

    # Infeasible solution rejected explicitly
    bad_sol = deepcopy(solution)
    bad_sol["solver_summary"]["has_feasible_solution"] = False
    bad_sol["solver_summary"]["math_status"] = "infeasible"
    with pytest.raises(ValueError, match="仅支持可行"):
        evaluate_forward_finance(bad_sol, artifact=assembly_doc, finance=finance, evaluation=parsed_eval)


def test_forward_finance_reconciles_zero_realized_loss_sign_crossing_base(tmp_path: Path) -> None:
    """Sign-crossing taxable base (base_lo < 0, realized base > 0) retains zero-balance v0 like 1.1.5."""
    init_registry()
    device_contribs = {}
    for dev_doc in list_devices():
        built_dev = build_math_contribution(dev_doc)
        assert built_dev.ok, (dev_doc.device.id, built_dev.diagnostics)
        device_contribs[dev_doc.device.id] = built_dev.contribution

    profile_payload = {
        "schema": "ies.finance-profile",
        "schema_version": "1.0.0",
        "profile": {
            "id": "zero-loss-sign-cross",
            "region": "CN-North",
            "currency": "CNY",
            "base_year": 2025,
            "price_basis": "tax_inclusive",
            "cost_method": "fixed_plus_linear",
        },
        "finance_types": {
            "grid_ft": {
                "upfront_capex": {"linear": {"cap": {"unit_cost": _money("10", "CNY/kW")}}},
                "annual_fixed_om": {"fixed": _money("50", "CNY/a")},
            },
        },
        "energy_prices": {
            "elec_sell": {
                "carrier": "electricity",
                "direction": "sale",
                "kind": "constant",
                "value": _money("1.0", "CNY/kWh"),
            }
        },
        "demand_charges": {},
        "taxes": {
            "income_tag": {
                "display_name": "CIT",
                "tax_type": "income_tax",
                "applies_to": "grid_ft.upfront_capex",
            }
        },
        "credit_pools": {},
        "tax_rules": {
            "cit": {
                "kind": "income_tax",
                "label_ref": "income_tag",
                "rate": _money("0.25", "1"),
                "revenue": ["elec_sell"],
                "energy_cost": [],
                "annual_om": ["grid_ft.annual_fixed_om"],
                "variable_om": [],
                "deductible_maintenance": {"mode": "explicit_items", "items": []},
                "depreciation": [],
                "loss_carryforward_years": 2,
                "opening_losses": [],
            }
        },
        "net_cashflows": {},
    }
    finance = merge_effective(
        FinanceProfile.from_dict(profile_payload), _empty_overrides("zero-loss-sign-cross")
    )
    assembly_yaml = """schema: ies.assembly
schema_version: "1.0.0"
assembly:
  id: zero_loss_sign_cross
  name: Zero Loss Sign Cross
project_baseline:
  resolution: 1h
  start_year: 2025
  scenario_mode: single
devices:
  grid:
    definition:
      id: ies.device.grid_connection
    asset_origin: new
    properties:
      max_import_power_kw:
        value: 0
        unit: kW
      max_export_power_kw:
        value: 20
        unit: kW
    predefined_interfaces: {}
  load:
    definition:
      id: ies.device.electric_load
    asset_origin: existing
    properties:
      peak_power_kw:
        value: 10
        unit: kW
    predefined_interfaces:
      electricity_demand:
        mode: constant
        value: 5
connections:
  e_export:
    from: load.electricity_in
    to: grid.electricity_export
finance:
  ref:
    kind: object
    object_id: fin:zero-cross
  profile_id: zero-loss-sign-cross
finance_binding:
  instances:
    grid:
      type: costed
      finance_type: grid_ft
      drivers:
        cap:
          property: max_export_power_kw
    load:
      type: none
tariff_bindings:
  sell_e:
    instance: grid
    interface: electricity_export
    aggregation: integrate_positive
    price: elec_sell
demand_bindings: {}
planning:
  objective:
    sense: maximize
    expression: system.after_tax_npv
  variables:
    v_cap:
      device_ref: grid
      parameter: max_export_power_kw
      unit: kW
      lower_bound: "10"
      upper_bound: "10"
  construction_units:
    u_grid:
      device_ref: grid
      mode: mandatory
      scales:
        v_cap:
          active_lower_bound: "10"
          active_upper_bound: "10"
  constraints: {}
outputs:
  series:
    - grid.electricity_export
  metrics: []
extensions: {}
"""
    # Replace fixed load with expandable PV ([0, 10] kWp) so base_lo < 0 (-50 fixed OM)
    # while optimal realized taxable_base > 0 (+~87k sale revenue), matching 1.1.5 v0 retention.
    pv_yaml = (
        assembly_yaml.replace(
            """  load:
    definition:
      id: ies.device.electric_load
    asset_origin: existing
    properties:
      peak_power_kw:
        value: 10
        unit: kW
    predefined_interfaces:
      electricity_demand:
        mode: constant
        value: 5
connections:
  e_export:
    from: load.electricity_in
    to: grid.electricity_export""",
            """  pv1:
    definition:
      id: ies.device.pv
    asset_origin: new
    properties:
      rated_capacity_kwp:
        value: 10
        unit: kWp
    predefined_interfaces:
      solar_irradiance:
        mode: constant
        value: 1000
      ambient_temperature:
        mode: constant
        value: 25
connections:
  e_export:
    from: pv1.electric_out
    to: grid.electricity_export""",
        )
        .replace(
            """    load:
      type: none""",
            """    pv1:
      type: none""",
        )
        .replace(
            """    v_cap:
      device_ref: grid
      parameter: max_export_power_kw
      unit: kW
      lower_bound: "10"
      upper_bound: "10"
  construction_units:
    u_grid:
      device_ref: grid
      mode: mandatory
      scales:
        v_cap:
          active_lower_bound: "10"
          active_upper_bound: "10\"""",
            """    v_cap:
      device_ref: grid
      parameter: max_export_power_kw
      unit: kW
      lower_bound: "10"
      upper_bound: "10"
    v_pv:
      device_ref: pv1
      parameter: rated_capacity_kwp
      unit: kWp
      lower_bound: "0"
      upper_bound: "10"
  construction_units:
    u_grid:
      device_ref: grid
      mode: mandatory
      scales:
        v_cap:
          active_lower_bound: "10"
          active_upper_bound: "10"
    u_pv:
      device_ref: pv1
      mode: mandatory
      scales:
        v_pv:
          active_lower_bound: "0"
          active_upper_bound: "10\"""",
        )
    )
    res = validate_assembly_text(pv_yaml, finance_snapshots={"fin:zero-cross": finance})
    assert res.artifact is not None, res.diagnostics
    artifact = res.artifact
    models = {
        "grid": device_contribs["ies.device.grid_connection"],
        "pv1": device_contribs["ies.device.pv"],
    }
    materialized = MaterializedSet(
        operation_years=1,
        series=(
            MaterializedSeries(
                device="pv1",
                interface="solar_irradiance",
                mode="constant",
                resolution="1h",
                point_count=8760,
                values=(1000.0,) * 8760,
                source={"kind": "constant"},
            ),
            MaterializedSeries(
                device="pv1",
                interface="ambient_temperature",
                mode="constant",
                resolution="1h",
                point_count=8760,
                values=(25.0,) * 8760,
                source={"kind": "constant"},
            ),
        ),
        prices=(),
    )
    from iesplan.tasks.contracts import parse_evaluation_input

    evaluation = parse_evaluation_input(
        {
            "evaluation_years": 1,
            "construction_period": 0,
            "operation_years": 1,
            "discount_rate": 0.08,
            "construction_payments": [{"t": 0, "ratio": 1}],
            "maintenance_expenses": [],
        }
    )
    target_bundle = generate_target_highs_bundle(
        artifact, models, materialized, finance, _target_campus_config(), evaluation
    )
    solution = _solve_target_bundle_to_solution(target_bundle, tmp_path / "t")
    legacy_cfg = _campus_inputs()[4]
    expected = _run_legacy_115_evaluation(
        artifact, models, materialized, finance, legacy_cfg, evaluation, tmp_path / "l"
    )
    actual = evaluate_forward_finance(
        solution,
        artifact=artifact,
        finance=finance,
        evaluation=evaluation,
        materialized=materialized,
        device_models=models,
    )
    assert Decimal(actual["tax_cash"]["income"][0]["taxable_base"]) > Decimal("0")
    assert Decimal(actual["tax_cash"]["income"][0]["loss_generated"]) == Decimal("0")
    assert expected["tax_cash"]["loss_balances"] == [
        {
            "rule": "cit",
            "vintage_id": "v0",
            "year": 1,
            "last_usable_year": 3,
            "amount": "0.0",
        }
    ]
    _assert_eval_close(actual, expected)


def test_forward_finance_reconciles_shared_expression_cancellation_no_loss_balances(
    tmp_path: Path,
) -> None:
    """Shared-expression cancellation (movement revenue == 1-yr capex depr) emits 0 loss_balances."""
    init_registry()
    device_contribs = {}
    for dev_doc in list_devices():
        built_dev = build_math_contribution(dev_doc)
        assert built_dev.ok, (dev_doc.device.id, built_dev.diagnostics)
        device_contribs[dev_doc.device.id] = built_dev.contribution

    profile_payload = {
        "schema": "ies.finance-profile",
        "schema_version": "1.0.0",
        "profile": {
            "id": "shared-cancel",
            "region": "CN-North",
            "currency": "CNY",
            "base_year": 2025,
            "price_basis": "tax_exclusive",
            "cost_method": "fixed_plus_linear",
        },
        "finance_types": {
            "grid_ft": {
                "upfront_capex": {
                    "fixed": _money("100"),
                    "linear": {"cap": {"unit_cost": _money("50", "CNY/kW")}},
                },
            },
        },
        "energy_prices": {
            "elec_buy": {
                "carrier": "electricity",
                "direction": "purchase",
                "kind": "constant",
                "value": _money("0.5", "CNY/kWh"),
            }
        },
        "demand_charges": {},
        "taxes": {
            "income_tag": {
                "display_name": "CIT",
                "tax_type": "income_tax",
                "applies_to": "grid_ft.upfront_capex",
            }
        },
        "credit_pools": {},
        "amounts": {
            "capex_net_amt": {
                "unit": "CNY",
                "axis": "scalar",
                "constant": _money("0"),
                "terms": {
                    "c": {
                        "coefficient": _money("1", "1"),
                        "source": {
                            "kind": "cost",
                            "path": "grid_ft.upfront_capex",
                            "basis": "net_basis",
                            "scope": {"kind": "instances", "instances": ["grid"]},
                            "period": {"kind": "scalar"},
                        },
                    }
                },
            }
        },
        "cash_movements": {
            "capex_offset": {
                "amount": {"kind": "amount", "id": "capex_net_amt", "period": {"kind": "scalar"}},
                "price_basis": "tax_exclusive",
                "direction": "inflow",
                "recognition": {"kind": "at", "at": {"kind": "financial_year", "year": 1}},
                "payments": [{"offset": 0, "ratio": "1"}],
            }
        },
        "assets": {
            "capex_asset": {
                "basis_from": {
                    "kind": "cost",
                    "path": "grid_ft.upfront_capex",
                    "basis": "net_basis",
                    "scope": {"kind": "instances", "instances": ["grid"]},
                    "period": {"kind": "scalar"},
                },
                "capitalized_at": {"kind": "financial_year", "year": 1},
                "available_from": 1,
            }
        },
        "tax_rules": {
            "cit": {
                "kind": "income_tax",
                "label_ref": "income_tag",
                "rate": _money("0.25", "1"),
                "revenue": [],
                "energy_cost": [],
                "annual_om": [],
                "variable_om": [],
                "deductible_maintenance": {"mode": "explicit_items", "items": []},
                "depreciation": [
                    {
                        "base_from": {"kind": "asset", "id": "capex_asset"},
                        "method": "straight_line",
                        "life_years": 1,
                        "first_year": 1,
                        "residual": _money("0", "1"),
                    }
                ],
                "movement_revenue": ["capex_offset"],
                "movement_cost": [],
                "loss_carryforward_years": 2,
                "opening_losses": [],
            }
        },
        "net_cashflows": {},
    }
    finance = merge_effective(
        FinanceProfile.from_dict(profile_payload), _empty_overrides("shared-cancel")
    )
    assembly_yaml = """schema: ies.assembly
schema_version: "1.0.0"
assembly:
  id: shared_cancel
  name: Shared Expression Cancellation
project_baseline:
  resolution: 1h
  start_year: 2025
  scenario_mode: single
devices:
  grid:
    definition:
      id: ies.device.grid_connection
    asset_origin: new
    properties:
      max_import_power_kw:
        value: 20
        unit: kW
      max_export_power_kw:
        value: 0
        unit: kW
    predefined_interfaces: {}
  load:
    definition:
      id: ies.device.electric_load
    asset_origin: existing
    properties:
      peak_power_kw:
        value: 20
        unit: kW
    predefined_interfaces:
      electricity_demand:
        mode: constant
        value: 0
connections:
  e_grid_load:
    from: grid.electricity_import
    to: load.electricity_in
finance:
  ref:
    kind: object
    object_id: fin:shared-cancel
  profile_id: shared-cancel
finance_binding:
  instances:
    grid:
      type: costed
      finance_type: grid_ft
      drivers:
        cap:
          property: max_import_power_kw
      cost_occurrence:
        upfront_capex:
          kind: construction_unit
          unit_ref: u_grid
    load:
      type: none
tariff_bindings: {}
demand_bindings: {}
planning:
  objective:
    sense: maximize
    expression: system.after_tax_npv
  variables:
    v_cap:
      device_ref: grid
      parameter: max_import_power_kw
      unit: kW
      lower_bound: "0"
      upper_bound: "20"
  construction_units:
    u_grid:
      device_ref: grid
      mode: optional
      scales:
        v_cap:
          active_lower_bound: "0"
          active_upper_bound: "20"
  constraints: {}
outputs:
  series:
    - grid.electricity_import
  metrics: []
extensions: {}
"""
    res = validate_assembly_text(assembly_yaml, finance_snapshots={"fin:shared-cancel": finance})
    assert res.artifact is not None, res.diagnostics
    artifact = res.artifact
    models = {
        "grid": device_contribs["ies.device.grid_connection"],
        "load": device_contribs["ies.device.electric_load"],
    }
    materialized = MaterializedSet(
        operation_years=1,
        series=(
            MaterializedSeries(
                device="load",
                interface="electricity_demand",
                mode="constant",
                resolution="1h",
                point_count=8760,
                values=(0.0,) * 8760,
                source={"kind": "constant"},
            ),
        ),
        prices=(),
    )
    from iesplan.tasks.contracts import parse_evaluation_input

    evaluation = parse_evaluation_input(
        {
            "evaluation_years": 1,
            "construction_period": 0,
            "operation_years": 1,
            "discount_rate": 0.08,
            "construction_payments": [{"t": 0, "ratio": 1}],
            "maintenance_expenses": [],
        }
    )
    target_bundle = generate_target_highs_bundle(
        artifact, models, materialized, finance, _target_campus_config(), evaluation
    )
    solution = _solve_target_bundle_to_solution(target_bundle, tmp_path / "t")
    legacy_cfg = _campus_inputs()[4]
    expected = _run_legacy_115_evaluation(
        artifact, models, materialized, finance, legacy_cfg, evaluation, tmp_path / "l"
    )
    actual = evaluate_forward_finance(
        solution,
        artifact=artifact,
        finance=finance,
        evaluation=evaluation,
        materialized=materialized,
        device_models={inst: contribution_to_dict(m) for inst, m in models.items()},
    )
    assert Decimal(actual["tax_cash"]["income"][0]["taxable_base"]) == Decimal("0")
    assert expected["tax_cash"]["loss_balances"] == []
    assert actual["tax_cash"]["loss_balances"] == []
    _assert_eval_close(actual, expected)


def test_forward_finance_reconciles_distinct_sources_no_false_cancellation(tmp_path: Path) -> None:
    """Distinct driver or system_quantity sources must never cancel falsely in _AffineExpr."""
    init_registry()
    device_contribs = {}
    for dev_doc in list_devices():
        built_dev = build_math_contribution(dev_doc)
        assert built_dev.ok, (dev_doc.device.id, built_dev.diagnostics)
        device_contribs[dev_doc.device.id] = built_dev.contribution

    profile_payload = {
        "schema": "ies.finance-profile",
        "schema_version": "1.0.0",
        "profile": {
            "id": "distinct-no-cancel",
            "region": "CN-North",
            "currency": "CNY",
            "base_year": 2025,
            "price_basis": "tax_exclusive",
            "cost_method": "fixed_plus_linear",
        },
        "finance_types": {
            "grid_ft": {
                "upfront_capex": {
                    "linear": {
                        "cap_imp": {"unit_cost": _money("10", "CNY/kW")},
                        "cap_exp": {"unit_cost": _money("0", "CNY/kW")},
                    }
                },
            },
        },
        "energy_prices": {
            "elec_buy": {
                "carrier": "electricity",
                "direction": "purchase",
                "kind": "constant",
                "value": _money("0.5", "CNY/kWh"),
            }
        },
        "demand_charges": {},
        "taxes": {
            "income_tag": {
                "display_name": "CIT",
                "tax_type": "income_tax",
                "applies_to": "grid_ft.upfront_capex",
            }
        },
        "credit_pools": {},
        "amounts": {
            "rev_imp_amt": {
                "unit": "CNY",
                "axis": "scalar",
                "constant": _money("0"),
                "terms": {
                    "d": {
                        "coefficient": _money("1", "CNY/kW"),
                        "source": {
                            "kind": "driver",
                            "finance_type": "grid_ft",
                            "driver": "cap_imp",
                            "scope": {"kind": "instances", "instances": ["grid"]},
                            "period": {"kind": "scalar"},
                        },
                    }
                },
            },
            "cost_exp_amt": {
                "unit": "CNY",
                "axis": "scalar",
                "constant": _money("0"),
                "terms": {
                    "d": {
                        "coefficient": _money("1", "CNY/kW"),
                        "source": {
                            "kind": "driver",
                            "finance_type": "grid_ft",
                            "driver": "cap_exp",
                            "scope": {"kind": "instances", "instances": ["grid"]},
                            "period": {"kind": "scalar"},
                        },
                    }
                },
            },
        },
        "cash_movements": {
            "rev_imp": {
                "amount": {"kind": "amount", "id": "rev_imp_amt", "period": {"kind": "scalar"}},
                "price_basis": "tax_exclusive",
                "direction": "inflow",
                "recognition": {"kind": "at", "at": {"kind": "financial_year", "year": 1}},
                "payments": [{"offset": 0, "ratio": "1"}],
            },
            "cost_exp": {
                "amount": {"kind": "amount", "id": "cost_exp_amt", "period": {"kind": "scalar"}},
                "price_basis": "tax_exclusive",
                "direction": "outflow",
                "recognition": {"kind": "at", "at": {"kind": "financial_year", "year": 1}},
                "payments": [{"offset": 0, "ratio": "1"}],
            },
        },
        "assets": {},
        "tax_rules": {
            "cit": {
                "kind": "income_tax",
                "label_ref": "income_tag",
                "rate": _money("0.25", "1"),
                "revenue": [],
                "energy_cost": [],
                "annual_om": [],
                "variable_om": [],
                "deductible_maintenance": {"mode": "explicit_items", "items": []},
                "depreciation": [],
                "movement_revenue": ["rev_imp"],
                "movement_cost": ["cost_exp"],
                "loss_carryforward_years": 2,
                "opening_losses": [],
            }
        },
        "net_cashflows": {},
    }
    finance = merge_effective(
        FinanceProfile.from_dict(profile_payload), _empty_overrides("distinct-no-cancel")
    )
    assembly_yaml = """schema: ies.assembly
schema_version: "1.0.0"
assembly:
  id: distinct_no_cancel
  name: Distinct Drivers No False Cancellation
project_baseline:
  resolution: 1h
  start_year: 2025
  scenario_mode: single
devices:
  grid:
    definition:
      id: ies.device.grid_connection
    asset_origin: new
    properties:
      max_import_power_kw:
        value: 10
        unit: kW
      max_export_power_kw:
        value: 10
        unit: kW
    predefined_interfaces: {}
  load:
    definition:
      id: ies.device.electric_load
    asset_origin: existing
    properties:
      peak_power_kw:
        value: 10
        unit: kW
    predefined_interfaces:
      electricity_demand:
        mode: constant
        value: 0
connections:
  e_grid_load:
    from: grid.electricity_import
    to: load.electricity_in
finance:
  ref:
    kind: object
    object_id: fin:distinct-no-cancel
  profile_id: distinct-no-cancel
finance_binding:
  instances:
    grid:
      type: costed
      finance_type: grid_ft
      drivers:
        cap_imp:
          property: max_import_power_kw
        cap_exp:
          property: max_export_power_kw
    load:
      type: none
tariff_bindings: {}
demand_bindings: {}
planning:
  objective:
    sense: maximize
    expression: system.after_tax_npv
  variables:
    v_imp:
      device_ref: grid
      parameter: max_import_power_kw
      unit: kW
      lower_bound: "0"
      upper_bound: "10"
    v_exp:
      device_ref: grid
      parameter: max_export_power_kw
      unit: kW
      lower_bound: "0"
      upper_bound: "10"
  construction_units:
    u_grid:
      device_ref: grid
      mode: mandatory
      scales:
        v_imp:
          active_lower_bound: "0"
          active_upper_bound: "10"
        v_exp:
          active_lower_bound: "0"
          active_upper_bound: "10"
  constraints: {}
outputs:
  series:
    - grid.electricity_import
  metrics: []
extensions: {}
"""
    res = validate_assembly_text(assembly_yaml, finance_snapshots={"fin:distinct-no-cancel": finance})
    assert res.artifact is not None, res.diagnostics
    artifact = res.artifact
    models = {
        "grid": device_contribs["ies.device.grid_connection"],
        "load": device_contribs["ies.device.electric_load"],
    }
    materialized = MaterializedSet(
        operation_years=1,
        series=(
            MaterializedSeries(
                device="load",
                interface="electricity_demand",
                mode="constant",
                resolution="1h",
                point_count=8760,
                values=(0.0,) * 8760,
                source={"kind": "constant"},
            ),
        ),
        prices=(),
    )
    from iesplan.tasks.contracts import parse_evaluation_input

    evaluation = parse_evaluation_input(
        {
            "evaluation_years": 1,
            "construction_period": 0,
            "operation_years": 1,
            "discount_rate": 0.08,
            "construction_payments": [{"t": 0, "ratio": 1}],
            "maintenance_expenses": [],
        }
    )
    target_bundle = generate_target_highs_bundle(
        artifact, models, materialized, finance, _target_campus_config(), evaluation
    )
    solution = _solve_target_bundle_to_solution(target_bundle, tmp_path / "t")
    legacy_cfg = _campus_inputs()[4]
    expected = _run_legacy_115_evaluation(
        artifact, models, materialized, finance, legacy_cfg, evaluation, tmp_path / "l"
    )
    actual = evaluate_forward_finance(
        solution,
        artifact=artifact,
        finance=finance,
        evaluation=evaluation,
        materialized=materialized,
        device_models={inst: contribution_to_dict(m) for inst, m in models.items()},
    )
    assert Decimal(actual["tax_cash"]["income"][0]["taxable_base"]) == Decimal("0")
    assert expected["tax_cash"]["loss_balances"] == [
        {
            "rule": "cit",
            "vintage_id": "v0",
            "year": 1,
            "last_usable_year": 3,
            "amount": "0.0",
        }
    ]
    _assert_eval_close(actual, expected)

    # Also verify distinct vs identical system_quantity references on pure forward path
    sys_profile = deepcopy(profile_payload)
    sys_profile["amounts"]["rev_imp_amt"] = {
        "unit": "CNY",
        "axis": "financial_year",
        "constant": _money("0"),
        "terms": {
            "d": {
                "coefficient": _money("1", "CNY/kWh"),
                "source": {
                    "kind": "system_quantity",
                    "block": "sys",
                    "variable": "a",
                    "aggregation": "integrate_positive",
                    "period": {"kind": "current"},
                },
            }
        },
    }
    sys_profile["amounts"]["cost_exp_amt"] = {
        "unit": "CNY",
        "axis": "financial_year",
        "constant": _money("0"),
        "terms": {
            "d": {
                "coefficient": _money("1", "CNY/kWh"),
                "source": {
                    "kind": "system_quantity",
                    "block": "sys",
                    "variable": "b",
                    "aggregation": "integrate_positive",
                    "period": {"kind": "current"},
                },
            }
        },
    }
    sys_profile["cash_movements"]["rev_imp"]["amount"] = {
        "kind": "amount",
        "id": "rev_imp_amt",
        "period": {"kind": "current"},
    }
    sys_profile["cash_movements"]["cost_exp"]["amount"] = {
        "kind": "amount",
        "id": "cost_exp_amt",
        "period": {"kind": "current"},
    }
    sys_finance = merge_effective(
        FinanceProfile.from_dict(sys_profile), _empty_overrides("distinct-no-cancel")
    )
    sys_sol = deepcopy(solution)
    sys_sol["system_series"] = {
        "sys.a": {"block": "sys", "variable": "a", "unit": "kW", "values": [1.0] * 8760},
        "sys.b": {"block": "sys", "variable": "b", "unit": "kW", "values": [1.0] * 8760},
    }
    distinct_out = evaluate_forward_finance(
        sys_sol,
        artifact=artifact,
        finance=sys_finance,
        evaluation=evaluation,
        materialized=materialized,
        device_models=models,
    )
    assert Decimal(distinct_out["tax_cash"]["income"][0]["taxable_base"]) == Decimal("0")
    assert len(distinct_out["tax_cash"]["loss_balances"]) == 1

    # Identical (block, variable, aggregation, period) system_quantity terms DO cancel
    sys_profile["amounts"]["cost_exp_amt"]["terms"]["d"]["source"]["variable"] = "a"
    same_finance = merge_effective(
        FinanceProfile.from_dict(sys_profile), _empty_overrides("distinct-no-cancel")
    )
    same_out = evaluate_forward_finance(
        sys_sol,
        artifact=artifact,
        finance=same_finance,
        evaluation=evaluation,
        materialized=materialized,
        device_models=models,
    )
    assert same_out["tax_cash"]["loss_balances"] == []


def test_finance_package_has_no_assembly_or_domain_imports() -> None:
    """Architecture check: iesplan.finance must not import assembly/computation/modeling/devices."""
    import ast

    finance_dir = Path(__file__).resolve().parent.parent / "iesplan" / "finance"
    forbidden = ("assembly", "computation", "modeling", "devices", "planning", "tasks", "results")
    for py_file in sorted(finance_dir.glob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for mod in forbidden:
                        assert not alias.name.startswith(f"iesplan.{mod}"), (
                            f"{py_file.name}: forbidden import {alias.name}"
                        )
            elif isinstance(node, ast.ImportFrom) and node.module:
                for mod in forbidden:
                    assert not node.module.startswith(f"iesplan.{mod}"), (
                        f"{py_file.name}: forbidden import from {node.module}"
                    )
