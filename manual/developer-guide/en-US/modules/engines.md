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

For large-scale, long-horizon planning models (e.g., 25-year 30-minute horizons spanning `438,288` steps), optional heuristic presolve plugins focus on **rapidly obtaining a high-quality engineering optimum that satisfies full-horizon physics and finance rules** (rather than spending multiples of the runtime proving a theoretical global dual bound over overly wide initial bounds). Built around a pluggable `HeuristicProvider` contract with a single built-in default engine for Phase 1, it narrows high-value search regions for downstream exact solvers while preserving the three-stage boundary across [Computation Generators](generators.md) and [Solver Runtime](solver-runtime.md).

### Pluggable Protocol, Single-Read Memory API, and Same-Format Adaptation

1. **Lightweight pluggable protocol, single default engine**: The platform fixes the outer execution harness (model loading, column-map preservation, unified monotonic-clock deadline control, and infeasibility fallback) while abstracting search cores as replaceable `HeuristicProvider` implementations frozen by `<ID>@<version>`. Phase 1 ships a single structure-aware default engine without heavy external evolutionary libraries or dynamic user-package loading.
2. **In-memory API first for in-process backends; same-format derived models across processes**:
   - For in-process solver bindings (such as built-in HiGHS), prefer loading the read-only model **once into memory** and applying bound tightening, zero-capacity fixing, and discrete-repair warm-starts directly via solver APIs (`changeColsBounds` / `setSolution`), avoiding 100-second-scale duplicate MPS serialization and disk parsing overhead;
   - For cross-process or multi-format backends, follow a same-format-in, same-format-narrowed-model-out contract where each format adapter declares supported problem classes (LP, MILP, QP/SOCP, NLP/MINLP) and fidelity-preserving transformations.
3. **Cross-class acceleration mechanisms**: Every pIES formulation shares a **"low-dimensional upper-level planning decisions (`planning.variables`, `planning.construction_units`) coupled with high-dimensional operational and tax time-step variables"** structure:

| Problem Class | Dominant Bottleneck | Engineering Acceleration Mechanism |
|---|---|---|
| Pure LP | Slow factorization or iteration on million-row sparse matrices | Fixes zero-capacity redundant devices so downstream presolve eliminates entire device-step blocks; seeds primal hint when supported |
| MILP (Primary) | No feasible primal within budget, massive pseudo-fractional complementarity binaries, loose Big-M | Tightens upper-level capacity bounds and propagates tighter Big-M via `{property_ref: ...}`; pairs discrete fixing with LP repair for rapid feasible incumbents |
| QP / SOCP | Cone coupling across long horizons inflates barrier decomposition cost | Eliminates inactive candidate cones via zero-capacity fixing and seeds an interior warm-start center |
| NLP / MINLP | Non-convexity, loose McCormick envelopes, extreme sensitivity to `x_0` | Compresses relaxation envelopes via tightened box bounds and stabilizes local/global convergence from feasible incumbents |

### Full-Horizon Preservation and Column-Mapping Invariance

- **Full-horizon fidelity in exact execution**: Representative-day and extreme-aware temporal surrogates are used solely inside the heuristic stage to select narrowing boxes; the model solved by the downstream exact solver **MUST retain complete full-horizon time-step constraints, calendar-month peak demand, inter-step storage continuity, and all tax rules**, guaranteeing 100% physical conservation and exact same-solve financial consistency.
- **In-place column identity preservation**: Tightening bounds, updating Big-M coefficients, or fixing redundant devices to `0` SHOULD modify bounds in place within the original column space so downstream presolve eliminates redundant blocks automatically while `ResultAdapter` continues restoring step series and `financial_facts` along `parameters.jsonl` indices; any adapter that removes columns MUST reconstruct the full original column vector before handoff to `ResultAdapter`.
- **Metadata bridging**: Generic solver formats (such as raw MPS) do not natively carry `{property_ref: ...}`, Gregorian month boundaries, or construction-unit links; [generators](generators.md#upper-level-planning-structure-export-for-presolve-narrowing) export those structured mappings inside the Bundle.

### Config Freezing, Unified Monotonic Deadline, and Two-Stage Pipeline

- **Reuse `CalculationConfig.options` with one monotonic deadline**: Heuristic toggles and parameters freeze inside `CalculationConfig.options` (validated strictly via `GeneratorDescriptor.options_schema` inside `CalcSnapshot`). When disabled, execution runs unmodified; when enabled, a single `time.monotonic()` deadline accounts for model reading, surrogate narrowing, repair warm-starting, and exact solving inside the remaining budget—never granting full budgets separately per stage.

```text
[When enabled] Frozen options + unified deadline = now + time_limit_seconds
  ↓ Single model read (in-memory handle in-process; same-format derived file cross-process if needed)
  ↓ Stage 1: Extreme-Aware Surrogate Narrowing
Tighten capacity bounds [L_tight, U_tight], fix zero-capacity units, shrink Big-M in place (column map intact)
  ↓ Stage 2: Structure-Aware Fix-and-Repair / Optional Relaxation Repair
Construct discrete/capacity candidates → solve full-model LP repair or local repair to fill continuous steps → inject incumbent
  ↓ Narrowed-domain exact solve within remaining monotonic budget
  ├─ Feasible primal passes original-model residual checks → publish engineering optimum directly (no full-space rerun!)
  └─ Subdomain unexpectedly infeasible → widen bounds or restore original bounds within remaining budget
```

1. **Stage 1 — Extreme-Aware Surrogate Narrowing**: Partitions the horizon by calendar month and deterministically samples (under `CalculationConfig.seed`) peak load days, multi-day sustained energy-deficit segments (capturing long-duration storage sizing drivers), high price-spread days, and representative windows. Solves the compact isomorphic surrogate to lock high-value capacity boxes `[L_tight, U_tight]` and fix uneconomic units to `0` directly in the exact model.
2. **Stage 2 — Structure-Aware `Fix-and-Repair / Fix-and-Optimize`**: Inside the narrowed box, supplies construction/switching decisions via structure-aware rules or sub-models and leverages native solver LP repair (e.g., `setSolution` discrete repair or fixed-integer sub-LP/MILP) to populate inter-step storage `SOC`, ramping, and tax schedules accurately; any local `O(T)` rounding scan serves only as a candidate generator and MUST pass original-model bound, row, and integrality residual checks before publication.

### Engineering-First Delivery and Honest Status Reporting

1. **Publish narrowed-domain engineering optima directly without full-space re-solve**: Once a feasible primal inside `[L_tight, U_tight]` satisfies tolerance and original-model residual checks, publish the full engineering solution, step series, and same-solve financial evaluation directly—never re-running the unrestricted full space solely to certify a theoretical global dual bound.
2. **Fallback widening only upon subdomain infeasibility**: Empirical subdomain infeasibility triggers automatic bound widening or original-domain fallback within remaining budget; if budget exhausts without a verified primal, publish `not_solved` rather than misreporting `infeasible`.
3. **Honest optimality status disclosure**: Never present a subdomain dual bound as an unrestricted global bound over `[L, U]`. When no global original-domain dual bound was computed, publish the result transparently as `feasible` (`restricted_results`) or leave unrestricted `best_bound` and `relative_gap` as `null` in `outputs.solver_summary` while disclosing heuristic narrowing—reserving `solved` strictly for provably equivalent bounds or unrestricted-domain tolerance stops; all GUI inspection, financial evaluation, and export workflows remain fully usable.

### Incremental Rollout and Docker Benchmark Gates (`S0 -> S3`)

- **`S0` HiGHS baseline profiling → `S1` minimal provider & budget contract → `S2` first Fix-and-Repair engine → `S3` Docker shared-budget switch benchmarks**: Profile native HiGHS options first to isolate infeasibility vs. gap-closing bottlenecks, wire `options` and in-memory bound/incumbent interfaces, implement the first structure-aware repair and extreme-aware narrowing engine, and gate further extensions (full-horizon LP relaxation, ALNS, or evolutionary search) strictly on end-to-end Docker benchmarks measuring total runtime, peak RSS, time-to-first-feasible, and objective quality under identical total budgets.

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
