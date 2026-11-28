"""Reference Python `highspy` solver plugin (`ies.solver.highspy_ref@1.0.0`)."""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from typing import Any

import highspy
from pies_sdk import (
    PrimalSolutionHeader,
    StageContext,
    StageIOError,
    read_json_file,
    write_primal_solution_streaming,
)

_DEFAULT_TIME_LIMIT = 3600.0
_DEFAULT_SEED = 0


def _extract_runtime_options(ctx: StageContext) -> tuple[float, int, int | None]:
    time_limit: float = _DEFAULT_TIME_LIMIT
    seed: int = _DEFAULT_SEED

    for filename in ("request.json", "calculation_config.json"):
        cfg_path = ctx.work_in / filename
        if not cfg_path.is_file():
            continue
        cfg = read_json_file(cfg_path)
        if not isinstance(cfg, Mapping):
            continue
        raw_tl = cfg.get("time_limit_seconds")
        if raw_tl is not None:
            if isinstance(raw_tl, bool) or not isinstance(raw_tl, (int, float)):
                raise StageIOError(f"Invalid solver time_limit_seconds: {raw_tl!r}")
            time_limit = float(raw_tl)
        raw_seed = cfg.get("seed")
        if raw_seed is not None:
            if isinstance(raw_seed, bool) or not isinstance(raw_seed, int):
                raise StageIOError(f"Invalid solver seed: {raw_seed!r}")
            seed = raw_seed

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


def _map_status(model_status_name: str, has_feasible: bool) -> tuple[str, str]:
    if model_status_name == "kOptimal":
        return ("solved", "optimal") if has_feasible else ("numeric_failure", "not_solved")
    if model_status_name == "kTimeLimit":
        return ("feasible" if has_feasible else "not_solved"), "time_limit"
    if model_status_name == "kInfeasible":
        return "infeasible", "infeasible"
    if model_status_name == "kUnbounded":
        return "unbounded", "unbounded"
    return "not_solved", "not_solved"


def solve(ctx: StageContext) -> None:
    """Solve `/work/in/bundle/input/problem.mps` with `highspy` and stream `primal_solution.json`."""
    mps_path = ctx.work_in / "bundle" / "input" / "problem.mps"
    time_limit, seed, threads = _extract_runtime_options(ctx)

    highs = highspy.Highs()
    highs.setOptionValue("output_flag", False)
    highs.setOptionValue("time_limit", time_limit)
    highs.setOptionValue("random_seed", seed)
    if threads is not None:
        highs.setOptionValue("threads", threads)

    read_status = highs.readModel(str(mps_path.resolve()))
    if read_status != highspy.HighsStatus.kOk:
        raise StageIOError(f"highspy failed to read MPS model: {read_status}")

    highs.run()

    model_status = highs.getModelStatus()
    model_status_name = getattr(model_status, "name", str(model_status))
    info = highs.getInfo()
    lp = highs.getLp()
    column_count = int(highs.getNumCol())
    is_mip = any(int(kind) != 0 for kind in getattr(lp, "integrality_", ()))
    has_feasible = (
        int(info.primal_solution_status) == int(highspy.SolutionStatus.kSolutionStatusFeasible)
    )

    math_status, termination_reason = _map_status(model_status_name, has_feasible)
    publish_feasible = math_status in ("solved", "feasible")

    raw_obj = float(info.objective_function_value)
    objective_value: float | None = raw_obj if publish_feasible and math.isfinite(raw_obj) else None

    if is_mip:
        raw_bound = float(info.mip_dual_bound)
        raw_gap = float(info.mip_gap)
        best_bound: float | None = raw_bound if math.isfinite(raw_bound) else None
        relative_gap: float | None = (
            raw_gap if publish_feasible and math.isfinite(raw_gap) else None
        )
    elif publish_feasible and model_status_name == "kOptimal" and objective_value is not None:
        best_bound = objective_value
        relative_gap = 0.0
    else:
        best_bound = None
        relative_gap = None

    receipt: dict[str, Any] = {
        "solver": f"{ctx.manifest.plugin_id}@{ctx.manifest.version}",
        "solve_domain": "original",
        "original_model_verified": True,
        "solver_diagnostics": {
            "is_mip": is_mip,
            "model_status": f"HighsModelStatus.{model_status_name}",
        },
    }

    header = PrimalSolutionHeader(
        math_status=math_status,
        termination_reason=termination_reason,
        has_feasible_solution=publish_feasible,
        column_count=column_count,
        objective_value=objective_value,
        best_bound=best_bound,
        relative_gap=relative_gap,
        receipt=receipt,
    )

    col_values = highs.getSolution().col_value if publish_feasible else ()

    def _iter_primals() -> Iterator[float]:
        if not publish_feasible:
            return
        if len(col_values) != column_count:
            raise StageIOError(
                f"highspy col_value count ({len(col_values)}) != column_count ({column_count})"
            )
        for val in col_values:
            yield float(val)

    write_primal_solution_streaming(ctx.work_out / "primal_solution.json", header, _iter_primals())
