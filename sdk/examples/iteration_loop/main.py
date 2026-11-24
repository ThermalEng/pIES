"""Minimal self-contained iteration loop plugin emitting /work/out/iterations.jsonl."""

from __future__ import annotations

from pies_sdk import ComputeResultPayload, IterationRecord, StageContext


def solve(ctx: StageContext) -> None:
    """Run deterministic damped fixed-point iterations and stream NDJSON progress."""
    request = ctx.read_request()
    convergence = request.get("convergence") or {}
    tolerance = float(convergence.get("tolerance", 1e-3))
    default_max = (
        ctx.manifest.iteration.max_iterations_default if ctx.manifest.iteration else 20
    ) or 20
    max_iterations = int(convergence.get("max_iterations", default_max))
    metric_name = (
        ctx.manifest.iteration.convergence_metric if ctx.manifest.iteration else "max_relative_delta"
    ) or "max_relative_delta"

    damping = float(ctx.parameters.get("damping", 0.5))
    target = float(request["target_value"])
    current = float(request["initial_guess"])
    scale = max(abs(target), 1.0)

    bundle_id = str(request.get("bundle_id") or ctx.manifest.ref)
    receipt_id = str(request.get("receipt_id") or "standalone-iter-receipt")

    converged = False
    rounds = 0
    final_delta = 0.0

    with ctx.iteration_writer(truncate=True) as writer:
        for step in range(1, max_iterations + 1):
            nxt = (1.0 - damping) * current + damping * target
            rel_delta = abs(nxt - current) / scale
            current = nxt
            rounds = step
            final_delta = rel_delta

            if rel_delta <= tolerance:
                converged = True
                status = "converged"
            elif step == max_iterations:
                status = "max_iterations_reached"
            else:
                status = "running"

            writer.append(
                IterationRecord(
                    iteration=step,
                    metric=metric_name,
                    value=round(rel_delta, 10),
                    unit="1",
                    status=status,
                )
            )
            if converged:
                break

    math_status = "solved" if converged else "feasible"
    ctx.write_result(
        ComputeResultPayload(
            bundle_id=bundle_id,
            receipt_id=receipt_id,
            execution_status="completed",
            math_status=math_status,
            outputs={
                "converged": converged,
                "estimate": round(current, 6),
                "iterations_completed": rounds,
                "final_metric_value": round(final_delta, 10),
            },
        )
    )
