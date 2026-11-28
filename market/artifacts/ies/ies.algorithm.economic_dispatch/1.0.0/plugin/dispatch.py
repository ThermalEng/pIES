"""Reference iterative economic dispatch algorithm (`ies.algorithm.economic_dispatch@1.0.0`)."""

from __future__ import annotations

import csv
import json
import math
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
    check_relative_path,
    read_algorithm_request,
    read_json_file,
)

_REG_QUAD = 0.0005
_POINT_COUNTS = {
    "15min": (35040, 35136),
    "30min": (17520, 17568),
    "1h": (8760, 8784),
}


def _horizon_point_count(resolution: str, start_year: int, years: int) -> int:
    if resolution not in _POINT_COUNTS:
        raise StageIOError(f"Unsupported project_baseline.resolution: {resolution!r}")
    if isinstance(start_year, bool) or not isinstance(start_year, int) or start_year < 1:
        raise StageIOError(f"Invalid project_baseline.start_year: {start_year!r}")
    if isinstance(years, bool) or not isinstance(years, int) or years < 1:
        raise StageIOError(f"Invalid evaluation.operation_years: {years!r}")
    normal, leap = _POINT_COUNTS[resolution]
    total = 0
    for offset in range(years):
        y = start_year + offset
        is_leap = y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
        total += leap if is_leap else normal
    return total


def _safe_work_in_file(work_in: Path, rel_path: str, label: str) -> Path:
    rel = check_relative_path(rel_path, label)
    root = work_in.resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise StageIOError(f"{label} escapes stage input directory: {rel_path!r}")
    if not target.is_file():
        raise StageIOError(f"Missing required stage input file for {label}: {rel_path!r}")
    return target


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
    key = column_name.lower()
    if key not in header:
        raise StageIOError(f"CSV resource {csv_path.name} missing column {column_name!r}")
    col_idx = header.index(key)
    out: list[float] = []
    for cells in rows[1:]:
        if not any(cell.strip() for cell in cells):
            continue
        val = float(cells[col_idx].strip())
        if not math.isfinite(val):
            raise StageIOError(f"Non-finite CSV value in {csv_path.name}: {val!r}")
        out.append(val)
    return out


def _materialize_series_map(
    ctx: StageContext,
    request: Mapping[str, Any],
    doc: Mapping[str, Any],
) -> tuple[dict[tuple[str, str], list[float]], int]:
    devices = doc.get("devices")
    if not isinstance(devices, Mapping) or not devices:
        raise StageIOError("Assembly artifact must declare non-empty 'devices'")

    frozen_inputs = request.get("frozen_series_inputs")
    if frozen_inputs is not None:
        if not isinstance(frozen_inputs, Mapping):
            raise StageIOError("request.frozen_series_inputs must be a JSON object")
        baseline = doc.get("project_baseline")
        evaluation = request.get("evaluation")
        if not isinstance(baseline, Mapping) or not isinstance(evaluation, Mapping):
            raise StageIOError(
                "frozen_series_inputs requires assembly 'project_baseline' and request 'evaluation'"
            )
        point_count = _horizon_point_count(
            str(baseline.get("resolution") or ""),
            baseline.get("start_year"),  # type: ignore[arg-type]
            evaluation.get("operation_years"),  # type: ignore[arg-type]
        )

        raw_fixed = frozen_inputs.get("fixed_resources") or {}
        if not isinstance(raw_fixed, Mapping):
            raise StageIOError("frozen_series_inputs.fixed_resources must be a JSON object")
        object_paths = {
            str(oid): _safe_work_in_file(
                ctx.work_in, str(rel), f"frozen_series_inputs.fixed_resources[{oid!r}]"
            )
            for oid, rel in raw_fixed.items()
        }

        predicted_map: dict[tuple[str, str], list[float]] = {}
        raw_pred = frozen_inputs.get("predicted_series") or ()
        if isinstance(raw_pred, list):
            for entry in raw_pred:
                if not isinstance(entry, Mapping):
                    continue
                dev = str(entry.get("device") or entry.get("instance") or "")
                iface = str(entry.get("interface") or "")
                vals = [float(v) for v in (entry.get("values") or [])]
                if dev and iface and vals:
                    predicted_map[(dev, iface)] = vals

        pred_file = ctx.work_in / "prediction.json"
        raw_pred_payload = request.get("prediction")
        if raw_pred_payload is None and pred_file.is_file():
            raw_pred_payload = read_json_file(pred_file)
        if isinstance(raw_pred_payload, Mapping):
            for entry in raw_pred_payload.get("predictions") or ():
                if isinstance(entry, Mapping):
                    dev = str(entry.get("instance") or entry.get("device") or "")
                    iface = str(entry.get("interface") or "")
                    vals = [float(v) for v in (entry.get("values") or [])]
                    if dev and iface and vals:
                        predicted_map[(dev, iface)] = vals

        series_map: dict[tuple[str, str], list[float]] = {}
        for inst_id, dev_spec in sorted(devices.items()):
            if not isinstance(dev_spec, Mapping):
                continue
            bindings = dev_spec.get("predefined_interfaces") or {}
            if not isinstance(bindings, Mapping):
                continue
            for iface_id, binding in sorted(bindings.items()):
                if not isinstance(binding, Mapping):
                    continue
                mode = binding.get("mode")
                if mode == "constant":
                    val = float(binding["value"])
                    series_map[(str(inst_id), str(iface_id))] = [val] * point_count
                elif mode == "data_repeat":
                    oid = str((binding.get("content") or {}).get("object_id") or "")
                    if oid not in object_paths:
                        raise StageIOError(
                            f"Missing fixed resource {oid!r} for {inst_id}.{iface_id}"
                        )
                    cycle = _project_csv_column(object_paths[oid], str(iface_id))
                    if not cycle:
                        raise StageIOError(f"Empty data_repeat series for {inst_id}.{iface_id}")
                    period = len(cycle)
                    series_map[(str(inst_id), str(iface_id))] = [
                        cycle[idx % period] for idx in range(point_count)
                    ]
                elif mode == "data_full":
                    oid = str((binding.get("content") or {}).get("object_id") or "")
                    if oid not in object_paths:
                        raise StageIOError(
                            f"Missing fixed resource {oid!r} for {inst_id}.{iface_id}"
                        )
                    full = _project_csv_column(object_paths[oid], str(iface_id))
                    if len(full) != point_count:
                        raise StageIOError(
                            f"data_full length ({len(full)}) != point_count ({point_count}) "
                            f"for {inst_id}.{iface_id}"
                        )
                    series_map[(str(inst_id), str(iface_id))] = full
                elif mode == "data_predict":
                    key = (str(inst_id), str(iface_id))
                    if key not in predicted_map or len(predicted_map[key]) != point_count:
                        raise StageIOError(
                            f"Missing or mismatched predicted series for {inst_id}.{iface_id}"
                        )
                    series_map[key] = predicted_map[key]
                else:
                    raise StageIOError(f"Unsupported predefined_interface mode: {mode!r}")
        return series_map, point_count

    raw_mat = request.get("materialized")
    if raw_mat is None and (ctx.work_in / "sequences.json").is_file():
        raw_mat = read_json_file(ctx.work_in / "sequences.json")
    if not isinstance(raw_mat, Mapping):
        raise StageIOError(
            "algorithm /work/in/request.json requires 'frozen_series_inputs' or 'materialized'"
        )
    raw_series = raw_mat.get("series")
    if not isinstance(raw_series, list) or not raw_series:
        raise StageIOError("request.materialized.series must be a non-empty list")

    series_map = {}
    point_count = 0
    for entry in raw_series:
        if not isinstance(entry, Mapping):
            raise StageIOError("materialized.series entries must be JSON objects")
        dev = str(entry.get("device") or entry.get("instance") or "")
        iface = str(entry.get("interface") or "")
        raw_vals = entry.get("values")
        if not dev or not iface or not isinstance(raw_vals, list) or not raw_vals:
            raise StageIOError(f"Invalid materialized series entry: {entry!r}")
        vals = [float(v) for v in raw_vals]
        for val in vals:
            if not math.isfinite(val):
                raise StageIOError(f"Non-finite value in materialized series {dev}.{iface}")
        if point_count == 0:
            point_count = len(vals)
        elif len(vals) != point_count:
            raise StageIOError(f"Inconsistent step length in materialized series {dev}.{iface}")
        series_map[(dev, iface)] = vals
    return series_map, point_count


def _resolve_device_roles(
    doc: Mapping[str, Any],
    device_models: Mapping[str, Any],
) -> tuple[str, str, list[str]]:
    devices = doc["devices"]
    grid_insts: list[str] = []
    gen_insts: list[str] = []
    load_insts: list[str] = []

    for inst_id, dev_spec in sorted(devices.items()):
        if not isinstance(dev_spec, Mapping):
            continue
        def_block = dev_spec.get("definition")
        model_id = str(def_block.get("id") or "") if isinstance(def_block, Mapping) else ""
        if not model_id:
            model_id = str(dev_spec.get("model_id") or "")
        iface_meta = ((device_models.get(inst_id) or {}).get("interfaces")) or {}
        if not isinstance(iface_meta, Mapping):
            iface_meta = {}

        if model_id == "ies.device.grid_connection" or (
            isinstance(iface_meta.get("electricity_import"), Mapping)
            and iface_meta["electricity_import"].get("carrier") == "electricity"
        ):
            grid_insts.append(str(inst_id))
            continue

        if model_id == "ies.device.electric_load" or (
            isinstance(iface_meta.get("electricity_demand"), Mapping)
            and iface_meta["electricity_demand"].get("carrier") == "electricity"
        ):
            load_insts.append(str(inst_id))
            continue

        if model_id in {"ies.device.wind_turbine", "ies.device.pv", "ies.device.solar_pv_sample"}:
            continue

        if model_id in {"ies.device.chp", "ies.device.gas_turbine", "ies.device.diesel_generator"}:
            gen_insts.append(str(inst_id))
        elif any(
            isinstance(spec, Mapping)
            and spec.get("type") == "out"
            and spec.get("carrier") == "electricity"
            for spec in iface_meta.values()
        ) and not any(
            isinstance(spec, Mapping)
            and spec.get("type") == "predefined"
            and spec.get("carrier") in {"solar", "wind"}
            for spec in iface_meta.values()
        ):
            gen_insts.append(str(inst_id))

    if not grid_insts:
        raise StageIOError("Assembly artifact missing an electric grid connection device")
    if not gen_insts:
        raise StageIOError("Assembly artifact missing a dispatchable electric generator device")
    if not load_insts:
        raise StageIOError("Assembly artifact missing an electric load device")
    return grid_insts[0], gen_insts[0], load_insts


def _property_float(dev_spec: Mapping[str, Any], prop_name: str) -> float | None:
    props = dev_spec.get("properties")
    if not isinstance(props, Mapping):
        return None
    entry = props.get(prop_name)
    if isinstance(entry, Mapping) and entry.get("value") is not None:
        return float(entry["value"])
    return None


def _bind_planning_outputs(
    doc: Mapping[str, Any],
    *,
    gen_inst: str,
    peak_dispatch: float,
) -> tuple[tuple[ConstructionUnit, ...], tuple[ConstructionScale, ...]]:
    planning = doc.get("planning") or {}
    plan_vars = planning.get("variables") or {}
    raw_units = planning.get("construction_units") or {}
    devices = doc["devices"]

    def _resolve_scale_value(pvar_id: str, pvar_spec: Mapping[str, Any]) -> float:
        dev_ref = str(pvar_spec.get("device_ref") or "")
        param = str(pvar_spec.get("parameter") or "")
        if dev_ref == gen_inst and (
            "electric" in param or "capacity" in param or "power" in param
        ):
            val = round(peak_dispatch, 6)
        else:
            dev_spec = devices.get(dev_ref) or {}
            prop_val = _property_float(dev_spec, param) if isinstance(dev_spec, Mapping) else None
            if prop_val is None:
                raise StageIOError(
                    f"Cannot resolve numeric binding for planning variable {pvar_id!r} "
                    f"({dev_ref}.{param})"
                )
            val = round(prop_val, 6)
        lo = pvar_spec.get("lower_bound")
        hi = pvar_spec.get("upper_bound")
        if lo is not None:
            val = max(float(lo), val)
        if hi is not None:
            val = min(float(hi), val)
        return round(val, 6)

    units: list[ConstructionUnit] = []
    unit_pvars: set[str] = set()
    for unit_id, unit_spec in sorted(raw_units.items()):
        if not isinstance(unit_spec, Mapping):
            continue
        raw_scales = unit_spec.get("scales") or ()
        scale_ids = list(raw_scales.keys()) if isinstance(raw_scales, Mapping) else list(raw_scales)
        scales: list[ConstructionScale] = []
        for pvar_id in scale_ids:
            unit_pvars.add(str(pvar_id))
            pvar_spec = plan_vars[pvar_id]
            scales.append(
                ConstructionScale(
                    planning_variable_id=str(pvar_id),
                    value=_resolve_scale_value(str(pvar_id), pvar_spec),
                    unit=str(pvar_spec["unit"]),
                )
            )
        inst_ref = str(unit_spec["device_ref"])
        built = (peak_dispatch > 0.0) if inst_ref == gen_inst else True
        units.append(
            ConstructionUnit(
                unit_id=str(unit_id),
                instance_id=inst_ref,
                mode=str(unit_spec["mode"]),
                built=built,
                scales=tuple(scales),
            )
        )

    standalone: list[ConstructionScale] = []
    for pvar_id, pvar_spec in sorted(plan_vars.items()):
        if str(pvar_id) in unit_pvars or not isinstance(pvar_spec, Mapping):
            continue
        standalone.append(
            ConstructionScale(
                planning_variable_id=str(pvar_id),
                value=_resolve_scale_value(str(pvar_id), pvar_spec),
                unit=str(pvar_spec["unit"]),
            )
        )
    return tuple(units), tuple(standalone)


def _resolve_params(ctx: StageContext, request: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    calc_cfg = request.get("calculation_config")
    if isinstance(calc_cfg, Mapping):
        algo_opts = (calc_cfg.get("options") or {}).get("algorithm")
        if isinstance(algo_opts, Mapping):
            merged.update(algo_opts)
    merged.update(ctx.parameters)
    return merged


def solve_dispatch(ctx: StageContext) -> None:
    """Solve convex economic dispatch from frozen assembly & series inputs."""
    request = read_algorithm_request(ctx.work_in)
    raw_artifact = request.get("artifact")
    if not isinstance(raw_artifact, Mapping) or not isinstance(raw_artifact.get("canonical_text"), str):
        raise StageIOError("algorithm /work/in/request.json requires 'artifact.canonical_text'")
    doc = json.loads(raw_artifact["canonical_text"])
    if not isinstance(doc, Mapping):
        raise StageIOError("artifact.canonical_text must decode to a JSON object")

    device_models = request.get("device_models") or {}
    if not isinstance(device_models, Mapping):
        raise StageIOError("request.device_models must be a JSON object")

    series_map, steps = _materialize_series_map(ctx, request, doc)
    grid_inst, gen_inst, load_insts = _resolve_device_roles(doc, device_models)

    load_series_by_inst: dict[str, list[float]] = {}
    demand = [0.0] * steps
    for l_inst in load_insts:
        vals = series_map.get((l_inst, "electricity_demand"))
        if vals is None:
            raise StageIOError(
                f"Missing frozen electricity_demand series for load instance {l_inst!r}"
            )
        load_series_by_inst[l_inst] = vals
        for idx, val in enumerate(vals):
            if val < 0.0:
                raise StageIOError(f"Negative electric demand at {l_inst}[{idx}]: {val!r}")
            demand[idx] += val

    params = _resolve_params(ctx, request)
    max_iterations = int(params.get("max_iterations", 10))
    tolerance = float(params.get("tolerance", 0.001))
    gen_cost = float(params.get("generator_cost_per_kwh", 0.35))
    grid_cost = float(params.get("grid_import_cost_per_kwh", 0.85))
    share_cap = float(params.get("local_energy_share_cap", 0.85))

    gen_spec = doc["devices"][gen_inst]
    default_cap = (
        _property_float(gen_spec, "rated_electric_kw")
        or _property_float(gen_spec, "rated_power_kw")
        or _property_float(gen_spec, "rated_capacity_kw")
    )
    if "generator_capacity_kw" in params:
        gen_cap = float(params["generator_capacity_kw"])
    elif default_cap is not None:
        gen_cap = default_cap
    else:
        raise StageIOError(f"Generator instance {gen_inst!r} missing rated capacity property")

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
    units, standalone_pvars = _bind_planning_outputs(
        doc, gen_inst=gen_inst, peak_dispatch=peak_dispatch
    )
    term_reason = "optimal" if final_gap <= tolerance else "not_solved"
    math_stat = "solved" if final_gap <= tolerance else "feasible"

    out_series: dict[str, DeviceSeries] = {
        f"{gen_inst}__electric_out": DeviceSeries(
            instance_id=gen_inst,
            variable_id="electric_out",
            unit="kW",
            values=tuple(best_g),
        ),
        f"{grid_inst}__electricity_import": DeviceSeries(
            instance_id=grid_inst,
            variable_id="electricity_import",
            unit="kW",
            values=tuple(best_m),
        ),
    }
    for l_inst, l_vals in sorted(load_series_by_inst.items()):
        out_series[f"{l_inst}__electricity_in"] = DeviceSeries(
            instance_id=l_inst,
            variable_id="electricity_in",
            unit="kW",
            values=tuple(round(v, 6) for v in l_vals),
        )

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
        planning_variables=standalone_pvars if standalone_pvars else None,
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
