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
from test_npv_objective import _mps_column_order, _solution_primals

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


def _finance_payload(*, refunds=(), opening="0"):
    return {
        "schema": "ies.finance-profile",
        "schema_version": "3.0.0",
        "profile": {
            "id": "vat-demo",
            "region": "CN-North",
            "currency": "CNY",
            "base_year": 2025,
            "price_basis": "tax_inclusive",
            "cost_method": "fixed_plus_linear",
        },
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


HEATER_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
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
schema_version: "2.0.0"
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
    source: {mode: constant, value: 5}
equations:
  variables:
    soc: {unit: "1", valid_range: {minimum: 0, maximum: 1}, initial: {property_ref: soc0}}
  relations:
    - id: soc_step
      expression: "soc[t] = soc[t-1] + flow[t] * step_c"
"""

LOAD_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
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
    source: {mode: constant, value: 7}
equations:
  variables: {}
  relations:
    - id: serve
      expression: "heat_in[t] = heat_demand[t]"
"""

GRID_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
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


def _doc(*, variables: dict | None = None):
    return {
        "schema": "ies.assembly",
        "schema_version": "3.0.0",
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
                "asset_origin": "new",
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


def _inputs(*, refunds=(), opening="0", objective=None, heater_yaml=HEATER_YAML):
    doc = _doc()
    if objective is not None:
        doc["planning"]["objective"] = objective
    models = {
        "heater1": _contribution(heater_yaml),
        "tank1": _contribution(TANK_YAML),
        "load1": _contribution(LOAD_YAML),
        "grid1": _contribution(GRID_YAML),
    }
    finance = merge_effective(
        FinanceProfile.from_dict(_finance_payload(refunds=refunds, opening=opening)),
        None,
    )
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
                values=_price_values(),
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


def _hand_monthly():
    """36 个月 (out, in) 手算（业务 Decimal，与实现公式独立书写）。"""
    out = [Decimal("0")] * 36
    inp = [Decimal("0")] * 36
    deductible_total = Decimal("6000") * GROSS_FACTOR
    inp[0] += Decimal("0.6") * deductible_total
    inp[12] += Decimal("0.4") * deductible_total
    for index, hours in enumerate(HOURS_2026):
        month = 13 + index
        energy = Decimal("7") * Decimal(str(hours))
        inp[month - 1] += energy * Decimal("0.7") * GROSS_FACTOR
        inp[month - 1] += Decimal("5") * Decimal(str(hours)) * Decimal("0.01") * GROSS_FACTOR
        out[month - 1] += Decimal(str(ELEC_KW)) * Decimal(str(hours)) * Decimal("-0.05") * GROSS_FACTOR
    for index, hours in enumerate(HOURS_2027):
        month = 25 + index
        energy = Decimal("7") * Decimal(str(hours))
        inp[month - 1] += energy * Decimal("0.7") * GROSS_FACTOR
        inp[month - 1] += Decimal("5") * Decimal(str(hours)) * Decimal("0.01") * GROSS_FACTOR
        out[month - 1] += Decimal(str(ELEC_KW)) * Decimal(str(hours)) * Decimal("2.0") * GROSS_FACTOR
    inp[23] += Decimal("2100") * GROSS_FACTOR * Decimal("0.5")
    inp[35] += Decimal("2100") * GROSS_FACTOR * Decimal("0.5")
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


def _hand_npv_with_vat(paids, arrivals=None):
    """手算 NPV：C1b 现金结构 + 月实缴/到账退款按年折现。"""
    arrivals = arrivals or {}
    energy2 = Decimal("7") * Decimal("8760") * Decimal("0.7")
    export2 = Decimal(str(ELEC_KW)) * Decimal("8760") * Decimal("-0.05")
    energy3 = energy2
    export3 = Decimal(str(ELEC_KW)) * Decimal("8760") * Decimal("2.0")
    varom = Decimal("5") * Decimal("8760") * Decimal("0.01")
    varom2 = Decimal(str(ELEC_KW)) * Decimal("8760") * Decimal("0.02")
    fixed = Decimal("2100")
    cf0 = -Decimal("3600")
    cf1 = -Decimal("2400")
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


def _mps_bound_order(mps_text: str) -> list[str]:
    """MPS BOUNDS 段变量名顺序（writer 与 COLUMNS 同序，可作补列对齐）。"""
    names: list[str] = []
    section = None
    for line in mps_text.splitlines():
        tag = line.strip()
        if tag in ("NAME", "ROWS", "COLUMNS", "RHS", "BOUNDS", "RANGES", "ENDATA"):
            section = tag
            continue
        if section != "BOUNDS":
            continue
        parts = line.split()
        if len(parts) >= 3 and parts[2] not in names:
            names.append(parts[2])
    return names


def _solve_internal(bundle, tmp_path):
    """真实求解：返回（目标值 + 偏移，列名 → 取值）。"""
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
    order = _mps_column_order(mps_text)
    # 无条目变量（如固定 soc_pin）在 COLUMNS 被省略、仅 BOUNDS 留 FX 界；
    # HiGHS 按 BOUNDS 补列并附于解末尾，此处按 BOUNDS 显式对齐，不猜顺序。
    bound_names = _mps_bound_order(mps_text)
    extra = [name for name in bound_names if name not in set(order)]
    assert len(primals) == len(order) + len(extra)
    values = dict(zip(order, primals[: len(order)], strict=True))
    for name, val in zip(extra, primals[len(order) :], strict=True):
        values[name] = val
    if extra:
        assert values.get("soc_pin") == pytest.approx(0.5)
    metadata = json.loads(bundle.files["input/parameters.json"].decode("utf-8"))
    return float(facts["objective_value"]) + metadata["objective_offset"], values


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
    value, solved = _solve_internal(bundle, tmp_path)
    for month in range(1, 37):
        paid = solved[f"vat__main_pool__paid__m{month}"]
        carry = solved[f"vat__main_pool__carry__m{month}"]
        assert paid == pytest.approx(float(paids[month - 1]), rel=1e-6)
        assert carry == pytest.approx(float(carries[month - 1]), rel=1e-6)
        # 互斥精确：正负分段不靠目标压紧（本用例 NPV 下亦须互斥）。
        assert min(paid, carry) == pytest.approx(0.0, abs=1e-6)
    assert value == pytest.approx(float(_hand_npv_with_vat(paids)), rel=1e-6)


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
    _, solved = _solve_internal(bundle, tmp_path)
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
    value, solved = _solve_internal(bundle, tmp_path)
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


HEATER_UNBOUNDED_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
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


def test_generate_rejects_income_tax() -> None:
    """所得税关系明确拒绝，不与 VAT 混合半生成。"""
    from test_config_revisions import _tax_profile_payload

    from iesplan.computation.errors import ComputationUnavailableError

    artifact, models, materialized, _, config, evaluation = _inputs()
    finance = merge_effective(FinanceProfile.from_dict(_tax_profile_payload()), None)
    from iesplan.finance import IncomeTaxRule

    assert any(isinstance(rule, IncomeTaxRule) for rule in finance.tax_rules.values())
    with pytest.raises(ComputationUnavailableError):
        HIGHS_MILP_PROVIDER.generate(artifact, models, materialized, finance, config, evaluation)
