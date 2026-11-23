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

For large-scale, long-horizon planning models (e.g., 25-year 30-minute horizons spanning `438,288` steps), optional heuristic presolve acceleration plugins guide arbitrary downstream exact solvers using an upper-level decision abstraction—preserving the three-stage isolation across [Computation Generators](generators.md) and [Solver Runtime](solver-runtime.md) without Worker-side cross-Bundle assembly mutations.

### Same-Format Primary Contract, Adapter Boundaries, and Optional Warm-Start

1. **Same-format-in, same-format-narrowed-model-out primary contract**: The presolve plugin conforms to the exact solver's native model input contract (e.g., MPS input emits a derived MPS directly readable by the same backend; other model formats read and write through their corresponding format adapters). Generic narrowing logic operates on variables, constraints, and Bundle-exported domain metadata, while each format adapter declares supported problem classes (pure LP, MILP, QP/SOCP, or NLP/MINLP) and fidelity-preserving transformations.
2. **Standalone operation via derived models; warm-start decoupled**: Narrowing works solely by emitting a same-format narrowed derived model (tightened bounds, fixed zero-capacity or redundant discrete variables, and updated Big-M constants) without assuming backend memory-handle integration. Primal warm-starts (`incumbent_hint`, optionally produced by Stage 2 full-horizon relaxation and analytic repair) are passed only when the target backend exposes a compatible initial-solution interface and controlled benchmarks demonstrate net gains.
3. **Cross-class acceleration mechanisms**: All pIES problem classes share a **"low-dimensional upper-level planning decisions (`planning.variables`, `planning.construction_units`) coupled with high-dimensional operational and tax time-step variables"** structure:

| Problem Class | Dominant Bottleneck | Same-Format Derived Model & Optional Warm-Start Mechanism |
|---|---|---|
| Pure LP | Slow factorization or iteration on million-row sparse matrices | Fixes zero-capacity redundant devices so downstream presolve eliminates device-step blocks for true matrix reduction; passes primal basis/hint when supported |
| MILP (Primary) | Massive pseudo-fractional complementarity binaries, loose Big-M, lack of pruning incumbent | Tightens Big-M constants jointly via `{property_ref: ...}` and fixes redundant switches; injects `x_init*` when supported |
| QP / SOCP | Cone coupling across long horizons inflates barrier decomposition cost | Fixes inactive candidates to eliminate active cones; seeds an interior warm-start center when supported |
| NLP / MINLP | Non-convexity, loose McCormick envelopes, extreme sensitivity to `x_0` | Compresses relaxation envelopes via tightened box bounds; stabilizes convergence from feasible `x_init*` when supported |

### Full-Horizon Preservation and Column-Mapping Invariance

- **Full-horizon fidelity in derived models**: Representative-day or extreme-window reduction is permitted solely internally inside the plugin to choose narrowing directions; the derived model handed to the exact solver **MUST preserve complete full-horizon time-step constraints, calendar-month peak demand, inter-step storage continuity, and all tax rules**.
- **In-place column identity preservation**: When tightening bounds, updating Big-M coefficients, or fixing variables to `0`, prefer preserving original variable identities and column ordering (e.g., updating column bounds or fixing bounds in place so the exact solver eliminates redundant columns internally). This guarantees `ResultAdapter` restores device/system series and `financial_facts` along `parameters.jsonl` column indices; if an adapter removes columns proactively, it **MUST reconstruct the full original column vector before handing outputs to `ResultAdapter`**.
- **Metadata bridging**: Because generic solver formats (such as raw MPS) do not inherently carry `{property_ref: ...}`, Gregorian month calendars, or construction-unit linkages, [generators](generators.md#upper-level-planning-structure-export-for-presolve-narrowing) export those structured mappings inside the Bundle.

### Config Freezing, Controlled Execution Flow, and Two-Stage Funnel

- **Frozen toggle and shared total time budget**: Presolve enablement and options freeze in [CalculationConfig](../formats/calculation-config.md) (`CalcSnapshot`) and are matched statically at submission against model format, problem class, and backend capabilities. When disabled, the exact solver reads the read-only original model directly; when enabled, the same controlled execution pipeline reads the read-only original model inside the isolated task workdir, emits the same-format derived model for the solver, and **retains the read-only original model for global bound certification or full-domain recovery**. Presolve work (including derived-model I/O) and exact solving **share the single requested `time_limit_seconds` budget**.

```text
[When enabled] Read-only original model + domain metadata (original file kept read-only in workdir)
  ↓ Funnel Stage 1: Peak-Preserving Temporal Surrogate Narrower
Same-format full-horizon derived model (tightened bounds / fixed redundant vars / updated Big-M; column map kept)
  ↓ Funnel Stage 2 (optional when backend supports warm-start): Full-Horizon Relaxation + O(T) Analytic Repair + O(N) Finance Pass
Consistent full-horizon primal x_init* (optionally injected into exact backend)
  ↓ Exact solve within shared budget (global bound check or full-domain fallback as needed)
Canonical primal solution (original column indexing)
```

1. **Funnel Stage 1 — Peak-Preserving Temporal Surrogate Narrowing**: Partitions the horizon by calendar month and deterministically extracts (under `CalculationConfig.seed`) "mandatory monthly peak/extreme windows (max net load and min renewable segments) plus day-weighted representative windows". Supports a linear/MILP surrogate kernel (solving the compressed surrogate with dual sensitivity expansion) and a derivative-free evolutionary kernel (DE / CMA-ES elite bounding envelope) combined with algebraic bound propagation to emit the narrowed same-format derived model.
2. **Funnel Stage 2 — Full-Horizon Relaxation & Analytic Repair Warm-Start**: Enabled when the backend supports warm-starts and benchmarks show net benefit: solves the full-horizon continuous relaxation with IPM/Barrier or PDLP-style first-order methods (avoiding Simplex degeneracy), runs an `O(T)` linear scan to snap pseudo-fractional `x * y = 0` complementarity binaries to `{0, 1}` (fixing only rare physical conflicts via net flow and local back-substitution), and runs an `O(N)` deterministic forward finance pass to populate tight monthly demand peaks, VAT credit pools, and income-tax/loss schedules into `x_init*`.

### Result Semantics: Provable Narrowing vs. Heuristic Empirical Narrowing

Two fundamentally different sources of bound narrowing MUST NOT be conflated:

| Narrowing Source | Mathematical Property | Global Optimality, Recovery Rule, and `math_status` Mapping |
|---|---|---|
| **Provable narrowing** (conservative bounds deduced strictly from original algebraic relations) | Strictly equivalent to the original feasible region without discarding any true global optimum | Safe to write directly into the exact model; reaching optimality tolerance within budget legitimately publishes `math_status: solved` (`optimal`) with native `best_bound` and `relative_gap`. |
| **Empirical narrowing** (heuristic search boxes from representative days, shadow prices, or elite populations) | Subdomain feasibility does **not** prove the global optimum lies inside (e.g., if the true optimum capacity is `8` while the narrowed box is `[3, 5]`, the subdomain stays feasible and triggers no infeasibility fallback, yet its local optimum and dual bound cover only `[3, 5]`); subdomain infeasibility likewise does **not** prove original infeasibility | (1) Publishing original-problem optimality (`solved`) REQUIRES either certifying the narrowed solution against a valid **global dual bound over the unrestricted original domain `[L, U]`**, or **restoring the read-only original model within remaining budget to continue solving**; (2) When the shared budget expires with a valid original-domain feasible primal, publish `feasible` (`restricted_results`), and **leave `best_bound` and `relative_gap` as `null`** in `outputs.solver_summary` whenever the global original-domain bound is unknown (never report a subdomain bound as global); (3) Empirical subdomain infeasibility proves only that the narrowed box is infeasible—within remaining budget restore the original domain and continue, and if budget exhausts without a proven original-domain status, publish `not_solved` rather than `infeasible`. |

### Two-Step Validation and Controlled Benchmarking Requirements

1. **Multi-backend format and counterexample verification**: Validate the same narrowing core across at least two solver backends or model formats, confirming that derived outputs load directly, recovered solutions map losslessly back to original device/system step series and `financial_facts`, and test cases explicitly cover both **"subdomain feasible yet misses true original optimum (e.g., original optimum `8`, narrowed box `[3, 5]`)"** and **"subdomain infeasible yet original problem feasible"**.
2. **Docker shared-budget toggle benchmark**: Benchmark each supported backend in Docker with presolve toggled on vs. off under the identical total `time_limit_seconds` budget—counting presolve runtime and derived-model I/O inside the total budget—comparing time-to-first-feasible, best objective at equal budget, and true original-domain gap (including isolating the net gain of Funnel Stage 2 relaxation repair and forward finance initialization).

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
