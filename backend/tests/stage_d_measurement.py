"""Explicit Docker-only public benchmark; never imported by production."""
from __future__ import annotations

import argparse
import cProfile
import json
import math
import resource
import tempfile
import time
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from bundle_testkit import materialized_file
from test_highs_milp_provider import SAMPLES, _campus_inputs

from iesplan.assembly import validate_assembly_text
from iesplan.computation.generators import bound_inference, highs_milp, project
from iesplan.computation.generators.linear import LinearModel


def public_inputs(years: int, purchase_budget_factor: int = 1):
    """Repeat only the declared public synthetic inputs, not state decisions."""
    artifact, models, materialized, finance, config, evaluation = _campus_inputs()
    if purchase_budget_factor != 1:
        # An explicit alternative INPUT for a single-factor diagnostic, not an
        # equivalent reformulation or a production rule inferred from its name.
        source = (SAMPLES / "valid" / "campus.assembly.yaml").read_text(encoding="utf-8")
        source = source.replace("<= 900000 CNY", f"<= {900000 * purchase_budget_factor} CNY")
        checked = validate_assembly_text(source, package_dir=SAMPLES)
        assert checked.ok, checked.diagnostics
        artifact = checked.artifact
    materialized = replace(
        materialized, operation_years=years,
        series=tuple(replace(s, point_count=s.point_count * years, values=s.values * years)
                     for s in materialized.series),
        prices=tuple(replace(s, point_count=s.point_count * years, values=s.values * years)
                     for s in materialized.prices),
    )
    evaluation = replace(evaluation, operation_years=years, evaluation_years=years)
    return artifact, models, materialized, finance, config, evaluation


def matrix_summary(model: LinearModel, sources: dict) -> dict:
    binary = {v.name for v in model.variables if v.kind == "binary"}
    objective = {key[0] for key, value in model.objective.terms if value != 0}
    origins = Counter()
    mapped = set()
    for group in sources["groups"]:
        if group["role"].startswith("column:"):
            hits = binary.intersection(group["targets"])
            if hits:
                origins[sources["sources"][group["source"]]["kind"]] += len(hits)
            mapped.update(hits)
    density = Counter()
    nnz = 0
    largest = []
    for row in model.constraints:
        count = sum(value != 0 for _, value in row.expression.terms)
        nnz += count
        bucket = ("0..8" if count <= 8 else "9..64" if count <= 64
                  else "65..1024" if count <= 1024 else ">1024")
        density[bucket] += 1
        largest.append((count, row.name))
    top = sorted(largest, reverse=True)[:5]
    top_names = {name for _, name in top}
    top_sources = {}
    for group in sources["groups"]:
        if group["role"].startswith("row:"):
            for name in top_names.intersection(group["targets"]):
                top_sources[name] = group["source"]
    return {
        "columns": len(model.variables), "rows": len(model.constraints),
        "row_nonzeros": nnz, "binary_columns": len(binary),
        "binary_by_source_kind": dict(sorted(origins.items())),
        "binary_without_source_mapping": len(binary - mapped),
        "binary_without_direct_objective_coefficient": len(binary - objective),
        "row_nonzero_histogram": dict(sorted(density.items())),
        "densest_rows": [{"row": name, "nonzeros": count, "source": top_sources.get(name)}
                         for count, name in top],
    }


def native_solve(mps: bytes, seconds: int) -> dict:
    """Observe native solver facts, independently of the display summary."""
    import highspy

    with tempfile.TemporaryDirectory(prefix="pies-stage-d-") as folder:
        path = Path(folder) / "problem.mps"
        path.write_bytes(mps)
        handle = highspy.Highs()
        handle.setOptionValue("output_flag", False)
        handle.setOptionValue("threads", 2)
        handle.setOptionValue("time_limit", seconds)
        handle.setOptionValue("random_seed", 0)
        start = time.perf_counter()
        read_status = handle.readModel(str(path))
        read_seconds = time.perf_counter() - start
        if read_status == highspy.HighsStatus.kError:
            raise RuntimeError("public benchmark MPS read failed")
        start = time.perf_counter()
        run_status = handle.run()
        solve_seconds = time.perf_counter() - start
        info = handle.getInfo()
        feasible = (handle.getSolution().value_valid
                    and info.primal_solution_status == highspy.SolutionStatus.kSolutionStatusFeasible)
        return {
            "solver_version": handle.version(), "model_status": str(handle.getModelStatus()),
            "run_status": str(run_status), "has_feasible_solution": bool(feasible),
            "objective_internal_without_offset": handle.getObjectiveValue() if feasible else None,
            "best_bound_internal_without_offset": info.mip_dual_bound
            if info.mip_node_count >= 0 and math.isfinite(info.mip_dual_bound) else None,
            "relative_gap": info.mip_gap
            if info.mip_node_count >= 0 and math.isfinite(info.mip_gap) else None,
            "mip_nodes": info.mip_node_count, "simplex_iterations": info.simplex_iteration_count,
            "read_seconds": read_seconds, "run_seconds": solve_seconds,
            "time_limit_seconds": seconds, "threads": 2,
        }


def measure(years: int, *, solve_seconds: int = 0, profile_path: str | None = None,
            purchase_budget_factor: int = 1, inputs_factory=public_inputs,
            fixture="public campus; repeated input only, independent state trajectory") -> dict:
    start = time.perf_counter()
    inputs = inputs_factory(years, purchase_budget_factor)
    input_seconds = time.perf_counter() - start
    times = {}
    captured = {}

    def timed(label, fn):
        def wrapped(*args, **kwargs):
            start = time.perf_counter()
            result = fn(*args, **kwargs)
            entry = times.setdefault(label, {"seconds": 0., "calls": 0})
            entry["seconds"] += time.perf_counter() - start
            entry["calls"] += 1
            return result
        return wrapped

    original_new = project._new_builder

    def capture_builder(*args, **kwargs):
        captured["builder"] = original_new(*args, **kwargs)
        return captured["builder"]

    original_build = highs_milp.build_project_model_package

    def capture_package(*args, **kwargs):
        package = original_build(*args, **kwargs)
        captured["model"], captured["sources"] = package[0], package[-1]
        return package

    profile = cProfile.Profile() if profile_path else None
    start = time.perf_counter()
    with (
        patch.object(project, "_new_builder", capture_builder),
        patch.object(project, "CompilerAnalysis",
                     timed("analysis_including_binding", project.CompilerAnalysis)),
        patch.object(bound_inference.BoundInferenceEngine, "_propagate",
                     timed("bound_propagation", bound_inference.BoundInferenceEngine._propagate)),
        patch.object(highs_milp, "build_project_model_package",
                     timed("model_and_declarations", capture_package)),
        patch.object(highs_milp.HighsMilpProvider, "_metadata",
                     timed("metadata_serialization", highs_milp.HighsMilpProvider._metadata)),
    ):
        if profile:
            profile.enable()
        bundle = highs_milp.HIGHS_MILP_PROVIDER.generate(*inputs)
        sizes = {}
        for path, content in bundle.files.items():
            label = "mps_write" if path == highs_milp.PROBLEM_PATH else "parameter_records"
            sizes[path] = timed(label, lambda content=content: sum(
                len(chunk) for chunk in content.iter_chunks()))()
        if profile:
            profile.disable()
    generate_seconds = time.perf_counter() - start
    if profile:
        profile.dump_stats(profile_path)
    builder = captured["builder"]
    result = {
        "fixture": fixture,
        "operation_years": years, "steps": builder.point_count,
        "whole_period_purchase_budget_factor": purchase_budget_factor,
        "instrumentation": "cProfile" if profile else "boundary timers only; no allocation tracer",
        "input_preparation_seconds": input_seconds, "generate_seconds": generate_seconds,
        "inclusive_stages_not_additive": times, "template_stats": builder.template_stats,
        "propagation_stats": asdict(builder.analysis.propagation_stats),
        "matrix": matrix_summary(captured["model"], captured["sources"]),
        "mps_bytes": sizes[highs_milp.PROBLEM_PATH],
        "metadata_bytes": sum(size for path, size in sizes.items() if path != highs_milp.PROBLEM_PATH),
    }
    if solve_seconds:
        result["native_solver"] = native_solve(
            materialized_file(bundle, highs_milp.PROBLEM_PATH), solve_seconds)
    result["fresh_process_peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result["rss_scope"] = (
        "input preparation + generate + statistics + optional native solve; not per-stage RSS"
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=int, choices=(1, 2), default=1)
    parser.add_argument("--solve-seconds", type=int, choices=range(31), default=0)
    parser.add_argument("--purchase-budget-factor", type=int, choices=(1, 2), default=1,
                        help="Explicit alternative public input; NOT equivalent optimization")
    parser.add_argument("--profile", help="Optional cProfile output; timings then include profiler overhead")
    parser.add_argument("--report", help="Optional path for generated measurement JSON")
    args = parser.parse_args()
    report = json.dumps(measure(args.years, solve_seconds=args.solve_seconds, profile_path=args.profile,
                                purchase_budget_factor=args.purchase_budget_factor),
                        ensure_ascii=False, sort_keys=True)
    if args.report:
        Path(args.report).write_text(report + "\n", encoding="utf-8")
    print(report, flush=True)
