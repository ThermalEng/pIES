"""C1b 显式全周期 NPV 目标：闸门识别、现金流仿射、真实求解对照。

只在 Docker 内运行其中真实求解部分（需镜像内 HiGHS 二进制）。
全部金额为合成手算数，不读外部实例。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from iesplan.assembly import parse_assembly_doc, validate_assembly_doc
from iesplan.computation import (
    HIGHS_ADAPTER_PATH,
    HIGHS_MILP_PROVIDER,
    CalculationConfig,
    build_project_model,
    materialize_non_predictive_series,
)
from iesplan.computation.generators.highs_milp import GENERATOR_REF
from iesplan.core.yamlmini import load as yaml_load
from iesplan.devices import init_registry
from iesplan.devices.parser2 import parse_device_model_v2
from iesplan.finance import (
    ConstructionPayment,
    EvaluationInput,
    FinanceProfile,
    MaintenanceExpense,
    merge_effective,
)
from iesplan.modeling import build_math_contribution

HERE = Path(__file__).resolve().parent
BACKEND_ROOT = HERE.parent
SAMPLES = BACKEND_ROOT / "iesplan" / "assembly" / "samples"

NPV_MARKER = "system.after_tax_npv"

#: 手算口径：K=1、N=2、E=3、r=0.08，两笔建设付款，同年两笔维护。
RATE = 0.08
CAP_KW = 60.0
UPFRONT_RATE = 1000.0
ANNUAL_RATE = 35.0
VAROM_RATE = 0.01
PURCHASE_PRICE = 0.7
EXPORT_PRICE = -0.05
HOURS = 8760.0
LOAD_KW = 7.0
FLOW_KW = 5.0
EFFICIENCY = 0.9

CONSTRUCTION = CAP_KW * UPFRONT_RATE
ENERGY_YEAR = LOAD_KW * HOURS * PURCHASE_PRICE
VAROM_YEAR = FLOW_KW * HOURS * VAROM_RATE
VAROM2_YEAR = 0.02 * (LOAD_KW / EFFICIENCY) * HOURS
FIXED_YEAR = CAP_KW * ANNUAL_RATE
EXPORT_YEAR = -EXPORT_PRICE * (LOAD_KW / EFFICIENCY) * HOURS


def _hand_npv() -> float:
    """手算 NPV（评价年口径，与实现公式独立书写；解处求值对照用）。"""
    cf0 = -0.6 * CONSTRUCTION
    cf1 = -0.4 * CONSTRUCTION
    base = ENERGY_YEAR + VAROM_YEAR + VAROM2_YEAR + FIXED_YEAR + EXPORT_YEAR
    cf2 = -base - (1200.50 + 300.0)
    cf3 = -base - 500.0
    return cf0 + cf1 / (1 + RATE) + cf2 / (1 + RATE) ** 2 + cf3 / (1 + RATE) ** 3


def _hand_const() -> float:
    """手算目标常数部分（建设/固定/可变预定义/维护；能量与倒送为变量项）。"""
    return (
        -0.6 * CONSTRUCTION
        - 0.4 * CONSTRUCTION / (1 + RATE)
        - FIXED_YEAR / (1 + RATE) ** 2
        - FIXED_YEAR / (1 + RATE) ** 3
        - VAROM_YEAR / (1 + RATE) ** 2
        - VAROM_YEAR / (1 + RATE) ** 3
        - 1500.50 / (1 + RATE) ** 2
        - 500.0 / (1 + RATE) ** 3
    )


def _finance_payload() -> dict:
    return {
        "schema": "ies.finance-profile",
        "schema_version": "6.0.0",
        "profile": {
            "id": "npv-demo",
            "region": "CN-North",
            "currency": "CNY",
            "base_year": 2025,
            "price_basis": "tax_inclusive",
            "cost_method": "fixed_plus_linear",
        },
        "demand_charges": {},
        "taxes": {},
        "credit_pools": {},
        "tax_rules": {},
        "net_cashflows": {},
        "finance_types": {
            "sys1": {
                "upfront_capex": {"linear": {"cap": {"unit_cost": {"value": "1000", "unit": "CNY/kW"}}}},
                "annual_fixed_om": {"linear": {"cap": {"unit_cost": {"value": "35", "unit": "CNY/kW/a"}}}},
                "period_variable_om": {
                    "linear": {"thr": {"unit_cost": {"value": "0.01", "unit": "CNY/kWh"}}}
                },
            },
            "sys_var": {
                "period_variable_om": {"linear": {"e": {"unit_cost": {"value": "0.02", "unit": "CNY/kWh"}}}},
            },
        },
        "energy_prices": {
            "heat_import": {
                "carrier": "heat",
                "direction": "purchase",
                "kind": "constant",
                "value": {"value": "0.7", "unit": "CNY/kWh"},
            },
            "export_neg": {
                "carrier": "electricity",
                "direction": "sale",
                "kind": "constant",
                "value": {"value": "-0.05", "unit": "CNY/kWh"},
            },
        },
    }


HEATER_YAML = """
schema: ies.device-model
schema_version: "2.2.0"
device: {id: acme.device.heater, names: {zh-CN: 加热器, en-US: Heater}}
properties:
  efficiency: {value: 0.9, unit: "1", valid_range: {minimum: 0, maximum: 1}}
interfaces:
  electricity_in: {type: in, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
  heat_out: {type: out, carrier: heat, unit: kW, valid_range: {minimum: 0, maximum: null}}
equations:
  variables: {}
  relations:
    - id: conversion
      expression: "heat_out[t] = electricity_in[t] * efficiency"
"""

TANK_YAML = """
schema: ies.device-model
schema_version: "2.2.0"
device: {id: acme.device.tank, names: {zh-CN: 罐, en-US: Tank}}
properties:
  soc0: {value: 0.5, unit: "1", valid_range: {minimum: 0, maximum: 1}}
  rated_kw: {value: 60, unit: kW, valid_range: {minimum: 0, maximum: 1000}}
  step_c: {value: 1, unit: "1", valid_range: {minimum: 0, maximum: 10}}
interfaces:
  flow:
    type: predefined
    carrier: heat
    unit: kW
    valid_range: {minimum: 0, maximum: null}
equations:
  variables:
    soc: {unit: "1", valid_range: {minimum: 0, maximum: 1}, initial: {property_ref: soc0}}
  relations:
    - id: soc_step
      expression: "soc[t] = soc[t-1] + flow[t] * step_c"
"""

LOAD_YAML = """
schema: ies.device-model
schema_version: "2.2.0"
device: {id: acme.device.load, names: {zh-CN: 负荷, en-US: Load}}
properties: {}
interfaces:
  heat_in: {type: in, carrier: heat, unit: kW, valid_range: {minimum: 0, maximum: null}}
  heat_demand:
    type: predefined
    carrier: heat
    unit: kW
    valid_range: {minimum: 0, maximum: null}
equations:
  variables: {}
  relations:
    - id: serve
      expression: "heat_in[t] = heat_demand[t]"
"""

GRID_YAML = """
schema: ies.device-model
schema_version: "2.2.0"
device: {id: acme.device.grid, names: {zh-CN: 电网, en-US: Grid}}
properties: {}
interfaces:
  supply:
    type: out
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: null}
equations:
  variables: {}
  relations: []
"""


def _contribution(yaml_text: str):
    result = parse_device_model_v2(yaml_load(yaml_text), file="test.yaml")
    assert result.ok, result.diagnostics
    built = build_math_contribution(result.document)
    assert built.ok, built.diagnostics
    return built.contribution


def _doc(
    *,
    sense: str = "maximize",
    expression: str = NPV_MARKER,
    variables: dict | None = None,
    start_year: int = 2026,
):
    return {
        "schema": "ies.assembly",
        "schema_version": "4.0.0",
        "assembly": {"id": "npv-mini", "name": "NPV 手算装配"},
        "project_baseline": {
            "resolution": "1h",
            "start_year": start_year,
            "scenario_mode": "single",
        },
        "devices": {
            "heater1": {
                "definition": {"id": "acme.device.heater"},
                "asset_origin": "existing",
                "properties": {},
                "predefined_interfaces": {},
            },
            "tank1": {
                "definition": {"id": "acme.device.tank"},
                "asset_origin": "new",
                "properties": {
                    "rated_kw": {"value": 60, "unit": "kW"},
                    # soc 动态与本目标无关：步进系数置零使 soc 恒定，
                    # 模型可解；可变 OM 仍按流量接口实际归集。
                    "step_c": {"value": 0, "unit": "1"},
                },
                "predefined_interfaces": {"flow": {"mode": "constant", "value": 5}},
            },
            "load1": {
                "definition": {"id": "acme.device.load"},
                "asset_origin": "existing",
                "properties": {},
                "predefined_interfaces": {"heat_demand": {"mode": "constant", "value": 7}},
            },
            "grid1": {
                "definition": {"id": "acme.device.grid"},
                "asset_origin": "existing",
                "properties": {},
                "predefined_interfaces": {},
            },
        },
        "connections": {
            "link1": {"from": "heater1.heat_out", "to": "load1.heat_in"},
            "link2": {"from": "grid1.supply", "to": "heater1.electricity_in"},
        },
        "finance": {
            "ref": {"kind": "object", "object_id": "obj-finance", "media_type": "application/yaml"},
            "profile_id": "npv-demo",
        },
        "finance_binding": {
            "instances": {
                "heater1": {
                    "type": "costed",
                    "finance_type": "sys_var",
                    "drivers": {
                        "e": {"interface": "electricity_in", "aggregation": "integrate_positive"},
                    },
                },
                "tank1": {
                    "type": "costed",
                    "finance_type": "sys1",
                    "drivers": {
                        "cap": {"property": "rated_kw"},
                        "thr": {"interface": "flow", "aggregation": "integrate_positive"},
                    },
                },
                "load1": {"type": "none"},
                "grid1": {"type": "none"},
            }
        },
        "tariff_bindings": {
            # 明确选定两个计量端口；价格购售符号不从技术 in/out 推导。
            "import_meter": {
                "price": "heat_import",
                "instance": "heater1",
                "interface": "heat_out",
                "aggregation": "integrate_positive",
            },
            "export_meter": {
                "price": "export_neg",
                "instance": "heater1",
                "interface": "electricity_in",
                "aggregation": "integrate_positive",
            },
        },
        "demand_bindings": {},
        "planning": {
            "objective": {"sense": sense, "expression": expression},
            # 产品要求至少一个规划变量：销钉 soc 初值（仅状态初值引用，
            # 不进入方程相乘），不影响财务与物理。
            "variables": (
                {
                    "soc_pin": {
                        "device_ref": "tank1",
                        "parameter": "soc0",
                        "lower_bound": "0.5",
                        "upper_bound": "0.5",
                        "unit": "1",
                    },
                }
                if variables is None
                else variables
            ),
            "constraints": {},
        },
        "outputs": {"series": [], "metrics": []},
        "extensions": {},
    }


def _evaluation(*, rate: float = RATE, payments=((0, "0.6"), (1, "0.4"))):
    return EvaluationInput(
        evaluation_years=3,
        construction_period=1,
        operation_years=2,
        discount_rate=rate,
        construction_payments=tuple(ConstructionPayment(t=t, ratio=ratio) for t, ratio in payments),
        maintenance_expenses=(
            MaintenanceExpense(year=2, amount="1200.50", currency="CNY"),
            MaintenanceExpense(year=2, amount="300", currency="CNY"),
            MaintenanceExpense(year=3, amount="500", currency="CNY"),
        ),
    )


def _device_documents():
    """合成设备显式文档（公开装配入口的设备参数，公开建模入口已校验）。"""
    docs = {}
    for device_id, yaml_text in (
        ("acme.device.heater", HEATER_YAML),
        ("acme.device.tank", TANK_YAML),
        ("acme.device.load", LOAD_YAML),
        ("acme.device.grid", GRID_YAML),
        ("acme.device.gen", GEN_YAML),
    ):
        result = parse_device_model_v2(yaml_load(yaml_text), file="test.yaml")
        assert result.ok, result.diagnostics
        docs[device_id] = result.document
    return docs


def _issued_artifact(doc: dict, finance, device_ids: tuple[str, ...]):
    """公开装配入口签发产物（不断言之外的手工装配不再使用）。"""
    init_registry()
    documents = _device_documents()
    result = validate_assembly_doc(
        doc,
        finance_snapshots={"obj-finance": finance},
        device_documents={key: documents[key] for key in device_ids},
    )
    assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]
    return result.artifact


def _inputs(**overrides):
    doc = _doc(
        sense=overrides.get("sense", "maximize"),
        expression=overrides.get("expression", NPV_MARKER),
        variables=overrides.get("variables"),
    )
    models = {
        "heater1": _contribution(HEATER_YAML),
        "tank1": _contribution(TANK_YAML),
        "load1": _contribution(LOAD_YAML),
        "grid1": _contribution(GRID_YAML),
    }
    finance = merge_effective(FinanceProfile.from_dict(_finance_payload()), None)
    artifact = _issued_artifact(
        doc,
        finance,
        ("acme.device.heater", "acme.device.tank", "acme.device.load", "acme.device.grid"),
    )
    materialized = materialize_non_predictive_series(artifact, {}, operation_years=2)
    config = CalculationConfig(
        mode="planning",
        generator=GENERATOR_REF,
        solver="ies.solver.highs@1.7.2",
        precision="medium",
        time_limit_seconds=600,
        outputs=["solution"],
    )
    evaluation = _evaluation(
        rate=overrides.get("rate", RATE),
        payments=overrides.get("payments", ((0, "0.6"), (1, "0.4"))),
    )
    return artifact, models, materialized, finance, config, evaluation


def _coeffs(expression) -> dict:
    return {name: coeff for (name, _), coeff in expression.terms}


# ---------------------------------------------------------------------------
# 装配闸门（公开入口 validate_assembly_doc + 园区样例）
# ---------------------------------------------------------------------------


def _campus_text() -> str:
    return (SAMPLES / "valid" / "campus.assembly.yaml").read_text(encoding="utf-8")


def _validate_campus(text: str):
    init_registry()
    parsed = parse_assembly_doc(text)
    assert parsed.doc is not None, [d.to_dict() for d in parsed.diagnostics]
    return validate_assembly_doc(parsed.doc, package_dir=SAMPLES)


def _strip_year_offset(text: str) -> str:
    return text.replace("      year_offset: 0\n", "").replace("      year_offset: 1\n", "")


_CAMPUS_OBJECTIVE = (
    "    expression: pv1__upfront_capex + 1 year * pv1__annual_fixed_om"
    " + pv1__period_variable_om + import_meter__period_energy_purchase"
)


def _campus_with_objective(sense: str, expression: str) -> str:
    text = _strip_year_offset(_campus_text())
    assert "    sense: minimize\n" in text
    assert _CAMPUS_OBJECTIVE in text
    text = text.replace("    sense: minimize\n", f"    sense: {sense}\n", 1)
    return text.replace(_CAMPUS_OBJECTIVE, f"    expression: {expression}", 1)


def test_gate_accepts_npv_marker_with_maximize() -> None:
    """公开装配入口签发合法完整目标（方向 maximize）。"""
    result = _validate_campus(_campus_with_objective("maximize", NPV_MARKER))
    assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]


def test_gate_rejects_npv_marker_with_minimize() -> None:
    """保留目标方向错误即阻断，不静默接受。"""
    result = _validate_campus(_campus_with_objective("minimize", NPV_MARKER))
    assert result.artifact is None
    assert any(
        d.code == "ASM-PLAN-003" and d.params.get("reason") == "objective_invalid" and d.blocking
        for d in result.diagnostics
    )


def test_gate_rejects_dotted_marker_variant() -> None:
    """伪点分属性不是完整目标，不放行。"""
    result = _validate_campus(_campus_with_objective("maximize", NPV_MARKER + ".x"))
    assert result.artifact is None
    assert any(d.blocking for d in result.diagnostics)


def test_gate_rejects_marker_in_constraint() -> None:
    """保留标记进入约束即阻断。"""
    text = _campus_with_objective("maximize", NPV_MARKER)
    old = '      expression: "import_meter__period_energy_purchase <= 900000 CNY"'
    assert old in text
    result = _validate_campus(text.replace(old, f'      expression: "{NPV_MARKER} <= 1"', 1))
    assert result.artifact is None
    assert any(d.blocking for d in result.diagnostics)


def test_gate_ordinary_objective_unchanged() -> None:
    """普通目标语义不变：园区样例原目标仍签发。"""
    result = _validate_campus(_strip_year_offset(_campus_text()))
    assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]


# ---------------------------------------------------------------------------
# 生成器目标仿射（手算对照，无需求解）
# ---------------------------------------------------------------------------


def test_npv_objective_affine_matches_hand_cashflows() -> None:
    """NPV 目标仿射式与手算现金流一致：常数与逐年系数。"""
    artifact, models, materialized, finance, config, evaluation = _inputs()
    model, _, objective_unit = build_project_model(
        artifact, models, materialized, finance, config, evaluation
    )
    assert model.sense == "maximize"
    assert objective_unit == "CNY"
    coeffs = _coeffs(model.objective)
    df2 = (1 + RATE) ** 2
    df3 = (1 + RATE) ** 3
    # 购电（计量在 heater 出水口，与负荷需求同值）：-0.7/1000 按年折现
    # （两年系数不同，证明未均摊）。
    assert coeffs["heater1__heat_out__t0"] == pytest.approx(-0.0007 / df2, rel=1e-9)
    assert coeffs["heater1__heat_out__t8759"] == pytest.approx(-0.0007 / df2, rel=1e-9)
    assert coeffs["heater1__heat_out__t8760"] == pytest.approx(-0.0007 / df3, rel=1e-9)
    assert coeffs["heater1__heat_out__t17519"] == pytest.approx(-0.0007 / df3, rel=1e-9)
    # 可变 OM：预定义流量为常数（无变量项），年金额进常数项（已对手算）。
    assert not any(name.startswith("tank1__flow__t") for name in coeffs)
    # 同列两项合并：可变 OM -0.02/1000，负价售电记账为 +0.05 系数（成本向），
    # NPV 取能源净收益（相反数），合计 -0.07/1000 按年折现。
    assert coeffs["heater1__electricity_in__t0"] == pytest.approx(-0.00007 / df2, rel=1e-9)
    assert coeffs["heater1__electricity_in__t8760"] == pytest.approx(-0.00007 / df3, rel=1e-9)
    # 负价售电：符号反转为正分量（与变量可变 OM 同列合并，见下）。
    # 常数项只含常数现金流（建设/固定/预定义可变/维护）；能量与倒送
    # 为变量项，在解处求值（见真实求解对照），此处不断言。
    assert model.objective.constant == pytest.approx(_hand_const(), rel=1e-9)


def test_npv_zero_discount_equals_undiscounted_sum() -> None:
    """r=0 时折现因子全为 1，目标退化为无折现合计。"""
    artifact, models, materialized, finance, config, evaluation = _inputs(rate=0.0)
    model, _, _ = build_project_model(artifact, models, materialized, finance, config, evaluation)
    coeffs = _coeffs(model.objective)
    assert coeffs["heater1__heat_out__t0"] == pytest.approx(-0.0007, rel=1e-9)
    assert coeffs["heater1__heat_out__t8760"] == pytest.approx(-0.0007, rel=1e-9)
    expected = -CONSTRUCTION - 2 * FIXED_YEAR - 2 * VAROM_YEAR - 2000.50
    assert model.objective.constant == pytest.approx(expected, rel=1e-9)


def test_npv_payment_timing_moves_value() -> None:
    """付款时点改变，目标值按手算变化（建设款后移一年更值钱）。"""
    late = _inputs(payments=((1, "1.0"),))
    model, _, _ = build_project_model(*late)
    expected = (
        -CONSTRUCTION / (1 + RATE)
        - FIXED_YEAR / (1 + RATE) ** 2
        - FIXED_YEAR / (1 + RATE) ** 3
        - VAROM_YEAR / (1 + RATE) ** 2
        - VAROM_YEAR / (1 + RATE) ** 3
        - 1500.50 / (1 + RATE) ** 2
        - 500.0 / (1 + RATE) ** 3
    )
    assert model.objective.constant == pytest.approx(expected, rel=1e-9)
    early = _inputs()
    early_model, _, _ = build_project_model(*early)
    assert model.objective.constant > early_model.objective.constant


def test_generate_rejects_unsupported_tax_relations() -> None:
    """未知税则类型在唯一建模能力边界明确拒绝，不按税前生成。"""
    from iesplan.computation.generators.linear import LoweringUnsupportedError

    artifact, models, materialized, finance, config, evaluation = _inputs()
    taxed = replace(finance, tax_rules={"rule1": object()})
    with pytest.raises(LoweringUnsupportedError, match="超出已实现关系"):
        HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, taxed, config, evaluation)


def test_generate_accepts_pool_without_rules() -> None:
    """仅抵扣池（无税则）可生成：期初留抵参与连续结转。"""
    from iesplan.finance import CreditPool

    artifact, models, materialized, finance, config, evaluation = _inputs()
    pool = CreditPool.from_dict(
        {
            "opening_credit": {"value": "100", "unit": "CNY"},
            "receivable_policy": "end_receivable",
            "refunds": [],
        }
    )
    credited = replace(finance, credit_pools={"pool1": pool})
    bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, credited, config, evaluation)
    assert bundle.manifest.bundle["generator"] == GENERATOR_REF


# ---------------------------------------------------------------------------
# 真实求解对照（Docker 内 HiGHS 二进制）
# ---------------------------------------------------------------------------


def _parse_solution_objective(text: str) -> float:
    assert text.splitlines()[0] == "HIGHS_SOLUTION_V1"
    for line in text.splitlines()[1:]:
        key, _, value = line.partition(":")
        if key.strip() == "objective_value":
            return float(value.strip())
    raise AssertionError("solution 缺少 objective_value")


def _solve_mps(mps_text: str, tmp_path) -> float:
    (tmp_path / "input").mkdir(exist_ok=True)
    (tmp_path / "output").mkdir(exist_ok=True)
    (tmp_path / "input" / "problem.mps").write_text(mps_text, encoding="utf-8")
    completed = subprocess.run(
        [
            HIGHS_ADAPTER_PATH,
            "input/problem.mps",
            "--solution_file",
            "output/solution.sol",
            "--time_limit",
            "300",
            "--random_seed",
            "0",
        ],
        capture_output=True,
        text=True,
        timeout=330,
        cwd=tmp_path,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    content = (tmp_path / "output" / "solution.sol").read_text(encoding="utf-8")
    assert "kOptimal" in content
    return _parse_solution_objective(content)


def test_static_npv_real_solve_matches_hand(tmp_path) -> None:
    """静态容量真实求解：目标值 + 偏移 == 手算 NPV。"""
    artifact, models, materialized, finance, config, evaluation = _inputs()
    bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
    assert bundle.manifest.bundle["generator"] == GENERATOR_REF
    metadata = json.loads(bundle.files["input/parameters.json"].decode("utf-8"))
    solved = _solve_mps(bundle.files["input/problem.mps"].decode("utf-8"), tmp_path)
    assert solved + metadata["objective_offset"] == pytest.approx(_hand_npv(), rel=1e-6)


# ---------------------------------------------------------------------------
# 真实优化选择：NPV 与无折现合计的最优角不同
# ---------------------------------------------------------------------------

#: 选择算例：K=1、N=2，建设款 t=0 全付；第 3 年售电 0.13，第 2 年零价。
#: 无折现合计为正（应建满 10kW），折现 NPV 为负（应不建）。
CHOICE_PRICE = 0.13
CHOICE_CAP_MAX = 10.0
CHOICE_UPFRONT = 1000.0
CHOICE_FIXED = 10.0

GEN_YAML = """
schema: ies.device-model
schema_version: "2.2.0"
device: {id: acme.device.gen, names: {zh-CN: 发电机, en-US: Generator}}
properties:
  cap_kw: {value: 10, unit: kW, valid_range: {minimum: 0, maximum: 100}}
interfaces:
  power_out:
    type: out
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: {property_ref: cap_kw}}
  meter_in:
    type: in
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: null}
equations:
  variables: {}
  relations: []
"""


def _choice_finance_payload() -> dict:
    return {
        "schema": "ies.finance-profile",
        "schema_version": "6.0.0",
        "profile": {
            "id": "npv-choice",
            "region": "CN-North",
            "currency": "CNY",
            "base_year": 2025,
            "price_basis": "tax_inclusive",
            "cost_method": "fixed_plus_linear",
        },
        "demand_charges": {},
        "taxes": {},
        "credit_pools": {},
        "tax_rules": {},
        "net_cashflows": {},
        "finance_types": {
            "gen_sys": {
                "upfront_capex": {"linear": {"cap": {"unit_cost": {"value": "1000", "unit": "CNY/kW"}}}},
                "annual_fixed_om": {"linear": {"cap": {"unit_cost": {"value": "10", "unit": "CNY/kW/a"}}}},
            }
        },
        "energy_prices": {
            "gen_sale": {
                "carrier": "electricity",
                "direction": "sale",
                "kind": "time_series",
                "ref": "c" * 64,
                "series_meta": {
                    "resolution": "1h",
                    "point_count": 17520,
                    "unit": "CNY/kWh",
                },
            },
        },
    }


def _choice_doc(objective: dict, *, cap_bounds: tuple[str, str] = ("0", "10")) -> dict:
    return {
        "schema": "ies.assembly",
        "schema_version": "4.0.0",
        "assembly": {"id": "npv-choice", "name": "NPV 选择算例"},
        "project_baseline": {
            "resolution": "1h",
            "start_year": 2026,
            "scenario_mode": "single",
        },
        "devices": {
            "gen1": {
                "definition": {"id": "acme.device.gen"},
                "asset_origin": "new",
                "properties": {},
                "predefined_interfaces": {},
            },
        },
        "connections": {
            # 计量分接边：母线守恒即出力等于计量值，无需设备内关系。
            "link1": {"from": "gen1.power_out", "to": "gen1.meter_in"},
        },
        "finance": {
            "ref": {"kind": "object", "object_id": "obj-finance", "media_type": "application/yaml"},
            "profile_id": "npv-choice",
        },
        "finance_binding": {
            "instances": {
                "gen1": {
                    "type": "costed",
                    "finance_type": "gen_sys",
                    "drivers": {"cap": {"property": "cap_kw"}},
                },
            }
        },
        "tariff_bindings": {
            # 此例在 in 口显式售电（计量分接与出力同值）；out 口也合法。
            "meter": {
                "price": "gen_sale",
                "instance": "gen1",
                "interface": "meter_in",
                "aggregation": "integrate_positive",
            },
        },
        "demand_bindings": {},
        "planning": {
            "objective": objective,
            "variables": {
                "gen_cap": {
                    "device_ref": "gen1",
                    "parameter": "cap_kw",
                    "lower_bound": cap_bounds[0],
                    "upper_bound": cap_bounds[1],
                    "unit": "kW",
                },
            },
            "constraints": {},
        },
        "outputs": {"series": [], "metrics": []},
        "extensions": {},
    }


def _choice_inputs(objective: dict, *, cap_bounds: tuple[str, str] = ("0", "10")):
    from iesplan.computation import MaterializedPriceSeries, MaterializedSet

    doc = _choice_doc(objective, cap_bounds=cap_bounds)
    models = {"gen1": _contribution(GEN_YAML)}
    finance = merge_effective(FinanceProfile.from_dict(_choice_finance_payload()), None)
    artifact = _issued_artifact(doc, finance, ("acme.device.gen",))
    materialized = materialize_non_predictive_series(artifact, {}, operation_years=2)
    prices = (
        MaterializedPriceSeries(
            price_id="gen_sale",
            point_count=17520,
            resolution="1h",
            unit="CNY/kWh",
            values=(0.0,) * 8760 + (CHOICE_PRICE,) * 8760,
            source={"kind": "price_series", "object_id": "c" * 64},
        ),
    )
    materialized = MaterializedSet(
        operation_years=materialized.operation_years,
        series=materialized.series,
        pending_predict=materialized.pending_predict,
        prices=prices,
    )
    config = CalculationConfig(
        mode="planning",
        generator=GENERATOR_REF,
        solver="ies.solver.highs@1.7.2",
        precision="medium",
        time_limit_seconds=600,
        outputs=["solution"],
    )
    evaluation = EvaluationInput(
        evaluation_years=3,
        construction_period=1,
        operation_years=2,
        discount_rate=RATE,
        construction_payments=(ConstructionPayment(t=0, ratio="1.0"),),
        maintenance_expenses=(),
    )
    return artifact, models, materialized, finance, config, evaluation


def _choice_hand_sums() -> tuple[float, float]:
    """手算（无折现合计, 折现 NPV）：合计为正，NPV 为负。

    首运营年在 t=2：固定 OM 按 t=2、t=3 折现，不在 t=1。
    """
    revenue = CHOICE_PRICE * CHOICE_CAP_MAX * HOURS
    total = revenue - CHOICE_UPFRONT * CHOICE_CAP_MAX - 2 * CHOICE_FIXED * CHOICE_CAP_MAX
    npv = (
        -CHOICE_UPFRONT * CHOICE_CAP_MAX
        - CHOICE_FIXED * CHOICE_CAP_MAX / (1 + RATE) ** 2
        + (revenue - CHOICE_FIXED * CHOICE_CAP_MAX) / (1 + RATE) ** 3
    )
    return total, npv


def _solution_primals(text: str) -> tuple[dict, float]:
    lines = text.splitlines()
    assert lines[0] == "HIGHS_SOLUTION_V1"
    facts: dict = {}
    primals: list[float] = []
    section = None
    for line in lines[1:]:
        if line == "primal:":
            section = "primal"
            continue
        if section == "primal":
            _, _, value = line.partition(":")
            primals.append(float(value.strip()))
        else:
            key, _, value = line.partition(":")
            facts[key.strip()] = value.strip()
    return facts, primals


def _mps_column_order(mps_text: str) -> list[str]:
    """MPS COLUMNS 段列名顺序（与 solution primal 顺序一致）。"""
    order: list[str] = []
    section = None
    for line in mps_text.splitlines():
        tag = line.strip()
        if tag in ("NAME", "ROWS", "COLUMNS", "RHS", "BOUNDS", "RANGES", "ENDATA"):
            section = tag
            continue
        if section != "COLUMNS":
            continue
        parts = line.split()
        # 列内多条目同行（col row val [row val]…）；续行以 marker 引号起首。
        if len(parts) >= 3 and not parts[0].startswith("'") and parts[0] not in order:
            order.append(parts[0])
    return order


def _solve_bundle(bundle, tmp_path) -> tuple[float, float]:
    """真实求解 Bundle：返回（目标值 + 偏移，gen_cap 求解值 SI）。"""
    mps_text = bundle.files["input/problem.mps"].decode("utf-8")
    (tmp_path / "input").mkdir(parents=True, exist_ok=True)
    (tmp_path / "output").mkdir(parents=True, exist_ok=True)
    (tmp_path / "input" / "problem.mps").write_text(mps_text, encoding="utf-8")
    completed = subprocess.run(
        [
            HIGHS_ADAPTER_PATH,
            "input/problem.mps",
            "--solution_file",
            "output/solution.sol",
            "--time_limit",
            "300",
            "--random_seed",
            "0",
        ],
        capture_output=True,
        text=True,
        timeout=330,
        cwd=tmp_path,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    content = (tmp_path / "output" / "solution.sol").read_text(encoding="utf-8")
    facts, primals = _solution_primals(content)
    assert facts.get("model_status") == "HighsModelStatus.kOptimal"
    metadata = json.loads(bundle.files["input/parameters.json"].decode("utf-8"))
    order = _mps_column_order(mps_text)
    assert len(primals) == len(order)
    cap = primals[order.index("gen_cap")]
    return float(facts["objective_value"]) + metadata["objective_offset"], cap


def test_npv_selects_zero_build_where_total_sum_selects_max(tmp_path) -> None:
    """真实优化在选 NPV：折现为负不建；无折现合计为正建满。"""
    total, npv = _choice_hand_sums()
    assert total > 0 > npv
    npv_bundle = HIGHS_MILP_PROVIDER.generate(
        *_choice_inputs({"sense": "maximize", "expression": NPV_MARKER})
    )
    npv_value, npv_cap = _solve_bundle(npv_bundle, tmp_path / "npv")
    assert npv_value == pytest.approx(0.0, abs=1e-3)
    assert npv_cap == pytest.approx(0.0, abs=1e-3)
    # 旧记账口径对照：成本为正、售电记账为负，minimize 下应建满。
    sum_bundle = HIGHS_MILP_PROVIDER.generate(
        *_choice_inputs(
            {
                "sense": "minimize",
                "expression": (
                    "gen1__upfront_capex + 2 year * gen1__annual_fixed_om + meter__period_energy_sale"
                ),
            }
        )
    )
    sum_value, sum_cap = _solve_bundle(sum_bundle, tmp_path / "sum")
    assert sum_value == pytest.approx(-total, rel=1e-6)
    assert sum_cap == pytest.approx(10000.0, rel=1e-6)


def test_npv_fixed_build_matches_discounted_cashflows(tmp_path) -> None:
    """非零对照：容量固定 10kW 时真实目标 == 独立 CF 折现手算。"""
    _, npv = _choice_hand_sums()
    assert npv == pytest.approx(-1124.9555453945031, rel=1e-9)
    bundle = HIGHS_MILP_PROVIDER.generate(
        *_choice_inputs(
            {"sense": "maximize", "expression": NPV_MARKER},
            cap_bounds=("10", "10"),
        )
    )
    value, cap = _solve_bundle(bundle, tmp_path / "fixed")
    assert cap == pytest.approx(10000.0, rel=1e-6)
    assert value == pytest.approx(npv, rel=1e-6)
