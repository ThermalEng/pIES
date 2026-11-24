"""Standalone reference `algorithm` plugin using only Python stdlib + pies-sdk."""

from __future__ import annotations

from pies_sdk import ComputeResultPayload, StageContext


def solve(ctx: StageContext) -> None:
    """Compute deterministic step-wise dispatch and total cost from /work/in/request.json."""
    request = ctx.read_request()
    demand_kw = [float(v) for v in request["demand_kw"]]
    prices = [float(v) for v in request["price_per_kwh"]]
    if len(demand_kw) != len(prices):
        raise ValueError("demand_kw and price_per_kwh must have the same length")

    tariff_multiplier = float(ctx.parameters.get("tariff_multiplier", 1.0))
    max_power_kw = float(ctx.parameters.get("max_power_kw", 100.0))

    bundle_id = str(request.get("bundle_id") or ctx.manifest.ref)
    receipt_id = str(request.get("receipt_id") or "standalone-receipt")

    feasible = all(0.0 <= d <= max_power_kw for d in demand_kw)
    if not feasible:
        ctx.write_result(
            ComputeResultPayload(
                bundle_id=bundle_id,
                receipt_id=receipt_id,
                execution_status="completed",
                math_status="infeasible",
                outputs={
                    "solver_summary": {
                        "termination_reason": "infeasible",
                        "math_status": "infeasible",
                        "has_feasible_solution": False,
                    }
                },
            )
        )
        return

    total_cost = round(
        sum(d * p * tariff_multiplier for d, p in zip(demand_kw, prices, strict=True)),
        6,
    )
    ctx.write_result(
        ComputeResultPayload(
            bundle_id=bundle_id,
            receipt_id=receipt_id,
            execution_status="completed",
            math_status="solved",
            outputs={
                "dispatch_kw": demand_kw,
                "total_cost": total_cost,
                "solver_summary": {
                    "termination_reason": "optimal",
                    "math_status": "solved",
                    "has_feasible_solution": True,
                    "objective_value": total_cost,
                    "objective_unit": "CNY",
                },
            },
        )
    )
