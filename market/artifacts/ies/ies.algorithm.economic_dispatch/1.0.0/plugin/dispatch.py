"""Reference iterative economic dispatch algorithm (`ies.algorithm.economic_dispatch@1.0.0`)."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

from pies_sdk import (
    ConstructionScale,
    ConstructionUnit,
    DeviceSeries,
    EngineeringSolutionPayload,
    IterationRecord,
    SolverSummary,
    StageContext,
    StageIOError,
    SystemSeries,
    read_algorithm_request,
)

_REG_QUAD = 0.0005


def _extract_demand_kw(request: Mapping[str, Any]) -> list[float]:
    raw_demand = request.get("demand_kw")
    if isinstance(raw_demand, list) and raw_demand:
        vals = [float(v) for v in raw_demand]
        if all(math.isfinite(v) and v >= 0.0 for v in vals):
            return vals
        raise StageIOError("request.demand_kw values must be finite and >= 0")

    mat = request.get("materialized")
    if isinstance(mat, Mapping):
        series_list = mat.get("series") or []
        summed: list[float] | None = None
        for entry in series_list:
            if not isinstance(entry, Mapping):
                continue
            iface = str(entry.get("interface") or "").lower()
            if "demand" in iface or "load" in iface:
                vals = [float(v) for v in (entry.get("values") or [])]
                if vals:
                    summed = vals if summed is None else [a + b for a, b in zip(summed, vals, strict=True)]
        if summed is not None:
            return summed

    frozen = request.get("frozen_series_inputs")
    if isinstance(frozen, Mapping):
        predicted = frozen.get("predicted_series") or []
        summed = None
        for entry in predicted:
            if not isinstance(entry, Mapping):
                continue
            vals = [float(v) for v in (entry.get("values") or [])]
            if vals:
                summed = vals if summed is None else [a + b for a, b in zip(summed, vals, strict=True)]
        if summed is not None:
            return summed

    return [120.0, 180.0, 240.0, 160.0]


def _extract_construction_units(
    request: Mapping[str, Any],
    *,
    peak_dispatch_kw: float,
) -> tuple[ConstructionUnit, ...]:
    artifact = request.get("artifact")
    if isinstance(artifact, Mapping) and isinstance(artifact.get("canonical_text"), str):
        doc = json.loads(artifact["canonical_text"])
        planning = doc.get("planning") or {}
        plan_vars = planning.get("variables") or {}
        raw_units = planning.get("construction_units") or {}
        if isinstance(raw_units, Mapping) and raw_units:
            units: list[ConstructionUnit] = []
            for unit_id, spec in sorted(raw_units.items()):
                if not isinstance(spec, Mapping):
                    continue
                raw_scales = spec.get("scales") or ()
                scale_ids = (
                    list(raw_scales.keys()) if isinstance(raw_scales, Mapping) else list(raw_scales)
                )
                scales = tuple(
                    ConstructionScale(
                        planning_variable_id=str(pvar),
                        value=round(peak_dispatch_kw, 6),
                        unit=str((plan_vars.get(pvar) or {}).get("unit") or "kW"),
                    )
                    for pvar in scale_ids
                )
                units.append(
                    ConstructionUnit(
                        unit_id=str(unit_id),
                        instance_id=str(spec["device_ref"]),
                        mode=str(spec["mode"]),
                        built=peak_dispatch_kw > 0.0,
                        scales=scales,
                    )
                )
            if units:
                return tuple(units)

    return (
        ConstructionUnit(
            unit_id="dispatch_unit_1",
            instance_id="chp_dispatch_1",
            mode="mandatory",
            built=True,
            scales=(
                ConstructionScale(
                    planning_variable_id="dispatch_capacity_kw",
                    value=round(peak_dispatch_kw, 6),
                    unit="kW",
                ),
            ),
        ),
    )


def solve_dispatch(ctx: StageContext) -> None:
    """Solve convex economic dispatch via deterministic Lagrangian dual bisection."""
    request = read_algorithm_request(ctx.work_in)
    demand = _extract_demand_kw(request)
    steps = len(demand)

    max_iterations = int(ctx.parameters.get("max_iterations", 10))
    tolerance = float(ctx.parameters.get("tolerance", 0.001))
    gen_cost = float(ctx.parameters.get("generator_cost_per_kwh", 0.35))
    grid_cost = float(ctx.parameters.get("grid_import_cost_per_kwh", 0.85))
    gen_cap = float(ctx.parameters.get("generator_capacity_kw", 600.0))
    share_cap = float(ctx.parameters.get("local_energy_share_cap", 0.85))

    if max_iterations < 1:
        raise StageIOError(f"max_iterations must be >= 1, got {max_iterations!r}")
    if tolerance <= 0:
        raise StageIOError(f"tolerance must be > 0, got {tolerance!r}")

    total_demand = sum(demand)
    energy_cap = share_cap * total_demand

    def _subproblem(lam: float) -> tuple[list[float], float]:
        g_raw: list[float] = []
        dual_val = -lam * energy_cap
        for d_t in demand:
            upper = min(d_t, gen_cap)
            if grid_cost <= gen_cost + lam:
                g_t = 0.0
            else:
                g_t = min(upper, max(0.0, (grid_cost - gen_cost - lam) / _REG_QUAD))
            g_raw.append(g_t)
            m_t = d_t - g_t
            dual_val += (gen_cost + lam) * g_t + 0.5 * _REG_QUAD * (g_t * g_t) + grid_cost * m_t
        return g_raw, dual_val

    def _project_primal(g_raw: list[float]) -> tuple[list[float], list[float], float]:
        raw_sum = sum(g_raw)
        scale = min(1.0, energy_cap / raw_sum) if raw_sum > 1e-12 else 1.0
        g_feas = [round(v * scale, 6) for v in g_raw]
        m_feas = [round(max(0.0, d_t - g_t), 6) for d_t, g_t in zip(demand, g_feas, strict=True)]
        primal_obj = sum(
            gen_cost * g_t + 0.5 * _REG_QUAD * (g_t * g_t) + grid_cost * m_t
            for g_t, m_t in zip(g_feas, m_feas, strict=True)
        )
        return g_feas, m_feas, primal_obj

    lo = 0.0
    hi = max(0.0, grid_cost - gen_cost)
    best_g: list[float] = [0.0] * steps
    best_m: list[float] = list(demand)
    best_primal = sum(grid_cost * d_t for d_t in demand)
    best_dual = best_primal
    final_gap = 0.0
    iters_run = 0

    g_zero, dual_zero = _subproblem(0.0)
    if sum(g_zero) <= energy_cap + 1e-9:
        best_g, best_m, best_primal = _project_primal(g_zero)
        best_dual = dual_zero
        final_gap = 0.0
        iters_run = 1
        with ctx.iteration_writer(truncate=True) as writer:
            writer.append(
                IterationRecord(
                    iteration=1,
                    metric="relative_gap",
                    value=0.0,
                    unit="1",
                    status="converged",
                    details={
                        "lambda": 0.0,
                        "primal_obj": round(best_primal, 6),
                        "dual_bound": round(best_dual, 6),
                    },
                )
            )
    else:
        with ctx.iteration_writer(truncate=True) as writer:
            for k in range(1, max_iterations + 1):
                lam = 0.5 * (lo + hi)
                g_raw, dual_val = _subproblem(lam)
                g_feas, m_feas, primal_obj = _project_primal(g_raw)
                if sum(g_raw) > energy_cap:
                    lo = lam
                else:
                    hi = lam
                if primal_obj <= best_primal:
                    best_primal = primal_obj
                    best_g = g_feas
                    best_m = m_feas
                best_dual = max(best_dual if k > 1 else dual_val, dual_val)
                best_dual = min(best_dual, best_primal)
                gap = max(0.0, best_primal - best_dual) / max(1.0, abs(best_primal))
                final_gap = gap
                iters_run = k

                if gap <= tolerance:
                    status = "converged"
                elif k == max_iterations:
                    status = "max_iterations_reached"
                else:
                    status = "running"

                writer.append(
                    IterationRecord(
                        iteration=k,
                        metric="relative_gap",
                        value=round(gap, 8),
                        unit="1",
                        status=status,
                        details={
                            "lambda": round(lam, 6),
                            "primal_obj": round(best_primal, 6),
                            "dual_bound": round(best_dual, 6),
                        },
                    )
                )
                if status in ("converged", "max_iterations_reached"):
                    break

    total_dispatch = round(sum(best_g), 6)
    total_grid = round(sum(best_m), 6)
    peak_dispatch = max(best_g) if best_g else 0.0
    units = _extract_construction_units(request, peak_dispatch_kw=peak_dispatch)
    term_reason = "optimal" if final_gap <= tolerance else "not_solved"
    math_stat = "solved" if final_gap <= tolerance else "feasible"

    payload = EngineeringSolutionPayload(
        solver_summary=SolverSummary(
            termination_reason=term_reason,
            math_status=math_stat,
            has_feasible_solution=True,
            objective_value=round(best_primal, 6),
            objective_unit="CNY",
            best_bound=round(best_dual, 6),
            relative_gap=round(final_gap, 8),
            solver_diagnostics={
                "algorithm": f"{ctx.manifest.plugin_id}@{ctx.manifest.version}",
                "iterations": iters_run,
            },
        ),
        construction_units=units,
        series={
            "dispatch_unit_1__electric_out": DeviceSeries(
                instance_id="chp_dispatch_1",
                variable_id="electric_out",
                unit="kW",
                values=tuple(best_g),
            ),
            "grid_1__electricity_import": DeviceSeries(
                instance_id="grid_1",
                variable_id="electricity_import",
                unit="kW",
                values=tuple(best_m),
            ),
            "load_1__electricity_in": DeviceSeries(
                instance_id="load_1",
                variable_id="electricity_in",
                unit="kW",
                values=tuple(round(v, 6) for v in demand),
            ),
        },
        system_series={
            "system__net_grid_import": SystemSeries(
                block="power_balance",
                variable="net_grid_import",
                unit="kW",
                values=tuple(best_m),
            )
        },
        metrics={
            "iterations": iters_run,
            "total_cost_cny": round(best_primal, 6),
            "total_demand_kwh": round(total_demand, 6),
            "total_dispatch_kwh": total_dispatch,
            "total_grid_import_kwh": total_grid,
        },
        model_sources={
            "algorithm": f"{ctx.manifest.plugin_id}@{ctx.manifest.version}",
            "step_count": steps,
        },
    )
    ctx.write_engineering_solution(payload)
