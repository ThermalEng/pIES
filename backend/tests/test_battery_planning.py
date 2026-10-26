"""合成电池规划回归：同一设备贡献支持固定、规划、初值、零容量与单位。

HiGHS 真实求解仅在 GitHub Actions Docker 环境运行；本文件不运行 GUI。
采用两步小时模型，禁止添加软件默认容量上界/终期 SOC 或时长约束。
"""
from __future__ import annotations

import subprocess
from fractions import Fraction

import pytest

from iesplan.computation import HIGHS_ADAPTER_PATH
from iesplan.computation.generators.linear import (
    LinearConstraint, LinearExpression, LinearModel, LinearVariable,
)
from iesplan.computation.generators.mps import write_mps
from iesplan.computation.generators.project import (
    _ProjectBuilder, _tighten_nonnegative_equality_bounds,
    _state_mutex_envelope_rows,
)
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
          maximum=0.9, charge_efficiency=0.95, discharge_efficiency=0.95):
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
        "connections": {}, "tariff_bindings": {}, "demand_bindings": {},
        "finance_binding": {"instances": {}},
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
        devices={"cell": {"asset_origin": "new", "properties": {
            key: {"value": value, "unit": unit} for key, (value, unit) in props.items()
        }}},
        models={"cell": battery},
        series=(), finance=None, evaluation=None,
        point_count=2, step_s=3600,
    )
    static = builder._declare_instance_statics("cell")
    builder._build_relations("cell", static)
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


def test_fixed_energy_bounds_deduced_without_changing_physics(battery):
    # C=20 kWh、SOC=[.1,.9] => 同一解的 E 在 7.2..64.8 MJ。
    # 保留原两条方程、非负辅助变量、原业务单位及结果列。
    b = _rows(battery, capacity=20, power=6)
    inferred = {v.name: v for v in _tighten_nonnegative_equality_bounds(b.variables, b.rows)}
    assert len(inferred) == len(b.variables)
    for step in (0, 1):
        energy = inferred[f"cell__stored_energy__t{step}"]
        # SOC guarantees these bounds, and dynamics may tighten them further.
        assert energy.lower is not None
        assert energy.upper is not None
        assert energy.lower >= 7_200_000 - 1e-5
        assert energy.upper <= 64_800_000 + 1e-5
        assert energy.lower <= energy.upper
        assert _row(b, f"cell__minimum_energy__t{step}").sense == "eq"
        assert _row(b, f"cell__maximum_energy__t{step}").sense == "eq"
        assert f"cell__above_minimum__t{step}" in inferred
        assert f"cell__below_maximum__t{step}" in inferred


def test_mutex_state_envelope_is_implied_and_tightens_fractional_modes(battery):
    # 固定电池两步：初始能量、上下限及效率全部来自已声明输入。
    # 原 Big-M 二元互斥和技术等式原样保留，只增加有效不等式。
    b = _rows(battery, capacity=20, power=6)
    bounds = _tighten_nonnegative_equality_bounds(b.variables, b.rows)
    cuts = _state_mutex_envelope_rows(bounds, b.rows)
    assert cuts
    assert any(row.name.endswith("__charge_before") for row in cuts)
    assert any(row.name.endswith("__discharge_before") for row in cuts)
    # 两步动态传播可使相邻状态界有不同紧度；双侧状态界相同的
    # 合成合法情况则必须推导出全部四种有效不等式。
    uniform = tuple(
        LinearVariable(var.name, 7_200_000, 64_800_000, var.kind)
        if "__stored_energy__" in var.name else var
        for var in bounds
    )
    full_cuts = _state_mutex_envelope_rows(uniform, b.rows)
    assert any(row.name.endswith("__charge_after") for row in full_cuts)
    assert any(row.name.endswith("__discharge_after") for row in full_cuts)
    assert len({row.name for row in cuts}) == len(cuts)
    rows = tuple(b.rows) + cuts
    assert all(row in rows for row in b.rows)
    for row in cuts:
        assert row.sense in ("le", "ge")
        assert row.rhs == pytest.approx(row.rhs)  # 求解器级纯数值常量

    # 物理上可行的单边调度，全部满足强化式；初态为 36 MJ。
    for charge, discharge in ((0, 0), (1000, 0), (0, 1000)):
        previous = 36_000_000
        current = previous + charge * 3420 - discharge * (3600 / 0.95)
        values = {
            "cell__charge__t0": charge,
            "cell__discharge__t0": discharge,
            "cell__stored_energy__t0": current,
        }
        for row in cuts:
            if not row.name.startswith("cell__energy_update__t0"):
                continue
            lhs = row.expression.constant + sum(
                coefficient * values[name]
                for (name, _), coefficient in row.expression.terms
            )
            if row.sense == "le":
                assert lhs <= row.rhs + 1e-5
            else:
                assert lhs >= row.rhs - 1e-5


def test_mutex_envelope_coefficients_round_toward_feasible_region():
    """用系数 5/7 的精确整数可行解检验 derived cut 不得变严。

    7*E - 5*charge + 5*discharge=0，充电 7、放电 0、E=5
    是严格的原约束可行解。float(5/7) 会向上舍入，若直接把
    该系数用于 charge_before，则错误排除 E=5 的可行点。
    """
    variables = (
        LinearVariable("cell__charge__t0", 0, 7),
        LinearVariable("cell__discharge__t0", 0, 7),
        LinearVariable("cell__energy__t0", 0, 5),
        LinearVariable("mutex_b", 0, 1, "binary"),
    )
    rows = [
        LinearConstraint("a_mutex",
            LinearExpression(terms=(
                (("cell__charge__t0", 0), 1.0), (("mutex_b", 0), -7.0),
            )), sense="le", rhs=0),
        LinearConstraint("b_mutex",
            LinearExpression(terms=(
                (("cell__discharge__t0", 0), 1.0), (("mutex_b", 0), 7.0),
            )), sense="le", rhs=7),
        LinearConstraint("cell__energy_update__t0",
            LinearExpression(terms=(
                (("cell__charge__t0", 0), -5.0),
                (("cell__discharge__t0", 0), 5.0),
                (("cell__energy__t0", 0), 7.0),
            )), sense="eq", rhs=0),
    ]
    cuts = _state_mutex_envelope_rows(variables, rows)
    assert cuts
    solution = {
        "cell__charge__t0": Fraction(7),
        "cell__discharge__t0": Fraction(0),
        "cell__energy__t0": Fraction(5),
    }
    for cut in cuts:
        lhs = sum((
            Fraction(coef) * solution[name]
            for (name, _), coef in cut.expression.terms
        ), Fraction(cut.expression.constant))
        rhs = Fraction(cut.rhs)
        if cut.sense == "le":
            assert lhs <= rhs, cut.name
        else:
            assert lhs >= rhs, cut.name



def test_generic_interval_tightening_preserves_binary_and_unknown_bounds():
    variables = {
        "x": LinearVariable("x", lower=0, upper=None),
        "s": LinearVariable("s", lower=0, upper=None),
        "b": LinearVariable("b", lower=0, upper=1, kind="binary"),
        "size": LinearVariable("size", lower=0, upper=None),
        "free": LinearVariable("free", lower=0, upper=None),
    }
    rows = [
        LinearConstraint("finite_x", LinearExpression(
            terms=((("s", 0), 1), (("x", 0), 1)),
            constant=-15,
        ), sense="eq", rhs=0),
        LinearConstraint("unknown_size", LinearExpression(
            terms=((("free", 0), 1), (("size", 0), -2),
                   (("x", 0), 1)),
        ), sense="eq", rhs=0),
    ]
    inferred = {v.name: v for v in _tighten_nonnegative_equality_bounds(variables, rows)}
    assert inferred["x"].upper == pytest.approx(15)
    assert inferred["x"].lower == 0
    assert inferred["size"].upper is None
    assert inferred["b"].kind == "binary"
    assert (inferred["b"].lower, inferred["b"].upper) == (0, 1)


def test_interval_tightening_retains_exact_cancellation_feasible_zero():
    """主审复现：float 累加会把 1e16 + 1 - 1e16 丢成 0。

    原约束 a+b+c+x=1、a=1e16、b=1、c=-1e16，x=0
    以有理数算术严格满足等式；预处理绝不可使 x.lower > 0。
    """
    variables = {
        "a": LinearVariable("a", 1e16, 1e16),
        "b": LinearVariable("b", 1, 1),
        "c": LinearVariable("c", -1e16, -1e16),
        "x": LinearVariable("x", 0, None),
    }
    row = LinearConstraint(
        "exact_cancellation",
        LinearExpression(terms=tuple(
            ((name, 0), 1.0) for name in ("a", "b", "c", "x")
        )),
        sense="eq", rhs=1,
    )
    exact_lhs = sum((Fraction(variables[n].lower) for n in ("a", "b", "c")), Fraction(0))
    assert exact_lhs == Fraction(row.rhs)
    inferred = {
        v.name: v for v in _tighten_nonnegative_equality_bounds(variables, [row])
    }
    assert inferred["x"].lower <= 0
    assert inferred["x"].upper >= 0
    assert inferred["x"].lower == 0
    assert inferred["x"].upper == 0


def test_interval_tightening_mixed_sign_and_rational_endpoints():
    """混合符号+大值消去；上界 1/3 须朝 +inf 舍入。"""
    variables = {
        "a": LinearVariable("a", 1e16, 1e16),
        "b": LinearVariable("b", 1, 2),
        "c": LinearVariable("c", 1e16, 1e16),
        "x": LinearVariable("x", 0, None),
    }
    # a + b - c - 3*x = 1 -> x = (b-1)/3 ∈ [0,1/3]。
    row = LinearConstraint(
        "mixed_sign",
        LinearExpression(terms=(
            (("a", 0), 1.0), (("b", 0), 1.0),
            (("c", 0), -1.0), (("x", 0), -3.0),
        )),
        sense="eq", rhs=1,
    )
    inferred = {
        v.name: v for v in _tighten_nonnegative_equality_bounds(variables, [row])
    }
    assert inferred["x"].lower <= 0
    assert inferred["x"].upper is not None
    assert Fraction(inferred["x"].upper) >= Fraction(1, 3)
    assert inferred["x"].upper < 0.34

    # 另一方向：-2/3 不可被向零舍入；不假定某一侧原本有界。
    variables["x"] = LinearVariable("x", None, None)
    negative = LinearConstraint(
        "negative_endpoints",
        LinearExpression(terms=(
            (("a", 0), 1.0), (("b", 0), 1.0),
            (("c", 0), -1.0), (("x", 0), 3.0),
        )),
        sense="eq", rhs=0,
    )
    inferred = {
        v.name: v for v in _tighten_nonnegative_equality_bounds(variables, [negative])
    }
    assert inferred["x"].lower is not None
    assert inferred["x"].upper is not None
    assert Fraction(inferred["x"].lower) <= Fraction(-2, 3)
    assert Fraction(inferred["x"].upper) >= Fraction(-1, 3)



def test_planned_energy_bounds_do_not_invent_capacity_limit(battery):
    b = _rows(battery, power=6)
    inferred = {v.name: v for v in _tighten_nonnegative_equality_bounds(b.variables, b.rows)}
    assert inferred["battery_size"].upper is None
    for step in (0, 1):
        assert inferred[f"cell__stored_energy__t{step}"].upper is None


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
        variables=_tighten_nonnegative_equality_bounds(b.variables, rows),
        constraints=tuple(rows) + _state_mutex_envelope_rows(
            _tighten_nonnegative_equality_bounds(b.variables, rows), rows
        ),
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
    from iesplan.finance import FinanceProfile, merge_effective
    from test_npv_objective import _finance_payload

    payload = _finance_payload()
    profile = payload["finance_types"]["sys1"]
    profile["upfront_capex"]["linear"]["cap"]["unit_cost"] = {
        "value": "50", "unit": "CNY/kWh",
    }
    profile["annual_fixed_om"]["linear"]["cap"]["unit_cost"] = {
        "value": "12", "unit": "CNY/kWh/a",
    }
    b = _rows(battery, power=6)
    b.finance = merge_effective(FinanceProfile.from_dict(payload), None)
    b.doc["finance_binding"]["instances"]["cell"] = {
        "type": "costed", "finance_type": "sys1",
        "drivers": {
            "cap": {"property": "capacity_kwh"},
            "thr": {"interface": "electricity", "aggregation": "integrate_positive"},
        },
    }
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
    assert any("量纲" in d.params.get("detail", "") or "单位" in d.params.get("detail", "") for d in result.diagnostics)


def test_planned_capacity_capex_and_dated_maintenance_recover_from_one_solution(
    battery, tmp_path,
):
    # 同一真实 HiGHS 主解 C=10 kWh → CAPEX=500 CNY；
    # 第 2 年维护 = 20 CNY 固定 + 3 CNY/kWh×10 kWh + 10%×CAPEX = 100 CNY。
    # 第 3 年未安排维护：不能把年度固定 O&M 误充 maintenance_expenses。
    from iesplan.computation.result_adapters.highs import _recover_financial_facts
    from iesplan.finance import (
        ConstructionPayment, CostComponentShareTerm, EvaluationInput,
        FinanceProfile, MaintenanceExpense, MaintenanceUnitCost,
        ScaleUnitCostTerm, merge_effective,
    )
    from test_npv_objective import _finance_payload

    payload = _finance_payload()
    profile = payload["finance_types"]["sys1"]
    profile["upfront_capex"]["linear"]["cap"]["unit_cost"] = {
        "value": "50", "unit": "CNY/kWh",
    }
    profile["annual_fixed_om"]["linear"]["cap"]["unit_cost"] = {
        "value": "12", "unit": "CNY/kWh/a",
    }
    profile.pop("period_variable_om")  # 只验证容量、建设、年费与指定年维护。

    b = _rows(battery, discharge_efficiency=1, power=6)
    b.finance = merge_effective(FinanceProfile.from_dict(payload), None)
    b.doc["finance_binding"]["instances"]["cell"] = {
        "type": "costed", "finance_type": "sys1",
        "drivers": {"cap": {"property": "capacity_kwh"}},
    }
    b.doc["project_baseline"] = {
        "resolution": "1h", "start_year": 2026, "scenario_mode": "single",
    }
    b.evaluation = EvaluationInput(
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
