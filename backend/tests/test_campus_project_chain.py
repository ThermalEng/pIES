"""园区真实链测试(0.8f-B 修正)：内置 catalog + 正式 validator 签发 artifact。

使用仓库内置设备目录、经 ``validate_assembly_text`` 签发的 campus
装配与有效 finance/data 样例构造项目模型，覆盖多支路母线、负荷
需求、step_duration、真实 grid/battery Big-M 有界与确定性字节。
小型合成单元测试仍在 test_project_model.py，本文件是真实证据。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest  # type: ignore[import-untyped]

from iesplan.assembly import validate_assembly_text
from iesplan.computation import (
    CalculationConfig,
    build_project_model,
    materialize_non_predictive_series,
    write_mps,
)
from iesplan.devices import init_registry, list_devices
from iesplan.finance import effective_from_yaml
from iesplan.modeling import build_math_contribution

SAMPLES = Path(__file__).resolve().parent.parent / "iesplan" / "assembly" / "samples"


def _evaluation():
    """campus 合成评价确认（与物化同一 N=1/K=0 口径，冻结值直传）。"""
    from iesplan.finance import EvaluationInput

    return EvaluationInput(
        evaluation_years=1,
        construction_period=0,
        operation_years=1,
        discount_rate=0.08,
        construction_payments=(),
        maintenance_expenses=(),
    )


def _row(model, name):
    for row in model.constraints:
        if row.name == name:
            return row
    raise AssertionError(f"约束行缺失: {name}")


def _coeffs(expr) -> dict:
    return {key[0]: coeff for key, coeff in expr.terms}


@pytest.fixture(scope="module")
def chain():
    """真实直接输入（validator 签发 artifact + 目录贡献 + 物化序列 + 评价确认）。"""
    init_registry()
    text = (SAMPLES / "valid" / "campus.assembly.yaml").read_text(encoding="utf-8")
    validated = validate_assembly_text(text, package_dir=SAMPLES)
    assert validated.ok, [
        (d.code, d.params) for d in validated.diagnostics if d.blocking
    ]
    artifact = validated.artifact
    contributions = {}
    for doc in list_devices():
        built = build_math_contribution(doc)
        assert built.ok, (doc.device.id, built.diagnostics)
        contributions[doc.device.id] = built.contribution
    doc = json.loads(artifact.canonical_text)
    models = {
        inst: contributions[dev["definition"]["id"]]
        for inst, dev in doc["devices"].items()
    }
    finance = effective_from_yaml(
        (SAMPLES / "finance" / "campus.effective-finance.yaml").read_text(
            encoding="utf-8"
        )
    )

    def collect(node, found: set):
        if isinstance(node, dict):
            content = node.get("content")
            if isinstance(content, dict) and "object_id" in content:
                found.add(content["object_id"])
            for value in node.values():
                collect(value, found)
        elif isinstance(node, list):
            for value in node:
                collect(value, found)

    object_ids: set = set()
    collect(doc["devices"], object_ids)
    resources = {}
    for object_id in sorted(object_ids):
        rel = object_id[5:] if object_id.startswith("file:") else object_id
        resources[object_id] = (SAMPLES / rel).read_bytes()
    mat = materialize_non_predictive_series(artifact, resources, operation_years=1)
    assert not mat.pending_predict
    config = CalculationConfig(
        mode="planning",
        generator="ies.generator.highs_milp@3.4.0",
        solver="ies.solver.highs@1.7.2",
        precision="medium",
        time_limit_seconds=3600,
        outputs=["solution"],
    )
    model, units, _ = build_project_model(
        artifact, models, mat, finance, config, _evaluation()
    )
    return {
        "model": model,
        "units": units,
        "inputs": (artifact, models, mat, finance, config, _evaluation()),
    }


def test_multi_branch_bus_single_row_per_step(chain) -> None:
    """多支路母线：同一 carrier 连通分量每 step 只一条守恒式。"""
    model = chain["model"]
    row = _row(model, "bus__electricity__0__t0")
    assert _coeffs(row.expression) == {
        "grid__electricity_import__t0": 1.0,
        "grid__electricity_export__t0": -1.0,
        "pv1__electric_out__t0": 1.0,
        "bat1__electricity__t0": 1.0,
        "hp1__electricity_in__t0": -1.0,
        "elec_load__electricity_in__t0": -1.0,
    }
    heat = _row(model, "bus__heat__1__t0")
    assert _coeffs(heat.expression) == {
        "hp1__heat_out__t0": 1.0,
        "heat_load__heat_in__t0": -1.0,
    }
    names = {row.name for row in model.constraints}
    assert "bus__electricity__0__t8759" in names
    assert not any(name.startswith("e_bat__t") for name in names)


def test_load_demand_enters_model(chain) -> None:
    """负荷需求序列经 serve 方程进入模型（负荷不再消失）。"""
    model = chain["model"]
    serve = _row(model, "elec_load__serve__t0")
    assert _coeffs(serve.expression) == {"elec_load__electricity_in__t0": 1.0}
    assert serve.expression.constant == pytest.approx(-78000.0)
    heat_serve = _row(model, "heat_load__serve__t0")
    assert heat_serve.expression.constant == pytest.approx(-118000.0)


def test_step_duration_seconds_applied(chain) -> None:
    """step_duration 以 SI 秒进入状态递推（1h → 3600，容量 400kWh）。"""
    model = chain["model"]
    row = _row(model, "bat1__soc_update__t0")
    coeffs = _coeffs(row.expression)
    assert coeffs["bat1__soc__t0"] == 1.0
    assert coeffs["bat1__charge__t0"] == pytest.approx(-0.95 * 3600 / 1.44e9)
    assert coeffs["bat1__discharge__t0"] == pytest.approx(3600 / 0.95 / 1.44e9)
    assert row.expression.constant == pytest.approx(-0.5)


def test_real_exclusivity_bounded(chain) -> None:
    """真实 grid/battery 互斥变量 0/1，流变量由额定能力给出有限上界。"""
    model = chain["model"]
    by_name = {var.name: var for var in model.variables}
    grid_mutex = by_name["grid__import_export_exclusivity__mutex__t0"]
    assert (grid_mutex.lower, grid_mutex.upper, grid_mutex.kind) == (0.0, 1.0, "binary")
    bat_mutex = by_name["bat1__charge_discharge_exclusivity__mutex__t0"]
    assert (bat_mutex.lower, bat_mutex.upper, bat_mutex.kind) == (0.0, 1.0, "binary")
    assert by_name["grid__electricity_import__t0"].upper == pytest.approx(800000.0)
    assert by_name["grid__electricity_export__t0"].upper == pytest.approx(200000.0)
    assert by_name["bat1__charge__t0"].upper == pytest.approx(200000.0)
    assert by_name["bat1__discharge__t0"].upper == pytest.approx(200000.0)
    assert by_name["hp1__heat_out__t0"].upper == pytest.approx(600000.0)


def test_tariff_and_objective_signs(chain) -> None:
    """购电记账进入目标（售电符号对称见合成测试）；规划上限约束存在。"""
    model = chain["model"]
    assert model.sense == "minimize"
    coeffs = _coeffs(model.objective)
    assert coeffs["grid__electricity_import__t0"] == pytest.approx(0.0007)
    assert coeffs["pv_cap"] > 0
    cap = _row(model, "annual_purchase_cap")
    assert cap.sense == "le"
    assert cap.expression.constant == pytest.approx(-900000.0)


def test_chain_deterministic_bytes(chain) -> None:
    """真实链确定性：重建重写逐字节相同。"""
    model = chain["model"]
    rebuilt, _, _ = build_project_model(*chain["inputs"])
    first, first_offset = write_mps(model)
    second, second_offset = write_mps(rebuilt)
    assert first == second
    assert first_offset == pytest.approx(second_offset)
    assert first.startswith("NAME          IES_MILP\nROWS\n N  OBJ\n")
    assert "\nOBJSENSE\n MIN\nENDATA\n" in first
    assert " BV       BND1  grid__import_export_exclusivity__mutex__t0\n" in first
    assert "bus__electricity__0__t0" in first
