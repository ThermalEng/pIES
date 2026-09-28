"""项目级线性模型与 MPS writer 契约测试(0.8f-B)。

覆盖最小真实装配的变量、目标、技术约束、连接守恒、规划约束、
财务/计费项、t-1 初值、Big-M binary、确定性字节与非仿射拒绝。
只测公开行为，不测私有实现细节。
"""

from __future__ import annotations

import json

import pytest  # type: ignore[import-untyped]

from iesplan.assembly.contracts import ValidatedAssemblyArtifact, ValidationReceipt
from iesplan.computation import (
    CalculationConfig,
    build_project_model,
    materialize_non_predictive_series,
    write_mps,
)
from iesplan.computation.generators.linear import (
    LinearConstraint,
    LinearExpression,
    LinearModel,
    LinearVariable,
    LoweringUnsupportedError,
)
from iesplan.core.yamlmini import load as yaml_load
from iesplan.devices.parser2 import parse_device_model_v2
from iesplan.finance import FinanceProfile, merge_effective
from iesplan.modeling import build_math_contribution

HEATER_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
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
schema_version: "2.0.0"
device: {id: acme.device.tank, names: {zh-CN: 罐, en-US: Tank}}
properties:
  soc0: {value: 0.5, unit: "1", valid_range: {minimum: 0, maximum: 1}}
  rated_kw: {value: 50, unit: kW, valid_range: {minimum: 0, maximum: 1000}}
  step_c: {value: 1, unit: "1", valid_range: {minimum: 0, maximum: 10}}
interfaces:
  flow: {type: predefined, carrier: heat, unit: kW, valid_range: {minimum: 0, maximum: null}, source: {mode: constant, value: 5}}
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
  heat_in: {type: in, carrier: heat, unit: kW, valid_range: {minimum: 0, maximum: null}}
  heat_demand: {type: predefined, carrier: heat, unit: kW, valid_range: {minimum: 0, maximum: null}, source: {mode: constant, value: 7}}
equations:
  variables: {}
  relations:
    - id: serve
      expression: "heat_in[t] = heat_demand[t]"
"""

HUB_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.hub, names: {zh-CN: 枢纽, en-US: Hub}}
properties: {}
interfaces:
  charge: {type: in, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: 100}}
  discharge: {type: in, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: 100}}
equations:
  variables: {}
  relations:
    - id: exclusivity
      expression: "charge[t] * discharge[t] = 0"
"""

BILINEAR_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.bl, names: {zh-CN: 双线性, en-US: Bilinear}}
properties: {}
interfaces:
  x: {type: in, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
  y: {type: out, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
  z: {type: in, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
equations:
  variables: {}
  relations:
    - id: bilin
      expression: "y[t] = x[t] * z[t]"
"""

def _finance_payload(direction: str = "purchase"):
    return {
        "schema": "ies.finance-profile",
        "schema_version": "1.0.0",
        "profile": {
            "id": "cn-north-demo",
            "region": "CN-North",
            "currency": "CNY",
            "base_year": 2025,
            "price_basis": "tax_inclusive",
            "cost_method": "fixed_plus_linear",
        },
        "finance_types": {
            "tank_sys": {
                "annual_fixed_om": {
                    "linear": {"cap": {"unit_cost": {"value": "35", "unit": "CNY/kW/a"}}}
                },
                "period_variable_om": {
                    "linear": {"thr": {"unit_cost": {"value": "0.01", "unit": "CNY/kWh"}}}
                },
            }
        },
        "energy_prices": {
            "heat_import": {
                "carrier": "heat",
                "direction": direction,
                "kind": "constant",
                "value": {"value": "0.7", "unit": "CNY/kWh"},
            }
        },
    }


FINANCE_PAYLOAD = _finance_payload()

POINTS = 8760


def _contribution(yaml_text: str):
    result = parse_device_model_v2(yaml_load(yaml_text), file="test.yaml")
    assert result.ok, result.diagnostics
    built = build_math_contribution(result.document)
    assert built.ok, built.diagnostics
    return built.contribution


def _artifact_doc(*, bilinear: bool = False) -> dict:
    devices: dict = {
        "heater1": {
            "definition": {"id": "acme.device.heater"},
            "asset_origin": "existing",
            "properties": {},
            "predefined_interfaces": {},
        },
        "tank1": {
            "definition": {"id": "acme.device.tank"},
            "asset_origin": "new",
            "properties": {"rated_kw": {"value": 60, "unit": "kW"}},
            "predefined_interfaces": {"flow": {"mode": "constant", "value": 5}},
        },
        "load1": {
            "definition": {"id": "acme.device.load"},
            "asset_origin": "existing",
            "properties": {},
            "predefined_interfaces": {
                "heat_demand": {"mode": "constant", "value": 7}
            },
        },
    }
    bindings: dict = {
        "heater1": {"type": "none"},
        "tank1": {
            "type": "costed",
            "finance_type": "tank_sys",
            "drivers": {
                "cap": {"property": "rated_kw"},
                "thr": {"interface": "flow", "aggregation": "integrate_positive"},
            },
        },
        "load1": {"type": "none"},
    }
    if bilinear:
        devices["bl1"] = {
            "definition": {"id": "acme.device.bl"},
            "asset_origin": "existing",
            "properties": {},
            "predefined_interfaces": {},
        }
        bindings["bl1"] = {"type": "none"}
    else:
        devices["hub1"] = {
            "definition": {"id": "acme.device.hub"},
            "asset_origin": "existing",
            "properties": {},
            "predefined_interfaces": {},
        }
        bindings["hub1"] = {"type": "none"}
    return {
        "schema": "ies.assembly",
        "schema_version": "2.0.0",
        "assembly": {"id": "mini", "name": "最小真实装配"},
        "project_baseline": {
            "resolution": "1h", "leap_year": False, "scenario_mode": "single",
        },
        "devices": devices,
        "connections": {
            "link1": {"from": "heater1.heat_out", "to": "load1.heat_in"},
        },
        "finance": {
            "ref": {
                "kind": "object",
                "object_id": "obj-finance",
                "media_type": "application/yaml",
            },
            "profile_id": "cn-north-demo",
        },
        "finance_binding": {"instances": bindings},
        "tariff_bindings": {
            "import_meter": {
                "price": "heat_import",
                "instance": "load1",
                "interface": "heat_in",
                "aggregation": "integrate_positive",
            },
        },
        "planning": {
            "objective": {
                "sense": "minimize",
                "expression": (
                    "1 year * tank1__annual_fixed_om"
                    " + tank1__period_variable_om"
                    " + import_meter__period_energy_purchase"
                ),
            },
            "variables": {
                "tank_cap": {
                    "device_ref": "tank1",
                    "parameter": "rated_kw",
                    "lower_bound": "0",
                    "upper_bound": "500",
                    "unit": "kW",
                },
            },
            "constraints": {
                "om_cap": {
                    "type": "capacity_limit",
                    "expression": "tank_cap <= 400 kW",
                },
                "om_off": {
                    "type": "ratio",
                    "expression": "tank_cap >= 0 kW",
                    "enabled": False,
                },
            },
        },
    }


def _inputs(
    *,
    bilinear: bool = False,
    tariff_direction: str = "purchase",
    objective: str | None = None,
):
    doc = _artifact_doc(bilinear=bilinear)
    if objective is not None:
        doc["planning"]["objective"]["expression"] = objective
    artifact = ValidatedAssemblyArtifact(
        canonical_text=json.dumps(doc), receipt=ValidationReceipt(),
    )
    yamls = {
        "heater1": HEATER_YAML,
        "tank1": TANK_YAML,
        "load1": LOAD_YAML,
    }
    yamls["bl1" if bilinear else "hub1"] = BILINEAR_YAML if bilinear else HUB_YAML
    models = {
        inst: _contribution(text)
        for inst, text in yamls.items()
        if inst in doc["devices"]
    }
    finance = merge_effective(
        FinanceProfile.from_dict(_finance_payload(tariff_direction)), None
    )
    result = materialize_non_predictive_series(artifact, {})
    config = CalculationConfig(
        mode="planning",
        generator="acme.generator@1.0.0",
        solver="ies.solver.highs@1.7.2",
        precision="medium",
        time_limit_seconds=600,
        outputs=["solution"],
    )
    return artifact, models, result.series, finance, config


def _build():
    artifact, models, series, finance, config = _inputs()
    return build_project_model(artifact, models, series, finance, config)


@pytest.fixture(scope="module")
def built():
    """模块内共享一次完整构建（确定性由专测另行双构建验证）。"""
    return _build()


def _row(model, name):
    for row in model.constraints:
        if row.name == name:
            return row
    raise AssertionError(f"约束行缺失: {name}")


def _coeffs(expr) -> dict:
    return {key[0]: coeff for key, coeff in expr.terms}


def test_project_inventory_counts(built) -> None:
    model, units, _ = built
    assert len(model.variables) == 61321
    assert len(model.constraints) == 52561
    assert model.sense == "minimize"
    assert "tank_cap" in units
    assert units["tank_cap"] == "kW"
    assert units["heater1__electricity_in__t0"] == "kW"
    assert not any(name.endswith("__mutex") for name in units)


def _build_with_objective(expression: str):
    """以替换目标表达式的真实装配走完整构造（公开行为）。"""
    artifact, models, series, finance, config = _inputs(objective=expression)
    return build_project_model(artifact, models, series, finance, config)


def test_objective_dimensionless_ratio_reports_unit_one() -> None:
    """tank_cap / 1 kW：无量纲目标记 "1"，系数为真实降级值。"""
    model, _, objective_unit = _build_with_objective("tank_cap / 1 kW")
    assert objective_unit == "1"
    assert _coeffs(model.objective) == {"tank_cap": pytest.approx(0.001)}


def test_objective_constant_power_reports_si() -> None:
    """纯常量 1 kW：功率取 SI 基准 W，常数即内部值。"""
    model, _, objective_unit = _build_with_objective("1 kW")
    assert objective_unit == "W"
    assert model.objective.terms == ()
    assert model.objective.constant == pytest.approx(1000.0)


def test_objective_power_var_reports_si() -> None:
    """kW 变量目标：功率取 SI 基准 W，系数为真实降级值。"""
    model, _, objective_unit = _build_with_objective("tank_cap")
    assert objective_unit == "W"
    assert _coeffs(model.objective) == {"tank_cap": pytest.approx(1.0)}


def test_objective_scaled_power_keeps_si() -> None:
    """tank_cap * 2：无量纲常量缩放不改变量纲与报告单位。"""
    model, _, objective_unit = _build_with_objective("tank_cap * 2")
    assert objective_unit == "W"
    assert _coeffs(model.objective) == {"tank_cap": pytest.approx(2.0)}


def test_objective_exotic_dimension_rejected() -> None:
    """复合量纲（CNY/年）无可报告业务单位，超出首版能力明确拒绝。"""
    artifact, models, series, finance, config = _inputs(
        objective="tank1__annual_fixed_om"
    )
    with pytest.raises(LoweringUnsupportedError):
        build_project_model(artifact, models, series, finance, config)


def test_objective_inverse_power_rejected() -> None:
    """1 / 1 kW 根量纲为 power^-1（非无量纲），明确拒绝。"""
    artifact, models, series, finance, config = _inputs(objective="1 / 1 kW")
    with pytest.raises(LoweringUnsupportedError):
        build_project_model(artifact, models, series, finance, config)


def test_objective_power_per_year_rejected() -> None:
    """tank_cap / 1 year 根量纲为 power·time^-1（非 power），明确拒绝。"""
    artifact, models, series, finance, config = _inputs(objective="tank_cap / 1 year")
    with pytest.raises(LoweringUnsupportedError):
        build_project_model(artifact, models, series, finance, config)


def test_objective_finance_and_tariff_terms(built) -> None:
    model, _, objective_unit = built
    coeffs = _coeffs(model.objective)
    # 年费率 SI 归一后 × 1 year(31536000s) → 0.035 CNY/W
    assert coeffs["tank_cap"] == pytest.approx(0.035, rel=1e-9)
    # 购能价 0.7 CNY/kWh → 每瓦负荷流量 0.0007 CNY
    assert coeffs["load1__heat_in__t0"] == pytest.approx(0.0007, rel=1e-9)
    assert coeffs["load1__heat_in__t8759"] == pytest.approx(0.0007, rel=1e-9)
    # 周期可变 O&M 全落常数偏移：8760 × 5000W × 3600s × 0.01/3.6e6
    _, offset = write_mps(model)
    assert offset == pytest.approx(438.0, rel=1e-9)
    # 1 year * annual_fixed_om 与附加金额项合并后仍为金额，取财务币种。
    assert objective_unit == "CNY"


def test_tech_rows_balance_and_initial(built) -> None:
    model, _, _ = built
    row = _row(model, "heater1__conversion__t0")
    assert _coeffs(row.expression) == {
        "heater1__heat_out__t0": 1.0,
        "heater1__electricity_in__t0": pytest.approx(-0.9),
    }
    balance = _row(model, "bus__heat__0__t0")
    assert _coeffs(balance.expression) == {
        "heater1__heat_out__t0": 1.0,
        "load1__heat_in__t0": -1.0,
    }
    serve = _row(model, "load1__serve__t0")
    assert _coeffs(serve.expression) == {"load1__heat_in__t0": 1.0}
    assert serve.expression.constant == pytest.approx(-7000.0)
    first = _row(model, "tank1__soc_step__t0")
    assert _coeffs(first.expression) == {"tank1__soc__t0": 1.0}
    assert first.expression.constant == pytest.approx(-5000.5)
    later = _row(model, "tank1__soc_step__t3")
    assert _coeffs(later.expression) == {
        "tank1__soc__t3": 1.0,
        "tank1__soc__t2": -1.0,
    }
    planning = _row(model, "om_cap")
    assert (planning.sense, planning.rhs) == ("le", 0.0)
    assert _coeffs(planning.expression) == {"tank_cap": 1.0}
    assert planning.expression.constant == pytest.approx(-400000.0)
    assert "om_off" not in {row.name for row in model.constraints}


def test_mutex_binary_and_bounds(built) -> None:
    model, _, _ = built
    by_name = {var.name: var for var in model.variables}
    binary = by_name["hub1__exclusivity__mutex__t5"]
    assert (binary.lower, binary.upper, binary.kind) == (0.0, 1.0, "binary")
    rows = {row.name: row for row in model.constraints}
    assert _coeffs(rows["hub1__exclusivity__mutex_x__t5"].expression) == {
        "hub1__charge__t5": 1.0,
        "hub1__exclusivity__mutex__t5": -100000.0,
    }
    assert rows["hub1__exclusivity__mutex_y__t5"].rhs == pytest.approx(100000.0)


def test_mps_deterministic_bytes() -> None:
    first, _, _ = _build()
    second, _, _ = _build()
    first_text, first_offset = write_mps(first)
    second_text, second_offset = write_mps(second)
    assert first_text == second_text
    assert first_offset == pytest.approx(second_offset)
    assert first_text.startswith("NAME          IES_MILP\nROWS\n N  OBJ\n")
    assert "\nOBJSENSE\n MIN\nENDATA\n" in first_text
    assert " BV       BND1  hub1__exclusivity__mutex__t5\n" in first_text
    assert "bus__heat__0__t0" in first_text


SRC_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.src, names: {zh-CN: 电源, en-US: Source}}
properties: {}
interfaces:
  power_out: {type: out, carrier: electricity, unit: kW, valid_range: {minimum: 0, maximum: null}}
equations:
  variables:
    dispatch: {unit: kW, valid_range: {minimum: 0, maximum: null}}
  relations:
    - id: follow
      expression: "power_out[t] = dispatch[t]"
"""

GEN_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.gen, names: {zh-CN: 机组, en-US: Unit}}
properties:
  cap_kw: {value: 60, unit: kW, valid_range: {minimum: 0, maximum: 1000}}
  floor_kw: {value: 5, unit: kW, valid_range: {minimum: 0, maximum: 1000}}
  spare_kw: {value: 20, unit: kW, valid_range: {minimum: 0, maximum: 1000}}
interfaces:
  power_out: {type: out, carrier: electricity, unit: kW, valid_range: {minimum: {property_ref: floor_kw}, maximum: {property_ref: cap_kw}}}
equations:
  variables:
    dispatch: {unit: kW, valid_range: {minimum: 0, maximum: {property_ref: cap_kw}}}
    spare: {unit: kW, valid_range: {minimum: 0, maximum: {property_ref: spare_kw}}}
  relations:
    - id: follow
      expression: "power_out[t] = dispatch[t]"
"""


def _island_doc() -> dict:
    devices = {}
    for inst, did in (
        ("srcA", "acme.device.src"), ("srcB", "acme.device.src"),
        ("heaterA", "acme.device.heater"), ("heaterB", "acme.device.heater"),
        ("load1", "acme.device.load"),
    ):
        devices[inst] = {
            "definition": {"id": did},
            "asset_origin": "existing",
            "properties": {},
            "predefined_interfaces": (
                {"heat_demand": {"mode": "constant", "value": 7}}
                if inst == "load1" else {}
            ),
        }
    return {
        "schema": "ies.assembly",
        "schema_version": "2.0.0",
        "assembly": {"id": "islands", "name": "双电岛共热网"},
        "project_baseline": {
            "resolution": "1h", "leap_year": False, "scenario_mode": "single",
        },
        "devices": devices,
        "connections": {
            "island1": {"from": "srcA.power_out", "to": "heaterA.electricity_in"},
            "island2": {"from": "srcB.power_out", "to": "heaterB.electricity_in"},
            "heat1": {"from": "heaterA.heat_out", "to": "load1.heat_in"},
            "heat2": {"from": "heaterB.heat_out", "to": "load1.heat_in"},
        },
        "finance": {
            "ref": {
                "kind": "object",
                "object_id": "obj-finance",
                "media_type": "application/yaml",
            },
            "profile_id": "cn-north-demo",
        },
        "finance_binding": {
            "instances": {inst: {"type": "none"} for inst in devices}
        },
        "tariff_bindings": {
            "import_meter": {
                "price": "heat_import",
                "instance": "load1",
                "interface": "heat_in",
                "aggregation": "integrate_positive",
            },
        },
        "planning": {
            "objective": {
                "sense": "minimize",
                "expression": "import_meter__period_energy_purchase",
            },
            "variables": {},
            "constraints": {},
        },
    }


def _island_inputs():
    doc = _island_doc()
    artifact = ValidatedAssemblyArtifact(
        canonical_text=json.dumps(doc), receipt=ValidationReceipt(),
    )
    yamls = {
        "srcA": SRC_YAML, "srcB": SRC_YAML,
        "heaterA": HEATER_YAML, "heaterB": HEATER_YAML,
        "load1": LOAD_YAML,
    }
    models = {inst: _contribution(text) for inst, text in yamls.items()}
    finance = merge_effective(FinanceProfile.from_dict(FINANCE_PAYLOAD), None)
    result = materialize_non_predictive_series(artifact, {})
    config = CalculationConfig(
        mode="planning",
        generator="acme.generator@1.0.0",
        solver="ies.solver.highs@1.7.2",
        precision="medium",
        time_limit_seconds=600,
        outputs=["solution"],
    )
    return artifact, models, result.series, finance, config


def test_buses_split_per_carrier_components() -> None:
    """两电岛经各自转换设备共用热网：electricity 仍两条母线行。

    旧全局连通实现会把五台设备合并，只产生一条 electricity 行。
    """
    artifact, models, series, finance, config = _island_inputs()
    model, _, _ = build_project_model(artifact, models, series, finance, config)
    by_name = {row.name: row for row in model.constraints}
    elec = {
        name: by_name[name] for name in by_name
        if name.startswith("bus__electricity__") and name.endswith("__t0")
    }
    assert len(elec) == 2
    varsets = [_coeffs(row.expression) for row in elec.values()]
    assert set(varsets[0]) & set(varsets[1]) == set()
    flat = {}
    for coeffs in varsets:
        flat.update(coeffs)
    assert flat["srcA__power_out__t0"] == 1.0
    assert flat["heaterA__electricity_in__t0"] == -1.0
    assert flat["srcB__power_out__t0"] == 1.0
    assert flat["heaterB__electricity_in__t0"] == -1.0
    heat = {
        name: by_name[name] for name in by_name
        if name.startswith("bus__heat__") and name.endswith("__t0")
    }
    assert len(heat) == 1
    assert _coeffs(next(iter(heat.values())).expression) == {
        "heaterA__heat_out__t0": 1.0,
        "heaterB__heat_out__t0": 1.0,
        "load1__heat_in__t0": -1.0,
    }


def _gen_inputs():
    doc = _island_doc()
    doc["assembly"] = {"id": "gen", "name": "规划接管容量"}
    doc["devices"] = {
        "gen1": {
            "definition": {"id": "acme.device.gen"},
            "asset_origin": "new",
            "properties": {},
            "predefined_interfaces": {},
        },
    }
    doc["connections"] = {}
    doc["finance_binding"] = {
        "instances": {
            "gen1": {
                "type": "costed",
                "finance_type": "tank_sys",
                "drivers": {
                    "cap": {"property": "cap_kw"},
                    "thr": {
                        "interface": "power_out",
                        "aggregation": "integrate_positive",
                    },
                },
            }
        }
    }
    doc["tariff_bindings"] = {}
    doc["planning"] = {
        "objective": {
            "sense": "minimize",
            "expression": (
                "1 year * gen1__annual_fixed_om"
                " + gen1__period_variable_om"
            ),
        },
        "variables": {
            "gen_cap": {
                "device_ref": "gen1",
                "parameter": "cap_kw",
                "lower_bound": "0",
                "upper_bound": "500",
                "unit": "kW",
            },
            "gen_floor": {
                "device_ref": "gen1",
                "parameter": "floor_kw",
                "lower_bound": "10",
                "upper_bound": "500",
                "unit": "kW",
            },
            "spare_cap": {
                "device_ref": "gen1",
                "parameter": "spare_kw",
                "lower_bound": "0",
                "upper_bound": None,
                "unit": "kW",
            },
        },
        "constraints": {},
    }
    artifact = ValidatedAssemblyArtifact(
        canonical_text=json.dumps(doc), receipt=ValidationReceipt(),
    )
    models = {"gen1": _contribution(GEN_YAML)}
    finance = merge_effective(FinanceProfile.from_dict(FINANCE_PAYLOAD), None)
    result = materialize_non_predictive_series(artifact, {})
    config = CalculationConfig(
        mode="planning",
        generator="acme.generator@1.0.0",
        solver="ies.solver.highs@1.7.2",
        precision="medium",
        time_limit_seconds=600,
        outputs=["solution"],
    )
    return artifact, models, result.series, finance, config


def test_planning_takeover_bounds_follow_planning_var() -> None:
    """被规划接管的额定界随规划变量：实例 60kW 不得锁死流量。

    旧实现用实例常数作界且无容量行；新行为静态界取规划界，
    并为每个 step 生成对规划变量的容量约束。缺失安全界保持 None。
    """
    artifact, models, series, finance, config = _gen_inputs()
    model, _, _ = build_project_model(artifact, models, series, finance, config)
    by_var = {var.name: var for var in model.variables}
    assert by_var["gen1__power_out__t0"].upper == pytest.approx(500000.0)
    assert by_var["gen1__power_out__t0"].lower == pytest.approx(10000.0)
    assert by_var["gen1__dispatch__t0"].upper == pytest.approx(500000.0)
    assert by_var["gen1__spare__t0"].upper is None
    rows = {row.name: row for row in model.constraints}
    cap = rows["gen1__power_out__plan_max__t0"]
    assert (cap.sense, cap.rhs) == ("le", 0.0)
    assert _coeffs(cap.expression) == {
        "gen1__power_out__t0": 1.0, "gen_cap": -1.0,
    }
    floor = rows["gen1__power_out__plan_min__t0"]
    assert (floor.sense, floor.rhs) == ("ge", 0.0)
    assert _coeffs(floor.expression) == {
        "gen1__power_out__t0": 1.0, "gen_floor": -1.0,
    }
    inner = rows["gen1__dispatch__plan_max__t0"]
    assert _coeffs(inner.expression) == {
        "gen1__dispatch__t0": 1.0, "gen_cap": -1.0,
    }
    assert "gen1__spare__plan_max__t0" in rows


def test_sale_tariff_sign_flips() -> None:
    """售电方向记账为负（与购电对称）。"""
    artifact, models, series, finance, config = _inputs(tariff_direction="sale")
    model, _, _ = build_project_model(artifact, models, series, finance, config)
    coeffs = _coeffs(model.objective)
    assert coeffs["load1__heat_in__t0"] == pytest.approx(-0.0007, rel=1e-9)


def test_bilinear_unsupported() -> None:
    artifact, models, series, finance, config = _inputs(bilinear=True)
    with pytest.raises(LoweringUnsupportedError):
        build_project_model(artifact, models, series, finance, config)


def test_model_rejects_bad_sense() -> None:
    with pytest.raises(ValueError, match="目标方向"):
        LinearModel(sense="sideways")


def test_mps_tiny_maximize_model_exact_text() -> None:
    model = LinearModel(
        variables=(
            LinearVariable(name="x", lower=0.0, upper=10.0),
            LinearVariable(name="b", lower=0.0, upper=1.0, kind="binary"),
            LinearVariable(name="free", lower=None, upper=None),
        ),
        constraints=(
            LinearConstraint(
                name="cap",
                expression=LinearExpression(
                    terms=((("x", 0), 2.0), (("b", 0), 5.0)),
                    constant=-1.0,
                ),
                sense="le",
                rhs=4.0,
            ),
        ),
        objective=LinearExpression(
            terms=((("x", 0), 1.0), (("b", 0), -3.0)), constant=7.0,
        ),
        sense="maximize",
    )
    text, offset = write_mps(model)
    assert offset == 7.0
    assert text == (
        "NAME          IES_MILP\n"
        "ROWS\n"
        " N  OBJ\n"
        " L  cap\n"
        "COLUMNS\n"
        "    b  OBJ  -3.0  cap  5.0\n"
        "    x  OBJ  1.0  cap  2.0\n"
        "RHS\n"
        "    RHS1  cap  5.0\n"
        "BOUNDS\n"
        " BV       BND1  b\n"
        " FR       BND1  free\n"
        " LO       BND1  x  0.0\n"
        " UP       BND1  x  10.0\n"
        "OBJSENSE\n"
        " MAX\n"
        "ENDATA\n"
    )
