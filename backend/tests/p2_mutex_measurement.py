"""Isolated P2 mutex strategy experiment; NOT imported by production code.

The exact generator/solver protocol is unchanged. Every benchmark receives the
original, explicitly constructed model. Report status and evidence, not a policy.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import subprocess
import time

from iesplan.computation import write_mps
from iesplan.computation.generators.linear import LinearModel
from iesplan.computation.generators.project import build_project_model

HIGHS_EXECUTABLE = "/usr/local/bin/ies-highs-solver"
_OPTIMAL = "HighsModelStatus.kOptimal"


@dataclass(frozen=True, slots=True)
class MutexStrategyComparison:
    full_status: str
    full_objective: float | None
    full_best_bound: float | None
    full_integers: int
    full_columns: int
    full_constraints: int
    full_mps_bytes: int
    full_wall_ms: float
    candidate_status: str
    candidate_best_bound: float | None
    candidate_resolves: int
    candidate_integers_added: int
    candidate_all_relations_feasible: bool
    candidate_objective: float | None
    candidate_wall_ms: float
    candidate_peak_columns: int
    candidate_peak_rows: int
    candidate_mps_bytes: int
    recommendation: str = "not_evaluated"


def _read_solution(content: bytes, variable_names: tuple[str, ...]) -> dict:
    lines = content.decode("utf-8").splitlines()
    if not lines or lines[0] != "HIGHS_SOLUTION_V1":
        raise ValueError("unexpected HiGHS solution format")
    facts: dict[str, str] = {}
    values: list[float] = []
    in_primal = False
    for line in lines[1:]:
        if line == "primal:":
            in_primal = True
            continue
        k, sep, v = line.partition(":")
        if not sep:
            raise ValueError("bad HiGHS solution line")
        if not in_primal:
            facts[k.strip()] = v.strip()
        else:
            if int(k.strip()) != len(values):
                raise ValueError("non-contiguous HiGHS primal")
            values.append(float(v.strip()))
    if len(values) != len(variable_names) or int(facts["num_columns"]) != len(values):
        raise ValueError("HiGHS solution columns do not match model")
    status = facts["model_status"]
    objective = float(facts["objective_value"]) if status == _OPTIMAL else None
    return {"status": status, "objective": objective,
            "primal": dict(zip(variable_names, values)) if status == _OPTIMAL else {}}


def _solve(model: LinearModel, directory: Path, iteration: int) -> tuple[dict, int]:
    directory.mkdir(exist_ok=True, parents=True)
    (directory / "solution").mkdir(exist_ok=True)
    mps, _offset = write_mps(model)
    # Fixed seed, time limit and a unique output file per re-solve; no stale
    # candidate solution can be accidentally interpreted as this attempt.
    name = f"values_{iteration}.sol"
    (directory / "problem.mps").write_text(mps, encoding="utf-8")
    result = subprocess.run(
        [HIGHS_EXECUTABLE, "problem.mps", "--solution_file", f"solution/{name}",
         "--time_limit", "30", "--random_seed", "0"],
        cwd=directory, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"HiGHS experiment execution failed: {result.stderr[-1200:]}")
    parsed = _read_solution((directory / "solution" / name).read_bytes(),
                            tuple(var.name for var in model.variables))
    return parsed, len(mps.encode("utf-8"))


def _mutex_groups(model: LinearModel) -> dict[str, tuple[object, object, object, str, str]]:
    rows = {row.name: row for row in model.constraints}
    groups = {}
    for var in model.variables:
        if var.kind != "binary" or not var.name.endswith("__mutex"):
            continue
        left, right = rows.get(var.name + "_x"), rows.get(var.name + "_y")
        if left is None or right is None:
            raise ValueError(f"incomplete mutex rows for {var.name}")
        def factor(row) -> str:
            others = {key[0] for key, value in row.expression.terms
                      if value and key[0] != var.name}
            if len(others) != 1:
                raise ValueError(f"not a simple P2 binary mutex: {row.name}")
            return next(iter(others))
        groups[var.name] = (var, left, right, factor(left), factor(right))
    return groups


def compare_mutex_model(full_model: LinearModel, tmp_path: Path) -> MutexStrategyComparison:
    """Solve full MILP, then relax and add ONLY genuinely violated mutexes."""
    groups = _mutex_groups(full_model)
    omitted = {name for name in groups}
    row_names = {row.name for _, l, r, _, _ in groups.values() for row in (l, r)}
    base_vars = [var for var in full_model.variables if var.name not in omitted]
    base_rows = [row for row in full_model.constraints if row.name not in row_names]
    if any(name in omitted for (name, _), coeff in full_model.objective.terms if coeff):
        raise ValueError("mutex auxiliary appears in objective")
    t_full = time.perf_counter()
    full, full_bytes = _solve(full_model, tmp_path / "full_exact", 0)
    full_ms = (time.perf_counter() - t_full) * 1000
    t_candidate = time.perf_counter()
    active: set[str] = set()
    rounds = 0
    final = {"status": "not_run", "objective": None, "primal": {}}
    candidate_ok = False
    peak_columns, peak_rows, peak_bytes = 0, 0, 0
    for i in range(len(groups) + 1):
        rounds += 1
        vars_now = [*base_vars, *(groups[n][0] for n in sorted(active))]
        rows_now = [*base_rows,
                    *(row for n in sorted(active) for row in groups[n][1:3])]
        candidate = LinearModel(tuple(vars_now), tuple(rows_now),
                                full_model.objective, full_model.sense)
        peak_columns = max(peak_columns, len(candidate.variables))
        peak_rows = max(peak_rows, len(candidate.constraints))
        final, used_bytes = _solve(candidate, tmp_path / "candidate", i)
        peak_bytes = max(peak_bytes, used_bytes)
        # No feasible primal is claimed for time limits / infeasibility.
        if final["status"] != _OPTIMAL:
            break
        primal = final["primal"]
        violations = []
        for name, (_, _left, _right, x, y) in groups.items():
            if name in active:
                continue
            xv, yv = primal[x], primal[y]
            # HiGHS' finite feasibility tolerance; both nonnegative
            # original factors must be positive for a true violation.
            if xv > 1e-7 and yv > 1e-7:
                violations.append(name)
        if not violations:
            candidate_ok = True
            break
        active.update(violations)
    cand_ms = (time.perf_counter() - t_candidate) * 1000
    return MutexStrategyComparison(
        full_status=full["status"], full_objective=full["objective"],
        full_best_bound=full["objective"] if full["status"] == _OPTIMAL else None,
        full_integers=sum(v.kind == "binary" for v in full_model.variables),
        full_columns=len(full_model.variables),
        full_constraints=len(full_model.constraints),
        full_mps_bytes=full_bytes,
        full_wall_ms=round(full_ms, 2),
        candidate_status=final["status"],
        candidate_best_bound=final["objective"] if final["status"] == _OPTIMAL else None,
        candidate_resolves=rounds,
        candidate_integers_added=len(active),
        candidate_all_relations_feasible=candidate_ok,
        candidate_objective=final["objective"],
        candidate_wall_ms=round(cand_ms, 2),
        candidate_peak_columns=peak_columns,
        candidate_peak_rows=peak_rows,
        candidate_mps_bytes=peak_bytes,
    )


def evaluate_mutex_strategies(
    artifact, models, materialized, finance, config, evaluation, tmp_path,
) -> MutexStrategyComparison:
    """Test-only adapter; never changes or augments the provided source model."""
    full_model, _, _ = build_project_model(
        artifact, models, materialized, finance, config, evaluation,
    )
    return compare_mutex_model(full_model, tmp_path)
