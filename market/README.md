# pIES Market (`ThermalEng/pIES-Market`)

Standalone static public market for pIES device models (`ies.device-model@1.0.0`) and algorithm plugins (`ies.algorithm-plugin@1.0.0`).

- **Zero backend / database dependency**: uses only `pies-sdk` and `PyYAML`.
- **Publisher registry (`publishers.yaml`)**: binds community/vendor prefixes to HMAC-SHA256 email digests (zero plaintext emails); official `ies.*` artifacts live under `artifacts/ies/...` without registering `ies`.
- **Read-only PR validation (`pies_market.check_pr`)**: validates `publishers.yaml`, artifact ownership, device YAML schemas, plugin packages (`require_market_fields=True`), and optional `--network=none --read-only` Docker container self-tests against `pies-plugin-base:latest`.
- **Deterministic static release (`pies_market.build_index`)**: builds `dist/index.json` (`ies.market-index@1.0.0`), deterministic packages under `dist/artifacts/`, and copies the zero-build static browse/download portal `site/` into `dist/`.

## Official Reference Catalog (`artifacts/ies/`)

### Device Models (`ies.device-model@1.0.0`, `revision: 1`)
- `ies.device.solar_pv_sample` (`r1`): Sample PV component (`solar_irradiance` -> `electric_out`).
- `ies.device.chp` (`r1`): Gas-fired combined heat and power unit (`gas_in` -> `electric_out` + `heat_out`).
- `ies.device.thermal_storage` (`r1`): Bidirectional thermal energy storage tank (`heat`, `stored_energy` with `standby_retention`).
- `ies.device.absorption_chiller` (`r1`): Heat-driven absorption chiller (`heat_in` + `electricity_in` -> `cool_out`).
- `ies.device.wind_turbine` (`r1`): Wind turbine generator (`wind_capacity_factor` -> `electric_out`).

### Computation Plugins (`ies.algorithm-plugin@1.0.0`, `1.0.0`)
- `ies.predictor.seasonal_joint@1.0.0` (`predictor`): Seasonal moving-average multi-target predictor (`ies.stage-input@1.0.0` -> `ies.prediction-output@1.0.0`).
- `ies.solver.highspy_ref@1.0.0` (`solver`): Reference `highspy==1.7.2` MILP/LP solver (`ies.solver-bundle.mps@1.0.0` -> `ies.primal-solution@1.0.0`).
- `ies.algorithm.economic_dispatch@1.0.0` (`algorithm`): Iterative Lagrangian dual-bisection dispatch (`ies.stage-input@1.0.0` -> `ies.engineering-solution@1.0.0` + `/work/out/iterations.jsonl`).
- `ies.evaluator.sample_carbon@1.0.0` (`evaluator`): Sample grid-import CO2 evaluator (`ies.engineering-solution@1.0.0` -> `ies.evaluation-report@1.0.0`).
- `ies.evaluator.carbon_reliability@1.0.0` (`evaluator`): Carbon emissions & supply/renewable reliability evaluator (`ies.engineering-solution@1.0.0` -> `ies.evaluation-report@1.0.0`).
