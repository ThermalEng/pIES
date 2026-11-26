"""Standalone reference `algorithm` plugin using only Python stdlib + pies-sdk."""

from __future__ import annotations

from pies_sdk import (
    DeviceSeries,
    EngineeringSolutionPayload,
    SolverSummary,
    StageContext,
)


def solve(ctx: StageContext) -> None:
    """Compute deterministic step-wise dispatch and total cost from /work/in/request.json."""
    request = ctx.read_request()
    demand_kw = [float(v) for v in request["demand_kw"]]
    prices = [float(v) for v in request["price_per_kwh"]]
    if len(demand_kw) != len(prices):
        raise ValueError("demand_kw and price_per_kwh must have the same length")

    tariff_multiplier = float(ctx.parameters.get("tariff_multiplier", 1.0))
    max_power_kw = float(ctx.parameters.get("max_power_kw", 100.0))

    feasible = all(0.0 <= d <= max_power_kw for d in demand_kw)
    if not feasible:
        ctx.write_engineering_solution(
            EngineeringSolutionPayload(
                solver_summary=SolverSummary(
                    math_status="infeasible",
                    termination_reason="infeasible",
                    has_feasible_solution=False,
                    objective_value=None,
                    objective_unit=None,
                    best_bound=None,
                    relative_gap=None,
                )
            )
        )
        return

    total_cost = round(
        sum(d * p * tariff_multiplier for d, p in zip(demand_kw, prices, strict=True)),
        6,
    )
    ctx.write_engineering_solution(
        EngineeringSolutionPayload(
            solver_summary=SolverSummary(
                math_status="solved",
                termination_reason="optimal",
                has_feasible_solution=True,
                objective_value=total_cost,
                best_bound=total_cost,
                relative_gap=0.0,
                objective_unit="CNY",
            ),
            construction_units=(),
            series={
                "grid.dispatch_kw": DeviceSeries(
                    instance_id="grid",
                    variable_id="dispatch_kw",
                    unit="kW",
                    values=tuple(demand_kw),
                )
            },
            system_series={},
            metrics={"total_cost": total_cost},
        )
    )
