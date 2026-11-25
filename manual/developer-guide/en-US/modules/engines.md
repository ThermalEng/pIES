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

## Solver-Embedded Structural Acceleration (Official Built-In HiGHS Quality Control)

For large-scale, long-horizon planning models, structural acceleration is provided as a built-in capability of the official solver (`ies.solver.highs`) governed by frozen [CalculationConfig](../formats/calculation-config.md) `options.generator`, preserving clean separation across [Computation Generators](generators.md), [Solver Runtime](solver-runtime.md), and `backend` bounded-subprocess restoration; third-party solver approaches integrate directly through the public `ies.algorithm-plugin` (`generator` / `solver` / `algorithm`) container contract.

- **Configuration and isolation contract**: Acceleration toggles and parameters freeze inside `CalcSnapshot` under `parameters_schema` validation; built-in `ies.generator.highs_milp@1.0.0` enables lossless `on_demand_mutex` acceleration by default (`acceleration.mode: "on_demand_mutex"`) and passes through unmodified to the original model when explicitly set to `"off"`.
- **Valid structural relaxation and original-model authority**: [Generators](generators.md#structural-metadata-export-for-solver-acceleration) export only compile-time-proven, safely relaxable independent auxiliary structure metadata without spawning solver runs during generation; built-in `ies-highs-solver` MAY temporarily omit proven constraints during iterative solving and performs full original-model constraint tolerance checks plus continuous demand upper-envelope tightness checks **internally inside the solver** (mapping uncertified inflated peaks to `not_solved` or `numeric_failure` per termination case).
- **Deadline, incumbent preservation, and status honesty**: Execution enforces one overall time budget without renewing expired budgets and preserves the best verified feasible incumbent before cutoff; the built-in solver emits `solved` only when an unmodified domain or valid relaxation reaches tolerance optimality and passes full original-model verification, whereas time-limited stops or restricted search domains emit `feasible` (`restricted_results`) with uncertified original-domain `best_bound` and `relative_gap` set to `null`.

## Unified Result Model and Bounded-Subprocess Restoration Contract

`ComputeResult` separates process outcomes from mathematical outcomes via two orthogonal fields so runtime status and mathematical status are never conflated:

- **`execution_status`**: Reflects execution receipt facts strictly: `completed`, `rejected` (policy/input rejection), `failed` (non-OOM process failure), `timed_out`, `cancelled`, `fenced` (lease revoked), `resource_exhausted` (OOM/memory exhaustion), or `protocol_failed` (corrupt or missing output protocol).
- **`math_status`**: Reflects solver mathematical outcome: `solved` (proven optimal within tolerance), `feasible` (feasible solution found without proven optimality, e.g., time limit), `infeasible`, `unbounded`, `numeric_failure`, or `not_solved`.

### `backend` Bounded-Subprocess Restoration Responsibilities (`ResultAdapter`)

- **Built-in platform infrastructure (no external adapter registry)**: `ResultAdapter` is a trusted cross-step coordination component inside `backend` (streaming inside an isolated `spawn` child subprocess without maintaining an external adapter plugin registry or exposing an `adapt(...)` extension hook).
- **Faithful parsing, status projection, and reverse unit conversion**:
  - Consumes solely `ExecutionReceipt`, `primal_solution.json` ordered strictly by **0-based input model columns**, and `generator`'s `variable_map`; supervisory timeouts, cancellations, OOM, or process crashes preserve their `execution_status` without promoting residual output files to success;
  - For `solved` and `feasible` outcomes, faithfully restores planning construction unit states/scales, device/system step series, and modeling provenance (`outputs.model_sources`) by 0-based column index, restoring objective offsets and executing the sole reverse physical unit conversion into the same canonical engineering solution schema emitted by `algorithm`—**never re-solving or certifying third-party `solver` plugins**.

### Solve Termination and Optimality Summary

When interpreting solver status normally, adapters publish `outputs.solver_summary` containing `termination_reason` (`optimal` / `time_limit` / `infeasible` / `unbounded` / `not_solved`), `math_status`, `has_feasible_solution`, `objective_value`, `objective_unit`, `best_bound`, and `relative_gap`:

- `objective_value` and `best_bound` share identical business units and objective offset; `relative_gap` retains the solver's native relative gap (MIP optimal stops within tolerance keep their real non-zero gap; optimal LPs lacking MIP statistics set bound equal to objective and gap to `0`; under restricted search domains or unavailable bounds, uncertified original-domain global `best_bound` and `relative_gap` remain `null`).
- Public result views project this sealed summary directly: `completed + feasible` corresponds at task level to `restricted_results` (allowing inspection of solutions and financial evaluation while marking optimality unproven); `completed + not_solved` corresponds to `insufficient_evidence`.

### Unified Step Series

`ResultAdapter` restores device step series `outputs.series` (keyed by `series_id` with `{instance_id, variable_id, unit, values}`) and auxiliary system series `outputs.system_series` (keyed by `series_id` with `{block, variable, unit, values}`) from Bundle metadata and the same primal solution, where `values` holds business-unit numeric values over every contiguous operating step. Both share one `series_id` namespace published whenever `solution` is requested; reading and export contracts appear in [Step Series Reading](../contracts.md#step-series-reading).
