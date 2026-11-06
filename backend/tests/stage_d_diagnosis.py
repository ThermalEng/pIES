"""Explicit Docker-only D2 experiments; no production policy or runtime hooks."""
from __future__ import annotations

import argparse
import json
import math
import resource
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from unittest.mock import patch

from stage_d_measurement import measure, public_inputs

from iesplan.computation.generators import highs_milp
from iesplan.computation.generators.linear import LinearModel


def technical_mutex_pairs(model: LinearModel, sources: dict) -> list[dict]:
    """Recognize ONLY explicit technical-source, two-row Big-M structures.

    No column-name conventions, no guess that every binary is a mutex. The
    original matrix remains the authority; unmatched columns stay unmatched.
    """
    variables = {v.name: v for v in model.variables}
    origins = {}
    for group in sources["groups"]:
        if group["role"].startswith("column:"):
            for name in group["targets"]:
                if (variables[name].kind == "binary"
                        and sources["sources"][group["source"]]["kind"] == "technical"):
                    origins[name] = group["source"]
    candidates = defaultdict(dict)
    for row in model.constraints:
        terms = {key[0]: a for key, a in row.expression.terms if a}
        binaries = origins.keys() & terms.keys()
        if row.sense != "le" or len(terms) != 2 or len(binaries) != 1:
            continue
        binary = next(iter(binaries))
        flow = next(name for name in terms if name != binary)
        rhs = row.rhs - row.expression.constant
        if terms[flow] != 1 or variables[flow].lower is None or variables[flow].lower < 0:
            continue
        coefficient = terms[binary]
        if coefficient < 0 and rhs == 0:
            candidates[binary]["x"] = flow
        elif coefficient > 0 and rhs == coefficient:
            candidates[binary]["y"] = flow
    return [{"binary": name, "x": pair["x"], "y": pair["y"], "source": origins[name]}
            for name, pair in sorted(candidates.items()) if pair.keys() == {"x", "y"}]


def check_original(model: LinearModel, values: dict[str, float], pairs: list[dict],
                   *, feasibility_tolerance=1e-7, integer_tolerance=1e-6) -> dict:
    """Diagnostic residuals in ORIGINAL solver units, not a production validator."""
    fractional = [v.name for v in model.variables if v.kind == "binary"
                  and abs(values[v.name] - round(values[v.name])) > integer_tolerance]
    maximum_row = (0., None)
    violated_rows = 0
    for row in model.constraints:
        delta = math.fsum(a * values[key[0]] for key, a in row.expression.terms)
        delta += row.expression.constant - row.rhs
        violation = abs(delta) if row.sense == "eq" else max(0., delta if row.sense == "le" else -delta)
        if violation > maximum_row[0]:
            maximum_row = (violation, row.name)
        violated_rows += violation > feasibility_tolerance
    bound_violation = max((max(0., (v.lower - values[v.name]) if v.lower is not None else 0.,
                               (values[v.name] - v.upper) if v.upper is not None else 0.)
                           for v in model.variables), default=0.)
    overlap = Counter()
    largest_overlap = 0.
    for pair in pairs:
        amount = min(values[pair["x"]], values[pair["y"]])
        if amount > feasibility_tolerance:
            overlap[pair["source"]] += 1
            largest_overlap = max(largest_overlap, amount)
    return {
        "fractional_integer_columns": len(fractional), "fractional_examples": fractional[:5],
        "recognized_technical_mutex_pairs": len(pairs),
        "positive_both_factors_by_source": dict(sorted(overlap.items())),
        "positive_both_factors_count": sum(overlap.values()),
        "maximum_min_factor_internal_units": largest_overlap,
        "raw_row_violations_above_tolerance": violated_rows,
        "maximum_raw_row_violation": maximum_row[0], "maximum_raw_row_violation_name": maximum_row[1],
        "maximum_bound_violation": bound_violation,
        "feasibility_tolerance": feasibility_tolerance, "integer_tolerance": integer_tolerance,
        "residual_scope": "original unscaled solver units; raw row errors are not normalized",
    }


def solve_diagnostic(model: LinearModel, mps: bytes, sources: dict, *, mode: str,
                     seconds: int, log_path: Path, presolve: str = "on",
                     financial_declarations: dict | None = None, lp_solver: str = "choose") -> dict:
    """Same MPS and parameters, only explicit diagnostic factors vary.

    Relaxation is NEVER an incumbent of the original MILP. Preserve the native
    log instead of inventing phase timings from wall-clock differences.
    """
    if mode != "relaxation" and lp_solver != "choose":
        raise ValueError("LP method controls are only allowed for an explicit relaxation")
    import highspy

    handle = highspy.Highs()
    options = {"output_flag": True, "log_to_console": False, "log_file": str(log_path),
               "threads": 2, "random_seed": 0, "time_limit": seconds,
               "presolve": presolve, "solve_relaxation": mode == "relaxation", "solver": lp_solver}
    for key, value in options.items():
        if handle.setOptionValue(key, value) != highspy.HighsStatus.kOk:
            raise RuntimeError(f"unsupported diagnostic option: {key}")
    with tempfile.TemporaryDirectory(prefix="pies-d2-") as folder:
        path = Path(folder) / "original.mps"
        path.write_bytes(mps)
        start = time.perf_counter()
        if handle.readModel(str(path)) == highspy.HighsStatus.kError:
            raise RuntimeError("diagnostic MPS read failed")
        read_seconds = time.perf_counter() - start
        start = time.perf_counter()
        status = handle.run()
        run_seconds = time.perf_counter() - start
    if status == highspy.HighsStatus.kError:
        raise RuntimeError("diagnostic native solve failed; see log")
    info, solution = handle.getInfo(), handle.getSolution()
    feasible = (solution.value_valid
                and info.primal_solution_status == highspy.SolutionStatus.kSolutionStatusFeasible)
    pairs = technical_mutex_pairs(model, sources)
    checks = None
    financial_facts = None
    if feasible:
        # HiGHS MPS column order is authoritative, not the DTO's order.
        lp = handle.getLp()
        values = dict(zip(lp.col_names_, solution.col_value, strict=True))
        checks = check_original(
            model, values, pairs,
            feasibility_tolerance=handle.getOptionValue("primal_feasibility_tolerance")[1],
            integer_tolerance=handle.getOptionValue("mip_feasibility_tolerance")[1],
        )
        if financial_declarations is not None:
            # Reuse the real adapter's affine recovery on this SAME primal;
            # never recalculate taxes or silently solve a second model.
            from iesplan.computation.result_adapters.highs import _recover_financial_facts

            financial_facts = _recover_financial_facts(
                financial_declarations,
                {name: i for i, name in enumerate(lp.col_names_)}, solution.col_value,
            )
    mip = mode == "exact" and info.mip_node_count >= 0
    return {
        "mode": mode, "presolve": presolve, "solver_version": handle.version(),
        "options": options, "model_status": str(handle.getModelStatus()), "run_status": str(status),
        "has_feasible_solution_of_experiment": bool(feasible),
        "is_original_milp_incumbent": bool(mode == "exact" and feasible),
        "objective_internal_without_offset": handle.getObjectiveValue() if feasible else None,
        "mip_best_bound_internal_without_offset": info.mip_dual_bound
        if mip and math.isfinite(info.mip_dual_bound) else None,
        "mip_relative_gap": info.mip_gap if mip and math.isfinite(info.mip_gap) else None,
        "mip_nodes": info.mip_node_count if mip else None,
        "simplex_iterations": info.simplex_iteration_count,
        "ipm_iterations": info.ipm_iteration_count, "crossover_iterations": info.crossover_iteration_count,
        "read_seconds": read_seconds, "run_seconds": run_seconds,
        "original_matrix_checks": checks,
        "financial_facts_of_experiment": financial_facts,
        "lp_limit": "standalone LP is not the MILP root with cuts/heuristics; compare the native logs",
    }


def diagnose(years: int, factor: int, mode: str, seconds: int, log_path: Path,
             presolve: str = "on", *, inputs_factory=public_inputs,
             fixture="public campus; repeated input only, independent state trajectory",
             lp_solver: str = "choose") -> dict:
    captured = {}
    build, write = highs_milp.build_project_model_package, highs_milp.write_mps

    def capture_build(*args, **kwargs):
        package = build(*args, **kwargs)
        captured["model"], captured["sources"] = package[0], package[-1]
        captured["financial_declarations"] = package[3]
        captured["tax_rules"] = len(args[3].tax_rules)
        return package

    def capture_mps(*args, **kwargs):
        result = write(*args, **kwargs)
        captured["mps"] = result[0].encode("utf-8")
        return result

    with (patch.object(highs_milp, "build_project_model_package", capture_build),
          patch.object(highs_milp, "write_mps", capture_mps)):
        generation = measure(years, purchase_budget_factor=factor,
                             inputs_factory=inputs_factory, fixture=fixture)
    native = solve_diagnostic(captured["model"], captured["mps"], captured["sources"],
                              mode=mode, seconds=seconds, log_path=log_path, presolve=presolve,
                              financial_declarations=captured["financial_declarations"], lp_solver=lp_solver)
    return {
        "generation": generation, "native": native,
        "fresh_process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "coverage": {
            "public_fixture": True, "tax_rules_in_this_fixture": captured["tax_rules"],
            "state": "original initial condition and adjacent steps; no resets or terminal additions",
            "input_comparison": "factor changes a whole-period budget, NOT an equivalent optimization",
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=int, choices=(1, 2), default=2)
    parser.add_argument("--purchase-budget-factor", type=int, choices=(1, 2), default=1)
    parser.add_argument("--mode", choices=("exact", "relaxation"), required=True)
    parser.add_argument("--presolve", choices=("on", "off"), default="on")
    parser.add_argument("--lp-solver", choices=("choose", "simplex", "ipm"), default="choose",
                        help="Diagnostic relaxation ONLY; never changes the production MIP solver")
    parser.add_argument("--solve-seconds", type=int, choices=range(1, 31), default=30)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = diagnose(args.years, args.purchase_budget_factor, args.mode, args.solve_seconds,
                      args.report.with_suffix(".solver.log"), args.presolve, lp_solver=args.lp_solver)
    content = json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
    args.report.write_text(content + "\n", encoding="utf-8")
    print(content, flush=True)
