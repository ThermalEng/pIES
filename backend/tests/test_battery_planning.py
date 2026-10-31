"""合成电池规划回归：同一设备贡献支持固定、规划、初值、零容量与单位。

HiGHS 真实求解仅在 Docker 环境运行；本文件不运行 GUI。
采用两步小时模型，禁止添加软件默认容量上界/终期 SOC 或时长约束。
"""
from __future__ import annotations

import subprocess

import pytest

from iesplan.computation import HIGHS_ADAPTER_PATH
from iesplan.computation.generators.compiler_ir import CompilerAnalysis
from iesplan.computation.generators.linear import (
    LinearConstraint,
    LinearExpression,
    LinearModel,
)
from iesplan.computation.generators.mps import write_mps
from iesplan.computation.generators.project import _ProjectBuilder
from iesplan.devices import init_registry, list_devices
from iesplan.devices.contracts2 import canonical_bytes, from_dict, to_dict
from iesplan.devices.parser2 import parse_device_model_v2
from iesplan.modeling import build_math_contribution
from iesplan.modeling.contract2 import contribution_from_dict, contribution_to_dict


@pytest.fixture(scope="module")
def battery():
    init_registry()
    document = next(d for d in list_devices() if d.device.id == "ies.device.battery")
    assert from_dict(to_dict(document)).equations.variables["stored_energy"].initial_expression == (
        "initial_soc * capacity_kwh"
    )
    result = build_math_contribution(document)
    assert result.ok, result.diagnostics
    contribution = contribution_from_dict(contribution_to_dict(result.contribution))
    assert contribution.variables["stored_energy"].initial_ast is not None
    return contribution


def _rows(battery, *, capacity=None, power=None, soc=0.5, minimum=0.1,
          maximum=0.9, charge_efficiency=0.95, discharge_efficiency=0.95,
          finance=None, finance_binding=None, evaluation=None, block_size=256):
    """由真实通用贡献生成两小时 SI 数学行；规划容量不设有限上界。"""
    props = {
        "initial_soc": (soc, "1"),
        "min_soc": (minimum, "1"),
        "max_soc": (maximum, "1"),
        "charge_efficiency": (charge_efficiency, "1"),
        "discharge_efficiency": (discharge_efficiency, "1"),
    }
    if capacity is not None:
        props["capacity_kwh"] = (capacity, "kWh")
    if power is not None:
        props["rated_power_kw"] = (power, "kW")
    doc = {
        "planning": {"objective": {"sense": "minimize", "expression": "0"},
                     "variables": {}},
        "project_baseline": {
            "resolution": "1h", "start_year": 2026, "scenario_mode": "single",
        },
        "connections": {}, "tariff_bindings": {}, "demand_bindings": {},
        "finance_binding": {
            "instances": {"cell": finance_binding} if finance_binding else {},
        },
    }
    if capacity is None:
        doc["planning"]["variables"]["battery_size"] = {
            "device_ref": "cell", "parameter": "capacity_kwh",
            "lower_bound": "0", "upper_bound": None, "unit": "kWh",
        }
    if power is None:
        # 当前 A 路 lowerer 需要有限 Big-M；此界来自规划输入，并非 B 默认。
        doc["planning"]["variables"]["battery_kw"] = {
            "device_ref": "cell", "parameter": "rated_power_kw",
            "lower_bound": "0", "upper_bound": "6", "unit": "kW",
        }
    builder = _ProjectBuilder(
        doc=doc,
        devices={"cell": {"definition": {"id": battery.device_id},
                          "asset_origin": "new", "properties": {
            key: {"value": value, "unit": unit} for key, (value, unit) in props.items()
        }}},
        models={"cell": battery},
        series=(), finance=finance, evaluation=evaluation,
        point_count=2, step_s=3600,
    )
    builder.template_block_size = block_size
    # The compiler now has an explicit declaration/binding barrier.
    # This harness keeps its trusted two-hour synthetic domain rather than
    # reintroducing the removed mixed declaration/expansion entrypoint.
    builder._declare_global_symbols()
    bound = builder._bind_instance_symbols("cell")
    # Two-step synthetic physics still shares the one immutable compiler
    # barrier. Finance tests must provide their *complete* frozen-like inputs
    # before collection, never mutate them after the inventory is sealed.
    if finance is not None:
        assert finance_binding is not None and evaluation is not None
        builder._collect_semantic_relations([], {"cell": bound})
    else:
        builder.inventory.seal()
        builder.analysis = CompilerAnalysis(builder.inventory, builder.variables)
    builder._compile_templates({"cell": bound})
    builder._expand_instance("cell", bound)
    builder._build_relations("cell", bound)
    return builder


def _row(builder, name):
    return next(r for r in builder.rows if r.name == name)


def _terms(row):
    return {key[0]: value for key, value in row.expression.terms}


def test_fixed_capacity_hand_energy_and_efficiency(battery):
    b = _rows(battery, capacity=20, power=6, charge_efficiency=0.8,
              discharge_efficiency=0.8)
    first = _row(b, "cell__energy_update__t0")
    # SI: 初始 20 kWh * 0.5 = 36 MJ；1 kW 一小时 = 3.6 MJ。
    assert first.expression.constant == pytest.approx(-36_000_000)
    assert _terms(first)["cell__charge__t0"] == pytest.approx(-2880)
    assert _terms(first)["cell__discharge__t0"] == pytest.approx(4500)
    second = _row(b, "cell__energy_update__t1")
    assert _terms(second)["cell__stored_energy__t0"] == -1
    assert second.expression.constant == 0
    assert b.unit_map["cell__stored_energy__t0"] == "kWh"
    assert b.unit_map["cell__charge__t0"] == "kW"


def test_planned_capacity_power_and_soc_boundaries_are_affine(battery):
    b = _rows(battery)
    first = _row(b, "cell__energy_update__t0")
    # All column coefficients are in solver SI: battery_size is Joules, not kWh.
    assert _terms(first)["battery_size"] == pytest.approx(-0.5)
    assert first.expression.constant == pytest.approx(0)
    lo = _row(b, "cell__minimum_energy__t0")
    hi = _row(b, "cell__maximum_energy__t0")
    assert _terms(lo)["battery_size"] == pytest.approx(0.1)
    assert _terms(hi)["battery_size"] == pytest.approx(-0.9)
    for step in (0, 1):
        for flow in ("charge", "discharge"):
            bound = _row(b, f"cell__{flow}__plan_max__t{step}")
            assert _terms(bound)["battery_kw"] == -1
    cap = b.variables["battery_size"]
    assert cap.lower == 0 and cap.upper is None
    assert b.unit_map["battery_size"] == "kWh"
    assert b.unit_map["battery_kw"] == "kW"


def _solve(b, tmp_path, *, demand_w=0, force_zero=False):
    """Docker 中通过实际 HiGHS 适配器求解，并以列名读取原始主解。"""
    rows = list(b.rows)
    if demand_w:
        rows.append(LinearConstraint(
            name="serve_discharge", expression=LinearExpression(
                terms=((("cell__discharge__t0", 0), 1),),
            ), sense="ge", rhs=demand_w,
        ))
    if force_zero:
        rows.append(LinearConstraint(
            name="zero_capacity", expression=LinearExpression(
                terms=((("battery_size", 0), 1),),
            ), sense="eq", rhs=0,
        ))
    obj = LinearExpression(
        terms=((("battery_size", 0), 1),) if "battery_size" in b.variables else ()
    )
    model = LinearModel(
        variables=tuple(b.variables.values()), constraints=tuple(rows),
        objective=obj, sense="minimize",
    )
    problem, offset = write_mps(model)
    assert offset == 0
    (tmp_path / "solution").mkdir(exist_ok=True)
    (tmp_path / "problem.mps").write_text(problem, encoding="utf-8")
    result = subprocess.run(
        [HIGHS_ADAPTER_PATH, "problem.mps", "--solution_file", "solution/values.sol",
         "--time_limit", "60", "--random_seed", "0"],
        cwd=tmp_path, text=True, capture_output=True, timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    solution = (tmp_path / "solution/values.sol").read_text(encoding="utf-8")
    lines = solution.splitlines()
    assert lines[0] == "HIGHS_SOLUTION_V1"
    assert "model_status: HighsModelStatus.kOptimal" in lines
    primal_start = lines.index("primal:")
    values = [float(line.partition(":")[2]) for line in lines[primal_start + 1:]]
    # MPS writer columns are emitted in sorted LinearModel.variables order.
    columns = [var.name for var in model.variables]
    assert len(values) == len(columns)
    return dict(zip(columns, values, strict=True))


def test_true_optimized_capacity_no_default_upper_bound(battery, tmp_path):
    # 4 kW 放电 1 h，eff=1，起始 SOC=.5，下限=.1：
    # (0.5 - 0.1) * C >= 4 kWh => 最优 C=10 kWh。
    b = _rows(battery, discharge_efficiency=1, power=6)
    primal = _solve(b, tmp_path, demand_w=4000)
    assert primal["battery_size"] == pytest.approx(36_000_000, abs=1)
    assert primal["cell__stored_energy__t0"] == pytest.approx(3_600_000, abs=1)
    assert primal["cell__discharge__t0"] == pytest.approx(4000, abs=1)
    assert primal["cell__above_minimum__t0"] == pytest.approx(0, abs=1)


def test_zero_capacity_has_no_free_initial_energy(battery, tmp_path):
    # A 已合入：零容量和零功率上界都应可行，无虚构初始能量。
    # 只消费主线互斥通用能力，不在 B 中复制降级实现。
    b = _rows(battery, power=0, soc=0.75)
    assert not any("__mutex" in name for name in b.variables)
    primal = _solve(b, tmp_path, force_zero=True)
    assert primal["battery_size"] == pytest.approx(0, abs=1e-6)
    for step in (0, 1):
        assert primal[f"cell__stored_energy__t{step}"] == pytest.approx(0, abs=1e-6)
        assert primal[f"cell__charge__t{step}"] == pytest.approx(0, abs=1e-6)
        assert primal[f"cell__discharge__t{step}"] == pytest.approx(0, abs=1e-6)


def test_cost_and_annual_maintenance_use_the_same_planned_size(battery):
    # Financial quantities must use the same capacity variable as energy constraints.
    from test_npv_objective import _finance_payload

    from iesplan.finance import FinanceProfile, merge_effective

    payload = _finance_payload()
    profile = payload["finance_types"]["sys1"]
    profile["upfront_capex"]["linear"]["cap"]["unit_cost"] = {
        "value": "50", "unit": "CNY/kWh",
    }
    profile["annual_fixed_om"]["linear"]["cap"]["unit_cost"] = {
        "value": "12", "unit": "CNY/kWh/a",
    }
    from iesplan.finance import EvaluationInput

    b = _rows(
        battery, power=6,
        finance=merge_effective(FinanceProfile.from_dict(payload), None),
        finance_binding={
            "type": "costed", "finance_type": "sys1",
            "drivers": {
                "cap": {"property": "capacity_kwh"},
                "thr": {"interface": "electricity",
                        "aggregation": "integrate_positive"},
            },
        },
        evaluation=EvaluationInput(
            evaluation_years=1, construction_period=0, operation_years=1,
            discount_rate=0.0, construction_payments=(),
            maintenance_expenses=(),
        ),
    )
    assert b.inventory.sealed
    assert any(op.kind == "cost" for op in b.inventory.operations)
    components = b.finance_components()
    for field in ("upfront_capex", "annual_fixed_om"):
        component = components[f"cell__{field}"]
        assert component["constant"] == 0
        terms = component["terms"]
        assert len(terms) == 1
        assert set(terms) == {"battery_size"}
        assert terms["battery_size"] > 0



def test_initial_expression_public_input_and_frozen_roundtrip(battery):
    # 真实公共设备 parser 入口；不是私有 builder 手工构造可信 MathVariable。
    init_registry()
    document = next(d for d in list_devices() if d.device.id == "ies.device.battery")
    raw = to_dict(document)
    parsed = parse_device_model_v2(raw, file="battery-public.yaml")
    assert parsed.ok, parsed.diagnostics
    assert parsed.document.equations.variables["stored_energy"].initial_expression == (
        "initial_soc * capacity_kwh"
    )
    frozen = from_dict(to_dict(parsed.document))
    assert canonical_bytes(frozen) == canonical_bytes(parsed.document)
    rebuilt = build_math_contribution(frozen)
    assert rebuilt.ok, rebuilt.diagnostics
    restored = contribution_from_dict(contribution_to_dict(rebuilt.contribution))
    assert restored.variables["stored_energy"].initial_ast == (
        rebuilt.contribution.variables["stored_energy"].initial_ast
    )


@pytest.mark.parametrize("initial", [
    {"expression": ""},
    {"expression": "missing_capacity * initial_soc"},
    {"expression": "capacity_kwh[t] * initial_soc"},
    {"expression": "capacity_kwh * charge[t]"},
    {"expression": "capacity_kwh * initial_soc", "property_ref": "capacity_kwh"},
])
def test_initial_expression_public_input_rejects_invalid_forms(initial):
    init_registry()
    document = next(d for d in list_devices() if d.device.id == "ies.device.battery")
    raw = to_dict(document)
    raw["equations"]["variables"]["stored_energy"]["initial"] = initial
    parsed = parse_device_model_v2(raw, file="invalid-initial.yaml")
    assert not parsed.ok
    assert parsed.document is None
    assert parsed.diagnostics


def test_initial_expression_public_input_rejects_unit_conflict():
    init_registry()
    document = next(d for d in list_devices() if d.device.id == "ies.device.battery")
    raw = to_dict(document)
    raw["equations"]["variables"]["stored_energy"]["initial"] = {
        "expression": "initial_soc + capacity_kwh"
    }
    parsed = parse_device_model_v2(raw, file="bad-unit-initial.yaml")
    # 公开结构解析器与公共建模器共同构成入模校验；不能让非法量纲入模。
    if not parsed.ok:
        assert parsed.diagnostics
        return
    result = build_math_contribution(parsed.document)
    assert not result.ok
    assert result.contribution is None
    assert any(
        "量纲" in d.params.get("detail", "") or "单位" in d.params.get("detail", "")
        for d in result.diagnostics
    )


def test_planned_capacity_capex_and_dated_maintenance_recover_from_one_solution(
    battery, tmp_path,
):
    # 同一真实 HiGHS 主解 C=10 kWh → CAPEX=500 CNY；
    # 第 2 年维护 = 20 CNY 固定 + 3 CNY/kWh×10 kWh + 10%×CAPEX = 100 CNY。
    # 第 3 年未安排维护：不能把年度固定 O&M 误充 maintenance_expenses。
    from test_npv_objective import _finance_payload

    from iesplan.computation.result_adapters.highs import _recover_financial_facts
    from iesplan.finance import (
        ConstructionPayment,
        CostComponentShareTerm,
        EvaluationInput,
        FinanceProfile,
        MaintenanceExpense,
        MaintenanceUnitCost,
        ScaleUnitCostTerm,
        merge_effective,
    )

    payload = _finance_payload()
    profile = payload["finance_types"]["sys1"]
    profile["upfront_capex"]["linear"]["cap"]["unit_cost"] = {
        "value": "50", "unit": "CNY/kWh",
    }
    profile["annual_fixed_om"]["linear"]["cap"]["unit_cost"] = {
        "value": "12", "unit": "CNY/kWh/a",
    }
    profile.pop("period_variable_om")  # 只验证容量、建设、年费与指定年维护。

    evaluation = EvaluationInput(
        evaluation_years=3, construction_period=1, operation_years=2,
        discount_rate=0.0,
        construction_payments=(ConstructionPayment(t=0, ratio="1"),),
        maintenance_expenses=(
            MaintenanceExpense(
                year=2, amount="20", currency="CNY", terms=(
                    ScaleUnitCostTerm(
                        kind="scale_unit_cost", instance_id="cell",
                        property="capacity_kwh",
                        unit_cost=MaintenanceUnitCost(value="3", unit="CNY/kWh"),
                    ),
                    CostComponentShareTerm(
                        kind="cost_component_share", instance_id="cell",
                        component="upfront_capex", ratio=0.1,
                    ),
                ),
            ),
        ),
    )
    b = _rows(
        battery, discharge_efficiency=1, power=6,
        finance=merge_effective(FinanceProfile.from_dict(payload), None),
        finance_binding={
            "type": "costed", "finance_type": "sys1",
            "drivers": {"cap": {"property": "capacity_kwh"}},
        },
        evaluation=evaluation,
    )
    assert b.inventory.sealed
    assert len(b.analysis.relations("maintenance")) == 1
    assert len(b.analysis.relations("annual_cash")) == 2
    # 复用生产 builder 的建设/维护同源仿射声明，不构造第二套财务算法。
    b._retain_maintenance()
    b._retain_operating_facts()
    declared = b.financial_facts()
    assert declared["annual_operation"][0]["financial_year"] == 2
    assert declared["annual_operation"][1]["financial_year"] == 3

    primal = _solve(b, tmp_path, demand_w=4000)
    assert primal["battery_size"] == pytest.approx(36_000_000, abs=1)
    # 复用生产 HiGHS 适配器的财务事实恢复：列值来自上述同一次主解。
    restored = _recover_financial_facts(
        declared, {name: index for index, name in enumerate(primal)},
        list(primal.values()),
    )
    assert restored["currency"] == "CNY"
    assert float(restored["construction_total"]) == pytest.approx(500, abs=1e-5)
    assert restored["payments_applied"][0]["t"] == 0
    assert float(restored["payments_applied"][0]["amount"]) == pytest.approx(500, abs=1e-5)
    annual = {row["financial_year"]: row for row in restored["annual_operation"]}
    assert set(annual) == {2, 3}
    assert float(annual[2]["maintenance"]) == pytest.approx(100, abs=1e-5)
    assert float(annual[3]["maintenance"]) == pytest.approx(0, abs=1e-5)
    # 常年固定 O&M 与指定年维护为独立业务事实。
    assert float(annual[2]["fixed_om"]) == pytest.approx(120, abs=1e-5)
    assert float(annual[3]["fixed_om"]) == pytest.approx(120, abs=1e-5)


def test_power_plan_with_explicit_finite_input_bound(battery):
    # A route's unbounded Big-M fallback is deliberately out of scope.
    b = _rows(battery, capacity=0, power=None)
    assert b.variables["battery_kw"].upper == 6000


@pytest.mark.parametrize(
    "initial_ref,initial_expression,port_type,expected",
    [
        ("initial_energy", None, "bidirectional", True),
        (None, "initial_ratio * size", "bidirectional", True),
        (None, None, "bidirectional", False),
        ("initial_energy", None, "out", False),
    ],
)
def test_storage_screen_uses_state_declaration_not_variable_name(
    initial_ref, initial_expression, port_type, expected,
):
    from types import SimpleNamespace

    from iesplan.assembly.rules.solvability import _has_storage_state

    state = SimpleNamespace(
        initial_property_ref=initial_ref, initial_expression=initial_expression,
    )
    model = SimpleNamespace(
        equations=SimpleNamespace(variables={"arbitrary_state_name": state}),
        interfaces={"arbitrary_port_name": SimpleNamespace(type=port_type)},
    )
    assert _has_storage_state(model) is expected
    assert not _has_storage_state(None)
