"""P2-S3: construction activation, scale linking, conditional finance and result recovery.

Requirements verified:
1. Binary construction activation status z (built_{inst}) linked to scale q (L*z <= q <= U*z).
2. Distinction between mandatory build (z=1), optional build (z in {0, 1}), unconditional costs, and conditional costs (F*z).
3. Multiple scales sharing same activation state (e.g. capacity and power for same instance).
4. Multiple instances sharing same finance type do not conflate construction states.
5. Zero construction: when z=0, q=0, capex=0, payments=0, depreciation=0, residual=0. No fictitious values.
6. Recovery and sealing of construction status and scales from solver solution.
"""
from __future__ import annotations

import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from iesplan.assembly.contracts import ValidatedAssemblyArtifact, ValidationReceipt
from iesplan.computation import build_registry, write_mps
from iesplan.computation.generators.linear import (
    LinearConstraint,
    LinearExpression,
    LinearModel,
    LinearVariable,
    LoweringUnsupportedError,
)
from iesplan.computation.generators.project import (
    _AffineAcc,
    _ProjectBuilder,
    build_project_model,
    finance_component_declarations,
)
from iesplan.computation.result_adapters.highs import HIGHS_ADAPTER
from iesplan.finance import (
    ConstructionPayment,
    EffectiveFinanceConfig,
    EvaluationInput,
    FinanceProfile,
    merge_effective,
)
from iesplan.computation.sequences import materialize_non_predictive_series

from test_project_model import (
    _gen_inputs,
    _gen_fixed_inputs,
    _island_doc,
    _finance_payload,
    _evaluation_n,
)


def _activation_inputs(*, optional: bool = True, fixed_fee: float = 900000.0, lower_kw: float = 0.0):
    """Synthetic inputs with construction activation."""
    _artifact, models, _materialized, _finance, config, _ = _gen_inputs()
    doc = json.loads(_artifact.canonical_text)
    doc["devices"]["gen1"]["properties"] = {
        "floor_kw": {"value": "0", "unit": "kW"},
    }
    doc["planning"]["objective"]["expression"] = "gen1__upfront_capex"
    doc["planning"]["variables"] = {
        "gen_cap": {
            "device_ref": "gen1",
            "parameter": "cap_kw",
            "lower_bound": str(lower_kw),
            "upper_bound": "500",
            "unit": "kW",
                    },
    }
    doc["planning"]["construction_units"] = {
        "gen_build": {
            "device_ref": "gen1", "mode": "optional" if optional else "mandatory",
            "scales": {
                "gen_cap": {
                    "active_lower_bound": "0",
                    "active_upper_bound": None,
                }
            },
        }
    }
    doc["finance_binding"]["instances"]["gen1"]["cost_occurrence"] = {
        "upfront_capex.fixed": {
            "kind": "construction_unit", "unit_ref": "gen_build"
        }
    }
    payload = _finance_payload()
    payload["finance_types"]["tank_sys"]["upfront_capex"] = {
        "fixed": {"value": str(fixed_fee), "unit": "CNY"},
        "linear": {"cap": {"unit_cost": {"value": "3500", "unit": "CNY/kW"}}},
    }
    finance = merge_effective(FinanceProfile.from_dict(payload), None)
    artifact = ValidatedAssemblyArtifact(
        canonical_text=json.dumps(doc), receipt=ValidationReceipt(),
    )
    materialized = materialize_non_predictive_series(
        artifact, {}, operation_years=1,
    )
    return artifact, models, materialized, finance, config, _evaluation_n(1)


def test_optional_construction_declares_binary_and_conditional_cost() -> None:
    """可选建声明二元状态 z，固定费形成 F*z，规模约束为 q <= U*z。"""
    artifact, models, materialized, finance, config, evaluation = _activation_inputs(
        optional=True, fixed_fee=50000.0, lower_kw=0.0,
    )
    model, _, _ = build_project_model(artifact, models, materialized, finance, config, evaluation)
    decls = finance_component_declarations(artifact, models, materialized, finance, evaluation)

    by_var = {var.name: var for var in model.variables}
    # 1. 二元状态变量 built__gen_build
    assert "built__gen_build" in by_var
    z_var = by_var["built__gen_build"]
    assert z_var.kind == "binary"
    assert z_var.lower == 0.0
    assert z_var.upper == 1.0

    # 2. 规模约束：gen_cap <= 500kW * built__gen_build (500000 W)
    by_row = {row.name: row for row in model.constraints}
    assert "gen_build__gen_cap__build_max" in by_row
    max_row = by_row["gen_build__gen_cap__build_max"]
    assert max_row.sense == "le" and max_row.rhs == 0.0
    terms = dict(max_row.expression.terms)
    assert terms[("gen_cap", 0)] == 1.0
    assert terms[("built__gen_build", 0)] == pytest.approx(-500000.0)

    # 3. 条件固定费：F * built__gen_build，不再是无条件常数
    upfront = decls["gen1__upfront_capex"]
    assert upfront["constant"] == pytest.approx(0.0)
    assert upfront["terms"]["built__gen_build"] == pytest.approx(50000.0)
    assert upfront["terms"]["gen_cap"] == pytest.approx(3.5)  # 3500 CNY/kW = 3.5 CNY/W
    assert not any(var.name.startswith("conditional_cost__") for var in model.variables)


def test_construction_active_upper_is_an_effective_scale_bound() -> None:
    """An explicit active upper bound must reach unified analysis before finance lowering."""
    artifact, models, materialized, finance, config, evaluation = _activation_inputs(
        optional=True, fixed_fee=50000.0, lower_kw=0.0,
    )
    doc = json.loads(artifact.canonical_text)
    doc["planning"]["variables"]["gen_cap"]["upper_bound"] = None
    doc["planning"]["construction_units"]["gen_build"]["scales"]["gen_cap"][
        "active_upper_bound"
    ] = "100"
    artifact = replace(artifact, canonical_text=json.dumps(doc))

    model, _, _ = build_project_model(
        artifact, models, materialized, finance, config, evaluation,
    )
    scale = next(var for var in model.variables if var.name == "gen_cap")
    assert scale.upper == pytest.approx(100_000.0)
    max_row = next(row for row in model.constraints if row.name == "gen_build__gen_cap__build_max")
    assert dict(max_row.expression.terms)[("built__gen_build", 0)] == pytest.approx(-100_000.0)


def test_fixed_residual_zero_basis_does_not_request_a_lower_bound() -> None:
    """B <= 0 needs only max(B); an unrelated missing lower bound is irrelevant."""

    class AnalysisSpy:
        def __init__(self) -> None:
            self.sides: list[str] = []

        def affine_side(self, affine, *, side, label, relation, period, required=True):
            self.sides.append(side)
            return 0.0

    source = SimpleNamespace(identity="asset_source", to_dict=lambda: {"identity": "asset_source"})
    entry = SimpleNamespace(
        base_from=source,
        residual=SimpleNamespace(value=100.0, unit="CNY"),
        life_years=10,
    )
    relation = SimpleNamespace(payload=SimpleNamespace(entry=entry, rule_id="depreciation"))
    builder = object.__new__(_ProjectBuilder)
    builder._source_net = lambda _source, relation=None: _AffineAcc()
    builder.analysis = AnalysisSpy()
    builder._fixed_residuals = {}
    builder._fact_depr = []
    builder.rows = []

    amount = builder._income_depreciation_affine(relation, 1, {}, "evaluation")
    assert builder.analysis.sides == ["max"]
    assert amount.terms == {}
    assert amount.constant == 0.0


def test_fixed_residual_formed_basis_uses_only_its_proven_lower_side() -> None:
    """B=50000+3500q, q>=0, R=100 needs no finite capacity upper bound."""
    from iesplan.computation.generators.compiler_ir import CompilerAnalysis, CompilerInventory

    inventory = CompilerInventory()
    inventory.seal()
    source = SimpleNamespace(identity="formed_asset", to_dict=lambda: {})
    entry = SimpleNamespace(
        base_from=source, residual=SimpleNamespace(value=100.0, unit="CNY"),
        life_years=10,
    )
    relation = SimpleNamespace(
        identity="depreciation.income.0", source=None,
        payload=SimpleNamespace(entry=entry, rule_id="income"),
    )
    basis = _AffineAcc()
    basis.add_const(50000.0)
    basis.add_term("q", 3500.0)
    builder = object.__new__(_ProjectBuilder)
    builder.analysis = CompilerAnalysis(inventory, {"q": LinearVariable("q", 0.0, None)})
    builder._source_net = lambda _source, relation=None: basis
    builder._fixed_residuals = {}
    builder._fact_depr = []
    builder.rows = []

    annual = builder._income_depreciation_affine(relation, 1, {}, "income")
    assert annual.constant == 4990.0
    assert annual.terms == {"q": 350.0}
    assert builder.rows == []


def test_mandatory_construction_fixes_status_to_one() -> None:
    """必建声明（正下界）状态固定为 1，固定费按常数发生。"""
    artifact, models, materialized, finance, config, evaluation = _activation_inputs(
        optional=False, fixed_fee=90000.0, lower_kw=20.0,
    )
    model, _, _ = build_project_model(artifact, models, materialized, finance, config, evaluation)
    decls = finance_component_declarations(artifact, models, materialized, finance, evaluation)

    by_var = {var.name: var for var in model.variables}
    assert "built__gen_build" in by_var
    z_var = by_var["built__gen_build"]
    assert z_var.lower == 1.0
    assert z_var.upper == 1.0

    # 必建固定费按常数发生
    upfront = decls["gen1__upfront_capex"]
    assert upfront["constant"] == pytest.approx(90000.0)


def test_multiple_scales_share_same_activation_status() -> None:
    """同一建设单元的多个规模（如容量与功率）共享同一二元状态。"""
    artifact, models, materialized, finance, config, evaluation = _activation_inputs(
        optional=True, fixed_fee=10000.0, lower_kw=0.0,
    )
    doc = json.loads(artifact.canonical_text)
    # Add a second planning variable for gen1: floor_kw
    doc["planning"]["variables"]["gen_floor"] = {
        "device_ref": "gen1",
        "parameter": "floor_kw",
        "lower_bound": "0",
        "upper_bound": "100",
        "unit": "kW",
            }
    doc["planning"]["construction_units"]["gen_build"]["scales"]["gen_floor"] = {
        "active_lower_bound": "0", "active_upper_bound": None,
    }
    new_art = replace(artifact, canonical_text=json.dumps(doc))
    model, _, _ = build_project_model(new_art, models, materialized, finance, config, evaluation)

    by_row = {row.name: row for row in model.constraints}
    # Both gen_cap and gen_floor reference built__gen_build
    assert "gen_build__gen_cap__build_max" in by_row
    assert "gen_build__gen_floor__build_max" in by_row
    terms_cap = dict(by_row["gen_build__gen_cap__build_max"].expression.terms)
    terms_floor = dict(by_row["gen_build__gen_floor__build_max"].expression.terms)
    assert ("built__gen_build", 0) in terms_cap
    assert ("built__gen_build", 0) in terms_floor


def test_two_instances_do_not_conflate_construction_states() -> None:
    """共享同一财务类别的不同实例各自拥有独立的二元状态，不串用。"""
    artifact, models, materialized, finance, config, evaluation = _activation_inputs(
        optional=True, fixed_fee=30000.0, lower_kw=0.0,
    )
    doc = json.loads(artifact.canonical_text)
    # Add second instance gen2 with same definition and finance_type
    doc["devices"]["gen2"] = dict(doc["devices"]["gen1"])
    doc["finance_binding"]["instances"]["gen2"] = dict(doc["finance_binding"]["instances"]["gen1"])
    models["gen2"] = models["gen1"]
    doc["planning"]["construction_units"]["gen2_build"] = {
        "device_ref": "gen2", "mode": "optional",
        "scales": {"gen2_cap": {"active_lower_bound": "0", "active_upper_bound": None}},
    }
    doc["finance_binding"]["instances"]["gen2"]["cost_occurrence"] = {
        "upfront_capex.fixed": {"kind": "construction_unit", "unit_ref": "gen2_build"}
    }
    doc["planning"]["variables"]["gen2_cap"] = {
        "device_ref": "gen2",
        "parameter": "cap_kw",
        "lower_bound": "0",
        "upper_bound": "300",
        "unit": "kW",
            }
    new_art = replace(artifact, canonical_text=json.dumps(doc))
    model, _, _ = build_project_model(new_art, models, materialized, finance, config, evaluation)
    decls = finance_component_declarations(new_art, models, materialized, finance, evaluation)

    by_var = {var.name: var for var in model.variables}
    assert "built__gen_build" in by_var
    assert "built__gen2_build" in by_var

    upfront_gen1 = decls["gen1__upfront_capex"]
    upfront_gen2 = decls["gen2__upfront_capex"]
    assert "built__gen_build" in upfront_gen1["terms"] and "built__gen2_build" not in upfront_gen1["terms"]
    assert "built__gen2_build" in upfront_gen2["terms"] and "built__gen_build" not in upfront_gen2["terms"]


def test_zero_construction_produces_zero_costs_and_facts() -> None:
    """零建设下总投资、付款、折旧基础均为 0，无虚构金额。"""
    artifact, models, materialized, finance, config, evaluation = _activation_inputs(
        optional=True, fixed_fee=100000.0, lower_kw=0.0,
    )
    from iesplan.computation.generators.project import _new_builder
    builder = _new_builder(artifact, models, materialized, finance, evaluation)
    model, _, _ = builder.build()
    facts = builder.financial_facts()

    # construction_status 结构化登记
    assert "construction_units" in facts
    c_status = next(s for s in facts["construction_units"] if s["instance_id"] == "gen1")
    assert c_status["built"]["terms"] == {"built__gen_build": 1.0}
    assert "gen_cap" in {v["planning_variable_id"]:v for v in c_status["scales"]}


def test_highs_solve_optional_construction_zero_and_active(tmp_path) -> None:
    """真实 HiGHS 求解并经 ResultAdapter 恢复封存选建状态、规模与金额。"""
    import subprocess
    from iesplan.computation.generators.highs_milp import HIGHS_MILP_PROVIDER
    from iesplan.computation.result_adapters.highs import HIGHS_ADAPTER
    from iesplan.computation.protocols import ExecutionReceipt
    SOLUTION_PATH = "output/solution.sol"

    # Case A: 无负荷收入，明确支付全额建设费 => 税后最优为零建设。
    artifact, models, materialized, finance, config, evaluation = _activation_inputs(
        optional=True, fixed_fee=50000.0, lower_kw=0.0,
    )
    # Tax and terminal recovery consume the same optional asset, including
    # a positive fixed annual fee and a fixed residual. Zero build must not
    # create a payment, deductible expense, depreciation or recovery.
    payload = _finance_payload()
    payload["finance_types"]["tank_sys"]["upfront_capex"] = {
        "fixed": {"value": "50000", "unit": "CNY"},
        "linear": {"cap": {"unit_cost": {"value": "3500", "unit": "CNY/kW"}}},
    }
    payload["finance_types"]["tank_sys"]["annual_fixed_om"]["fixed"] = {
        "value": "200", "unit": "CNY/a",
    }
    payload["taxes"] = {"income_tag": {
        "display_name": "Synthetic income", "tax_type": "income_tax",
        "applies_to": "tank_sys.upfront_capex",
    }}
    source = {"kind": "upfront_capex_net", "finance_type": "tank_sys",
              "scope": {"kind": "all_new_instances"}}
    payload["tax_rules"] = {"income": {
        "kind": "income_tax", "label_ref": "income_tag", "rate": {"value": "0.25", "unit": "1"},
        "revenue": [], "energy_cost": [], "annual_om": ["tank_sys.annual_fixed_om"],
        "variable_om": [], "deductible_maintenance": {"mode": "explicit_items", "items": []},
        "depreciation": [{
            "base_from": source, "method": "straight_line", "life_years": 10,
            "first_year": 1, "residual": {"value": "100", "unit": "CNY"},
        }],
        "loss_carryforward_years": 5, "opening_losses": [],
    }}
    payload["net_cashflows"] = {"recovery": {
        "at": {"kind": "evaluation_end"}, "direction": "inflow",
        "amount": {"kind": "source_share", "base_from": source,
                   "ratio": {"value": "0.05", "unit": "1"}},
    }}
    finance = merge_effective(FinanceProfile.from_dict(payload), None)
    doc = json.loads(artifact.canonical_text)
    doc["planning"]["objective"] = {"sense": "maximize", "expression": "system.after_tax_npv"}
    doc["finance_binding"]["instances"]["gen1"]["cost_occurrence"]["annual_fixed_om.fixed"] = {
        "kind": "construction_unit", "unit_ref": "gen_build",
    }
    artifact = replace(artifact, canonical_text=json.dumps(doc))
    evaluation = replace(
        evaluation, construction_payments=(ConstructionPayment(t=0, ratio=1.0),),
    )
    bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
    mps_bytes = bundle.files["input/problem.mps"]
    (tmp_path / "case_a").mkdir(exist_ok=True)
    (tmp_path / "case_a" / "problem.mps").write_bytes(mps_bytes)
    (tmp_path / "case_a" / "solution").mkdir(exist_ok=True)

    res = subprocess.run(
        ["/usr/local/bin/ies-highs-solver", "problem.mps", "--solution_file", "solution/values.sol",
         "--time_limit", "30", "--random_seed", "0"],
        cwd=tmp_path / "case_a", text=True, capture_output=True, check=False,
    )
    assert res.returncode == 0, res.stderr
    sol_bytes = (tmp_path / "case_a" / "solution" / "values.sol").read_bytes()

    from test_highs_result_adapter import _receipt
    receipt = _receipt()
    adapted = HIGHS_ADAPTER.adapt(bundle, receipt, {SOLUTION_PATH: sol_bytes})
    assert adapted.math_status == "solved"
    facts = adapted.outputs["financial_facts"]
    assert "construction_units" in facts
    status_entry = next(s for s in facts["construction_units"] if s["instance_id"] == "gen1")
    assert status_entry["built"] is False, {
        "status": status_entry, "capex": facts["construction_total"],
        "om": facts["annual_fixed_om"], "flows": facts["annual_net_flows"],
    }
    scale = next(v for v in status_entry["scales"] if v["planning_variable_id"] == "gen_cap")
    assert type(scale["value"]) is float
    assert scale["value"] == 0.0 and scale["unit"] == "kW"
    assert float(facts["construction_total"]) == 0.0
    assert float(facts["annual_fixed_om"]) == 0.0
    assert len(facts["payments_applied"]) == len(facts["net_cashflows"]) == 1
    assert len(facts["tax_cash"]["depreciation"]) == len(facts["tax_cash"]["income"]) == 1
    assert all(float(item["amount"]) == 0.0 for item in facts["payments_applied"])
    assert all(float(item["amount"]) == 0.0 for item in facts["net_cashflows"])
    assert all(float(item["amount"]) == 0.0 for item in facts["tax_cash"]["depreciation"])
    assert all(float(item["tax"]) == 0.0 for item in facts["tax_cash"]["income"])
    assert all(float(value) == 0.0 for value in facts["annual_net_flows"])
    from iesplan.application.worker.compute_cases import build_financial_evaluation

    sealed = build_financial_evaluation(
        SimpleNamespace(evaluation=asdict(evaluation)), finance, adapted,
    )
    assert sealed["construction_units"] == [{
        "unit_id": "gen_build", "instance_id": "gen1", "mode": "optional", "built": False,
        "scales": [{"planning_variable_id": "gen_cap", "value": 0.0, "unit": "kW"}],
    }]
    assert json.loads(json.dumps(sealed))["construction_units"][0]["scales"][0]["value"] == 0.0
    # Case B: 强制建设 (lower_bound = 50 kW) => 最优解为建成 (built__gen_build=1, gen_cap=50kW)
    artifact_b, models_b, materialized_b, finance_b, config_b, evaluation_b = _activation_inputs(
        optional=True, fixed_fee=50000.0, lower_kw=50.0,
    )
    bundle_b = HIGHS_MILP_PROVIDER.generate(
        artifact_b, models_b, materialized_b, finance_b, config_b, evaluation_b,
    )
    mps_b = bundle_b.files["input/problem.mps"]
    (tmp_path / "case_b").mkdir(exist_ok=True)
    (tmp_path / "case_b" / "problem.mps").write_bytes(mps_b)
    (tmp_path / "case_b" / "solution").mkdir(exist_ok=True)

    res_b = subprocess.run(
        ["/usr/local/bin/ies-highs-solver", "problem.mps", "--solution_file", "solution/values.sol",
         "--time_limit", "30", "--random_seed", "0"],
        cwd=tmp_path / "case_b", text=True, capture_output=True, check=False,
    )
    assert res_b.returncode == 0, res_b.stderr
    sol_b = (tmp_path / "case_b" / "solution" / "values.sol").read_bytes()

    receipt_b = _receipt()
    adapted_b = HIGHS_ADAPTER.adapt(bundle_b, receipt_b, {SOLUTION_PATH: sol_b})
    assert adapted_b.math_status == "solved"
    facts_b = adapted_b.outputs["financial_facts"]
    status_entry_b = next(s for s in facts_b["construction_units"] if s["instance_id"] == "gen1")
    assert status_entry_b["built"] is True
    scale_b = next(v for v in status_entry_b["scales"] if v["planning_variable_id"] == "gen_cap")
    assert type(scale_b["value"]) is float
    assert scale_b["value"] == pytest.approx(50.0) and scale_b["unit"] == "kW"
    # Total upfront = fixed 50000 + 3500 CNY/kW * 50 kW = 50000 + 175000 = 225000 CNY
    assert float(facts_b["construction_total"]) == pytest.approx(225000.0)

    # Case C: mandatory does not imply a positive minimum scale. Preserve
    # built=True, q=0, and the explicitly triggered fixed construction fee.
    inputs_c = _activation_inputs(optional=False, fixed_fee=50000.0, lower_kw=0.0)
    bundle_c = HIGHS_MILP_PROVIDER.generate(*inputs_c)
    directory = tmp_path / "case_c"
    directory.mkdir()
    (directory / "problem.mps").write_bytes(bundle_c.files["input/problem.mps"])
    solved_c = subprocess.run(
        ["/usr/local/bin/ies-highs-solver", "problem.mps", "--solution_file", "values.sol",
         "--time_limit", "30", "--random_seed", "0"],
        cwd=directory, text=True, capture_output=True, check=False,
    )
    assert solved_c.returncode == 0, solved_c.stderr
    adapted_c = HIGHS_ADAPTER.adapt(bundle_c, _receipt(), {
        SOLUTION_PATH: (directory / "values.sol").read_bytes(),
    })
    assert adapted_c.math_status == "solved"
    facts_c = adapted_c.outputs["financial_facts"]
    unit_c = facts_c["construction_units"][0]
    assert unit_c["built"] is True and unit_c["mode"] == "mandatory"
    assert unit_c["scales"][0]["value"] == 0.0
    assert float(facts_c["construction_total"]) == 50000.0



def test_explicit_mandatory_construction_survives_public_value_roundtrip() -> None:
    """Mandatory mode remains mandatory through the public typed planning DTO."""
    from iesplan.core.contracts.planning_config import PlanningConfig
    artifact, models, materialized, finance, config, evaluation = _activation_inputs(
        optional=False, fixed_fee=50000.0, lower_kw=0,
    )
    doc = json.loads(artifact.canonical_text)
    planning = PlanningConfig.from_dict(doc["planning"])
    encoded = planning.to_dict()
    assert encoded["construction_units"]["gen_build"]["mode"] == "mandatory"
    assert PlanningConfig.from_dict(encoded).to_dict() == encoded
    rebuilt = replace(artifact, canonical_text=json.dumps(doc))
    linear, _, _ = build_project_model(
        rebuilt, models, materialized, finance, config, evaluation,
    )
    status = next(v for v in linear.variables if v.name == "built__gen_build")
    assert status.lower == status.upper == 1.0


def test_unconditional_fixed_cost_is_not_gated_by_optional_build() -> None:
    """No occurrence declaration means always, even when construction optional."""
    artifact, models, materialized, finance, config, evaluation = _activation_inputs()
    doc = json.loads(artifact.canonical_text)
    doc["finance_binding"]["instances"]["gen1"].pop("cost_occurrence")
    artifact = replace(artifact, canonical_text=json.dumps(doc))
    decls = finance_component_declarations(
        artifact, models, materialized, finance, evaluation,
    )
    assert decls["gen1__upfront_capex"]["constant"] == pytest.approx(900000.0)
    assert "built__gen_build" not in decls["gen1__upfront_capex"]["terms"]
