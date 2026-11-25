# Computation Generation and Solving

> Code Boundary: `backend/iesplan/computation/`; Related Submodules: [Computation Generators](generators.md), [Solver Runtime](solver-runtime.md), [Solver Bundle](../formats/solver-bundle.md)

`computation` compiles a validated business system into concrete solver calculation bundles, executes them safely, and restores raw solver results back into uniform business outputs and post-solve financial evaluation facts—decoupling solver extensibility and container isolation from project and UI logic.

## End-to-End Computation Pipeline and Submodule Split

Under the `1.2.0` unidirectional pipeline architecture, prediction (`predictor`), generation (`generator`), solving (`solver`), unified algorithms (`algorithm`), and parallel post-solve evaluation (`evaluator`) follow the unified `/work/in → /work/out` contract and run as per-step network-disabled ephemeral containers orchestrated by `backend` via `job_runner`, with cross-stage read-only context staged via zero-copy hardlinks by the orchestrator:

```text
ValidatedAssemblyArtifact + CalculationConfig
  ↓ Series Materialization (constant / data_repeat / data_full + grouped joint predictions[])
  ├─ Staged Mode:  GeneratorProvider ──→ SolverRuntime ──→ backend Bounded Subprocess Restores Canonical Engineering Solution
  └─ Unified Mode: Algorithm Container ──────────────────→ Emits Canonical Engineering Solution Directly
                                                                  ↓
        Sealed Result ←── evaluators[] (runs only when feasible; partial evaluator failure preserves solution & supports retry)
```

| Submodule | Responsibility | Non-Responsibility |
|---|---|---|
| Series Materialization & Prediction | Merges static sources and per-plugin grouped single-pass predictions into a full-horizon aligned `MaterializedSet` | Mutating assembly or cross-container loops |
| [Generators](generators.md) | Math lowering, forward unit conversion, solver input & `variable_map` emission | Accessing databases or network |
| [Solver Runtime](solver-runtime.md) | Executes solvers via the container proxy inside isolated ephemeral containers to emit 0-based original-column-order primal solutions and `ExecutionReceipt` | Interpreting device semantics, converting units, or relaying business files |
| Bounded-Subprocess Restoration & Parallel Evaluators | `backend` faithfully restores canonical engineering solutions via `variable_map` in a bounded subprocess; runs `0..N` parallel `evaluator` plugins with per-evaluator retry | Re-running core solves, reading live drafts, or certifying third-party solvers |

## Per-Plugin Grouped Prediction and Intra-Container Loop Interface

- **Grouped & joint multi-target prediction**: At config save and task submission, `predictions[].parameters` validates independently against each predictor plugin's `parameters_schema`. At runtime, targets group by selected `predictor` plugin within the logical prediction stage so correlated targets run jointly in one container pass (preserving shared latent weather/solar correlations) before the framework merges all predicted series, with no solver-coupled cross-container outer loop.
- **Intra-container loop interface (`iteration`)**: Plugins containing internal iterative search (metaheuristics, simulation-optimization, or intra-solver recovery) execute **self-contained inside one ephemeral container**, appending per-round convergence metrics (`iteration`, `metric`, `value`, `unit: "1"`) to `/work/out/iterations.jsonl` under the single container timeout.

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
