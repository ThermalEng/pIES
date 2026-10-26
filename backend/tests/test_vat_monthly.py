"""C1c VAT 月度精确关系与同次 NPV：共享池、互斥分段、退款与手算对照。

只在 Docker 内运行其中真实求解部分（需镜像内 HiGHS 二进制）。
全部金额为合成手算数，不读外部实例。生产不得反解 LP 命名充当
公共输出；本测试读取内部解仅为对照约束精确性。
"""

from __future__ import annotations

import json
import subprocess
from decimal import Decimal

import pytest
from test_highs_result_adapter import SOLUTION_PATH, _receipt
from test_highs_result_adapter import _bundle as _wrap_bundle

from iesplan.assembly import validate_assembly_doc
from iesplan.computation import (
    HIGHS_ADAPTER_PATH,
    HIGHS_MILP_PROVIDER,
    CalculationConfig,
    MaterializedPriceSeries,
    MaterializedSet,
    build_project_model,
    materialize_non_predictive_series,
)
from iesplan.computation.generators.highs_milp import GENERATOR_REF
from iesplan.computation.generators.linear import LoweringUnsupportedError
from iesplan.computation.result_adapters.highs import HIGHS_ADAPTER
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

NPV_MARKER = "system.after_tax_npv"

#: 合成口径：K=1、N=2、E=3、r=0.08；建设 6000 两笔付款；购电 0.7 常数；
#: 倒送时变（2026 年 -0.05，2027 年 +2.0）；VAT 率 0.13 含税价。
RATE = 0.08
VAT_RATE = Decimal("0.13")
GROSS_FACTOR = VAT_RATE / (Decimal("1") + VAT_RATE)
CAP_KW = 60.0
UPFRONT_TOTAL = 6000.0
HOURS_2026 = (744, 672, 744, 720, 744, 720, 744, 744, 720, 744, 720, 744)
HOURS_2027 = (744, 672, 744, 720, 744, 720, 744, 744, 720, 744, 720, 744)
LOAD_KW = 7.0
FLOW_KW = 5.0
EFFICIENCY = 0.9
ELEC_KW = LOAD_KW / EFFICIENCY


def _finance_payload(*, refunds=(), opening="0", basis="tax_inclusive", rules="all", maintenance_tax=True):
    payload = {
        "schema": "ies.finance-profile",
        "schema_version": "5.0.0",
        "profile": {
            "id": "vat-demo",
            "region": "CN-North",
            "currency": "CNY",
            "base_year": 2025,
            "price_basis": basis,
            "cost_method": "fixed_plus_linear",
        },
        "demand_charges": {},
        "taxes": {
            "vat_demo": {
                "display_name": "增值税",
                "tax_type": "vat",
                "applies_to": "heat_import",
            },
        },
        "credit_pools": {
            "main_pool": {
                "opening_credit": {"value": opening, "unit": "CNY"},
                "receivable_policy": "end_receivable",
                "refunds": list(refunds),
            },
        },
        "tax_rules": {
            "vat_op": {
                "kind": "vat",
                "label_ref": "vat_demo",
                "settlement": "monthly",
                "in_tax": [
                    {
                        "price": "heat_import",
                        "rate": {"value": "0.13", "unit": "1"},
                        "deductible_ratio": {"value": "1", "unit": "1"},
                    },
                ],
                "out_tax": [],
                "om_input_tax": [
                    {
                        "for": "sys1.period_variable_om",
                        "rate": {"value": "0.13", "unit": "1"},
                        "deductible_ratio": {"value": "1", "unit": "1"},
                    },
                    {
                        "for": "sys1.annual_fixed_om",
                        "rate": {"value": "0.13", "unit": "1"},
                        "deductible_ratio": {"value": "0.5", "unit": "1"},
                    },
                ],
                "maintenance_input_tax": [
                    {
                        "year": 2,
                        "position": 1,
                        "rate": {"value": "0.13", "unit": "1"},
                        "deductible_ratio": {"value": "1", "unit": "1"},
                    },
                    {
                        "year": 2,
                        "position": 2,
                        "rate": {"value": "0.13", "unit": "1"},
                        "deductible_ratio": {"value": "0", "unit": "1"},
                    },
                ],
                "credit_pool": "main_pool",
                "confirmation": "same_period",
            },
            "vat_sale": {
                "kind": "vat",
                "label_ref": "vat_demo",
                "settlement": "monthly",
                "in_tax": [],
                "out_tax": [
                    {
                        "price": "export_ts",
                        "rate": {"value": "0.13", "unit": "1"},
                    },
                ],
                "om_input_tax": [],
                "maintenance_input_tax": [],
                "credit_pool": "main_pool",
                "confirmation": "same_period",
            },
            "con_vat": {
                "kind": "construction_input_vat",
                "label_ref": "vat_demo",
                "for": "sys1.upfront_capex",
                "rate": {"value": "0.13", "unit": "1"},
                "deductible_ratio": {"value": "1", "unit": "1"},
                "credit_pool": "main_pool",
                "confirmation": {"method": "payment_period"},
            },
        },
        "finance_types": {
            "sys1": {
                "upfront_capex": {"linear": {"cap": {"unit_cost": {"value": "100", "unit": "CNY/kW"}}}},
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
            "export_ts": {
                "carrier": "electricity",
                "direction": "sale",
                "kind": "time_series",
                "ref": "d" * 64,
                "series_meta": {
                    "resolution": "1h",
                    "point_count": 17520,
                    "unit": "CNY/kWh",
                },
            },
        },
    }
    if rules == "construction_only":
        payload["tax_rules"] = {"con_vat": payload["tax_rules"]["con_vat"]}
    elif rules != "all":
        raise ValueError(f"未知规则子集: {rules!r}")
    if not maintenance_tax and "vat_op" in payload["tax_rules"]:
        payload["tax_rules"]["vat_op"]["maintenance_input_tax"] = []
    return payload


HEATER_YAML = """
schema: ies.device-model
schema_version: "2.2.0"
device: {id: acme.device.heater, names: {zh-CN: 加热器, en-US: Heater}}
properties:
  efficiency: {value: 0.9, unit: "1", valid_range: {minimum: 0, maximum: 1}}
interfaces:
  electricity_in:
    type: in
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: 100}
  heat_out:
    type: out
    carrier: heat
    unit: kW
    valid_range: {minimum: 0, maximum: 100}
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
  heat_in:
    type: in
    carrier: heat
    unit: kW
    valid_range: {minimum: 0, maximum: 100}
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


def _doc(*, variables: dict | None = None, tank_origin: str = "new"):
    return {
        "schema": "ies.assembly",
        "schema_version": "4.0.0",
        "assembly": {"id": "vat-mini", "name": "VAT 手算装配"},
        "project_baseline": {
            "resolution": "1h",
            "start_year": 2026,
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
                "asset_origin": tank_origin,
                "properties": {
                    "rated_kw": {"value": 60, "unit": "kW"},
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
            "profile_id": "vat-demo",
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
            "import_meter": {
                "price": "heat_import",
                "instance": "heater1",
                "interface": "heat_out",
                "aggregation": "integrate_positive",
            },
            "export_meter": {
                "price": "export_ts",
                "instance": "heater1",
                "interface": "electricity_in",
                "aggregation": "integrate_positive",
            },
        },
        "demand_bindings": {},
        "planning": {
            "objective": {"sense": "maximize", "expression": NPV_MARKER},
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


def _contribution(yaml_text: str):
    result = parse_device_model_v2(yaml_load(yaml_text), file="test.yaml")
    assert result.ok, result.diagnostics
    from iesplan.modeling import build_math_contribution

    built = build_math_contribution(result.document)
    assert built.ok, built.diagnostics
    return built.contribution


def _device_docs(*, heater_yaml: str = HEATER_YAML):
    docs = {}
    for device_id, yaml_text in (
        ("acme.device.heater", heater_yaml),
        ("acme.device.tank", TANK_YAML),
        ("acme.device.load", LOAD_YAML),
        ("acme.device.grid", GRID_YAML),
    ):
        result = parse_device_model_v2(yaml_load(yaml_text), file="test.yaml")
        assert result.ok, result.diagnostics
        docs[device_id] = result.document
    return docs


def _evaluation():
    return EvaluationInput(
        evaluation_years=3,
        construction_period=1,
        operation_years=2,
        discount_rate=0.08,
        construction_payments=(
            ConstructionPayment(t=0, ratio="0.6"),
            ConstructionPayment(t=1, ratio="0.4"),
        ),
        maintenance_expenses=(
            MaintenanceExpense(year=2, amount="1200.50", currency="CNY"),
            MaintenanceExpense(year=2, amount="300", currency="CNY"),
            MaintenanceExpense(year=3, amount="500", currency="CNY"),
        ),
    )


def _price_values() -> tuple:
    """时变倒送价：2026 年 -0.05（8760 步），2027 年 +2.0。"""
    return (-0.05,) * 8760 + (2.0,) * 8760


def _inputs(
    *,
    refunds=(),
    opening="0",
    objective=None,
    heater_yaml=HEATER_YAML,
    basis="tax_inclusive",
    rules="all",
    tank_origin="new",
    maintenance_tax=True,
    payload=None,
    export_values=None,
):
    doc = _doc(tank_origin=tank_origin)
    if objective is not None:
        doc["planning"]["objective"] = objective
    models = {
        "heater1": _contribution(heater_yaml),
        "tank1": _contribution(TANK_YAML),
        "load1": _contribution(LOAD_YAML),
        "grid1": _contribution(GRID_YAML),
    }
    if payload is None:
        payload = _finance_payload(
            refunds=refunds,
            opening=opening,
            basis=basis,
            rules=rules,
            maintenance_tax=maintenance_tax,
        )
    finance = merge_effective(FinanceProfile.from_dict(payload), None)
    init_registry()
    result = validate_assembly_doc(
        doc,
        finance_snapshots={"obj-finance": finance},
        device_documents=_device_docs(heater_yaml=heater_yaml),
    )
    assert result.artifact is not None, [d.to_dict() for d in result.diagnostics]
    materialized = materialize_non_predictive_series(result.artifact, {}, operation_years=2)
    materialized = MaterializedSet(
        operation_years=materialized.operation_years,
        series=materialized.series,
        pending_predict=materialized.pending_predict,
        prices=(
            MaterializedPriceSeries(
                price_id="export_ts",
                point_count=17520,
                resolution="1h",
                unit="CNY/kWh",
                values=_price_values() if export_values is None else export_values,
                source={"kind": "price_series", "object_id": "d" * 64},
            ),
        ),
    )
    config = CalculationConfig(
        mode="planning",
        generator=GENERATOR_REF,
        solver="ies.solver.highs@1.7.2",
        precision="medium",
        time_limit_seconds=600,
        outputs=["solution"],
    )
    return result.artifact, models, materialized, finance, config, _evaluation()


# ---------------------------------------------------------------------------
# B0 语义独立手算（Decimal）：月销项/进项 → 连续结转 → 年现金
# ---------------------------------------------------------------------------


def _hand_monthly(
    *,
    tax_factor=GROSS_FACTOR,
    upfront=Decimal("6000"),
    fixed_om=Decimal("2100"),
    maintenance_tax=True,
    con_ratio=Decimal("1"),
    energy_ratio=Decimal("1"),
):
    """36 个月 (out, in) 手算（业务 Decimal，与实现公式独立书写）。

    ``tax_factor`` 为税额系数（含税 ``r/(1+r)``，不含税 ``r``），仅用于
    建设/价格/OM；维护金额为实际含税付款，恒按含税拆价计税，不随文件
    ``price_basis`` 改变。``upfront``/``fixed_om`` 为建设总额/年固定 OM
    （既有沉没时取 0，对应无建设进项、无固定 OM 进项）。``energy_ratio``
    为购能可抵比例（仅购能进项行）。
    """
    out = [Decimal("0")] * 36
    inp = [Decimal("0")] * 36
    deductible_total = upfront * tax_factor * con_ratio
    inp[0] += Decimal("0.6") * deductible_total
    inp[12] += Decimal("0.4") * deductible_total
    for index, hours in enumerate(HOURS_2026):
        month = 13 + index
        energy = Decimal("7") * Decimal(str(hours))
        inp[month - 1] += energy * Decimal("0.7") * tax_factor * energy_ratio
        inp[month - 1] += Decimal("5") * Decimal(str(hours)) * Decimal("0.01") * tax_factor
        out[month - 1] += Decimal(str(ELEC_KW)) * Decimal(str(hours)) * Decimal("-0.05") * tax_factor
    for index, hours in enumerate(HOURS_2027):
        month = 25 + index
        energy = Decimal("7") * Decimal(str(hours))
        inp[month - 1] += energy * Decimal("0.7") * tax_factor * energy_ratio
        inp[month - 1] += Decimal("5") * Decimal(str(hours)) * Decimal("0.01") * tax_factor
        out[month - 1] += Decimal(str(ELEC_KW)) * Decimal(str(hours)) * Decimal("2.0") * tax_factor
    inp[23] += fixed_om * tax_factor * Decimal("0.5")
    inp[35] += fixed_om * tax_factor * Decimal("0.5")
    if maintenance_tax:
        inp[23] += Decimal("1200.50") * GROSS_FACTOR
    return out, inp


def _hand_chain(out, inp, *, opening=Decimal("0"), claims=None):
    """B0 结算语义连续结转：paid/carry 逐月精确值。"""
    claims = claims or {}
    paids, carries = [], []
    balance = opening
    for month in range(1, 37):
        net = out[month - 1] - inp[month - 1] - balance
        paid = max(net, Decimal("0"))
        carry = max(-net, Decimal("0"))
        claim = claims.get(month, Decimal("0"))
        assert claim <= carry, (month, claim, carry)
        paids.append(paid)
        carries.append(carry)
        balance = carry - claim
    return paids, carries


def _hand_npv_with_vat(
    paids, arrivals=None, *, cash=Decimal("1"), upfront=Decimal("6000"), fixed_om=Decimal("2100")
):
    """手算 NPV：C1b 现金结构 + 月实缴/到账退款按年折现。

    ``cash`` 为不含税口径下已声明税率部分的价税合计系数（1.13）；
    未声明税率的 sys_var 可变 OM 与明确金额口径的维护不加成。
    """
    arrivals = arrivals or {}
    energy2 = Decimal("7") * Decimal("8760") * Decimal("0.7") * cash
    export2 = Decimal(str(ELEC_KW)) * Decimal("8760") * Decimal("-0.05") * cash
    energy3 = energy2
    export3 = Decimal(str(ELEC_KW)) * Decimal("8760") * Decimal("2.0") * cash
    varom = Decimal("5") * Decimal("8760") * Decimal("0.01") * cash
    varom2 = Decimal(str(ELEC_KW)) * Decimal("8760") * Decimal("0.02")
    fixed = fixed_om * cash
    cf0 = -Decimal("0.6") * upfront * cash
    cf1 = -Decimal("0.4") * upfront * cash
    cf2 = -(energy2 - export2) - varom - varom2 - fixed - Decimal("1500.50")
    cf3 = -(energy3 - export3) - varom - varom2 - fixed - Decimal("500")
    total = cf0 + cf1 / Decimal("1.08") + cf2 / Decimal("1.08") ** 2 + cf3 / Decimal("1.08") ** 3
    for month, paid in enumerate(paids, start=1):
        year = (month + 11) // 12
        total -= paid / Decimal("1.08") ** year
    for month, amount in arrivals.items():
        year = (month + 11) // 12
        total += amount / Decimal("1.08") ** year
    return total


# ---------------------------------------------------------------------------
# 求解与对照
# ---------------------------------------------------------------------------


def _solve_internal(bundle, tmp_path, *, check_soc_pin=True):
    """真实求解：返回（目标值 + 偏移，列名 → 取值，解字节）。

    列顺序与解映射走生产读取（``_mps_column_names`` / ``_parse_solution``）；
    无系数固定变量（soc_pin）在生产 writer 显式占列，交接严格等长。
    无该变量的夹具传 ``check_soc_pin=False``。
    """
    from iesplan.computation.result_adapters.highs import (
        _mps_column_names,
        _parse_solution,
    )

    mps_raw = bundle.files["input/problem.mps"]
    (tmp_path / "input").mkdir(parents=True, exist_ok=True)
    (tmp_path / "output").mkdir(parents=True, exist_ok=True)
    (tmp_path / "input" / "problem.mps").write_bytes(mps_raw)
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
    solution_raw = (tmp_path / "output" / "solution.sol").read_bytes()
    solution = _parse_solution(solution_raw)
    assert solution["model_status"] == "HighsModelStatus.kOptimal"
    order = _mps_column_names(mps_raw)
    primals = solution["primals"]
    assert len(primals) == len(order)
    values = dict(zip(order, primals, strict=True))
    if check_soc_pin:
        assert values.get("soc_pin") == pytest.approx(0.5)
    metadata = json.loads(bundle.files["input/parameters.json"].decode("utf-8"))
    return float(solution["objective"]) + metadata["objective_offset"], values, solution_raw


def test_vat_chain_matches_hand_and_npv(tmp_path) -> None:
    """建设留抵跨首运营月消耗：逐月税款/余额对手算，NPV 含税现金。"""
    artifact, models, materialized, finance, config, evaluation = _inputs()
    bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
    assert bundle.manifest.bundle["generator"] == GENERATOR_REF
    out, inp = _hand_monthly()
    paids, carries = _hand_chain(out, inp)
    # 建设期留抵跨到首运营月逐月消耗：m1 留抵 > 0，m13 仍有留抵无实缴。
    assert paids[0] == Decimal("0") and carries[0] > Decimal("0")
    assert paids[12] == Decimal("0") and carries[12] > carries[0]
    value, solved, solution_raw = _solve_internal(bundle, tmp_path)
    for month in range(1, 37):
        paid = solved[f"vat__main_pool__paid__m{month}"]
        carry = solved[f"vat__main_pool__carry__m{month}"]
        assert paid == pytest.approx(float(paids[month - 1]), rel=1e-6)
        assert carry == pytest.approx(float(carries[month - 1]), rel=1e-6)
        # 互斥精确：正负分段不靠目标压紧（本用例 NPV 下亦须互斥）。
        assert min(paid, carry) == pytest.approx(0.0, abs=1e-6)
    assert value == pytest.approx(float(_hand_npv_with_vat(paids)), rel=1e-6)
    # 生产 Adapter 交接：同一 Bundle 与同一次真实解，无列数/映射失败，
    # 目标值与固定变量恢复同源（税列不提前发布，不在此断言）。
    adapted = HIGHS_ADAPTER.adapt(
        _wrap_bundle(
            {
                "input/problem.mps": bundle.files["input/problem.mps"],
                "input/parameters.json": bundle.files["input/parameters.json"],
            }
        ),
        _receipt(),
        {SOLUTION_PATH: solution_raw},
    )
    assert adapted.execution_status == "completed"
    published = adapted.outputs["solution"]
    assert published["objective_value"] == pytest.approx(float(_hand_npv_with_vat(paids)), rel=1e-6)
    by_name = {entry["name"]: entry["value"] for entry in published["variables"]}
    assert by_name["soc_pin"] == pytest.approx(0.5)


def test_vat_non_npv_objective_keeps_exact_balances(tmp_path) -> None:
    """非 NPV 目标下税额与余额同样精确（不靠目标驱动松弛）。"""
    ordinary = {
        "sense": "minimize",
        "expression": (
            "import_meter__period_energy_purchase"
            " + export_meter__period_energy_sale"
            " + 1 year * tank1__annual_fixed_om"
            " + tank1__period_variable_om"
            " + heater1__period_variable_om"
        ),
    }
    artifact, models, materialized, finance, config, evaluation = _inputs(objective=ordinary)
    bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
    _, solved, _ = _solve_internal(bundle, tmp_path)
    out, inp = _hand_monthly()
    paids, carries = _hand_chain(out, inp)
    for month in range(1, 37):
        paid = solved[f"vat__main_pool__paid__m{month}"]
        carry = solved[f"vat__main_pool__carry__m{month}"]
        assert paid == pytest.approx(float(paids[month - 1]), rel=1e-6)
        assert carry == pytest.approx(float(carries[month - 1]), rel=1e-6)
        assert min(paid, carry) == pytest.approx(0.0, abs=1e-6)


def test_vat_refund_arrival_separate_from_confirmation(tmp_path) -> None:
    """退款确认月扣减、到账月记现金；期后到账仅应收不进现金。"""
    out, inp = _hand_monthly()
    _, carries_plain = _hand_chain(out, inp)
    claim_month, claim_amount = 20, Decimal("1500")
    assert claim_amount < carries_plain[claim_month - 1]
    # 第二笔在 25 月确认（手算可用留抵约 3921）、40 月到账（期后仅应收）。
    late_month, late_amount = 25, Decimal("2000")
    _, carries_claimed = _hand_chain(out, inp, claims={claim_month: claim_amount})
    assert late_amount < carries_claimed[late_month - 1]
    refunds = (
        {
            "period": claim_month,
            "amount": {"value": "1500", "unit": "CNY"},
            "arrival_period": 22,
        },
        {
            "period": late_month,
            "amount": {"value": "2000", "unit": "CNY"},
            "arrival_period": 40,
        },
    )
    artifact, models, materialized, finance, config, evaluation = _inputs(refunds=refunds)
    bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
    claims = {claim_month: claim_amount, late_month: late_amount}
    paids, carries = _hand_chain(out, inp, claims=claims)
    value, solved, _ = _solve_internal(bundle, tmp_path)
    for month in range(1, 37):
        assert solved[f"vat__main_pool__paid__m{month}"] == pytest.approx(float(paids[month - 1]), rel=1e-6)
        assert solved[f"vat__main_pool__carry__m{month}"] == pytest.approx(
            float(carries[month - 1]), rel=1e-6
        )
    # 到账 22（第 2 年）进现金，到账 40（期后）仅应收。
    expected = _hand_npv_with_vat(paids, {22: claim_amount})
    assert value == pytest.approx(float(expected), rel=1e-6)


def test_vat_overclaimed_refund_is_infeasible(tmp_path) -> None:
    """声明超过可用留抵即不可行，不静默截小为部分退款。"""
    out, inp = _hand_monthly()
    _, carries = _hand_chain(out, inp)
    over = (carries[19 - 1] + Decimal("1000")).quantize(Decimal("0.01"))
    assert over > carries[19 - 1] > 0
    refunds = (
        {
            "period": 19,
            "amount": {"value": str(over), "unit": "CNY"},
            "arrival_period": 20,
        },
    )
    artifact, models, materialized, finance, config, evaluation = _inputs(refunds=refunds)
    bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
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
    assert "kInfeasible" in (completed.stdout + completed.stderr) or (
        completed.returncode == 0
        and "kInfeasible" in (tmp_path / "output" / "solution.sol").read_text(encoding="utf-8")
    )


def test_vat_exclusive_basis_gross_cash_matches_hand(tmp_path) -> None:
    """不含税口径：同值陈述视作净额，基础收付按价税合计出现金。

    建设/价格/已声明 OM 加成 1.13，可抵比例不缩小付款；税基按净额
    计税；维护按明确金额口径不变。36 月链与 NPV 与独立 gross 手算一致。
    """
    artifact, models, materialized, finance, config, evaluation = _inputs(basis="tax_exclusive")
    assert finance.price_basis == "tax_exclusive"
    bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
    out, inp = _hand_monthly(tax_factor=Decimal("0.13"))
    paids, carries = _hand_chain(out, inp)
    value, solved, _ = _solve_internal(bundle, tmp_path)
    for month in range(1, 37):
        assert solved[f"vat__main_pool__paid__m{month}"] == pytest.approx(float(paids[month - 1]), rel=1e-6)
        assert solved[f"vat__main_pool__carry__m{month}"] == pytest.approx(
            float(carries[month - 1]), rel=1e-6
        )
    expected = _hand_npv_with_vat(paids, cash=Decimal("1.13"))
    assert value == pytest.approx(float(expected), rel=1e-6)


def test_vat_construction_only_exclusive_pays_declared_tax(tmp_path) -> None:
    """仅建设 VAT 时，不含税口径相对含税多付 6000×13% 的现值。

    同值陈述 6000、付款 0.6/0.4：NPV 差 = -(3600×0.13 + 2400×0.13/1.08)，
    与独立手算基线一致（无销项，实缴恒零，无 VAT 现金干扰）。
    """
    from iesplan.computation.generators.project import build_project_model

    artifact, models, materialized, finance, config, evaluation = _inputs(rules="construction_only")
    model, _, _ = build_project_model(artifact, models, materialized, finance, config, evaluation)
    inclusive_const = model.objective.constant
    artifact, models, materialized, finance, config, evaluation = _inputs(
        rules="construction_only", basis="tax_exclusive"
    )
    bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
    model, _, _ = build_project_model(artifact, models, materialized, finance, config, evaluation)
    delta_const = -(Decimal("0.6") * Decimal("6000") * Decimal("0.13")) - (
        Decimal("0.4") * Decimal("6000") * Decimal("0.13") / Decimal("1.08")
    )
    assert model.objective.constant == pytest.approx(
        float(Decimal(str(inclusive_const)) + delta_const),
        abs=1e-6,
    )
    value, solved, _ = _solve_internal(bundle, tmp_path)
    for month in range(1, 37):
        assert solved[f"vat__main_pool__paid__m{month}"] == pytest.approx(0.0, abs=1e-6)
    base = _hand_npv_with_vat([Decimal("0")] * 36)
    delta = -(Decimal("0.6") * Decimal("6000") * Decimal("0.13")) - (
        Decimal("0.4") * Decimal("6000") * Decimal("0.13") / Decimal("1.08")
    )
    assert value == pytest.approx(float(base + delta), rel=1e-6)


def test_vat_existing_fixed_om_not_credited(tmp_path) -> None:
    """既有设备沉没固定 OM：基础现金不消费，进项亦不抵扣。

    tank1 改 existing 后，建设总额与年固定 OM 同时沉没；可变 OM 仍
    真实消费。全链与 NPV 与剔除两项的独立手算一致（m24 留抵同步
    减少约 120.80，即 0.5×2100×0.13/1.13 的幽灵抵扣不再出现）。
    """
    artifact, models, materialized, finance, config, evaluation = _inputs(tank_origin="existing")
    bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
    out, inp = _hand_monthly(upfront=Decimal("0"), fixed_om=Decimal("0"))
    paids, carries = _hand_chain(out, inp)
    assert carries[0] == Decimal("0")
    value, solved, _ = _solve_internal(bundle, tmp_path)
    for month in range(1, 37):
        assert solved[f"vat__main_pool__paid__m{month}"] == pytest.approx(float(paids[month - 1]), rel=1e-6)
        assert solved[f"vat__main_pool__carry__m{month}"] == pytest.approx(
            float(carries[month - 1]), rel=1e-6
        )
    expected = _hand_npv_with_vat(paids, upfront=Decimal("0"), fixed_om=Decimal("0"))
    assert value == pytest.approx(float(expected), rel=1e-6)


def test_vat_maintenance_credit_basis_independent(tmp_path) -> None:
    """同一笔维护付款在两种文件口径下税额相等；抵扣只动税额不动现金。

    1200.50 实际含税付款、13%、抵扣 100% → 可抵税额 138.110619469，
    不随文件 ``price_basis`` 改变。用同口径内有/无维护税务引用的隔离
    差值度量（不引入其他金额变动）：m24 留抵差即该税额；NPV 差只来自
    该税额，基础现金（含 300 不可抵维护的实际付款）保持不变。
    """
    expected_credit = Decimal("1200.50") * Decimal("0.13") / Decimal("1.13")
    assert expected_credit == pytest.approx(Decimal("138.110619469"), abs=Decimal("1e-9"))
    carry_deltas = {}
    for basis in ("tax_inclusive", "tax_exclusive"):
        tax_factor = GROSS_FACTOR if basis == "tax_inclusive" else Decimal("0.13")
        cash = Decimal("1") if basis == "tax_inclusive" else Decimal("1.13")
        states = {}
        for maintenance_tax in (True, False):
            artifact, models, materialized, finance, config, evaluation = _inputs(
                basis=basis, maintenance_tax=maintenance_tax
            )
            bundle = HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
            out, inp = _hand_monthly(tax_factor=tax_factor, maintenance_tax=maintenance_tax)
            paids, carries = _hand_chain(out, inp)
            value, solved, _ = _solve_internal(bundle, tmp_path)
            for month in range(1, 37):
                assert solved[f"vat__main_pool__paid__m{month}"] == pytest.approx(
                    float(paids[month - 1]), rel=1e-6
                )
                assert solved[f"vat__main_pool__carry__m{month}"] == pytest.approx(
                    float(carries[month - 1]), rel=1e-6
                )
            assert value == pytest.approx(float(_hand_npv_with_vat(paids, cash=cash)), rel=1e-6)
            states[maintenance_tax] = (carries, value)
        delta = states[True][0][24 - 1] - states[False][0][24 - 1]
        assert float(delta) == pytest.approx(float(expected_credit), abs=1e-6)
        carry_deltas[basis] = delta
    assert carry_deltas["tax_exclusive"] == pytest.approx(carry_deltas["tax_inclusive"], abs=1e-6)


HEATER_UNBOUNDED_YAML = """
schema: ies.device-model
schema_version: "2.2.0"
device: {id: acme.device.heater, names: {zh-CN: 加热器, en-US: Heater}}
properties:
  efficiency: {value: 0.9, unit: "1", valid_range: {minimum: 0, maximum: 1}}
interfaces:
  electricity_in:
    type: in
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: null}
  heat_out:
    type: out
    carrier: heat
    unit: kW
    valid_range: {minimum: 0, maximum: null}
equations:
  variables: {}
  relations:
    - id: conversion
      expression: "heat_out[t] = electricity_in[t] * efficiency"
"""


def test_vat_unbounded_flow_is_unsupported() -> None:
    """缺有限界即明确不支持：无界交易流无法精确互斥，不猜大常量。"""
    artifact, models, materialized, finance, config, evaluation = _inputs(heater_yaml=HEATER_UNBOUNDED_YAML)
    with pytest.raises(LoweringUnsupportedError):
        build_project_model(artifact, models, materialized, finance, config, evaluation)


def test_generate_accepts_income_tax_with_vat() -> None:
    """所得税与 VAT 混合可生成：3.3.0 同次建模，不再整单拒绝。

    所得税规则引用本夹具真实价格/分量（公开入口完整通过）；未知类型
    仍在唯一建模边界拒绝（见 C1b 未知类型用例）。
    """
    payload = _finance_payload()
    payload["tax_rules"]["income_tax"] = {
        "kind": "income_tax",
        "label_ref": "vat_demo",
        "rate": {"value": "0.25", "unit": "1"},
        "revenue": ["export_ts"],
        "energy_cost": ["heat_import"],
        "annual_om": ["sys1.annual_fixed_om"],
        "variable_om": ["sys1.period_variable_om"],
        "deductible_maintenance": {"mode": "explicit_items", "items": []},
        "depreciation": [],
        "loss_carryforward_years": 5,
        "opening_losses": [],
    }
    artifact, models, materialized, _, config, evaluation = _inputs()
    finance = merge_effective(FinanceProfile.from_dict(payload), None)
    from iesplan.finance import IncomeTaxRule

    assert any(isinstance(rule, IncomeTaxRule) for rule in finance.tax_rules.values())
    bundle = HIGHS_MILP_PROVIDER.generate(
        artifact, models, materialized, finance, config, evaluation
    )
    assert bundle.manifest.bundle["generator"] == GENERATOR_REF
