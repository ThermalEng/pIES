"""Tests for HiGHS solver cohesion (`ies-highs-solver`, `solver_stage:solve`, `generator_stage:generate`)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import highspy
import pytest
from iesplan.assembly import validate_assembly_text
from iesplan.computation import materialize_non_predictive_series
from iesplan.computation.protocols import CalculationConfig
from iesplan.devices import init_registry, list_devices
from iesplan.finance import EvaluationInput, effective_from_yaml
from iesplan.modeling import build_math_contribution, contribution_to_dict
from pies_sdk import (
    StageContext,
    StageIOError,
    iter_variable_map_records,
    load_plugin_manifest,
    read_primal_solution,
    read_variable_map_header,
    write_json_file,
)
from pies_sdk.runner import resolve_entrypoint

PLUGINS_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PLUGINS_ROOT.parent
IESPLAN_SAMPLES = REPO_ROOT / "backend" / "iesplan" / "assembly" / "samples"
HIGHS_EXECUTABLE_SRC = REPO_ROOT / "backend" / "solver-executables" / "ies-highs-solver"

generate_stage = resolve_entrypoint("generator_stage:generate", PLUGINS_ROOT / "generator_highs_milp")
solve_stage = resolve_entrypoint("solver_stage:solve", PLUGINS_ROOT / "solver_highs")
_solver_stage_mod = sys.modules["solver_stage"]
HIGHS_EXECUTABLE_PATH = _solver_stage_mod.HIGHS_EXECUTABLE_PATH
_stream_v1_solution_to_primal_json = _solver_stage_mod._stream_v1_solution_to_primal_json


_SOLVER_MANIFEST = load_plugin_manifest(
    PLUGINS_ROOT / "solver_highs" / "plugin.yaml", check_compat=False
)
_GENERATOR_MANIFEST = load_plugin_manifest(
    PLUGINS_ROOT / "generator_highs_milp" / "plugin.yaml", check_compat=False
)


def _load_solver_module() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader(
        "_ies_highs_solver_f2_test", str(HIGHS_EXECUTABLE_SRC)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _parse_v1(path: Path) -> tuple[dict[str, str], dict[int, float]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "HIGHS_SOLUTION_V1"
    facts: dict[str, str] = {}
    primal: dict[int, float] = {}
    in_primal = False
    for line in lines[1:]:
        if line == "primal:":
            in_primal = True
            continue
        k, _, v = line.partition(":")
        if in_primal:
            primal[int(k.strip())] = float(v.strip())
        else:
            facts[k.strip()] = v.strip()
    return facts, primal


def _write_bundle(
    work_in: Path,
    *,
    mps_text: str,
    acceleration: dict[str, Any] | None = None,
    epigraphs: dict[str, Any] | None = None,
) -> Path:
    bundle_dir = work_in / "bundle"
    input_dir = bundle_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / "problem.mps").write_text(mps_text, encoding="utf-8")

    inputs = [{"path": "input/problem.mps", "media_type": "application/mps"}]
    if acceleration is not None:
        write_json_file(input_dir / "acceleration.json", acceleration)
        inputs.append({"path": "input/acceleration.json", "media_type": "application/json"})
    if epigraphs is not None:
        write_json_file(input_dir / "epigraphs.json", epigraphs)
        inputs.append({"path": "input/epigraphs.json", "media_type": "application/json"})

    manifest = "\n".join(
        [
            "schema: ies.solver-bundle",
            "schema_version: 1.0.0",
            "bundle:",
            "  generator: ies.generator.highs_milp@1.0.0",
            "  contract: ies.solver-bundle.mps@1.0.0",
            "inputs:",
            *[
                f"  - path: {entry['path']}\n    media_type: {entry['media_type']}"
                for entry in inputs
            ],
            "outputs:",
            "  - path: output/primal_solution.json",
            "    media_type: application/json",
            "    required: true",
            "extensions: {}",
            "",
        ]
    )
    (bundle_dir / "bundle.yaml").write_text(manifest, encoding="utf-8")
    return bundle_dir


def _build_epigraph_mps(*, peak_lb: float, peak_cost: float, include_mutex: bool = False) -> str:
    """Build 0-based column LP/MILP with epigraph row(s) and optional binary mutex pair."""
    h = highspy.Highs()
    h.setOptionValue("output_flag", False)
    inf = highspy.kHighsInf
    # col 0: peak, col 1: flow0, col 2: flow1
    h.addVar(peak_lb, inf)
    h.addVar(0.0, 2.0)
    h.addVar(0.0, 3.0)
    h.changeColCost(0, peak_cost)
    h.changeColCost(1, -1.0)
    h.changeColCost(2, -1.0)
    # peak - flow0 >= 0 ; peak - flow1 >= 0
    h.addRow(0.0, inf, 2, [0, 1], [1.0, -1.0])
    h.addRow(0.0, inf, 2, [0, 2], [1.0, -1.0])
    if include_mutex:
        # col 3: a (>=0), col 4: b (>=0), col 5: z (bin)
        # a <= 5*z (row 2) ; b + 5*z <= 5 -> b <= 5*(1-z) (row 3)
        h.addVar(0.0, 5.0)
        h.addVar(0.0, 5.0)
        h.addVar(0.0, 1.0)
        h.changeColIntegrality(5, highspy.HighsVarType.kInteger)
        h.changeColCost(3, -1.0)
        h.changeColCost(4, -2.0)
        # row 2: a - 5*z <= 0
        h.addRow(-inf, 0.0, 2, [3, 5], [1.0, -5.0])
        # row 3: b + 5*z <= 5
        h.addRow(-inf, 5.0, 2, [4, 5], [1.0, 5.0])
    import tempfile

    with tempfile.NamedTemporaryFile("w+", suffix=".mps", delete=True) as tmp:
        h.writeModel(tmp.name)
        tmp.seek(0)
        return tmp.read()


def _build_infeasible_mps() -> str:
    h = highspy.Highs()
    h.setOptionValue("output_flag", False)
    inf = highspy.kHighsInf
    h.addVar(0.0, 1.0)
    h.addVar(0.0, 1.0)
    h.addRow(5.0, inf, 2, [0, 1], [1.0, 1.0])
    import tempfile

    with tempfile.NamedTemporaryFile("w+", suffix=".mps", delete=True) as tmp:
        h.writeModel(tmp.name)
        tmp.seek(0)
        return tmp.read()


def _build_unbounded_mps() -> str:
    h = highspy.Highs()
    h.setOptionValue("output_flag", False)
    inf = highspy.kHighsInf
    h.addVar(0.0, inf)
    h.addVar(0.0, 1.0)
    h.changeColCost(0, -1.0)
    h.addRow(0.0, inf, 2, [0, 1], [1.0, 1.0])
    import tempfile

    with tempfile.NamedTemporaryFile("w+", suffix=".mps", delete=True) as tmp:
        h.writeModel(tmp.name)
        tmp.seek(0)
        return tmp.read()


def test_executable_tight_epigraph_and_default_v1_compat(tmp_path: Path) -> None:
    mps_text = _build_epigraph_mps(peak_lb=0.0, peak_cost=0.5)
    mps_path = tmp_path / "tight.mps"
    mps_path.write_text(mps_text, encoding="utf-8")
    epi_path = tmp_path / "epigraphs.json"
    write_json_file(
        epi_path,
        {
            "schema": "ies.solver-epigraph",
            "schema_version": "1.0.0",
            "num_columns": 3,
            "epigraphs": [{"peak_col": 0, "flow_cols": [1, 2]}],
        },
    )

    sol_default = tmp_path / "default.sol"
    res_default = subprocess.run(
        [
            HIGHS_EXECUTABLE_PATH,
            "tight.mps",
            "--solution_file",
            "default.sol",
            "--time_limit",
            "30",
            "--random_seed",
            "0",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_default.returncode == 0, res_default.stderr
    facts_default, _ = _parse_v1(sol_default)
    assert "epigraph_verified" not in facts_default
    assert "model_num_columns" not in facts_default

    sol_epi = tmp_path / "epi.sol"
    res_epi = subprocess.run(
        [
            HIGHS_EXECUTABLE_PATH,
            "tight.mps",
            "--solution_file",
            "epi.sol",
            "--time_limit",
            "30",
            "--random_seed",
            "0",
            "--epigraph_file",
            "epigraphs.json",
            "--report_model_columns",
            "true",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_epi.returncode == 0, res_epi.stderr
    facts_epi, primal_epi = _parse_v1(sol_epi)
    assert facts_epi["model_status"] == "HighsModelStatus.kOptimal"
    assert facts_epi["epigraph_verified"] == "true"
    assert facts_epi.get("original_model_verified", "true") == "true"
    assert facts_epi["model_num_columns"] == "3"
    assert primal_epi[0] == pytest.approx(3.0, abs=1e-7)
    assert primal_epi[1] == pytest.approx(2.0, abs=1e-7)
    assert primal_epi[2] == pytest.approx(3.0, abs=1e-7)


def test_solver_module_rejects_inflated_epigraph_and_lp_violation(tmp_path: Path) -> None:
    solver_mod = _load_solver_module()
    assert solver_mod.verify_epigraphs([3.0, 2.0, 3.0], [(0, [1, 2])]) is True
    assert solver_mod.verify_epigraphs([3.5, 2.0, 3.0], [(0, [1, 2])]) is False
    # Empty flow_cols group imposes no envelope constraint
    assert solver_mod.verify_epigraphs([0.1, 2.0, 3.0], [(0, [])]) is True

    h = highspy.Highs()
    h.setOptionValue("output_flag", False)
    h.addVar(0.0, 10.0)
    h.addVar(0.0, 10.0)
    h.addRow(0.0, 5.0, 2, [0, 1], [1.0, 1.0])
    lp = h.getLp()
    assert solver_mod.verify_primal_against_lp(lp, [2.0, 3.0]) is True
    # Exceeds row upper bound 5.0 by 1e-4 (> feasibility tolerance 1e-7)
    assert solver_mod.verify_primal_against_lp(lp, [2.0, 3.0001]) is False

    # Verify kTimeLimit + unverified -> not_solved; kOptimal + unverified -> numeric_failure
    sol_tl = tmp_path / "tl_unverified.sol"
    sol_tl.write_text(
        "\n".join(
            [
                "HIGHS_SOLUTION_V1",
                "model_status: HighsModelStatus.kTimeLimit",
                "has_feasible_solution: true",
                "objective_value: 10.0",
                "best_bound: 5.0",
                "relative_gap: 0.5",
                "solve_domain: valid_relaxation",
                "original_model_verified: true",
                "epigraph_verified: false",
                "num_columns: 2",
                "model_num_columns: 2",
                "primal:",
                "0: 1.0",
                "1: 2.0",
                "",
            ]
        ),
        encoding="utf-8",
    )
    out_tl = _stream_v1_solution_to_primal_json(
        sol_tl,
        tmp_path / "tl_primal.json",
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:00:01Z",
        elapsed_seconds=1.0,
    )
    payload_tl = read_primal_solution(out_tl)
    assert payload_tl.math_status == "not_solved"
    assert payload_tl.termination_reason == "not_solved"
    assert payload_tl.has_feasible_solution is False
    assert payload_tl.values == ()

    # Restricted feasible solution strips global dual bound & gap
    sol_rest = tmp_path / "restricted.sol"
    sol_rest.write_text(
        "\n".join(
            [
                "HIGHS_SOLUTION_V1",
                "model_status: HighsModelStatus.kTimeLimit",
                "has_feasible_solution: true",
                "objective_value: 12.0",
                "best_bound: 8.0",
                "relative_gap: 0.33",
                "solve_domain: restricted",
                "original_model_verified: true",
                "num_columns: 2",
                "model_num_columns: 2",
                "primal:",
                "0: 1.0",
                "1: 2.0",
                "",
            ]
        ),
        encoding="utf-8",
    )
    out_rest = _stream_v1_solution_to_primal_json(
        sol_rest,
        tmp_path / "rest_primal.json",
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:00:01Z",
        elapsed_seconds=1.0,
    )
    payload_rest = read_primal_solution(out_rest)
    assert payload_rest.math_status == "feasible"
    assert payload_rest.termination_reason == "time_limit"
    assert payload_rest.has_feasible_solution is True
    assert payload_rest.best_bound is None
    assert payload_rest.relative_gap is None
    assert payload_rest.values == (1.0, 2.0)


@pytest.mark.parametrize("use_acceleration", [False, True])
def test_solve_stage_rejects_inflated_epigraph_in_real_solver(
    tmp_path: Path, use_acceleration: bool
) -> None:
    mps_text = _build_epigraph_mps(peak_lb=10.0, peak_cost=0.0, include_mutex=use_acceleration)
    work_in = tmp_path / "in"
    work_out = tmp_path / "out"
    work_out.mkdir(parents=True)
    num_cols = 6 if use_acceleration else 3
    acceleration = None
    if use_acceleration:
        acceleration = {
            "schema": "ies.solver-acceleration",
            "schema_version": "1.0.0",
            "num_columns": num_cols,
            "num_rows": 4,
            "technical_mutex_pairs": [[5, 3, 4, 2, 3]],
        }
    _write_bundle(
        work_in,
        mps_text=mps_text,
        acceleration=acceleration,
        epigraphs={
            "schema": "ies.solver-epigraph",
            "schema_version": "1.0.0",
            "num_columns": num_cols,
            "epigraphs": [{"peak_col": 0, "flow_cols": [1, 2]}],
        },
    )
    write_json_file(work_in / "request.json", {"time_limit_seconds": 30, "seed": 7})

    solve_stage(
        StageContext(
            manifest=_SOLVER_MANIFEST,
            work_in=work_in,
            work_out=work_out,
            parameters={"threads": 1},
        )
    )

    assert sorted(p.name for p in work_out.iterdir()) == ["primal_solution.json"]
    primal = read_primal_solution(work_out / "primal_solution.json")
    assert primal.math_status == "numeric_failure"
    assert primal.has_feasible_solution is False
    assert primal.column_count == num_cols
    assert primal.values == ()
    assert primal.receipt["solver_diagnostics"]["epigraph_verified"] is False


def test_solve_stage_statuses_and_accelerated_recovery(tmp_path: Path) -> None:
    # 1) Solved with on_demand_mutex + tight epigraph in 0-based order
    acc_in = tmp_path / "acc_in"
    acc_out = tmp_path / "acc_out"
    acc_out.mkdir()
    _write_bundle(
        acc_in,
        mps_text=_build_epigraph_mps(peak_lb=0.0, peak_cost=0.5, include_mutex=True),
        acceleration={
            "schema": "ies.solver-acceleration",
            "schema_version": "1.0.0",
            "num_columns": 6,
            "num_rows": 4,
            "technical_mutex_pairs": [[5, 3, 4, 2, 3]],
        },
        epigraphs={
            "schema": "ies.solver-epigraph",
            "schema_version": "1.0.0",
            "num_columns": 6,
            "epigraphs": [{"peak_col": 0, "flow_cols": [1, 2]}],
        },
    )
    solve_stage(
        StageContext(
            manifest=_SOLVER_MANIFEST, work_in=acc_in, work_out=acc_out, parameters={}
        )
    )
    acc_primal = read_primal_solution(acc_out / "primal_solution.json")
    assert acc_primal.math_status == "solved"
    assert acc_primal.termination_reason == "optimal"
    assert acc_primal.has_feasible_solution is True
    assert acc_primal.column_count == 6
    assert len(acc_primal.values) == 6
    assert acc_primal.values[0] == pytest.approx(3.0, abs=1e-7)
    assert acc_primal.values[3] == pytest.approx(0.0, abs=1e-7)
    assert acc_primal.values[4] == pytest.approx(5.0, abs=1e-7)
    assert acc_primal.receipt["solve_domain"] == "valid_relaxation"
    assert acc_primal.receipt["original_model_verified"] is True
    assert acc_primal.receipt["solver_diagnostics"]["epigraph_verified"] is True

    # 2) Infeasible preserves original model column_count
    inf_in = tmp_path / "inf_in"
    inf_out = tmp_path / "inf_out"
    inf_out.mkdir()
    _write_bundle(inf_in, mps_text=_build_infeasible_mps())
    solve_stage(
        StageContext(
            manifest=_SOLVER_MANIFEST, work_in=inf_in, work_out=inf_out, parameters={}
        )
    )
    inf_primal = read_primal_solution(inf_out / "primal_solution.json")
    assert inf_primal.math_status == "infeasible"
    assert inf_primal.termination_reason == "infeasible"
    assert inf_primal.has_feasible_solution is False
    assert inf_primal.column_count == 2
    assert inf_primal.values == ()

    # 3) Unbounded preserves original model column_count
    unb_in = tmp_path / "unb_in"
    unb_out = tmp_path / "unb_out"
    unb_out.mkdir()
    _write_bundle(unb_in, mps_text=_build_unbounded_mps())
    solve_stage(
        StageContext(
            manifest=_SOLVER_MANIFEST, work_in=unb_in, work_out=unb_out, parameters={}
        )
    )
    unb_primal = read_primal_solution(unb_out / "primal_solution.json")
    assert unb_primal.math_status == "unbounded"
    assert unb_primal.termination_reason == "unbounded"
    assert unb_primal.has_feasible_solution is False
    assert unb_primal.column_count == 2
    assert unb_primal.values == ()


def test_generate_stage_and_solve_stage_campus_end_to_end(tmp_path: Path) -> None:
    init_registry()
    text = (IESPLAN_SAMPLES / "valid" / "campus.assembly.yaml").read_text(encoding="utf-8")
    validated = validate_assembly_text(text, package_dir=IESPLAN_SAMPLES)
    assert validated.ok
    artifact = validated.artifact

    contributions = {}
    for dev in list_devices():
        built = build_math_contribution(dev)
        assert built.ok
        contributions[dev.device.id] = built.contribution

    doc = json.loads(artifact.canonical_text)
    models = {inst: contributions[dev["definition"]["id"]] for inst, dev in doc["devices"].items()}
    finance = effective_from_yaml(
        (IESPLAN_SAMPLES / "finance" / "campus.effective-finance.yaml").read_text(encoding="utf-8")
    )
    found: set[str] = set()

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            content = node.get("content")
            if isinstance(content, dict) and "object_id" in content:
                found.add(str(content["object_id"]))
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(doc.get("devices") or {})
    resources = {
        oid: (IESPLAN_SAMPLES / (oid[5:] if oid.startswith("file:") else oid)).read_bytes()
        for oid in sorted(found)
    }
    materialized = materialize_non_predictive_series(artifact, resources, operation_years=1)
    evaluation = EvaluationInput(
        evaluation_years=1,
        construction_period=0,
        operation_years=1,
        discount_rate=0.08,
        construction_payments=(),
        maintenance_expenses=(),
    )
    target_cfg = CalculationConfig(
        mode="planning",
        predictions=(),
        generator="ies.generator.highs_milp@1.0.0",
        solver="ies.solver.highs@1.7.2",
        algorithm=None,
        evaluators=({"evaluator": "ies.evaluator.finance@1.0.0", "parameters": {}},),
        precision="medium",
        time_limit_seconds=120,
        options={"generator": {}, "solver": {}},
        seed=None,
        outputs=("solution", "metrics"),
    )

    gen_in = tmp_path / "gen_in"
    gen_out = tmp_path / "gen_out"
    gen_in.mkdir()
    gen_out.mkdir()
    write_json_file(
        gen_in / "request.json",
        {
            "artifact": artifact.to_dict(),
            "device_models": {inst: contribution_to_dict(c) for inst, c in models.items()},
            "materialized": materialized.to_dict(),
            "finance": finance.to_dict(),
            "calculation_config": target_cfg.to_dict(),
            "evaluation": {
                "evaluation_years": evaluation.evaluation_years,
                "construction_period": evaluation.construction_period,
                "operation_years": evaluation.operation_years,
                "discount_rate": evaluation.discount_rate,
                "construction_payments": [],
                "maintenance_expenses": [],
                "comparison": evaluation.comparison.to_dict(),
            },
        },
    )

    generate_stage(
        StageContext(
            manifest=_GENERATOR_MANIFEST, work_in=gen_in, work_out=gen_out, parameters={}
        )
    )

    bundle_dir = gen_out / "bundle"
    assert (bundle_dir / "bundle.yaml").is_file()
    assert (bundle_dir / "input" / "problem.mps").is_file()
    assert (bundle_dir / "input" / "parameters.json").is_file()
    assert (bundle_dir / "input" / "parameters.jsonl").is_file()

    vmap_header = read_variable_map_header(bundle_dir / "input" / "parameters.json")
    vmap_records = list(iter_variable_map_records(bundle_dir / "input" / "parameters.jsonl"))
    assert vmap_header.column_count > 0
    assert len(vmap_records) > 0

    solver_in = tmp_path / "solver_in"
    solver_out = tmp_path / "solver_out"
    solver_in.mkdir()
    solver_out.mkdir()
    (solver_in / "bundle").symlink_to(bundle_dir)
    write_json_file(
        solver_in / "request.json",
        {"time_limit_seconds": 120, "seed": 0, "parameters": {"threads": 1}},
    )

    from pies_sdk.runner import run_plugin

    run_plugin(
        manifest_path=PLUGINS_ROOT / "solver_highs" / "plugin.yaml",
        work_in=solver_in,
        work_out=solver_out,
        pies_version="1.2.0",
    )

    # Solver outputs strictly primal_solution.json (never variable_map/finance artifacts)
    assert sorted(p.name for p in solver_out.iterdir()) == ["primal_solution.json"]
    primal = read_primal_solution(solver_out / "primal_solution.json")
    assert primal.math_status == "solved"
    assert primal.termination_reason == "optimal"
    assert primal.has_feasible_solution is True
    assert primal.column_count == vmap_header.column_count
    assert len(primal.values) == vmap_header.column_count


def test_original_lp_verified_without_epigraph_file_and_slot_to_solution_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    solver_mod = _load_solver_module()

    # 1) VerifiedIncumbentSlot.to_solution_text serializes model_num_columns & epigraph_verified
    slot = solver_mod.VerifiedIncumbentSlot(
        sense="minimize",
        epigraphs=[(0, [1, 2])],
        model_num_columns=3,
    )
    recorded = slot.record(
        objective_value=5.0,
        primals=[3.0, 2.0, 3.0],
        model_status="HighsModelStatus.kOptimal",
        best_bound=5.0,
        relative_gap=0.0,
        solve_domain="valid_relaxation",
        original_model_verified=True,
    )
    assert recorded is True
    text = slot.to_solution_text()
    assert "model_num_columns: 3\n" in text
    assert "epigraph_verified: true\n" in text

    # 2) _cmd_solve with --report_model_columns true (no --epigraph_file) verifies original LP
    mps_path = tmp_path / "lp.mps"
    mps_path.write_text(_build_epigraph_mps(peak_lb=0.0, peak_cost=0.5), encoding="utf-8")
    sol_path = tmp_path / "violated.sol"

    orig_extract = solver_mod._extract_solution_data

    def _tampered_extract(handle: Any, status: Any):
        feasible, obj, bound, gap, primals = orig_extract(handle, status)
        # Violate row `peak - flow1 >= 0` (`0.0 - 3.0 = -3.0`) even though HiGHS returned kOptimal
        bad = list(primals)
        bad[0] = 0.0
        return feasible, obj, bound, gap, bad

    monkeypatch.setattr(solver_mod, "_extract_solution_data", _tampered_extract)
    monkeypatch.chdir(tmp_path)
    rc = solver_mod._cmd_solve(
        [
            "lp.mps",
            "--solution_file",
            "violated.sol",
            "--time_limit",
            "30",
            "--random_seed",
            "0",
            "--report_model_columns",
            "true",
        ]
    )
    assert rc == 0
    facts, primal = _parse_v1(sol_path)
    assert facts["has_feasible_solution"] == "false"
    assert facts["original_model_verified"] == "false"
    assert facts["model_num_columns"] == "3"
    assert "epigraph_verified" not in facts
    assert primal == {}

    primal_out = _stream_v1_solution_to_primal_json(
        sol_path,
        tmp_path / "primal_solution.json",
        started_at="2026-01-01T00:00:00Z",
        ended_at="2026-01-01T00:00:01Z",
        elapsed_seconds=0.1,
    )
    payload = read_primal_solution(primal_out)
    assert payload.math_status == "numeric_failure"
    assert payload.has_feasible_solution is False
    assert payload.column_count == 3

    # 3) Non-finite primal values & truncated count still rejected at canonical streaming boundary
    bad_sol = tmp_path / "nan.sol"
    bad_sol.write_text(
        "\n".join(
            [
                "HIGHS_SOLUTION_V1",
                "model_status: HighsModelStatus.kOptimal",
                "has_feasible_solution: true",
                "objective_value: 1.0",
                "best_bound: 1.0",
                "relative_gap: 0.0",
                "num_columns: 2",
                "model_num_columns: 2",
                "primal:",
                "0: 1.0",
                "1: nan",
                "",
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises((StageIOError, ValueError)):
        _stream_v1_solution_to_primal_json(
            bad_sol,
            tmp_path / "bad_primal.json",
            started_at="2026-01-01T00:00:00Z",
            ended_at="2026-01-01T00:00:01Z",
            elapsed_seconds=0.1,
        )
