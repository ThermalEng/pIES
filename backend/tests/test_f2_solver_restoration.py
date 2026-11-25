"""F2 tests: target HiGHS bundle/variable_map emitter, spawn ResultAdapter, and 3rd-party solver."""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from test_highs_milp_provider import _campus_inputs

from iesplan.computation import (
    HIGHS_ADAPTER,
    HIGHS_MILP_PROVIDER,
    ComputationUnavailableError,
    ExecutionReceipt,
    build_registry,
)
from iesplan.computation.contracts import (
    PRIMAL_SOLUTION_OUTPUT_PATH,
    SOLVER_BUNDLE_MPS_CONTRACT,
    TargetCalculationConfig,
    TargetSolverBundle,
    TargetSolverBundleManifest,
    validate_target_bundle,
)
from iesplan.computation.generators.target_bundle import (
    EPIGRAPHS_PATH,
    VARIABLE_MAP_SCHEMA,
    VARIABLE_MAP_VERSION,
    generate_target_highs_bundle,
)
from iesplan.computation.result_adapters.target_adapter import (
    ENGINEERING_SOLUTION_SCHEMA,
    ENGINEERING_SOLUTION_VERSION,
    PRIMAL_SOLUTION_SCHEMA,
    PRIMAL_SOLUTION_VERSION,
    TargetResultAdapter,
    restore_from_bundle_dir,
)
from iesplan.core.content import GeneratedContent, MemoryContent, read_content


def _read_restored(res: Any) -> dict[str, Any]:
    sol_path = Path(str(res.outputs["engineering_solution_path"]))
    assert sol_path.is_file()
    return json.loads(sol_path.read_text(encoding="utf-8"))

HIGHS_EXECUTABLE = Path("/usr/local/bin/ies-highs-solver")


def _find_sdk_schema_dir() -> Path | None:
    env_root = os.environ.get("PIES_REPO_ROOT")
    candidates = [Path(env_root)] if env_root else []
    candidates.extend([Path(__file__).resolve().parents[2], Path("/repo")])
    for root in candidates:
        candidate = root / "sdk" / "pies_sdk" / "schema"
        if candidate.is_dir():
            return candidate
    return None


def _target_campus_config(
    *, accel_mode: str = "on_demand_mutex", seed: int | None = 7
) -> TargetCalculationConfig:
    return TargetCalculationConfig(
        mode="planning",
        predictions=(),
        generator="ies.generator.highs_milp@1.0.0",
        solver="ies.solver.highs@1.7.2",
        algorithm=None,
        evaluators=(),
        precision="medium",
        time_limit_seconds=60,
        options={"generator": {"acceleration": {"mode": accel_mode}}, "solver": {}},
        seed=seed,
        outputs=("solution", "metrics"),
    )


def _succeeded_receipt(*, bundle_id: str = "bundle-f2", receipt_id: str = "rcpt-f2") -> ExecutionReceipt:
    return ExecutionReceipt(
        receipt_id=receipt_id,
        bundle_id=bundle_id,
        assembly_id="asm-1",
        calculation_config_id="cfg-1",
        attempt_id="att-1",
        generator_ref="ies.generator.highs_milp@1.0.0",
        solver_ref="ies.solver.highs@1.7.2",
        executor_ref="ies.executor.local_process@1.0.0",
        executable_ref="ies.solver.highs@1.7.2",
        result_adapter_ref="ies.result-adapter.highs@1.0.0",
        status="succeeded",
        exit_code=0,
        termination_reason="exited",
        started_at="2026-11-25T10:00:00Z",
        ended_at="2026-11-25T10:00:01Z",
    )


def _v1_sol_to_primal_json(sol_text: str) -> dict[str, Any]:
    lines = sol_text.splitlines()
    assert lines[0] == "HIGHS_SOLUTION_V1"
    facts: dict[str, str] = {}
    values: list[float] = []
    in_primal = False
    for line in lines[1:]:
        if line == "primal:":
            in_primal = True
            continue
        k, _, v = line.partition(":")
        if not in_primal:
            facts[k.strip()] = v.strip()
        elif line.strip():
            values.append(float(v.strip()))

    status = facts["model_status"]
    has_feasible = facts["has_feasible_solution"] == "true"
    solve_domain = facts.get("solve_domain", "original")
    verified = facts.get("original_model_verified", "true") == "true"
    if facts.get("epigraph_verified") == "false":
        verified = False

    if not verified:
        math_status = "numeric_failure" if status == "HighsModelStatus.kOptimal" else "not_solved"
        term_reason = "not_solved"
        has_feasible = False
    elif solve_domain == "restricted":
        math_status = "feasible" if has_feasible else "not_solved"
        term_reason = "optimal" if status == "HighsModelStatus.kOptimal" else "time_limit"
    elif status == "HighsModelStatus.kOptimal":
        math_status, term_reason = "solved", "optimal"
    elif status == "HighsModelStatus.kTimeLimit":
        math_status, term_reason = ("feasible" if has_feasible else "not_solved"), "time_limit"
    elif status == "HighsModelStatus.kInfeasible":
        math_status, term_reason = "infeasible", "infeasible"
    elif status == "HighsModelStatus.kUnbounded":
        math_status, term_reason = "unbounded", "unbounded"
    else:
        math_status, term_reason = "not_solved", "not_solved"

    def num(key: str) -> float | None:
        return None if facts[key] == "none" else float(facts[key])

    col_count = int(facts.get("model_num_columns", facts["num_columns"]))
    return {
        "schema": PRIMAL_SOLUTION_SCHEMA,
        "schema_version": PRIMAL_SOLUTION_VERSION,
        "math_status": math_status,
        "termination_reason": term_reason,
        "has_feasible_solution": has_feasible,
        "column_count": col_count,
        "values": values if has_feasible else [],
        "objective_value": num("objective_value") if has_feasible else None,
        "best_bound": None if (not has_feasible or solve_domain == "restricted") else num("best_bound"),
        "relative_gap": None if (not has_feasible or solve_domain == "restricted") else num("relative_gap"),
        "receipt": {
            "solver": "ies.solver.highs@1.7.2",
            "solve_domain": solve_domain,
            "original_model_verified": verified,
        },
        "extensions": {},
    }


def _minimal_bundle(
    *,
    columns: list[str],
    records: list[list[Any]],
    objective_scale: float = 1.0,
    objective_offset: float = 0.0,
    objective_unit: str = "CNY",
    outputs: list[str] | None = None,
) -> TargetSolverBundle:
    header = {
        "schema": VARIABLE_MAP_SCHEMA,
        "schema_version": VARIABLE_MAP_VERSION,
        "assembly": {"id": "asm-min", "schema_version": "1.0.0"},
        "generator": "acme.generator.custom@1.0.0",
        "records": "input/parameters.jsonl",
        "column_count": len(columns),
        "objective_offset": objective_offset,
        "objective_scale": objective_scale,
        "objective_unit": objective_unit,
        "outputs": outputs or ["solution", "metrics"],
        "precision": "medium",
        "seed": 0,
    }
    jsonl_bytes = "".join(
        json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n" for rec in records
    ).encode("utf-8")
    manifest = TargetSolverBundleManifest(
        bundle={"generator": "acme.generator.custom@1.0.0", "contract": SOLVER_BUNDLE_MPS_CONTRACT},
        inputs=[
            {"path": "input/problem.mps", "media_type": "application/mps"},
            {"path": "input/parameters.json", "media_type": "application/json"},
            {"path": "input/parameters.jsonl", "media_type": "application/x-ndjson"},
        ],
        outputs=[
            {"path": PRIMAL_SOLUTION_OUTPUT_PATH, "media_type": "application/json", "required": True}
        ],
        extensions={},
    )
    return TargetSolverBundle(
        manifest=manifest,
        files={
            "input/problem.mps": MemoryContent(b"NAME DEMO\nENDATA\n"),
            "input/parameters.json": MemoryContent(json.dumps(header).encode("utf-8")),
            "input/parameters.jsonl": MemoryContent(jsonl_bytes),
        },
    )


def test_f2_target_generator_and_highs_e2e_equivalence(tmp_path: Path) -> None:
    """Real campus bundle -> ies-highs-solver -> spawn ResultAdapter matches 1.1.5 output."""
    artifact, models, materialized, finance, legacy_cfg, evaluation = _campus_inputs(seed=7)
    target_cfg = _target_campus_config(seed=7)

    target_bundle = generate_target_highs_bundle(
        artifact, models, materialized, finance, target_cfg, evaluation
    )
    manifest_dict = target_bundle.manifest.to_dict()
    validate_target_bundle(manifest_dict, config=target_cfg)
    assert "solver" not in manifest_dict["bundle"]
    assert "command" not in manifest_dict
    assert "result_adapter" not in manifest_dict

    header = json.loads(read_content(target_bundle.files["input/parameters.json"]))
    assert header["schema"] == VARIABLE_MAP_SCHEMA
    assert header["schema_version"] == VARIABLE_MAP_VERSION
    assert header["objective_scale"] == 1.0
    assert "solver" not in header and "result_adapter" not in header

    sdk_schema_dir = _find_sdk_schema_dir()
    if sdk_schema_dir is not None:
        Draft202012Validator(
            json.loads((sdk_schema_dir / "variable-map-header-1.0.0.schema.json").read_text())
        ).validate(header)

    # Verify target_parameter_chunks emits 4-tuple units and scale/offset on series metadata.
    saw_units = False
    saw_series = False
    for line in read_content(target_bundle.files["input/parameters.jsonl"]).decode("utf-8").splitlines():
        rec = json.loads(line)
        if rec[0] == "units" and rec[1]:
            saw_units = True
            assert len(rec[1][0]) == 4
        if rec[0] == "series":
            saw_series = True
            assert "scale" in rec[1] and "offset" in rec[1]
    assert saw_units and saw_series

    # Materialize bundle inputs and execute real ies-highs-solver (7-arg + F2 flags).
    run_dir = tmp_path / "run"
    for rel, content in target_bundle.files.items():
        dest = run_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(read_content(content))
    (run_dir / "output").mkdir()

    args = [
        str(HIGHS_EXECUTABLE),
        "input/problem.mps",
        "--solution_file",
        "output/solution.sol",
        "--time_limit",
        "60",
        "--random_seed",
        "7",
        "--report_model_columns",
        "true",
    ]
    if "input/acceleration.json" in target_bundle.files:
        args.extend(
            ["--acceleration_mode", "on_demand_mutex", "--acceleration_file", "input/acceleration.json"]
        )
    if EPIGRAPHS_PATH in target_bundle.files:
        args.extend(["--epigraph_file", EPIGRAPHS_PATH])

    proc = subprocess.run(args, cwd=run_dir, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    sol_text = (run_dir / "output" / "solution.sol").read_text(encoding="utf-8")
    primal_payload = _v1_sol_to_primal_json(sol_text)

    adapter = TargetResultAdapter()
    sol_out = tmp_path / "campus_eng_solution.json"
    outcome = adapter.restore_with_outcome(
        target_bundle,
        _succeeded_receipt(),
        {PRIMAL_SOLUTION_OUTPUT_PATH: json.dumps(primal_payload).encode("utf-8")},
        solution_output_path=sol_out,
    )
    assert outcome.child_pid is not None and outcome.child_pid != os.getpid()
    assert outcome.solution_path == sol_out
    assert "engineering_solution" not in outcome.compute_result.outputs
    assert "series" not in outcome.compute_result.outputs
    assert outcome.compute_result.outputs["engineering_solution_path"] == str(sol_out)

    eng = json.loads(b"".join(outcome.solution_content.iter_chunks()).decode("utf-8"))
    assert eng == outcome.load_engineering_solution()
    assert eng["schema"] == ENGINEERING_SOLUTION_SCHEMA
    assert eng["schema_version"] == ENGINEERING_SOLUTION_VERSION
    if sdk_schema_dir is not None:
        Draft202012Validator(
            json.loads((sdk_schema_dir / "engineering-solution-1.0.0.schema.json").read_text())
        ).validate(eng)

    # Compare against 1.1.5 HIGHS_ADAPTER reference on the identical campus inputs.
    legacy_bundle = build_registry((HIGHS_MILP_PROVIDER,)).resolve(legacy_cfg).generate(
        artifact, models, materialized, finance, legacy_cfg, evaluation
    )
    legacy_result = HIGHS_ADAPTER.adapt(
        legacy_bundle,
        _succeeded_receipt(),
        {"output/solution.sol": MemoryContent(sol_text.encode("utf-8"))},
    )
    assert outcome.compute_result.math_status == legacy_result.math_status
    assert eng["solver_summary"]["objective_value"] == pytest.approx(
        legacy_result.outputs["solver_summary"]["objective_value"]
    )
    assert eng["construction_units"] == legacy_result.outputs["financial_facts"]["construction_units"]

    def _assert_series_equal(actual: Any, expected: Any) -> None:
        assert set(actual.keys()) == set(expected.keys())
        for sid, left in actual.items():
            right = expected[sid]
            for meta_key in ("instance_id", "variable_id", "block", "variable", "unit"):
                if meta_key in left or meta_key in right:
                    assert left.get(meta_key) == right.get(meta_key)
            assert list(left["values"]) == pytest.approx(list(right["values"]), rel=1e-12, abs=1e-12)

    _assert_series_equal(eng["series"], legacy_result.outputs["series"])
    _assert_series_equal(eng["system_series"], legacy_result.outputs["system_series"])


def test_f2_epigraphs_emitted_when_monthly_demand_present() -> None:
    """When demand epigraphs exist, input/epigraphs.json is emitted cleanly."""
    from iesplan.computation.generators.linear import LinearExpression, LinearModel, LinearVariable
    from iesplan.computation.generators.matrix import SparseModel
    from iesplan.computation.generators.target_bundle import epigraphs_bytes

    lin = LinearModel(
        variables=(
            LinearVariable("peak", lower=0.0),
            LinearVariable("f0", lower=0.0),
            LinearVariable("f1", lower=0.0),
        ),
        constraints=(),
        objective=LinearExpression(terms=((("peak", 0), 1.0),), constant=0.0),
    )
    sparse = SparseModel.from_public(lin)
    raw = json.loads(epigraphs_bytes(sparse, [("peak", ("f0", "f1"))]))
    assert raw == {
        "schema": "ies.solver-epigraph",
        "schema_version": "1.0.0",
        "num_columns": 3,
        "epigraphs": [{"peak_col": 2, "flow_cols": [0, 1]}],
    }


def test_f2_dual_state_statuses_and_receipt_failures(tmp_path: Path) -> None:
    """Verify feasible/time_limit/infeasible/unbounded and non-succeeded receipt handling."""
    bundle = _minimal_bundle(
        columns=["cap_kw", "p_t0", "p_t1"],
        records=[
            ["column_names", ["cap_kw", "p_t0", "p_t1"]],
            ["expression", 0, 0.0],
            ["terms", [[0, 1.0]]],
            ["expression_end"],
            ["expression", 1, 1.0],
            ["expression_end"],
            ["units", [[0, "kW", 0.001, 0.0]]],
            ["facts", {"construction_units": [
                {
                    "unit_id": "u1",
                    "instance_id": "pv1",
                    "mode": "optional",
                    "built": {"expression": 1},
                    "scales": [{"planning_variable_id": "cap", "value": {"expression": 0}, "unit": "kW"}],
                }
            ]}],
            [
                "series",
                {
                    "series_id": "pv1.p",
                    "instance_id": "pv1",
                    "variable_id": "p",
                    "unit": "kW",
                    "scale": 0.001,
                    "offset": 0.0,
                },
            ],
            ["positions", [1, 2]],
            ["series_end"],
        ],
        objective_scale=2.0,
        objective_offset=10.0,
        objective_unit="CNY",
    )
    adapter = TargetResultAdapter()

    # 1. feasible (time_limit, restricted domain -> null bound/gap)
    primal_restricted = {
        "schema": PRIMAL_SOLUTION_SCHEMA,
        "schema_version": PRIMAL_SOLUTION_VERSION,
        "math_status": "feasible",
        "termination_reason": "time_limit",
        "has_feasible_solution": True,
        "column_count": 3,
        "values": [5000.0, 1000.0, 2000.0],
        "objective_value": 100.0,
        "best_bound": 90.0,
        "relative_gap": 0.1,
        "receipt": {
            "solver": "ies.solver.highs@1.7.2",
            "solve_domain": "restricted",
            "original_model_verified": True,
        },
        "extensions": {},
    }
    res_rest = adapter.restore(
        bundle,
        _succeeded_receipt(),
        {PRIMAL_SOLUTION_OUTPUT_PATH: json.dumps(primal_restricted).encode()},
        solution_output_path=tmp_path / "rest.json",
    )
    assert res_rest.math_status == "feasible"
    assert "engineering_solution" not in res_rest.outputs
    eng_rest = _read_restored(res_rest)
    assert eng_rest["solver_summary"]["objective_value"] == 210.0
    assert eng_rest["solver_summary"]["best_bound"] is None
    assert eng_rest["solver_summary"]["relative_gap"] is None
    assert eng_rest["construction_units"][0]["scales"][0]["value"] == 5.0
    assert eng_rest["series"]["pv1.p"]["values"] == [1.0, 2.0]

    # 2. feasible (time_limit, original domain -> scaled bound & preserved gap)
    primal_tl_orig = {
        **primal_restricted,
        "receipt": {
            "solver": "ies.solver.highs@1.7.2",
            "solve_domain": "original",
            "original_model_verified": True,
        },
    }
    res_orig = adapter.restore(
        bundle,
        _succeeded_receipt(),
        {PRIMAL_SOLUTION_OUTPUT_PATH: json.dumps(primal_tl_orig).encode()},
        solution_output_path=tmp_path / "orig.json",
    )
    eng_orig = _read_restored(res_orig)
    assert eng_orig["solver_summary"]["best_bound"] == 190.0
    assert eng_orig["solver_summary"]["relative_gap"] == 0.1

    # 3. infeasible / unbounded / not_solved / numeric_failure -> solver_summary only
    for math_st, term in (
        ("infeasible", "infeasible"),
        ("unbounded", "unbounded"),
        ("not_solved", "not_solved"),
        ("numeric_failure", "not_solved"),
    ):
        primal_fail = {
            "schema": PRIMAL_SOLUTION_SCHEMA,
            "schema_version": PRIMAL_SOLUTION_VERSION,
            "math_status": math_st,
            "termination_reason": term,
            "has_feasible_solution": False,
            "column_count": 3,
            "values": [],
            "objective_value": None,
            "best_bound": None,
            "relative_gap": None,
            "receipt": {"solver": "ies.solver.highs@1.7.2"},
            "extensions": {},
        }
        res_fail = adapter.restore(
            bundle,
            _succeeded_receipt(),
            {PRIMAL_SOLUTION_OUTPUT_PATH: json.dumps(primal_fail).encode()},
            solution_output_path=tmp_path / f"fail_{math_st}.json",
        )
        assert res_fail.math_status == math_st
        eng_fail = _read_restored(res_fail)
        assert eng_fail["solver_summary"]["has_feasible_solution"] is False
        assert "construction_units" not in eng_fail
        assert "series" not in eng_fail

    # 4. Non-succeeded ExecutionReceipts never inspect residual primal output.
    corrupt_bytes = b"{not valid json"
    for status, reason, expected_exec in (
        ("process_failed", "exited", "failed"),
        ("process_failed", "oom", "resource_exhausted"),
        ("timed_out", "timeout", "timed_out"),
        ("cancelled", "cancelled", "cancelled"),
        ("fenced", "fenced", "fenced"),
        ("output_protocol_failed", "exited", "protocol_failed"),
        ("policy_rejected", "", "rejected"),
    ):
        receipt_kwargs: dict[str, Any] = {
            "receipt_id": "r1",
            "bundle_id": "b1",
            "assembly_id": "a1",
            "calculation_config_id": "c1",
            "attempt_id": "att1",
            "generator_ref": "acme.gen@1.0.0",
            "solver_ref": "acme.sol@1.0.0",
            "executor_ref": "ies.executor.local_process@1.0.0",
            "executable_ref": "acme.sol@1.0.0",
            "result_adapter_ref": "ies.result-adapter.highs@1.0.0",
            "status": status,
            "termination_reason": reason,
        }
        if status not in ("policy_rejected", "cancelled", "fenced"):
            receipt_kwargs["started_at"] = "2026-11-25T10:00:00Z"
            receipt_kwargs["ended_at"] = "2026-11-25T10:00:01Z"
            if reason == "oom":
                receipt_kwargs["signal_number"] = 9
            else:
                receipt_kwargs["exit_code"] = 1
        res_err = adapter.restore(
            bundle,
            ExecutionReceipt(**receipt_kwargs),
            {PRIMAL_SOLUTION_OUTPUT_PATH: corrupt_bytes},
            solution_output_path=tmp_path / f"err_{status}_{reason}.json",
        )
        assert res_err.execution_status == expected_exec
        assert res_err.math_status == "not_solved"
        assert res_err.outputs["solver_summary"]["has_feasible_solution"] is False


def test_f2_affine_conversions_streaming_boundary_and_strict_errors(tmp_path: Path) -> None:
    """Test affine scale/offset (e.g. Kelvin->Celsius), 64KiB boundary token split, and strict errors."""
    bundle = _minimal_bundle(
        columns=["temp_k", "sys_w"],
        records=[
            ["column_names", ["temp_k", "sys_w"]],
            ["units", [[0, "C", 1.0, -273.15], [1, "kW", 0.001, 0.0]]],
            ["facts", {"construction_units": []}],
            [
                "series",
                {
                    "series_id": "dev.t",
                    "instance_id": "hx1",
                    "variable_id": "temp",
                    "unit": "C",
                    "scale": 1.0,
                    "offset": -273.15,
                },
            ],
            ["positions", [0]],
            ["series_end"],
            [
                "series",
                {
                    "series_id": "sys.p",
                    "block": "grid",
                    "variable": "net",
                    "unit": "kW",
                    "scale": 0.001,
                    "offset": 0.0,
                },
            ],
            ["positions", [1]],
            ["series_end"],
        ],
        objective_scale=0.5,
        objective_offset=12.5,
        objective_unit="CNY",
    )

    # Place a float in primal_solution.json right across the 65,536-byte streaming boundary (`1.` | `5`).
    prefix = json.dumps(
        {
            "schema": PRIMAL_SOLUTION_SCHEMA,
            "schema_version": PRIMAL_SOLUTION_VERSION,
            "math_status": "solved",
            "termination_reason": "optimal",
            "has_feasible_solution": True,
            "column_count": 2,
            "objective_value": 20.0,
            "best_bound": 20.0,
            "relative_gap": 0.0,
            "receipt": {"solver": "acme.solver@1.0.0"},
        }
    )
    pad = " " * (65536 - len(prefix) - len('  , "values": [298.') + 2)
    # Construct valid JSON where "298.15" is split at the 65536 chunk edge.
    primal_text = (
        prefix[:-1]
        + ","
        + pad
        + '"values": [298.15, 2500.0]}\n'
    )
    adapter = TargetResultAdapter()
    res = adapter.restore(
        bundle,
        _succeeded_receipt(),
        {PRIMAL_SOLUTION_OUTPUT_PATH: primal_text.encode("utf-8")},
        solution_output_path=tmp_path / "affine.json",
    )
    eng = _read_restored(res)
    assert eng["solver_summary"]["objective_value"] == pytest.approx(22.5)
    assert eng["series"]["dev.t"]["values"][0] == pytest.approx(25.0)
    assert eng["system_series"]["sys.p"]["values"][0] == pytest.approx(2.5)

    # Missing facts.construction_units must fail explicitly (never silent zero-fill).
    no_facts_bundle = _minimal_bundle(
        columns=["x"],
        records=[["column_names", ["x"]], ["units", [[0, "kW", 0.001, 0.0]]]],
    )
    primal_1 = {
        "schema": PRIMAL_SOLUTION_SCHEMA,
        "schema_version": PRIMAL_SOLUTION_VERSION,
        "math_status": "solved",
        "termination_reason": "optimal",
        "has_feasible_solution": True,
        "column_count": 1,
        "values": [1.0],
        "objective_value": 1.0,
        "best_bound": 1.0,
        "relative_gap": 0.0,
        "receipt": {"solver": "acme.solver@1.0.0"},
        "extensions": {},
    }
    with pytest.raises(ComputationUnavailableError, match="construction_units"):
        adapter.restore(
            no_facts_bundle,
            _succeeded_receipt(),
            {PRIMAL_SOLUTION_OUTPUT_PATH: json.dumps(primal_1).encode()},
            solution_output_path=tmp_path / "nofacts.json",
        )

    # Non-finite values must fail explicitly.
    bad_nan = json.dumps({**primal_1, "values": [1.0]}).replace('"values": [1.0]', '"values": [NaN]', 1)
    with pytest.raises(ComputationUnavailableError, match="非有限"):
        adapter.restore(
            bundle,
            _succeeded_receipt(),
            {PRIMAL_SOLUTION_OUTPUT_PATH: bad_nan.encode()},
            solution_output_path=tmp_path / "nan.json",
        )


def test_f2_third_party_mps_solver_replacement_and_no_reverification(tmp_path: Path) -> None:
    """Minimal standalone non-HiGHS .mps solver replaces HiGHS and ResultAdapter never re-verifies math."""
    # Bundle includes an epigraph block with slack (peak=100 != max(flow)=10);
    # ResultAdapter MUST faithfully restore without re-checking constraints or epigraph tightness.
    mps_text = "\n".join(
        [
            "NAME          MINI_3P",
            "ROWS",
            " N  OBJ",
            " G  DEMAND",
            "COLUMNS",
            "    x0        OBJ       2.0    DEMAND    1.0",
            "    x1        OBJ       5.0    DEMAND    1.0",
            "RHS",
            "    RHS1      DEMAND    10.0",
            "BOUNDS",
            " LO BND1      x0        0.0",
            " LO BND1      x1        0.0",
            "ENDATA",
            "",
        ]
    )
    bundle = _minimal_bundle(
        columns=["x0", "x1"],
        records=[
            ["column_names", ["x0", "x1"]],
            ["epigraph", 0],
            ["epigraph_flows", [1]],
            ["epigraph_end"],
            ["expression", 0, 0.0],
            ["terms", [[0, 1.0]]],
            ["expression_end"],
            ["expression", 1, 1.0],
            ["expression_end"],
            ["units", [[0, "kW", 0.001, 0.0], [1, "kW", 0.001, 0.0]]],
            ["facts", {"construction_units": [
                {
                    "unit_id": "unit_3p",
                    "instance_id": "gen1",
                    "mode": "mandatory",
                    "built": {"expression": 1},
                    "scales": [{"planning_variable_id": "cap", "value": {"expression": 0}, "unit": "kW"}],
                }
            ]}],
            [
                "series",
                {
                    "series_id": "gen1.p",
                    "instance_id": "gen1",
                    "variable_id": "p",
                    "unit": "kW",
                    "scale": 0.001,
                    "offset": 0.0,
                },
            ],
            ["positions", [0, 1]],
            ["series_end"],
        ],
    )
    # Replace MPS input with the 2-var MPS.
    bundle = TargetSolverBundle(
        manifest=bundle.manifest,
        files={**dict(bundle.files), "input/problem.mps": MemoryContent(mps_text.encode("utf-8"))},
    )

    # Independent minimal 3rd-party Python MPS solver script (zero highspy / iesplan imports).
    solver_script = tmp_path / "mini_mps_solver.py"
    solver_script.write_text(
        """import json, sys
from pathlib import Path

mps_path, out_path = Path(sys.argv[1]), Path(sys.argv[2])
cols = []
rhs_val = 0.0
obj_coeffs = {}
section = None
for line in mps_path.read_text().splitlines():
    toks = line.split()
    if not toks:
        continue
    if toks[0] in ("NAME", "ROWS", "COLUMNS", "RHS", "BOUNDS", "ENDATA"):
        section = toks[0]
        continue
    if section == "COLUMNS":
        name = toks[0]
        if name not in cols:
            cols.append(name)
        for i in range(1, len(toks), 2):
            if toks[i] == "OBJ":
                obj_coeffs[name] = float(toks[i + 1])
    elif section == "RHS" and toks[1] == "DEMAND":
        rhs_val = float(toks[2])

# Deliberately pick x0=rhs_val, x1=0.0 (optimal for min 2*x0 + 5*x1 s.t. x0+x1>=rhs).
values = [rhs_val if c == cols[0] else 0.0 for c in cols]
obj = sum(obj_coeffs.get(c, 0.0) * v for c, v in zip(cols, values))
out_path.write_text(json.dumps({
    "schema": "ies.primal-solution",
    "schema_version": "1.0.0",
    "math_status": "solved",
    "termination_reason": "optimal",
    "has_feasible_solution": True,
    "column_count": len(cols),
    "values": values,
    "objective_value": obj,
    "best_bound": obj,
    "relative_gap": 0.0,
    "receipt": {"solver": "acme.solver.mini_mps@1.0.0"},
    "extensions": {},
}))
""",
        encoding="utf-8",
    )

    mps_file = tmp_path / "problem.mps"
    primal_file = tmp_path / "primal_solution.json"
    mps_file.write_text(mps_text, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(solver_script), str(mps_file), str(primal_file)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.returncode == 0

    res = TargetResultAdapter().restore(
        bundle,
        _succeeded_receipt(),
        {PRIMAL_SOLUTION_OUTPUT_PATH: MemoryContent(primal_file.read_bytes())},
        solution_output_path=tmp_path / "third_party_eng.json",
    )
    assert res.math_status == "solved"
    eng = _read_restored(res)
    assert eng["solver_summary"]["objective_value"] == 20.0
    assert eng["series"]["gen1.p"]["values"] == [0.01, 0.0]
    assert eng["construction_units"][0]["scales"][0]["value"] == 0.01


def _slow_restore_stub(*_args: Any) -> None:
    time.sleep(5.0)


def _rlimit_hard_capped_entry(
    bundle_dir_str: str,
    primal_path_str: str,
    solution_out_str: str,
    result_meta_str: str,
    memory_limit_mb: int,
) -> None:
    import iesplan.computation.result_adapters.target_adapter as ta

    _soft, _hard = resource.getrlimit(resource.RLIMIT_AS)
    tiny_hard = 64 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (tiny_hard, tiny_hard))
    ta._subprocess_restore_entry(
        bundle_dir_str,
        primal_path_str,
        solution_out_str,
        result_meta_str,
        memory_limit_mb,
    )


def test_f2_subprocess_isolation_memory_timeout_and_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify spawn PID isolation, streamed large output RSS bound, limits, and cancellation."""
    import iesplan.computation.result_adapters.target_adapter as ta

    # 1. Restore 500,000 series points (>3.5 MiB JSON) in spawn child with bounded parent RSS.
    ncols = 500_000
    bundle_dir = tmp_path / "large_bundle"
    (bundle_dir / "input").mkdir(parents=True)
    (bundle_dir / "output").mkdir(parents=True)
    header = {
        "schema": VARIABLE_MAP_SCHEMA,
        "schema_version": VARIABLE_MAP_VERSION,
        "assembly": {"id": "asm-large", "schema_version": "1.0.0"},
        "generator": "acme.gen@1.0.0",
        "records": "input/parameters.jsonl",
        "column_count": ncols,
        "objective_offset": 0.0,
        "objective_scale": 1.0,
        "objective_unit": "CNY",
        "outputs": ["metrics"],
        "precision": "medium",
        "seed": 0,
    }
    (bundle_dir / "input" / "parameters.json").write_text(json.dumps(header), encoding="utf-8")
    with (bundle_dir / "input" / "parameters.jsonl").open("w", encoding="utf-8") as fh:
        for start in range(0, ncols, 2000):
            names = [f"x{i}" for i in range(start, min(ncols, start + 2000))]
            fh.write(json.dumps(["column_names", names]) + "\n")
        fh.write(json.dumps(["units", [[0, "kW", 0.001, 0.0], [ncols - 1, "kW", 0.001, 0.0]]]) + "\n")
        fh.write(json.dumps(["facts", {"construction_units": []}]) + "\n")
        for s_idx in range(5):
            series_meta = {
                "series_id": f"s{s_idx}",
                "instance_id": "d1",
                "variable_id": f"p{s_idx}",
                "unit": "kW",
                "scale": 0.001,
                "offset": 0.0,
            }
            fh.write(json.dumps(["series", series_meta]) + "\n")
            base = s_idx * 100_000
            for start in range(base, base + 100_000, 2000):
                fh.write(json.dumps(["positions", list(range(start, start + 2000))]) + "\n")
            fh.write(json.dumps(["series_end"]) + "\n")

    primal_path = bundle_dir / "output" / "primal_solution.json"
    with primal_path.open("w", encoding="utf-8") as fh:
        fh.write(
            '{"schema":"ies.primal-solution","schema_version":"1.0.0","math_status":"solved",'
            '"termination_reason":"optimal","has_feasible_solution":true,"column_count":'
            f'{ncols},"objective_value":12.0,"best_bound":12.0,"relative_gap":0.0,'
            '"receipt":{"solver":"acme.sol@1.0.0","solver_diagnostics":{"trace":['
        )
        diag_chunk = ",".join("1" for _ in range(5000))
        for start in range(0, 500_000, 5000):
            if start > 0:
                fh.write(",")
            fh.write(diag_chunk)
        fh.write(']}},"values":[')
        chunk = ",".join("1000.0" for _ in range(5000))
        for start in range(0, ncols, 5000):
            if start > 0:
                fh.write(",")
            fh.write(chunk)
        fh.write("]}\n")

    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    sol_out = tmp_path / "solution.json"
    outcome = restore_from_bundle_dir(
        bundle_dir,
        primal_path,
        _succeeded_receipt(),
        solution_output_path=sol_out,
        memory_limit_mb=512,
        timeout_seconds=60.0,
    )
    rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    assert outcome.child_pid is not None and outcome.child_pid != os.getpid()
    assert sol_out.is_file() and sol_out.stat().st_size > 2_000_000
    assert outcome.solution_content.size_bytes == sol_out.stat().st_size
    assert "engineering_solution" not in outcome.compute_result.outputs
    assert "series" not in outcome.compute_result.outputs
    assert "solver_diagnostics" not in outcome.solver_summary
    assert "solver_diagnostics" not in outcome.compute_result.outputs["solver_summary"]
    assert outcome.compute_result.outputs["engineering_solution_path"] == str(sol_out)
    # Parent RSS growth remains bounded (< 16 MiB) for 500k series + 500k solver_diagnostics.
    assert (rss_after - rss_before) < 16 * 1024
    assert (
        len(outcome.load_engineering_solution()["solver_summary"]["solver_diagnostics"]["trace"])
        == 500_000
    )

    # 2. Invalid budgets raise ValueError, and real RLIMIT_AS failure + OOM surface cleanly.
    with pytest.raises(ValueError):
        restore_from_bundle_dir(bundle_dir, primal_path, _succeeded_receipt(), memory_limit_mb=0)
    with pytest.raises(ValueError):
        restore_from_bundle_dir(bundle_dir, primal_path, _succeeded_receipt(), timeout_seconds=0.0)
    with pytest.raises(ValueError):
        restore_from_bundle_dir(
            bundle_dir, primal_path, _succeeded_receipt(), timeout_seconds=float("nan")
        )

    with pytest.raises(ComputationUnavailableError) as exc_oom:
        restore_from_bundle_dir(
            bundle_dir,
            primal_path,
            _succeeded_receipt(),
            solution_output_path=tmp_path / "oom.json",
            memory_limit_mb=1,
            timeout_seconds=30.0,
        )
    assert exc_oom.value.reason == "resource_exhausted"

    # 3. Pre-spawn stop probe aborts without starting child process.
    with pytest.raises(ComputationUnavailableError) as exc_pre:
        restore_from_bundle_dir(
            bundle_dir, primal_path, _succeeded_receipt(), stop=lambda: "cancelled"
        )
    assert exc_pre.value.reason == "cancelled"

    # 4. Cancellation and single shared timeout cover streaming input materialization.
    small_bundle = _minimal_bundle(
        columns=["x0"],
        records=[["column_names", ["x0"]], ["units", [[0, "kW", 0.001, 0.0]]]],
    )
    primal_small = json.dumps(
        {
            "schema": PRIMAL_SOLUTION_SCHEMA,
            "schema_version": PRIMAL_SOLUTION_VERSION,
            "math_status": "solved",
            "termination_reason": "optimal",
            "has_feasible_solution": True,
            "column_count": 1,
            "values": [1.0],
            "objective_value": 1.0,
            "best_bound": 1.0,
            "relative_gap": 0.0,
            "receipt": {"solver": "acme.sol@1.0.0"},
            "extensions": {},
        }
    ).encode("utf-8")
    cancel_state = {"stopped": False}

    def _slow_chunks() -> Any:
        yield b'{"schema":'
        cancel_state["stopped"] = True
        yield b'"ies.primal-solution"}'

    with pytest.raises(ComputationUnavailableError) as exc_mat_cancel:
        TargetResultAdapter().restore_with_outcome(
            small_bundle,
            _succeeded_receipt(),
            {PRIMAL_SOLUTION_OUTPUT_PATH: GeneratedContent(_slow_chunks)},
            solution_output_path=tmp_path / "mat_cancel.json",
            stop=lambda: "cancelled" if cancel_state["stopped"] else None,
        )
    assert exc_mat_cancel.value.reason == "cancelled"

    def _timeout_chunks() -> Any:
        yield b" "
        time.sleep(0.25)
        yield primal_small

    with pytest.raises(ComputationUnavailableError) as exc_mat_to:
        TargetResultAdapter().restore_with_outcome(
            small_bundle,
            _succeeded_receipt(),
            {PRIMAL_SOLUTION_OUTPUT_PATH: GeneratedContent(_timeout_chunks)},
            solution_output_path=tmp_path / "mat_to.json",
            timeout_seconds=0.1,
        )
    assert exc_mat_to.value.reason == "timed_out"

    # 5. setrlimit failures, timeout, cancellation, and auto-allocated temp cleanup.
    auto_tmp = tmp_path / "auto_out_tmp"
    auto_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(auto_tmp))
    bad_primal = tmp_path / "bad_primal.json"
    bad_primal.write_text("{broken", encoding="utf-8")
    with pytest.raises(ComputationUnavailableError):
        restore_from_bundle_dir(bundle_dir, bad_primal, _succeeded_receipt())
    assert list(auto_tmp.iterdir()) == []

    tests_dir = str(Path(__file__).resolve().parent)
    prev_pythonpath = os.environ.get("PYTHONPATH")
    monkeypatch.setenv(
        "PYTHONPATH",
        f"{tests_dir}{os.pathsep}{prev_pythonpath}" if prev_pythonpath else tests_dir,
    )
    monkeypatch.setattr(ta, "_subprocess_restore_entry", _rlimit_hard_capped_entry)
    with pytest.raises(ComputationUnavailableError) as exc_rlimit:
        restore_from_bundle_dir(
            bundle_dir,
            primal_path,
            _succeeded_receipt(),
            memory_limit_mb=512,
        )
    assert exc_rlimit.value.reason == "resource-limit-failed"
    assert list(auto_tmp.iterdir()) == []

    monkeypatch.setattr(ta, "_subprocess_restore_entry", _slow_restore_stub)
    with pytest.raises(ComputationUnavailableError) as exc_to:
        restore_from_bundle_dir(
            bundle_dir, primal_path, _succeeded_receipt(), timeout_seconds=0.15
        )
    assert exc_to.value.reason == "timed_out"
    assert list(auto_tmp.iterdir()) == []

    calls = 0

    def _cancel_after_two() -> str | None:
        nonlocal calls
        calls += 1
        return "cancelled" if calls >= 3 else None

    with pytest.raises(ComputationUnavailableError) as exc_cancel:
        restore_from_bundle_dir(
            bundle_dir, primal_path, _succeeded_receipt(), stop=_cancel_after_two, timeout_seconds=5.0
        )
    assert exc_cancel.value.reason == "cancelled"
    assert list(auto_tmp.iterdir()) == []


def test_f2_planning_scale_explicit_conversions_and_no_unit_label_guessing(tmp_path: Path) -> None:
    """Planning scales require explicit or unit-matched conversions; never infer from unit labels."""
    from iesplan.computation.generators.linear import LinearExpression, LinearModel, LinearVariable
    from iesplan.computation.generators.matrix import SparseModel
    from iesplan.computation.generators.target_bundle import target_parameter_records

    # 1. target_parameter_records attaches explicit scale/offset on every construction_units[*].scales[*].
    lin = LinearModel(
        variables=(LinearVariable("cap_w", lower=0.0),),
        constraints=(),
        objective=LinearExpression(terms=((("cap_w", 0), 1.0),), constant=0.0),
    )
    sparse = SparseModel.from_public(lin)
    records_with_val = list(
        target_parameter_records(
            sparse,
            {"cap_w": "W"},
            {},
            (),
            {
                "construction_units": [
                    {
                        "unit_id": "u1",
                        "instance_id": "pv1",
                        "mode": "optional",
                        "built": {"constant": 1.0, "terms": {}},
                        "scales": [
                            {
                                "planning_variable_id": "cap",
                                "value": {"constant": 0.0, "terms": {"cap_w": 1.0}},
                                "unit": "kW",
                            }
                        ],
                    }
                ]
            },
            {"groups": []},
        )
    )
    facts_doc = next(rec[1] for rec in records_with_val if rec[0] == "facts")
    sc_emitted = facts_doc["construction_units"][0]["scales"][0]
    assert sc_emitted["scale"] == pytest.approx(0.001)
    assert sc_emitted["offset"] == pytest.approx(0.0)

    generated_bundle = _minimal_bundle(columns=["cap_w"], records=records_with_val)

    primal_1 = json.dumps(
        {
            "schema": PRIMAL_SOLUTION_SCHEMA,
            "schema_version": PRIMAL_SOLUTION_VERSION,
            "math_status": "solved",
            "termination_reason": "optimal",
            "has_feasible_solution": True,
            "column_count": 1,
            "values": [1000.0],
            "objective_value": 100.0,
            "best_bound": 100.0,
            "relative_gap": 0.0,
            "receipt": {"solver": "acme.sol@1.0.0"},
            "extensions": {},
        }
    ).encode("utf-8")

    res_gen = TargetResultAdapter().restore(
        generated_bundle,
        _succeeded_receipt(),
        {PRIMAL_SOLUTION_OUTPUT_PATH: primal_1},
        solution_output_path=tmp_path / "gen_explicit.json",
    )
    assert _read_restored(res_gen)["construction_units"][0]["scales"] == [
        {"planning_variable_id": "cap", "value": pytest.approx(1.0), "unit": "kW"}
    ]

    # 2. Mismatched column unit [0, "W", 1, 0] with fact unit "kW" and no explicit scale must fail.
    mismatched_bundle = _minimal_bundle(
        columns=["x0"],
        records=[
            ["column_names", ["x0"]],
            ["expression", 0, 0.0],
            ["terms", [[0, 1.0]]],
            ["expression_end"],
            ["expression", 1, 1.0],
            ["expression_end"],
            ["units", [[0, "W", 1.0, 0.0]]],
            [
                "facts",
                {
                    "construction_units": [
                        {
                            "unit_id": "u1",
                            "instance_id": "pv1",
                            "mode": "optional",
                            "built": {"expression": 1},
                            "scales": [
                                {"planning_variable_id": "cap", "value": {"expression": 0}, "unit": "kW"}
                            ],
                        }
                    ]
                },
            ],
        ],
    )
    with pytest.raises(ComputationUnavailableError, match="显式 scale/offset"):
        TargetResultAdapter().restore(
            mismatched_bundle,
            _succeeded_receipt(),
            {PRIMAL_SOLUTION_OUTPUT_PATH: primal_1},
            solution_output_path=tmp_path / "mismatch.json",
        )

    # 3. Same unit label "kW" with distinct explicit conversions obeys each quantity's own coefficients.
    explicit_bundle = _minimal_bundle(
        columns=["x0"],
        records=[
            ["column_names", ["x0"]],
            ["expression", 0, 0.0],
            ["terms", [[0, 1.0]]],
            ["expression_end"],
            ["expression", 1, 1.0],
            ["expression_end"],
            ["units", [[0, "kW", 10.0, 0.0]]],
            [
                "facts",
                {
                    "construction_units": [
                        {
                            "unit_id": "u1",
                            "instance_id": "pv1",
                            "mode": "optional",
                            "built": {"expression": 1},
                            "scales": [
                                {
                                    "planning_variable_id": "cap_a",
                                    "value": {"expression": 0},
                                    "unit": "kW",
                                    "scale": 0.001,
                                    "offset": 0.0,
                                },
                                {
                                    "planning_variable_id": "cap_b",
                                    "value": {"expression": 0},
                                    "unit": "kW",
                                    "scale": 2.0,
                                    "offset": 5.0,
                                },
                            ],
                        }
                    ]
                },
            ],
        ],
    )
    res_exp = TargetResultAdapter().restore(
        explicit_bundle,
        _succeeded_receipt(),
        {PRIMAL_SOLUTION_OUTPUT_PATH: primal_1},
        solution_output_path=tmp_path / "explicit.json",
    )
    eng_exp = _read_restored(res_exp)
    assert eng_exp["construction_units"][0]["scales"] == [
        {"planning_variable_id": "cap_a", "value": 1.0, "unit": "kW"},
        {"planning_variable_id": "cap_b", "value": 2005.0, "unit": "kW"},
    ]


def test_f2_f1_boundary_validation_rejects_malformed_inputs(tmp_path: Path) -> None:
    """Enforce canonical F1 header, record-order, series-identity, and primal field validation."""
    adapter = TargetResultAdapter()
    valid_bundle = _minimal_bundle(
        columns=["x0"],
        records=[
            ["column_names", ["x0"]],
            ["units", [[0, "kW", 0.001, 0.0]]],
            ["facts", {"construction_units": []}],
        ],
    )
    valid_primal = {
        "schema": PRIMAL_SOLUTION_SCHEMA,
        "schema_version": PRIMAL_SOLUTION_VERSION,
        "math_status": "solved",
        "termination_reason": "optimal",
        "has_feasible_solution": True,
        "column_count": 1,
        "values": [1.0],
        "objective_value": 1.0,
        "best_bound": 1.0,
        "relative_gap": 0.0,
        "receipt": {"solver": "acme.solver@1.0.0"},
        "extensions": {},
    }
    valid_primal_bytes = json.dumps(valid_primal).encode("utf-8")

    # 1. Invalid variable_map headers (unknown field, bad generator ref, array extensions).
    base_header = json.loads(read_content(valid_bundle.files["input/parameters.json"]))
    for bad_header in (
        {**base_header, "unknown_key": 1},
        {**base_header, "generator": "not-a-semver-ref"},
        {**base_header, "extensions": []},
        {**base_header, "assembly": {"id": "asm-1"}},
    ):
        bad_b = TargetSolverBundle(
            manifest=valid_bundle.manifest,
            files={
                **dict(valid_bundle.files),
                "input/parameters.json": MemoryContent(json.dumps(bad_header).encode("utf-8")),
            },
        )
        with pytest.raises(ComputationUnavailableError):
            adapter.restore(
                bad_b,
                _succeeded_receipt(),
                {PRIMAL_SOLUTION_OUTPUT_PATH: valid_primal_bytes},
                solution_output_path=tmp_path / "bad_hdr.json",
            )

    # 2. Invalid variable_map records (mixed device+system series, unknown core key, wrong order).
    bad_record_sets = (
        [
            ["column_names", ["x0"]],
            ["facts", {"construction_units": []}],
            [
                "series",
                {
                    "series_id": "s1",
                    "instance_id": "d1",
                    "variable_id": "p",
                    "block": "grid",
                    "variable": "net",
                    "unit": "kW",
                    "scale": 0.001,
                    "offset": 0.0,
                },
            ],
            ["positions", [0]],
            ["series_end"],
        ],
        [
            ["column_names", ["x0"]],
            ["facts", {"construction_units": []}],
            [
                "series",
                {
                    "series_id": "s1",
                    "instance_id": "d1",
                    "variable_id": "p",
                    "unit": "kW",
                    "scale": 0.001,
                    "offset": 0.0,
                    "extra_core": 1,
                },
            ],
            ["positions", [0]],
            ["series_end"],
        ],
        [
            ["column_names", ["x0"]],
            ["facts", {"construction_units": []}],
            ["units", [[0, "kW", 0.001, 0.0]]],
        ],
    )
    for rec_list in bad_record_sets:
        jsonl = "".join(json.dumps(r) + "\n" for r in rec_list).encode("utf-8")
        bad_b = TargetSolverBundle(
            manifest=valid_bundle.manifest,
            files={**dict(valid_bundle.files), "input/parameters.jsonl": MemoryContent(jsonl)},
        )
        with pytest.raises(ComputationUnavailableError):
            adapter.restore(
                bad_b,
                _succeeded_receipt(),
                {PRIMAL_SOLUTION_OUTPUT_PATH: valid_primal_bytes},
                solution_output_path=tmp_path / "bad_rec.json",
            )

    # 3. Invalid primal_solution fields (bool obj, string bound/gap, malformed receipt, list extensions).
    for bad_primal in (
        {**valid_primal, "objective_value": True},
        {**valid_primal, "best_bound": "1.0"},
        {**valid_primal, "relative_gap": "0.0"},
        {**valid_primal, "receipt": {"solver": "bad-ref"}},
        {**valid_primal, "receipt": {"solver": "acme.solver@1.0.0", "extra": 1}},
        {**valid_primal, "extensions": []},
    ):
        with pytest.raises(ComputationUnavailableError):
            adapter.restore(
                valid_bundle,
                _succeeded_receipt(),
                {PRIMAL_SOLUTION_OUTPUT_PATH: json.dumps(bad_primal).encode("utf-8")},
                solution_output_path=tmp_path / "bad_primal.json",
            )
