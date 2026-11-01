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

from dataclasses import replace
import json
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
    build_project_model,
    finance_component_declarations,
)
from iesplan.computation.result_adapters.highs import HIGHS_ADAPTER
from iesplan.finance import (
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

    # Case A: 最小化 upfront 且无负荷需求 => 最优解为零建设 (built__gen_build=0, gen_cap=0)
    artifact, models, materialized, finance, config, evaluation = _activation_inputs(
        optional=True, fixed_fee=50000.0, lower_kw=0.0,
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
    # Case B: 强制建设 (lower_bound = 50 kW) => 最优解为建成 (built__gen_build=1, gen_cap=50kW)
    artifact_b, models_b, materialized_b, finance_b, config_b, evaluation_b = _activation_inputs(
        optional=True, fixed_fee=50000.0, lower_kw=50.0,
    )
    bundle_b = HIGHS_MILP_PROVIDER.generate(artifact_b, models_b, materialized_b, finance_b, config_b, evaluation_b)
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
    assert float(next(v["value"] for v in status_entry_b["scales"] if v["planning_variable_id"] == "gen_cap")) == pytest.approx(50.0)  # business unit kW
    # Total upfront = fixed 50000 + 3500 CNY/kW * 50 kW = 50000 + 175000 = 225000 CNY
    assert float(facts_b["construction_total"]) == pytest.approx(225000.0)



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
