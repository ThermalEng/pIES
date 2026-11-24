# Computation Generation and Solving

> Code Boundary: `backend/iesplan/computation/`; Related Submodules: [Computation Generators](generators.md), [Solver Runtime](solver-runtime.md), [Solver Bundle](../formats/solver-bundle.md)

`computation` compiles a validated business system into concrete solver calculation bundles, executes them safely, and restores raw solver results back into uniform business outputs and post-solve financial evaluation facts—decoupling solver extensibility and container isolation from project and UI logic.

## End-to-End Computation Pipeline and Submodule Split

Under the `1.1.5` target architecture, prediction (`predictor`), generation (`generator`), solving (`solver`), and result adaptation & post-solve financial evaluation (`evaluator`) follow the unified `/work/in → /work/out` contract and run as per-step network-disabled ephemeral containers orchestrated by `backend` via `job_runner`; simple third-party algorithms MAY use a unified single-step `algorithm` container before evaluation.

```text
ValidatedAssemblyArtifact + CalculationConfig
  ↓ Series Materialization (constant / data_repeat / data_full / data_predict)
GeneratorProvider ──→ Solver Bundle ──→ SolverRuntime
                                             ↓
ComputeResult ←── ResultAdapter & Evaluation ←── ExecutionReceipt + Raw Outputs
  │
  ├─ Standalone prediction: run ephemeral predictor container once before subsequent stages
  └─ Coupled / internal-loop algorithms: execute self-contained inside one ephemeral container via the iteration interface
```

| Submodule | Responsibility | Non-Responsibility |
|---|---|---|
| Series Materialization | Materializes the four source modes into a full-horizon aligned `MaterializedSet` per project baseline | Mutating assembly or dataset versions |
| [Generators](generators.md) | Math lowering, unit conversion, Solver Bundle emission | Accessing databases or network |
| [Solver Runtime](solver-runtime.md) | Validates and dispatches Bundles through the container proxy inside isolated ephemeral containers to produce `ExecutionReceipt` | Interpreting device semantics or solver solutions |
| Result Adapter & Evaluation | Maps raw outputs, receipt, and frozen finance inputs into canonical-unit `ComputeResult` and financial evaluation facts | Re-running solvers, reading live drafts |

## Prediction Orchestration and Internal-Loop Interface

Task execution picks its path from the selected predictor descriptor's public `depends_on_solve_result` flag and manifest `iteration` declaration:

- **Standalone prediction (`false`)**: Trains and generates series once inside an isolated ephemeral `predictor` container before generation and holds them constant throughout;
- **Coupled / internal-loop algorithms (`true` or `iteration.enabled: true`)**: `predict(previous=...)` and companion solving/iterative logic execute **self-contained inside a single ephemeral container** (`None` on iteration 1 and read-only outputs thereafter) until every coupled target meets `CalculationConfig.convergence.tolerance` or reaches `max_iterations` (preserving actual convergence status and final solution if unconverged), emitting per-round convergence metrics to `/work/out/iterations.jsonl`.

### Prediction Parameters and Convergence Capabilities

- **Parameter validation**: Prediction Providers expose `validate_parameters(parameters)` invoked at calculation config save and task submission boundaries; execution directly consumes the frozen parameters.
- **Convergence metrics**: Coupled predictors MUST implement pure function `measure_convergence(previous_prediction, current_prediction, previous_outputs, current_outputs, parameters)` returning an immutable `PredictionConvergence` (`metric` ID, non-negative finite `value`, dimensionless unit `"1"`); when multiple targets coexist, total convergence error equals the maximum across targets. Iteration 1 has no predecessor and never fabricates zero error.

## Solver-Embedded Structural Acceleration

For large-scale, long-horizon planning models, structural acceleration is provided as a built-in solver capability governed by frozen [CalculationConfig](../formats/calculation-config.md) `options`, preserving the three-stage isolation across [Computation Generators](generators.md), Solver Bundle, [Solver Runtime](solver-runtime.md), and `ResultAdapter`; alternative non-HiGHS solver approaches continue integrating through the public `GeneratorProvider`, `ExecutorProvider`, and `ResultAdapterProvider` contracts.

- **Configuration and isolation contract**: Acceleration toggles and parameters freeze inside `CalcSnapshot` under public `options_schema` validation; built-in `ies.generator.highs_milp@1.0.0` enables lossless `on_demand_mutex` acceleration by default (`acceleration.mode: "on_demand_mutex"`), passes through unmodified to the original model when explicitly set to `"off"`, and keeps all accelerated execution within the same isolated runtime boundary.
- **Valid structural relaxation and original-model authority**: [Generators](generators.md#structural-metadata-export-for-solver-acceleration) export only compile-time-proven, safely relaxable independent auxiliary structure metadata inside the Bundle without spawning solver runs during generation; the solver executable MAY temporarily omit constraints on those proven structures during iterative solving, provided the original model remains immutable as the sole authority for final feasibility, column mappings, and optimality proof (falling back to the original solve path whenever structural safety cannot be proven), and the delivered solution satisfies physical conservation, calendar-month demand, storage continuity, and tax rules on the complete full-horizon original model.
- **Deadline, incumbent preservation, and status honesty**: Execution enforces one overall time budget without renewing expired budgets and preserves the best verified feasible incumbent before cutoff; `ResultAdapter` publishes `solved` only when a lossless relaxation reaches proven tolerance optimality and passes full original-model constraint verification, whereas time-limited stops or domain-restricted runs publish `feasible` (`restricted_results`) with uncertified original-domain `best_bound` and `relative_gap` set to `null`.

## Unified Result Model and Adapter Contract

`ComputeResult` separates process outcomes from mathematical outcomes via two orthogonal fields so runtime status and mathematical status are never conflated:

- **`execution_status`**: Reflects execution receipt facts strictly: `completed`, `rejected` (policy/input rejection), `failed` (non-OOM process failure), `timed_out`, `cancelled`, `fenced` (lease revoked), `resource_exhausted` (OOM/memory exhaustion), or `protocol_failed` (corrupt or missing output protocol).
- **`math_status`**: Reflects solver mathematical outcome: `solved` (proven optimal within tolerance), `feasible` (feasible solution found without proven optimality, e.g., time limit), `infeasible`, `unbounded`, `numeric_failure`, or `not_solved`.

### ResultAdapter Spec and Built-In HiGHS Adapter

- **Registration and invocation**: Extension adapters expose a public descriptor and `adapt(bundle, receipt, outputs)`, resolved by exact version ref in an independent registry.
- **Status and incumbent verification (`ies.result-adapter.highs@1.0.0`)**:
  - `ResultAdapter` determines `math_status` jointly from solver status, actual solved domain, and original-model verification evidence rather than `kOptimal` alone: maps to `solved` only when an unmodified domain or valid relaxation reaches `kOptimal` and passes original-model verification; maps to `feasible` with uncertified original-domain global bound/gap set to `null` whenever the search domain is restricted (even if the subdomain reports `kOptimal`); normal `kTimeLimit` maps to `feasible` when a complete primal solution exists and `not_solved` otherwise; `kInfeasible` / `kUnbounded` map to matching statuses; `kUnboundedOrInfeasible` maps to `not_solved`; supervisory timeouts, cancellations, OOM, or process crashes preserve their execution status without promoting residual output files to success.
  - When continuous demand upper envelopes are active and strict minimization wasn't enforced, the adapter checks monthly peak tightness prior to publication; inflated peaks reject distorted bills and map to `not_solved` (time limit) or `numeric_failure` (optimal tolerance stop).
- **Same-solve primal recovery**: Both `solved` and `feasible` restore planning variables, financial components, device/system step series, financial facts (`outputs.financial_facts`), and modeling provenance (`outputs.model_sources`) from the same primal vector by column position, restoring objective offsets and applying the sole reverse physical unit conversion.

### Solve Termination and Optimality Summary

When interpreting solver status normally, adapters publish `outputs.solver_summary` containing `termination_reason` (`optimal` / `time_limit` / `infeasible` / `unbounded` / `not_solved`), `math_status`, `has_feasible_solution`, `objective_value`, `objective_unit`, `best_bound`, and `relative_gap`:

- `objective_value` and `best_bound` share identical business units and objective offset; `relative_gap` retains the solver's native relative gap (MIP optimal stops within tolerance keep their real non-zero gap; optimal LPs lacking MIP statistics set bound equal to objective and gap to `0`; under restricted search domains or unavailable bounds, uncertified original-domain global `best_bound` and `relative_gap` remain `null`).
- Public result views project this sealed summary directly: `completed + feasible` corresponds at task level to `restricted_results` (allowing inspection of solutions and financial evaluation while marking optimality unproven); `completed + not_solved` corresponds to `insufficient_evidence`.

### Unified Step Series

`ResultAdapter` restores device step series `outputs.series` (keyed by `series_id` with `{instance_id, variable_id, unit, values}`) and auxiliary system series `outputs.system_series` (keyed by `series_id` with `{block, variable, unit, values}`) from Bundle metadata and the same primal solution, where `values` holds business-unit numeric values over every contiguous operating step. Both share one `series_id` namespace published whenever `solution` is requested; reading and export contracts appear in [Step Series Reading](../contracts.md#step-series-reading).
