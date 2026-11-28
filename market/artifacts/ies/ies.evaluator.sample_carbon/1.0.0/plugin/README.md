# Sample Carbon Evaluator (`ies.evaluator.sample_carbon@1.0.0`)

Self-describing post-solve evaluator plugin consuming `ies.engineering-solution@1.0.0` and producing `ies.evaluation-report@1.0.0`.

- **Parameters**: `emission_factor_t_per_mwh` (default `0.5703` tCO2/MWh), `step_hours` (default `1.0` h)
- **Metrics**: `total_grid_import_kwh` (`kWh`), `total_co2_tonnes` (`t`)
