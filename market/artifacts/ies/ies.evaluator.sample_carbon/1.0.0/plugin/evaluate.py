"""Sample carbon emission evaluator plugin (`ies.evaluator.sample_carbon@1.0.0`)."""

from __future__ import annotations

from pies_sdk import (
    EvaluationMetric,
    EvaluationReportPayload,
    StageContext,
)


def evaluate(ctx: StageContext) -> None:
    """Compute total CO2 emissions from imported electricity series."""
    solution = ctx.read_engineering_solution()
    factor = float(ctx.parameters.get("emission_factor_t_per_mwh", 0.5703))

    total_kwh = 0.0
    if solution.solver_summary.has_feasible_solution and solution.series:
        for entry in solution.series.values():
            if entry.variable_id == "grid_import_kw" and entry.unit == "kW":
                total_kwh += sum(float(v) for v in entry.values)

    total_co2_tonnes = round((total_kwh / 1000.0) * factor, 6)
    report = EvaluationReportPayload(
        title="Sample Carbon Emission Report",
        evaluator=f"{ctx.manifest.plugin_id}@{ctx.manifest.version}",
        metrics=(
            EvaluationMetric(
                id="total_grid_import_kwh",
                label="Total Grid Import Energy",
                value=round(total_kwh, 6),
                unit="kWh",
            ),
            EvaluationMetric(
                id="total_co2_tonnes",
                label="Total CO2 Emissions",
                value=total_co2_tonnes,
                unit="t",
            ),
        ),
        charts=(),
        tables=(),
    )
    ctx.write_evaluation_report(report)
