"""Reference iterative economic dispatch algorithm (`ies.algorithm.economic_dispatch@1.0.0`)."""

from __future__ import annotations

import csv
import json
from collections.abc import Mapping
from pathlib import Path
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
    read_json_file,
)

_REG_QUAD = 0.0005
_POINT_COUNTS = {
    "15min": (35040, 35136),
    "30min": (17520, 17568),
    "1h": (8760, 8784),
}
_RESOLUTION_HOURS = {
    "15min": 0.25,
    "30min": 0.5,
    "1h": 1.0,
}


def _horizon_point_count(resolution: str, start_year: int, years: int) -> int:
    normal, leap = _POINT_COUNTS[resolution]
    total = 0
    for offset in range(years):
        yr = start_year + offset
        is_leap = yr % 4 == 0 and (yr % 100 != 0 or yr % 400 == 0)
        total += leap if is_leap else normal
    return total


def _step_hours(doc: Mapping[str, Any]) -> float:
    baseline = doc.get("project_baseline") or {}
    if isinstance(baseline, Mapping) and baseline.get("step_duration") is not None:
        return float(baseline["step_duration"])
    res = str(baseline.get("resolution") or "1h") if isinstance(baseline, Mapping) else "1h"
    return _RESOLUTION_HOURS.get(res, 1.0)


def _project_csv_column(csv_path: Path, column_name: str) -> list[float]:
    lines = csv_path.read_bytes().decode("utf-8-sig").splitlines()
    header_idx = next(
        (idx for idx, line in enumerate(lines) if line.strip() and not line.strip().startswith("#")),
        None,
    )
    if header_idx is None:
        raise StageIOError(f"Empty CSV resource file: {csv_path}")
    rows = list(csv.reader(lines[header_idx:]))
    header = [cell.strip().lower() for cell in rows[0]]
    col_idx = header.index(column_name.lower())
    return [
        float(cells[col_idx].strip())
        for cells in rows[1:]
        if any(cell.strip() for cell in cells)
    ]


def _extract_demand_and_instances(
    ctx: StageContext,
    request: Mapping[str, Any],
    doc: Mapping[str, Any],
) -> tuple[list[float], dict[str, list[float]], str, str, float]:
    devices = doc.get("devices")
    if not isinstance(devices, Mapping) or not devices:
        raise StageIOError("Assembly artifact must declare non-empty 'devices'")

    grid_inst = ""
    gen_inst = ""
    load_insts: list[str] = []
    for inst_id, dev_spec in sorted(devices.items()):
        if not isinstance(dev_spec, Mapping):
            continue
        def_block = dev_spec.get("definition")
        model_id = str(def_block.get("id") or "") if isinstance(def_block, Mapping) else ""
        if model_id == "ies.device.grid_connection" and not grid_inst:
            grid_inst = str(inst_id)
        elif model_id == "ies.device.electric_load":
            load_insts.append(str(inst_id))
        elif model_id in {
            "ies.device.chp",
            "ies.device.gas_turbine",
            "ies.device.diesel_generator",
        } and not gen_inst:
            gen_inst = str(inst_id)

    if not grid_inst or not gen_inst or not load_insts:
        raise StageIOError(
            "ies.algorithm.economic_dispatch requires grid_connection, generator, and electric_load devices"
        )

    series_map: dict[tuple[str, str], list[float]] = {}
    frozen = request.get("frozen_series_inputs")
    if frozen is not None:
        if not isinstance(frozen, Mapping):
            raise StageIOError("request.frozen_series_inputs must be a JSON object")
        baseline = doc.get("project_baseline")
        evaluation = request.get("evaluation")
        if not isinstance(baseline, Mapping) or not isinstance(evaluation, Mapping):
            raise StageIOError("frozen_series_inputs requires 'project_baseline' and 'evaluation'")
        point_count = _horizon_point_count(
            str(baseline["resolution"]),
            int(baseline["start_year"]),
            int(evaluation["operation_years"]),
        )
        raw_fixed = frozen.get("fixed_resources") or {}
        object_paths = {str(oid): ctx.work_in / str(rel) for oid, rel in raw_fixed.items()}
        predicted_map = {
            (str(e.get("device") or e.get("instance") or ""), str(e.get("interface") or "")): [
                float(v) for v in e["values"]
            ]
            for e in (frozen.get("predicted_series") or ())
            if isinstance(e, Mapping) and e.get("values")
        }
        for inst_id in load_insts:
            bindings = devices[inst_id].get("predefined_interfaces") or {}
            for iface_id, binding in sorted(bindings.items()):
                mode = binding.get("mode")
                if mode == "constant":
                    series_map[(inst_id, iface_id)] = [float(binding["value"])] * point_count
                elif mode == "data_repeat":
                    oid = str(binding["content"]["object_id"])
                    cycle = _project_csv_column(object_paths[oid], iface_id)
                    series_map[(inst_id, iface_id)] = [cycle[i % len(cycle)] for i in range(point_count)]
                elif mode == "data_full":
                    oid = str(binding["content"]["object_id"])
                    series_map[(inst_id, iface_id)] = _project_csv_column(object_paths[oid], iface_id)
                elif mode == "data_predict":
                    series_map[(inst_id, iface_id)] = predicted_map[(inst_id, iface_id)]
    else:
        raw_mat = request.get("materialized")
        if raw_mat is None and (ctx.work_in / "sequences.json").is_file():
            raw_mat = read_json_file(ctx.work_in / "sequences.json")
        if not isinstance(raw_mat, Mapping) or not raw_mat.get("series"):
            raise StageIOError("algorithm /work/in requires 'frozen_series_inputs' or 'materialized'")
        for entry in raw_mat["series"]:
            dev = str(entry.get("device") or entry.get("instance") or "")
            iface = str(entry.get("interface") or "")
            series_map[(dev, iface)] = [float(v) for v in entry["values"]]

    load_series = {
        inst: series_map[(inst, "electricity_demand")]
        for inst in load_insts
        if (inst, "electricity_demand") in series_map
    }
    if not load_series:
        raise StageIOError("Missing electric demand series for assembly electric_load instances")

    steps = len(next(iter(load_series.values())))
    demand = [sum(vals[idx] for vals in load_series.values()) for idx in range(steps)]
    props = devices[gen_inst].get("properties") or {}
    rated_cap = float(
        (props.get("rated_electric_kw") or props.get("rated_power_kw") or {}).get("value", 600.0)
    )
    return demand, load_series, grid_inst, gen_inst, rated_cap


def solve_dispatch(ctx: StageContext) -> None:
    """Solve convex economic dispatch via deterministic Lagrangian dual bisection."""
    request = read_algorithm_request(ctx.work_in)
    raw_artifact = request.get("artifact")
    if not isinstance(raw_artifact, Mapping) or not isinstance(raw_artifact.get("canonical_text"), str):
        raise StageIOError("algorithm /work/in/request.json requires 'artifact.canonical_text'")
    doc = json.loads(raw_artifact["canonical_text"])
    if not isinstance(doc, Mapping):
        raise StageIOError("artifact.canonical_text must decode to a JSON object")

    step_hours = _step_hours(doc)
    demand, load_series, grid_inst, gen_inst, default_cap = _extract_demand_and_instances(
        ctx, request, doc
    )
    steps = len(demand)

    params = dict((request.get("calculation_config") or {}).get("options", {}).get("algorithm") or {})
    params.update(ctx.parameters)
    max_iterations = int(params.get("max_iterations", 10))
    tolerance = float(params.get("tolerance", 0.001))
    gen_cost = float(params.get("generator_cost_per_kwh", 0.35))
    grid_cost = float(params.get("grid_import_cost_per_kwh", 0.85))
    gen_cap = float(params.get("generator_capacity_kw", default_cap))
    share_cap = float(params.get("local_energy_share_cap", 0.85))

    total_demand_kwh = sum(demand) * step_hours
    energy_cap_kwh = share_cap * total_demand_kwh

    def _subproblem(lam: float) -> tuple[list[float], float]:
        g_raw: list[float] = []
        dual_val = -lam * energy_cap_kwh
        for d_t in demand:
            upper = min(d_t, gen_cap)
            g_t = (
                0.0
                if grid_cost <= gen_cost + lam
                else min(upper, max(0.0, (grid_cost - gen_cost - lam) / _REG_QUAD))
            )
            g_raw.append(g_t)
            dual_val += (
                ((gen_cost + lam) * g_t + 0.5 * _REG_QUAD * (g_t * g_t) + grid_cost * (d_t - g_t))
                * step_hours
            )
        return g_raw, dual_val

    def _project(g_raw: list[float]) -> tuple[list[float], list[float], float]:
        raw_kwh = sum(g_raw) * step_hours
        scale = min(1.0, energy_cap_kwh / raw_kwh) if raw_kwh > 1e-12 else 1.0
        g_feas = [round(v * scale, 6) for v in g_raw]
        m_feas = [round(max(0.0, d_t - g_t), 6) for d_t, g_t in zip(demand, g_feas, strict=True)]
        obj = sum(
            (gen_cost * g_t + 0.5 * _REG_QUAD * (g_t * g_t) + grid_cost * m_t) * step_hours
            for g_t, m_t in zip(g_feas, m_feas, strict=True)
        )
        return g_feas, m_feas, obj

    lo, hi = 0.0, max(0.0, grid_cost - gen_cost)
    best_g, best_m = [0.0] * steps, list(demand)
    best_primal = sum(grid_cost * d_t * step_hours for d_t in demand)
    best_dual = best_primal
    final_gap = 0.0
    iters_run = 0

    g_zero, dual_zero = _subproblem(0.0)
    with ctx.iteration_writer(truncate=True) as writer:
        if sum(g_zero) * step_hours <= energy_cap_kwh + 1e-9:
            best_g, best_m, best_primal = _project(g_zero)
            best_dual = min(dual_zero, best_primal)
            iters_run = 1
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
            for k in range(1, max_iterations + 1):
                lam = 0.5 * (lo + hi)
                g_raw, dual_val = _subproblem(lam)
                g_feas, m_feas, primal_obj = _project(g_raw)
                if sum(g_raw) * step_hours > energy_cap_kwh:
                    lo = lam
                else:
                    hi = lam
                if primal_obj <= best_primal:
                    best_primal, best_g, best_m = primal_obj, g_feas, m_feas
                best_dual = min(max(best_dual if k > 1 else dual_val, dual_val), best_primal)
                final_gap = max(0.0, best_primal - best_dual) / max(1.0, abs(best_primal))
                iters_run = k
                status = (
                    "converged"
                    if final_gap <= tolerance
                    else ("max_iterations_reached" if k == max_iterations else "running")
                )
                writer.append(
                    IterationRecord(
                        iteration=k,
                        metric="relative_gap",
                        value=round(final_gap, 8),
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

    peak_dispatch = max(best_g) if best_g else 0.0
    planning = doc.get("planning") or {}
    plan_vars = planning.get("variables") or {}
    raw_units = planning.get("construction_units") or {}
    unit_pvars: set[str] = set()
    units: list[ConstructionUnit] = []
    for unit_id, unit_spec in sorted(raw_units.items()):
        raw_scales = unit_spec.get("scales") or {}
        scale_keys = sorted(raw_scales) if isinstance(raw_scales, Mapping) else list(raw_scales)
        unit_pvars.update(scale_keys)
        scales = tuple(
            ConstructionScale(
                planning_variable_id=pvar,
                value=round(peak_dispatch, 6),
                unit=str((plan_vars.get(pvar) or {}).get("unit") or "kW"),
            )
            for pvar in scale_keys
        )
        units.append(
            ConstructionUnit(
                unit_id=str(unit_id),
                instance_id=str(unit_spec["device_ref"]),
                mode=str(unit_spec["mode"]),
                built=(str(unit_spec["mode"]) == "mandatory" or peak_dispatch > 0.0),
                scales=scales,
            )
        )
    standalone = tuple(
        ConstructionScale(
            planning_variable_id=pvar,
            value=round(peak_dispatch, 6),
            unit=str(spec.get("unit") or "kW"),
        )
        for pvar, spec in sorted(plan_vars.items())
        if pvar not in unit_pvars and isinstance(spec, Mapping)
    )

    out_series = {
        f"{gen_inst}__electric_out": DeviceSeries(
            instance_id=gen_inst, variable_id="electric_out", unit="kW", values=tuple(best_g)
        ),
        f"{grid_inst}__electricity_import": DeviceSeries(
            instance_id=grid_inst, variable_id="electricity_import", unit="kW", values=tuple(best_m)
        ),
        **{
            f"{inst}__electricity_in": DeviceSeries(
                instance_id=inst,
                variable_id="electricity_in",
                unit="kW",
                values=tuple(round(v, 6) for v in vals),
            )
            for inst, vals in sorted(load_series.items())
        },
    }

    ctx.write_engineering_solution(
        EngineeringSolutionPayload(
            solver_summary=SolverSummary(
                termination_reason="optimal" if final_gap <= tolerance else "not_solved",
                math_status="solved" if final_gap <= tolerance else "feasible",
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
            construction_units=tuple(units),
            planning_variables=standalone or None,
            series=out_series,
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
                "step_hours": step_hours,
                "total_cost_cny": round(best_primal, 6),
                "total_demand_kwh": round(total_demand_kwh, 6),
                "total_dispatch_kwh": round(sum(best_g) * step_hours, 6),
                "total_grid_import_kwh": round(sum(best_m) * step_hours, 6),
            },
            model_sources={
                "algorithm": f"{ctx.manifest.plugin_id}@{ctx.manifest.version}",
                "step_count": steps,
            },
        )
    )
