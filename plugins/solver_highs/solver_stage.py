"""Official built-in `ies.solver.highs@1.7.2` stage plugin entrypoint emitting `ies.primal-solution@1.0.0`."""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pies_sdk import (
    PrimalSolutionHeader,
    StageContext,
    StageIOError,
    check_relative_path,
    read_json_file,
    read_solver_bundle,
    write_primal_solution_streaming,
)

HIGHS_EXECUTABLE_PATH = "/usr/local/bin/ies-highs-solver"
SOLVER_REF = "ies.solver.highs@1.7.2"
_SOLVE_DOMAINS = frozenset(("original", "valid_relaxation", "restricted"))
_DEFAULT_TIME_LIMIT = 3600
_DEFAULT_SEED = 0


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _extract_runtime_options(ctx: StageContext) -> tuple[float, int, int | None]:
    """Read time_limit_seconds, seed, and threads from /work/in request or config."""
    time_limit: float = float(_DEFAULT_TIME_LIMIT)
    seed: int = _DEFAULT_SEED

    req_path = ctx.work_in / "request.json"
    if req_path.is_file():
        req = read_json_file(req_path)
        if isinstance(req, Mapping):
            if req.get("time_limit_seconds") is not None:
                time_limit = float(req["time_limit_seconds"])
            if req.get("seed") is not None:
                seed = int(req["seed"])

    cfg_path = ctx.work_in / "calculation_config.json"
    if cfg_path.is_file():
        cfg = read_json_file(cfg_path)
        if isinstance(cfg, Mapping):
            if cfg.get("time_limit_seconds") is not None:
                time_limit = float(cfg["time_limit_seconds"])
            if cfg.get("seed") is not None:
                seed = int(cfg["seed"])

    if not math.isfinite(time_limit) or time_limit <= 0:
        raise StageIOError(f"Invalid solver time_limit_seconds: {time_limit!r}")
    if seed < 0:
        raise StageIOError(f"Invalid solver seed: {seed!r}")

    threads_raw = ctx.parameters.get("threads")
    threads: int | None = None
    if threads_raw is not None:
        if isinstance(threads_raw, bool) or not isinstance(threads_raw, int) or threads_raw < 1:
            raise StageIOError(f"Invalid solver threads parameter: {threads_raw!r}")
        threads = threads_raw
    return time_limit, seed, threads


def _parse_optional_float(raw: str, name: str) -> float | None:
    if raw == "none":
        return None
    val = float(raw)
    if not math.isfinite(val):
        raise StageIOError(f"Non-finite {name} in HIGHS_SOLUTION_V1: {raw!r}")
    return val


def _map_math_status(
    model_status: str,
    has_feasible: bool,
    solve_domain: str,
    original_model_verified: bool,
) -> tuple[str, str]:
    if not original_model_verified:
        status = "numeric_failure" if model_status == "HighsModelStatus.kOptimal" else "not_solved"
        return status, "not_solved"
    if solve_domain == "restricted":
        if has_feasible:
            if model_status == "HighsModelStatus.kOptimal":
                return "feasible", "optimal"
            if model_status == "HighsModelStatus.kTimeLimit":
                return "feasible", "time_limit"
            return "feasible", "not_solved"
        if model_status == "HighsModelStatus.kTimeLimit":
            return "not_solved", "time_limit"
        return "not_solved", "not_solved"

    if model_status == "HighsModelStatus.kOptimal":
        return ("solved", "optimal") if has_feasible else ("numeric_failure", "not_solved")
    if model_status == "HighsModelStatus.kTimeLimit":
        return ("feasible" if has_feasible else "not_solved"), "time_limit"
    if model_status == "HighsModelStatus.kInfeasible":
        return "infeasible", "infeasible"
    if model_status == "HighsModelStatus.kUnbounded":
        if solve_domain == "valid_relaxation":
            return "not_solved", "not_solved"
        return "unbounded", "unbounded"
    return "not_solved", "not_solved"


def _stream_v1_solution_to_primal_json(
    sol_path: Path,
    out_path: Path,
    *,
    started_at: str,
    ended_at: str,
    elapsed_seconds: float,
) -> Path:
    """Stream HIGHS_SOLUTION_V1 directly into 0-based `ies.primal-solution@1.0.0` JSON."""
    with sol_path.open("r", encoding="utf-8") as handle:
        first = handle.readline().rstrip("\r\n")
        if first != "HIGHS_SOLUTION_V1":
            raise StageIOError(f"Expected HIGHS_SOLUTION_V1 header, got {first!r}")

        facts: dict[str, str] = {}
        for line in handle:
            stripped = line.rstrip("\r\n")
            if stripped == "primal:":
                break
            key, sep, value = stripped.partition(":")
            if not sep or not key.strip():
                raise StageIOError(f"Malformed HIGHS_SOLUTION_V1 header line: {stripped!r}")
            facts[key.strip()] = value.strip()
        else:
            raise StageIOError("HIGHS_SOLUTION_V1 missing 'primal:' section")

        for req in (
            "model_status",
            "has_feasible_solution",
            "objective_value",
            "best_bound",
            "relative_gap",
            "num_columns",
        ):
            if req not in facts:
                raise StageIOError(f"HIGHS_SOLUTION_V1 missing required header {req!r}")

        model_status = facts["model_status"]
        raw_feasible = facts["has_feasible_solution"]
        if raw_feasible not in ("true", "false"):
            raise StageIOError(f"Invalid has_feasible_solution: {raw_feasible!r}")
        raw_has_feasible = raw_feasible == "true"

        solve_domain = facts.get("solve_domain", "original")
        if solve_domain not in _SOLVE_DOMAINS:
            raise StageIOError(f"Invalid solve_domain: {solve_domain!r}")

        raw_verified = facts.get("original_model_verified")
        if raw_verified is None:
            original_model_verified = solve_domain == "original"
        elif raw_verified in ("true", "false"):
            original_model_verified = raw_verified == "true"
        else:
            raise StageIOError(f"Invalid original_model_verified: {raw_verified!r}")

        if "epigraph_verified" in facts:
            raw_epi = facts["epigraph_verified"]
            if raw_epi not in ("true", "false"):
                raise StageIOError(f"Invalid epigraph_verified: {raw_epi!r}")
            if raw_epi == "false":
                original_model_verified = False

        num_primal_rows = int(facts["num_columns"])
        column_count = int(facts.get("model_num_columns", facts["num_columns"]))
        raw_obj = _parse_optional_float(facts["objective_value"], "objective_value")
        raw_bound = _parse_optional_float(facts["best_bound"], "best_bound")
        raw_gap = _parse_optional_float(facts["relative_gap"], "relative_gap")

        math_status, termination_reason = _map_math_status(
            model_status,
            raw_has_feasible,
            solve_domain,
            original_model_verified,
        )
        publish_feasible = math_status in ("solved", "feasible") and original_model_verified
        if publish_feasible and num_primal_rows != column_count:
            raise StageIOError(
                f"HIGHS_SOLUTION_V1 primal count ({num_primal_rows}) != model column_count ({column_count})"
            )

        diagnostics: dict[str, Any] = {
            "model_status": model_status,
        }
        if "epigraph_verified" in facts:
            diagnostics["epigraph_verified"] = facts["epigraph_verified"] == "true"
        if "subdomain_status" in facts:
            diagnostics["subdomain_status"] = (
                None if facts["subdomain_status"] == "none" else facts["subdomain_status"]
            )
        if "subdomain_best_bound" in facts:
            diagnostics["subdomain_best_bound"] = _parse_optional_float(
                facts["subdomain_best_bound"], "subdomain_best_bound"
            )
        if "subdomain_relative_gap" in facts:
            diagnostics["subdomain_relative_gap"] = _parse_optional_float(
                facts["subdomain_relative_gap"], "subdomain_relative_gap"
            )

        receipt: dict[str, Any] = {
            "solver": SOLVER_REF,
            "started_at": started_at,
            "ended_at": ended_at,
            "elapsed_seconds": elapsed_seconds,
            "solve_domain": solve_domain,
            "original_model_verified": original_model_verified,
            "solver_diagnostics": diagnostics,
        }

        header = PrimalSolutionHeader(
            math_status=math_status,
            termination_reason=termination_reason,
            has_feasible_solution=publish_feasible,
            column_count=column_count,
            objective_value=raw_obj if publish_feasible else None,
            best_bound=None if solve_domain == "restricted" else raw_bound,
            relative_gap=(
                None
                if (not publish_feasible or solve_domain == "restricted")
                else raw_gap
            ),
            receipt=receipt,
        )

        def _iter_primals() -> Iterator[float]:
            if not publish_feasible:
                return
            expected_idx = 0
            for raw_line in handle:
                stripped = raw_line.rstrip("\r\n")
                if not stripped:
                    continue
                idx_str, sep, val_str = stripped.partition(":")
                if not sep:
                    raise StageIOError(f"Malformed primal line in HIGHS_SOLUTION_V1: {stripped!r}")
                pos = int(idx_str.strip())
                if pos != expected_idx:
                    raise StageIOError(
                        f"Non-sequential 0-based primal index: expected {expected_idx}, got {pos}"
                    )
                expected_idx += 1
                yield float(val_str.strip())

        return write_primal_solution_streaming(out_path, header, _iter_primals())


def solve(ctx: StageContext) -> None:
    """Execute `ies-highs-solver` against `ies.solver-bundle@1.0.0` and emit `primal_solution.json`."""
    manifest, bundle_dir = read_solver_bundle(ctx.work_in)
    if manifest.get("schema") != "ies.solver-bundle" or manifest.get("schema_version") != "1.0.0":
        raise StageIOError("Invalid Solver Bundle schema in /work/in/bundle/bundle.yaml")
    bundle_meta = manifest.get("bundle")
    if not isinstance(bundle_meta, Mapping) or "contract" not in bundle_meta:
        raise StageIOError("Solver Bundle missing bundle.contract")

    declared_inputs = {
        check_relative_path(item["path"], "bundle.inputs.path")
        for item in manifest.get("inputs", [])
    }
    if "input/problem.mps" not in declared_inputs or not (bundle_dir / "input/problem.mps").is_file():
        raise StageIOError("Solver Bundle missing required input/problem.mps")

    declared_outputs = {
        check_relative_path(item["path"], "bundle.outputs.path")
        for item in manifest.get("outputs", [])
    }
    if "output/primal_solution.json" not in declared_outputs:
        raise StageIOError("Solver Bundle outputs must declare output/primal_solution.json")

    time_limit, seed, threads = _extract_runtime_options(ctx)

    arguments = [
        "input/problem.mps",
        "--solution_file",
        "tmp_out/solution.sol",
        "--time_limit",
        str(time_limit),
        "--random_seed",
        str(seed),
        "--report_model_columns",
        "true",
    ]
    if (
        "input/acceleration.json" in declared_inputs
        and (bundle_dir / "input/acceleration.json").is_file()
    ):
        arguments.extend(
            [
                "--acceleration_mode",
                "on_demand_mutex",
                "--acceleration_file",
                "input/acceleration.json",
            ]
        )
    if "input/epigraphs.json" in declared_inputs and (bundle_dir / "input/epigraphs.json").is_file():
        arguments.extend(["--epigraph_file", "input/epigraphs.json"])
    if threads is not None:
        arguments.extend(["--threads", str(threads)])

    run_dir = Path(tempfile.mkdtemp(prefix="ies-highs-run-"))
    try:
        os.symlink(str((bundle_dir / "input").resolve()), str(run_dir / "input"))
        tmp_out = run_dir / "tmp_out"
        tmp_out.mkdir()

        started_at = _utc_now()
        t0 = time.monotonic()
        proc = subprocess.run(
            [HIGHS_EXECUTABLE_PATH, *arguments],
            cwd=run_dir,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp"},
            capture_output=True,
            text=True,
            check=False,
        )
        elapsed_seconds = max(0.0, time.monotonic() - t0)
        ended_at = _utc_now()

        if proc.returncode != 0:
            raise RuntimeError(
                f"ies-highs-solver failed with exit code {proc.returncode}: {proc.stderr.strip()}"
            )

        sol_path = tmp_out / "solution.sol"
        if not sol_path.is_file():
            raise StageIOError("ies-highs-solver exited 0 without writing temporary solution file")

        _stream_v1_solution_to_primal_json(
            sol_path,
            ctx.work_out / "primal_solution.json",
            started_at=started_at,
            ended_at=ended_at,
            elapsed_seconds=elapsed_seconds,
        )
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
