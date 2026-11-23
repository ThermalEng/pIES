# Computation Generation and Solving

> Code Boundary: `backend/iesplan/computation/`; Related Submodules: [Computation Generators](generators.md), [Solver Runtime](solver-runtime.md), [Solver Bundle](../formats/solver-bundle.md)

`computation` compiles a validated business system into concrete solver calculation bundles, executes them safely, and restores raw solver results back into uniform business outputs—decoupling solver extensibility and process security from project and UI logic.

## End-to-End Computation Pipeline and Submodule Split

```text
ValidatedAssemblyArtifact + CalculationConfig
  ↓ Series Materialization (constant / data_repeat / data_full / data_predict)
GeneratorProvider ──→ Solver Bundle ──→ SolverRuntime
                                             ↓
ComputeResult ←── ResultAdapter ←── ExecutionReceipt + Raw Outputs
  │
  ├─ Standalone prediction: publish result directly
  └─ Coupled prediction: update predicted series from prior outputs → rebuild & solve → check convergence
```

| Submodule | Responsibility | Non-Responsibility |
|---|---|---|
| Series Materialization | Materializes the four source modes into a full-horizon aligned `MaterializedSet` per project baseline | Mutating assembly or dataset versions |
| [Generators](generators.md) | Math lowering, unit conversion, Solver Bundle emission | Spawning processes, accessing databases |
| [Solver Runtime](solver-runtime.md) | Executes Bundle commands in isolation and produces `ExecutionReceipt` | Interpreting device semantics or solver solutions |
| Result Adapter | Maps raw outputs and receipt into `ComputeResult` with canonical units | Re-running solvers, reading live drafts |

## Prediction Orchestration and Convergence Capabilities

Task execution automatically picks its path from the selected predictor descriptor's public `depends_on_solve_result` flag:

- **Standalone prediction (`false`)**: Trains and generates series once before initial generation and holds them constant throughout;
- **Coupled prediction (`true`)**: `predict(previous=...)` receives `None` on iteration 1 and read-only `ComputeResult.outputs` from the preceding iteration thereafter, re-materializing, re-generating, and re-solving until every coupled target meets `CalculationConfig.convergence.tolerance` or reaches `max_iterations` (preserving actual convergence status and final solution if unconverged).

### Prediction Parameters and Convergence Capabilities

- **Parameter validation**: Prediction Providers expose `validate_parameters(parameters)` invoked at calculation config save and task submission boundaries; execution directly consumes the frozen parameters.
- **Convergence metrics**: Coupled predictors MUST implement pure function `measure_convergence(previous_prediction, current_prediction, previous_outputs, current_outputs, parameters)` returning an immutable `PredictionConvergence` (`metric` ID, non-negative finite `value`, dimensionless unit `"1"`); when multiple targets coexist, total convergence error equals the maximum across targets. Iteration 1 has no predecessor and never fabricates zero error.

## Universal Heuristic Presolve Acceleration and Two-Stage Funnel Coordination

For large-scale, long-horizon planning models (e.g., 25-year 30-minute horizons spanning `438,288` steps), optional heuristic presolve plugins focus on **rapidly finding a high-quality, best known engineering-feasible solution that satisfies full-horizon physical, operational, and financial constraints within finite compute budgets** rather than spending multiples of runtime proving theoretical global optimality over overly wide initial bounds. Built around a pluggable `HeuristicProvider` contract with a single Phase 1 default engine, it guides downstream exact solvers while preserving the three-stage isolation across [Computation Generators](generators.md) and [Solver Runtime](solver-runtime.md).

### Pluggable Protocol, Single-Read Memory API, and Phase 1 Scope

1. **Lightweight pluggable protocol, single default engine**: The platform owns the fixed outer execution harness (model loading, column-map preservation, unified monotonic-clock deadline control, original-model residual verification, and expansion/fallback handling) while abstracting search cores as replaceable `HeuristicProvider` implementations frozen by `<ID>@<version>`. Phase 1 ships a single structure-aware default engine without heavy external evolutionary frameworks or dynamic code loading.
2. **In-memory API first for in-process backends; same-format derived models across processes**:
   - For in-process solver bindings (such as built-in HiGHS), prefer loading the read-only model **once into memory** and applying bound tightening, zero-capacity fixing, and discrete-repair warm-starts directly via solver APIs (`changeColsBounds` / `setSolution`), avoiding 100-second-scale duplicate MPS serialization and disk parsing overhead;
   - For cross-process or multi-format backends, follow a same-format-in, same-format-narrowed-model-out contract where each format adapter declares supported problem classes and fidelity-preserving transformations.
3. **Phase 1 focus on `MILP` (and `LP`)**: Every pIES formulation shares a **"low-dimensional upper-level planning decisions (`planning.variables`, `planning.construction_units`) coupled with high-dimensional operational and tax time-step variables"** structure. While the contract stays open to future problem classes, `1.1.0` Phase 1 capability declarations and acceptance explicitly focus on **MILP (plus pure LP)** without making `QP/SOCP` or `NLP/MINLP` an acceptance burden for Phase 1:

| Problem Class | Phase 1 Status | Dominant Bottleneck and Acceleration Mechanism |
|---|---|---|
| MILP | **Phase 1 primary** | Slow or missing feasible primals, massive pseudo-fractional complementarity binaries, loose Big-M; narrows upper bounds and tightens Big-M via `{property_ref: ...}`, paired with discrete fixing and full-model repair |
| Pure LP | **Phase 1 supported** | Slow factorization or iteration on million-row sparse matrices; fixes zero-capacity redundant devices so downstream presolve eliminates entire device-step blocks and seeds a primal hint |
| QP / SOCP / NLP / MINLP | Reserved (outside Phase 1 acceptance) | Future adapters reuse the same upper-level narrowing and candidate incumbent channels |

### Full-Horizon Preservation and Column-Mapping Invariance

- **Full-horizon fidelity in exact execution**: Representative-day and extreme-aware temporal surrogates are used solely inside the heuristic stage to select narrowing boxes; the model solved by the downstream exact solver **MUST retain complete full-horizon time-step constraints, calendar-month peak demand, inter-step storage continuity, and all tax rules**, ensuring all specified constraints are satisfied within numerical tolerances across the full horizon alongside exact same-solve financial consistency.
- **In-place column identity preservation**: Tightening bounds, updating Big-M coefficients, or fixing redundant devices to `0` SHOULD modify bounds in place within the original column space so downstream presolve eliminates redundant blocks automatically while `ResultAdapter` continues restoring step series and `financial_facts` along `parameters.jsonl` indices; any adapter that removes columns MUST reconstruct the full original column vector before handoff to `ResultAdapter`.
- **Metadata bridging**: Generic solver formats (such as raw MPS) do not natively carry `{property_ref: ...}`, Gregorian month boundaries, or construction-unit links; [generators](generators.md#upper-level-planning-structure-export-for-presolve-narrowing) export those structured mappings inside the Bundle.

### Config Freezing, Unified Monotonic Deadline, and Lightweight Quality Control

- **Reuse `CalculationConfig.options` with one monotonic deadline**: Heuristic toggles and parameters freeze inside `CalculationConfig.options` (validated strictly via `GeneratorDescriptor.options_schema` inside `CalcSnapshot`). When disabled, execution runs unmodified; when enabled, a single `time.monotonic()` deadline accounts for model reading, surrogate narrowing, repair warm-starting, original-model residual verification, and exact solving inside the remaining budget—never granting separate full budgets per stage.

```text
[When enabled] Frozen options + unified deadline = now + time_limit_seconds
  ↓ Single model read (in-memory handle in-process; same-format derived file cross-process if needed)
  ↓ Stage 1: Extreme-Aware Surrogate Narrowing
Tighten capacity bounds [L_tight, U_tight], fix zero-capacity units, shrink Big-M in place (column map intact)
  ↓ Stage 2: Structure-Aware Fix-and-Repair / Optional Relaxation Repair
Construct discrete/capacity candidates → solve full-model LP repair or local repair to fill continuous steps → inject incumbent
  ↓ Narrowed-domain solve & lightweight quality control within remaining monotonic budget
  ├─ High-quality feasible primal inside subdomain → verify original-model tolerances & deliver best known engineering-feasible solution
  ├─ Best candidate clearly hits empirical narrowed boundary (budget allowing) → attempt local expansion along active boundary
  ├─ Subdomain infeasible or slow to find feasible → attempt wider box or fall back to original domain
  └─ Budget exhausted with valid feasible solution → publish best known engineering-feasible solution immediately
```

1. **Stage 1 — Extreme-Aware Surrogate Narrowing**: Partitions the horizon by calendar month and deterministically samples (under `CalculationConfig.seed`) peak load days, multi-day sustained energy-deficit segments (capturing long-duration storage sizing drivers), high price-spread days, and representative windows. Solves the compact isomorphic surrogate to lock high-value capacity boxes `[L_tight, U_tight]` and fix uneconomic units to `0` directly in the exact model.
2. **Stage 2 — Structure-Aware `Fix-and-Repair / Fix-and-Optimize`**: Inside the narrowed box, supplies construction/switching decisions via structure-aware rules or sub-models and leverages native solver repair (e.g., `setSolution` discrete LP/MIP repair or fixed-integer sub-LP/MILP) to populate inter-step storage `SOC`, ramping, and tax schedules accurately; any local `O(T)` rounding scan acts solely as a candidate generator without promising closed-form completion of all continuous steps, and candidate primals MUST pass full-model repair and residual checks.

| Trigger Condition | Handling Strategy |
|---|---|
| High-quality feasible solution found inside subdomain | Deliver the best known engineering-feasible solution directly without mandatory expansion or full-space rerun |
| Best candidate clearly hugs an empirical narrowed boundary | When remaining budget permits, attempt targeted local expansion along the active bound to avoid leaving large gains behind |
| Subdomain infeasible or fails to find feasible promptly | Attempt bound expansion or fall back to the read-only original domain |
| Total budget exhausted with a valid feasible solution | Publish the best known engineering-feasible solution immediately |

### Engineering-First Delivery, Original-Model Verification, and Status Honesty

1. **Empirical narrowing MUST publish as `feasible` AND set original-domain bounds to `null`**: Any valid solution produced under empirical narrowing (e.g., narrowing `0..10 MW` to `3..5 MW`) **MUST publish as `math_status: feasible`** (`restricted_results` at task level)—**even if the solver reports `kOptimal` inside the subdomain, it MUST NOT map to global `solved`**—**and** both uncertified original-domain `best_bound` and `relative_gap` **MUST be `null`** (never presenting a subdomain dual bound as global).
2. **Separate subdomain diagnostics**: Preserve subdomain termination status (e.g., `subdomain_status`), subdomain bound/`subdomain_relative_gap`, applied narrowed bounds, and `HeuristicProvider` ref in separate diagnostic fields of `outputs.solver_summary`; GUI inspection, same-solve financial evaluation, and exports remain fully functional under `feasible`. Only provably equivalent algebraic bounds or unrestricted-domain tolerance stops publish as `solved`.
3. **Incremental original-model constraint verification**: Before publishing any heuristically narrowed or repaired solution, verify feasibility against the **pre-narrowing original mathematical relations (including original Big-M constraints, variable bounds, row constraints, integrality, and objective)** so all specified constraints hold within numerical tolerances. In-process executors SHOULD retain compact original constraints or bound/coefficient modification deltas in memory to avoid re-parsing large MPS files.

### Incremental Rollout and Docker Verification Gates (`S0 -> S3`)

- **`S0` HiGHS baseline profiling**: Benchmark native `highspy 1.7.2` presolve, heuristics, and solver options to separate model-read time, infeasibility bottlenecks, and gap-closing bottlenecks.
- **`S1` minimal contract and `setSolution` verification loop**: Wire frozen `CalculationConfig.options`, monotonic deadline control, and `HeuristicProvider` contracts, explicitly testing partial integer incumbent acceptance, LP/MIP repair success, repair failure on infeasible discrete seeds, and timeout behavior on the pinned `highspy 1.7.2` runtime.
- **`S2` first structure-aware Fix-and-Repair engine**: Deliver `Extreme-Aware` narrowing and `Fix-and-Repair`, in-memory bound updates, and incremental original-model residual verification while guaranteeing identical behavior when disabled.
- **`S3` Docker shared-budget toggle benchmarks**: Compare presolve on vs. off in Docker across total wall-clock time, peak RSS, time-to-first-feasible, **best feasible objective value under identical time budgets**, and original-domain gap (where available), gating full-horizon LP relaxation, ALNS, or evolutionary extensions on proven net gains.

## Unified Result Model and Adapter Contract

`ComputeResult` separates process outcomes from mathematical outcomes via two orthogonal fields so runtime status and mathematical status are never conflated:

- **`execution_status`**: Reflects execution receipt facts strictly: `completed`, `rejected` (policy/input rejection), `failed` (non-OOM process failure), `timed_out`, `cancelled`, `fenced` (lease revoked), `resource_exhausted` (OOM/memory exhaustion), or `protocol_failed` (corrupt or missing output protocol).
- **`math_status`**: Reflects solver mathematical outcome: `solved` (proven optimal within tolerance), `feasible` (feasible solution found without proven optimality, e.g., time limit), `infeasible`, `unbounded`, `numeric_failure`, or `not_solved`.

### ResultAdapter Spec and Built-In HiGHS Adapter

- **Registration and invocation**: Extension adapters expose a public descriptor and `adapt(bundle, receipt, outputs)`, resolved by exact version ref in an independent registry.
- **Status and incumbent verification (`ies.result-adapter.highs@1.0.0`)**:
  - `kOptimal` maps to `solved`; normal `kTimeLimit` maps to `feasible` when a complete primal solution exists and `not_solved` otherwise; `kInfeasible` / `kUnbounded` map to matching statuses; `kUnboundedOrInfeasible` maps to `not_solved`; supervisory timeouts, cancellations, OOM, or process crashes preserve their execution status without promoting residual output files to success.
  - When continuous demand upper envelopes are active and strict minimization wasn't enforced, the adapter checks monthly peak tightness prior to publication; inflated peaks reject distorted bills and map to `not_solved` (time limit) or `numeric_failure` (optimal tolerance stop).
- **Same-solve primal recovery**: Both `solved` and `feasible` restore planning variables, financial components, device/system step series, financial facts (`outputs.financial_facts`), and modeling provenance (`outputs.model_sources`) from the same primal vector by column position, restoring objective offsets and applying the sole reverse physical unit conversion.

### Solve Termination and Optimality Summary

When interpreting solver status normally, adapters publish `outputs.solver_summary` containing `termination_reason` (`optimal` / `time_limit` / `infeasible` / `unbounded` / `not_solved`), `math_status`, `has_feasible_solution`, `objective_value`, `objective_unit`, `best_bound`, and `relative_gap`:

- `objective_value` and `best_bound` share identical business units and objective offset; `relative_gap` retains the solver's native relative gap (MIP optimal stops within tolerance keep their real non-zero gap; optimal LPs lacking MIP statistics set bound equal to objective and gap to `0`; unavailable values remain `null`).
- Public result views project this sealed summary directly: `completed + feasible` corresponds at task level to `restricted_results` (allowing inspection of solutions and financial evaluation while marking optimality unproven); `completed + not_solved` corresponds to `insufficient_evidence`.

### Unified Step Series

`ResultAdapter` restores device step series `outputs.series` (keyed by `series_id` with `{instance_id, variable_id, unit, values}`) and auxiliary system series `outputs.system_series` (keyed by `series_id` with `{block, variable, unit, values}`) from Bundle metadata and the same primal solution, where `values` holds business-unit numeric values over every contiguous operating step. Both share one `series_id` namespace published whenever `solution` is requested; reading and export contracts appear in [Step Series Reading](../contracts.md#step-series-reading).
