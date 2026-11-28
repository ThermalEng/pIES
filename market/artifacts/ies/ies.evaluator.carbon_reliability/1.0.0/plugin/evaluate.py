"""Reference carbon & reliability evaluator (`ies.evaluator.carbon_reliability@1.0.0`)."""

from __future__ import annotations

from typing import Any

from pies_sdk import (
    EvaluationChart,
    EvaluationChartSeries,
    EvaluationMetric,
    EvaluationReportPayload,
    EvaluationTable,
    EvaluationTableColumn,
    StageContext,
    StageIOError,
)


def _str_set(raw: Any, default: tuple[str, ...]) -> set[str]:
    if raw is None:
        return set(default)
    if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
        raise StageIOError(f"Semantic selector parameter must be a list of non-empty strings: {raw!r}")
    return set(raw)


def evaluate(ctx: StageContext) -> None:
    """Compute carbon emissions, renewable share, and supply reliability KPIs, charts, and tables."""
    solution = ctx.read_engineering_solution()
    grid_ef = float(ctx.parameters.get("grid_emission_factor_kg_per_kwh", 0.5703))
    gas_ef = float(ctx.parameters.get("gas_emission_factor_kg_per_m3", 2.162))
    step_hours = float(ctx.parameters.get("step_hours", 1.0))

    grid_var_ids = _str_set(
        ctx.parameters.get("grid_import_variable_ids"),
        ("electricity_import", "grid_import_kw"),
    )
    gas_var_ids = _str_set(
        ctx.parameters.get("gas_input_variable_ids"),
        ("gas_in", "gas_import_m3_h"),
    )
    gen_var_ids = _str_set(
        ctx.parameters.get("generation_variable_ids"),
        ("electric_out",),
    )
    renewable_instances = _str_set(ctx.parameters.get("renewable_instance_ids"), ())
    demand_instances = _str_set(ctx.parameters.get("demand_instance_ids"), ())
    demand_var_ids = _str_set(
        ctx.parameters.get("demand_variable_ids"),
        ("electricity_in", "load_kw"),
    )

    grid_kw_by_step: list[float] = []
    gas_m3h_by_step: list[float] = []
    renewable_kw_by_step: list[float] = []
    demand_kw_by_step: list[float] = []
    supply_kw_by_step: list[float] = []
    demand_series_matched = False

    def _accumulate(target: list[float], values: tuple[float, ...]) -> None:
        if not target:
            target.extend(float(v) for v in values)
        else:
            for idx, v in enumerate(values):
                target[idx] += float(v)

    feasible = solution.solver_summary.has_feasible_solution
    step_count = 0
    if feasible and solution.series:
        for entry in solution.series.values():
            step_count = max(step_count, len(entry.values))
            var = entry.variable_id
            unit = entry.unit
            if unit == "kW" and var in grid_var_ids:
                _accumulate(grid_kw_by_step, entry.values)
                _accumulate(supply_kw_by_step, entry.values)
            elif unit in {"m3/h", "m³/h"} and var in gas_var_ids:
                _accumulate(gas_m3h_by_step, entry.values)
            elif unit == "kW" and var in gen_var_ids:
                _accumulate(supply_kw_by_step, entry.values)
                if entry.instance_id in renewable_instances:
                    _accumulate(renewable_kw_by_step, entry.values)

            if unit == "kW" and (
                (demand_instances and entry.instance_id in demand_instances and var in demand_var_ids)
                or (not demand_instances and var == "load_kw" and var in demand_var_ids)
            ):
                _accumulate(demand_kw_by_step, entry.values)
                demand_series_matched = True

    if step_count == 0:
        step_count = 1
    for bucket in (
        grid_kw_by_step,
        gas_m3h_by_step,
        renewable_kw_by_step,
        demand_kw_by_step,
        supply_kw_by_step,
    ):
        if not bucket:
            bucket.extend([0.0] * step_count)

    total_grid_kwh = round(sum(grid_kw_by_step) * step_hours, 6)
    total_gas_m3 = round(sum(gas_m3h_by_step) * step_hours, 6)
    total_renewable_kwh = round(sum(renewable_kw_by_step) * step_hours, 6)
    total_supply_kwh = round(sum(supply_kw_by_step) * step_hours, 6)
    total_demand_kwh = round(sum(demand_kw_by_step) * step_hours, 6)

    grid_co2_tonnes = round((total_grid_kwh * grid_ef) / 1000.0, 6)
    gas_co2_tonnes = round((total_gas_m3 * gas_ef) / 1000.0, 6)
    total_co2_tonnes = round(grid_co2_tonnes + gas_co2_tonnes, 6)

    supply_reliability_pct: float | None
    if not feasible:
        supply_reliability_pct = 0.0
    elif not demand_series_matched or total_demand_kwh <= 0.0:
        supply_reliability_pct = None
    else:
        supply_reliability_pct = round(min(100.0, (total_supply_kwh / total_demand_kwh) * 100.0), 4)

    renewable_share_pct = (
        round((total_renewable_kwh / total_supply_kwh) * 100.0, 4)
        if total_supply_kwh > 0.0
        else 0.0
    )

    x_steps = tuple(f"t{idx}" for idx in range(step_count))
    co2_step_kg = tuple(
        round((g_kw * grid_ef + gas_v * gas_ef) * step_hours, 6)
        for g_kw, gas_v in zip(grid_kw_by_step, gas_m3h_by_step, strict=True)
    )

    grid_share = round((grid_co2_tonnes / total_co2_tonnes) * 100.0, 4) if total_co2_tonnes > 0.0 else 0.0
    gas_share = round((gas_co2_tonnes / total_co2_tonnes) * 100.0, 4) if total_co2_tonnes > 0.0 else 0.0
    rel_text = (
        f"supply reliability {supply_reliability_pct}%"
        if supply_reliability_pct is not None
        else "supply reliability unavailable (no demand series bound)"
    )

    report = EvaluationReportPayload(
        title="Carbon & Reliability Report",
        evaluator=f"{ctx.manifest.plugin_id}@{ctx.manifest.version}",
        summary=(
            f"Total CO2: {total_co2_tonnes} t "
            f"(grid {grid_co2_tonnes} t, gas {gas_co2_tonnes} t); {rel_text}."
        ),
        metrics=(
            EvaluationMetric(
                id="total_co2_tonnes",
                label="Total CO2 Emissions",
                value=total_co2_tonnes,
                unit="t",
            ),
            EvaluationMetric(
                id="grid_co2_tonnes",
                label="Grid Electricity CO2",
                value=grid_co2_tonnes,
                unit="t",
            ),
            EvaluationMetric(
                id="gas_co2_tonnes",
                label="Natural Gas CO2",
                value=gas_co2_tonnes,
                unit="t",
            ),
            EvaluationMetric(
                id="supply_reliability_pct",
                label="Supply Reliability",
                value=supply_reliability_pct,
                unit="%",
            ),
            EvaluationMetric(
                id="renewable_share_pct",
                label="Renewable Electricity Share",
                value=renewable_share_pct,
                unit="%",
            ),
            EvaluationMetric(
                id="total_grid_import_kwh",
                label="Grid Electricity Import",
                value=total_grid_kwh,
                unit="kWh",
            ),
            EvaluationMetric(
                id="total_gas_consumption_m3",
                label="Natural Gas Consumption",
                value=total_gas_m3,
                unit="m3",
            ),
        ),
        charts=(
            EvaluationChart(
                id="hourly_dispatch_and_emissions",
                title="Stepwise Grid Import, Renewable Output & CO2",
                x_label="Step",
                y_label="Power / CO2",
                y_unit="kW",
                chart_type="line",
                x_values=x_steps,
                series=(
                    EvaluationChartSeries(
                        id="grid_import_kw",
                        label="Grid Import",
                        unit="kW",
                        values=tuple(round(v, 6) for v in grid_kw_by_step),
                    ),
                    EvaluationChartSeries(
                        id="renewable_output_kw",
                        label="Renewable Output",
                        unit="kW",
                        values=tuple(round(v, 6) for v in renewable_kw_by_step),
                    ),
                    EvaluationChartSeries(
                        id="step_co2_kg",
                        label="Step CO2",
                        unit="kg",
                        values=co2_step_kg,
                    ),
                ),
            ),
            EvaluationChart(
                id="co2_source_breakdown",
                title="CO2 Emissions by Source",
                x_label="Source",
                y_label="Emissions",
                y_unit="t",
                chart_type="bar",
                x_values=("Grid Electricity", "Natural Gas"),
                series=(
                    EvaluationChartSeries(
                        id="co2_tonnes",
                        label="CO2 Emissions",
                        unit="t",
                        values=(grid_co2_tonnes, gas_co2_tonnes),
                    ),
                ),
            ),
        ),
        tables=(
            EvaluationTable(
                id="carbon_reliability_summary",
                title="Carbon & Reliability Summary",
                columns=(
                    EvaluationTableColumn(id="source", label="Source"),
                    EvaluationTableColumn(id="consumption", label="Consumption"),
                    EvaluationTableColumn(id="unit", label="Unit"),
                    EvaluationTableColumn(id="co2_tonnes", label="CO2", unit="t"),
                    EvaluationTableColumn(id="share_pct", label="CO2 Share", unit="%"),
                ),
                rows=(
                    ("Grid Electricity", total_grid_kwh, "kWh", grid_co2_tonnes, grid_share),
                    ("Natural Gas", total_gas_m3, "m3", gas_co2_tonnes, gas_share),
                    (
                        "Total",
                        round(total_co2_tonnes, 6),
                        "tCO2",
                        total_co2_tonnes,
                        100.0 if total_co2_tonnes > 0 else 0.0,
                    ),
                ),
            ),
        ),
    )
    ctx.write_evaluation_report(report)
