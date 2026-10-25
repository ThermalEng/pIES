"""ISSUE-02: 独立合成单位装机出力曲线，走公开模型/装配/生成/真实求解链。

真实 HiGHS 用例只在仓库 Docker CI 环境执行；本测试不需要气象输入。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from iesplan.assembly import validate_assembly_text
from iesplan.computation import (
    HIGHS_ADAPTER_PATH,
    HIGHS_MILP_PROVIDER,
    CalculationConfig,
    build_project_model,
    build_registry,
    materialize_non_predictive_series,
)
from iesplan.computation.generators.highs_milp import GENERATOR_REF, SOLVER_REF
from iesplan.core.yamlmini import load as yaml_load
from iesplan.devices import (
    get_device,
    init_registry,
    instantiate_template,
    parse_device_model_v2,
    to_dict,
)
from iesplan.finance import EffectiveFinanceConfig, EvaluationInput
from iesplan.modeling import build_math_contribution

CATALOG = Path(__file__).resolve().parents[1] / "iesplan/devices/catalog"
CURVE_ID = "ies.device.power_curve"
SYNTHETIC_DAY = (0.0, 0.25, 0.75, 1.0) * 6


@pytest.fixture(autouse=True)
def device_catalog():
    init_registry()


def _curve_csv() -> bytes:
    return ("\n".join([
        "# schema: ies.device-data",
        "# schema_version: 2.1.0",
        "# dataset_id: synthetic_curve_issue02",
        f"# device_id: {CURVE_ID}",
        "# source_mode: data_repeat",
        "# resolution: 1h",
        "# unit.unit_output: 1",
        "step,unit_output",
        *(f"{index},{value}" for index, value in enumerate(SYNTHETIC_DAY)),
        "",
    ])).encode("utf-8")


def _finance() -> EffectiveFinanceConfig:
    # 装配的必需空财务快照；无任何价格、成本、税或案例假设。
    return EffectiveFinanceConfig.from_dict({
        "schema": "ies.effective-finance-config",
        "schema_version": "4.0.0",
        "profile_id": "synthetic-curve",
        "currency": "CNY",
        "base_year": 2026,
        "price_basis": "tax_inclusive",
        "cost_method": "fixed_plus_linear",
        "finance_types": {},
        "energy_prices": {},
        "demand_charges": {},
        "taxes": {},
        "credit_pools": {},
        "tax_rules": {},
    })


def _assembly(capacity_kw: float, *, optimize: bool) -> str:
    # 固定容量场景的唯一规划变量指向电网端口上界，曲线容量并未被接管。
    parameter = "installed_capacity_kw" if optimize else "max_export_power_kw"
    device = "curve1" if optimize else "grid1"
    objective = "curve_capacity" if optimize else "0"
    lower = "0" if optimize else "1000"
    upper = "100" if optimize else "1000"
    return f"""schema: ies.assembly
schema_version: "4.0.0"
assembly:
  id: synthetic_curve
  name: Synthetic curve
project_baseline:
  resolution: 1h
  start_year: 2026
  scenario_mode: single
devices:
  curve1:
    definition:
      id: {CURVE_ID}
    asset_origin: new
    properties:
      installed_capacity_kw:
        value: {capacity_kw}
        unit: kW
    predefined_interfaces:
      unit_output:
        mode: data_repeat
        path: data/synthetic_curve.data.csv
  grid1:
    definition:
      id: ies.device.grid_connection
    asset_origin: new
    properties:
      max_import_power_kw:
        value: 1000
        unit: kW
      max_export_power_kw:
        value: 1000
        unit: kW
    predefined_interfaces: {{}}
connections:
  curve_export:
    from: curve1.electric_out
    to: grid1.electricity_export
finance:
  ref:
    kind: object
    object_id: synthetic-finance
    media_type: application/yaml
  profile_id: synthetic-curve
finance_binding:
  instances:
    curve1:
      type: none
    grid1:
      type: none
tariff_bindings: {{}}
demand_bindings: {{}}
planning:
  objective:
    sense: maximize
    expression: "{objective}"
  variables:
    curve_capacity:
      device_ref: {device}
      parameter: {parameter}
      lower_bound: "{lower}"
      upper_bound: "{upper}"
      unit: kW
  constraints: {{}}
outputs:
  series:
    - curve1.electric_out
  metrics: []
extensions: {{}}
"""


def _inputs(capacity_kw: float, *, optimize: bool = False):
    raw = _curve_csv()
    result = validate_assembly_text(
        _assembly(capacity_kw, optimize=optimize),
        dataset_contents={"data/synthetic_curve.data.csv": {
            "object_id": "synthetic-curve-object",
            "media_type": "text/csv",
            "data": raw,
        }},
        finance_snapshots={"synthetic-finance": _finance()},
    )
    assert result.ok, [(d.code, d.params) for d in result.diagnostics if d.blocking]
    assert result.artifact is not None
    models = {}
    for instance, entry in json.loads(result.artifact.canonical_text)["devices"].items():
        math = build_math_contribution(get_device(entry["definition"]["id"]))
        assert math.ok, math.diagnostics
        models[instance] = math.contribution
    materialized = materialize_non_predictive_series(
        result.artifact, {"synthetic-curve-object": raw}, operation_years=1,
    )
    assert not materialized.pending_predict
    config = CalculationConfig(
        mode="planning", generator=GENERATOR_REF, solver=SOLVER_REF,
        precision="medium", time_limit_seconds=300, outputs=["solution"],
    )
    evaluation = EvaluationInput(
        evaluation_years=1, construction_period=0, operation_years=1,
        discount_rate=0.0, construction_payments=(), maintenance_expenses=(),
    )
    return result.artifact, models, materialized, _finance(), config, evaluation


def _relation(model, step: int):
    return next(row for row in model.constraints
                if row.name == f"curve1__normalized_generation__t{step}")


def _coeffs(row) -> dict[str, float]:
    return {name: coefficient for (name, _offset), coefficient in row.expression.terms}


def test_public_model_parser_and_template_instantiator_preserve_units() -> None:
    raw = yaml_load((CATALOG / "power_curve.yaml").read_text(encoding="utf-8"))
    parsed = parse_device_model_v2(raw)
    assert parsed.ok and parsed.document is not None
    assert parsed.document.interfaces["unit_output"].unit == "1"
    assert parsed.document.properties["installed_capacity_kw"].unit == "kW"
    assert parsed.document.interfaces["electric_out"].unit == "kW"

    template = dict(raw)
    template["inputs"] = {
        "properties": {"installed_capacity_kw": {"value": {
            "type": "number", "unit": "kW",
            "valid_range": {"minimum": 0, "maximum": 1000000},
        }}},
        "interfaces": {"unit_output": {"type": "data_repeat"}},
    }
    instance, diagnostics = instantiate_template(template, {
        "properties": {"installed_capacity_kw": {"value": 75}},
        "interfaces": {"unit_output": "data/synthetic_curve.data.csv"},
    })
    assert instance is not None, diagnostics
    assert instance.document.properties["installed_capacity_kw"].value == 75
    assert instance.receipt["data_bindings"] == [{
        "interface": "unit_output", "mode": "data_repeat",
        "path": "data/synthetic_curve.data.csv",
    }]
    assert "inputs" not in to_dict(instance.document)

    # 两种模型独立共存，气象驱动光伏未改变方程及两个必需气象槽。
    pv = get_device("ies.device.pv")
    assert set(pv.interfaces) == {"solar_irradiance", "ambient_temperature", "electric_out"}
    assert "solar_irradiance[t]" in pv.equations.relations[0].expression
    assert build_math_contribution(pv).ok


@pytest.mark.parametrize("capacity_kw", [40.0, 80.0])
def test_fixed_capacity_hand_calculation_and_public_generation(capacity_kw: float) -> None:
    inputs = _inputs(capacity_kw)
    artifact, models, materialized, finance, config, evaluation = inputs
    series = next(s for s in materialized.series if s.interface == "unit_output")
    assert series.values[:24] == SYNTHETIC_DAY
    assert len(series.values) == 8760
    model, units, _ = build_project_model(*inputs)
    for step in (0, 1, 2, 3, 24):
        row = _relation(model, step)
        assert _coeffs(row) == {f"curve1__electric_out__t{step}": 1.0}
        assert row.rhs - row.expression.constant == pytest.approx(
            capacity_kw * SYNTHETIC_DAY[step % 24] * 1000
        )
        assert units[f"curve1__electric_out__t{step}"] == "kW"
    provider = build_registry((HIGHS_MILP_PROVIDER,)).resolve(config)
    bundle = provider.generate(artifact, models, materialized, finance, config, evaluation)
    assert b"curve1__electric_out__t1" in bundle.files["input/problem.mps"]
    metadata = json.loads(bundle.files["input/parameters.json"].decode("utf-8"))
    assert metadata["unit_map"]["curve1__electric_out__t1"] == "kW"


def test_planning_capacity_is_variable_in_every_step() -> None:
    model, units, _ = build_project_model(*_inputs(13, optimize=True))
    assert units["curve_capacity"] == "kW"
    for step, factor in enumerate(SYNTHETIC_DAY[:4]):
        assert _coeffs(_relation(model, step)) == {
            f"curve1__electric_out__t{step}": 1.0,
            **({"curve_capacity": -factor} if factor else {}),
        }
        assert _relation(model, step).expression.constant == 0
    assert dict(model.objective.terms)[("curve_capacity", 0)] == 1.0


def test_real_highs_solution_takes_over_capacity(tmp_path: Path) -> None:
    """仅 Docker CI 执行；真实求解值必须来自变量，而非 YAML 默认 13 kW。"""
    inputs = _inputs(13, optimize=True)
    bundle = build_registry((HIGHS_MILP_PROVIDER,)).resolve(inputs[4]).generate(*inputs)
    mps = bundle.files["input/problem.mps"].decode("utf-8")
    (tmp_path / "input").mkdir()
    (tmp_path / "output").mkdir()
    (tmp_path / "input/problem.mps").write_text(mps, encoding="utf-8")
    completed = subprocess.run([
        HIGHS_ADAPTER_PATH, "input/problem.mps", "--solution_file", "output/solution.sol",
        "--time_limit", "300", "--random_seed", "0",
    ], cwd=tmp_path, capture_output=True, text=True, timeout=330, check=False)
    assert completed.returncode == 0, completed.stderr
    solution = (tmp_path / "output/solution.sol").read_text(encoding="utf-8")
    assert "model_status: HighsModelStatus.kOptimal" in solution
    primals = [float(line.partition(":")[2].strip()) for line in
               solution.split("primal:\n", 1)[1].splitlines()]
    columns = []
    in_columns = False
    for line in mps.splitlines():
        if line == "COLUMNS":
            in_columns = True
            continue
        if line == "RHS":
            break
        if in_columns:
            name = line.split()[0]
            if name not in columns:
                columns.append(name)
    assert len(columns) == len(primals)
    assert primals[columns.index("curve_capacity")] == pytest.approx(100000.0)
    assert primals[columns.index("curve1__electric_out__t1")] == pytest.approx(25000.0)
