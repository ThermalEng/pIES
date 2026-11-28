# Computation Generators

> Code Boundary: `backend/iesplan/computation/generators/`; Handoff Format: [Solver Bundle](../formats/solver-bundle.md)

Generators act as the "model compiler": accepting trusted inputs already validated by assembly and upstream gates, lowering mathematical relations, deriving unified bounds, applying the sole business-to-solver unit conversion, and deterministically emitting solver input files, structured commands, parameter records, and result mapping declarations packaged as a [Solver Bundle](../formats/solver-bundle.md).

## Responsibilities and Boundaries

- **Responsible for**: Declaring `GeneratorDescriptor` and independent registration; consuming the six frozen inputs directly to construct math models; combining technical contributions, planning objectives/constraints, and financial/tax rules; converting business units to solver internal units; and emitting canonical Bundles (chunked streams, commands, output manifests, and paired `ResultAdapter` refs).
- **Not responsible for**: Database queries, object-storage I/O, network requests, spawning solver processes, task scheduling, or publishing business results.
- **Unified ephemeral container boundary (`1.1.5` target)**: Built-in and third-party generators adhere uniformly to the [Algorithm Plugin Package](../formats/algorithm-plugin-package.md) (`kind: generator`) contract, consuming frozen inputs from `/work/in` and streaming standard `Solver Bundle` outputs to `/work/out` inside isolated network-disabled ephemeral containers launched via `job_runner`.

## Direct Inputs and Capability Gate

Each generation call directly accepts these six immutable inputs from trusted upstream layers (without outer wrapper objects or re-running user-boundary validation):

| Input | Origin and Contents |
|---|---|
| `ValidatedAssemblyArtifact` | Canonical assembly bytes and validation receipt (device instances, bus partitions, planning config, bindings, project baseline) |
| `models` | Device technical math contributions keyed by assembly instance ID |
| `materialized` | Full-horizon contiguous step-aligned `MaterializedSet` |
| `finance` | Immutable effective finance snapshot `EffectiveFinanceConfig` |
| `CalculationConfig` | Mode, predictions, generator/solver refs, precision, solver options, seed, output requests |
| `evaluation` | Frozen explicit evaluation confirmation (construction/operation years, discount rate, payment schedule, maintenance list, comparison baseline) |

### `GeneratorDescriptor` and Public Capability Declarations

`GeneratorDescriptor` declares supported assembly contract schemas, modes, precision levels, compatible solvers/executors, supported outputs, a Draft 2020-12 `options_schema` (strict empty-object schema when no options exist), and internal unit system.

At task submission, the application layer checks features actively used by the submitted inputs against the chosen generator's `capabilities` set; missing any required capability blocks submission immediately with `TASK-REQ-007` identifying the triggering field, while unused capabilities never block:

| Capability ID | Feature Scope |
|---|---|
| `planning.construction_activation` | Non-empty `planning.construction_units` construction units and active intervals |
| `technical.step_constraints` | Enabled cross-instance step constraints `step_relation` |
| `network.closed_ports` / `modeling.system_equations` / `outputs.system_series` | Unconnected closed-port semantics, system equation blocks, auxiliary series outputs |
| `finance.vat` / `finance.construction_input_vat` / `finance.income_tax` / `finance.credit_pools` | VAT, construction input VAT, income tax, and shared credit pool rules |
| `finance.monthly_demand` / `finance.monthly_daily_prices` / `finance.affine_maintenance` | Calendar-month demand charges, monthly daily price curves, scale/cost-linked maintenance terms |
| `finance.named_items` / `finance.amount_references` / `finance.cash_movements` / `finance.assets` / `finance.derived_tax` / `finance.amount_constraints` | Named cost items, amount definitions, cash movements, assets, derived taxes, and `amount_relation` |
| `finance.frozen_baseline` / `finance.cashflow_difference` | Non-`absolute` comparison baselines and after-tax cash-flow differences |
| `finance.financial_facts` | Same-solve financial facts and model provenance outputs (shared dependency for tax, demand, and relational finance capabilities) |

## Modeling Pipeline and Core Mechanisms

```text
Accept six frozen inputs
  ↓ Collect device, bus partition, planning, and finance/tax relations
  ↓ Resolve references, dimensions, and unified bound analysis
  ↓ Lower conditional relations exactly (complementarity, construction activation, monthly peaks, piecewise taxes)
  ↓ Instantiate sparse matrix, provenance map & result declarations along frozen timeline
  ↓ Stream MPS / parameter records (parameters.json & parameters.jsonl) & command manifest in chunks
Publish Solver Bundle atomically
```

### Unified Bounds

Pre-solve bound propagation proves finite bounds required for exact discrete/piecewise encodings (such as Big-M coefficients)—never replacing full LP presolve or user input range checks:

1. **Bound source intersection**: Intersects model `valid_range` with project planning bounds (taking maximum of lower bounds and minimum of upper bounds); undeclared sides remain unknown (`null`), never substituting template `properties.value` as an artificial planning upper bound.
2. **Relational propagation**: Derives conservative finite bounds across equalities, inequalities, and cross-step state transitions strictly from declared relations without guessing arbitrary large constants.
3. **Fine-grained isolation**: Rated capacity, instantaneous flows, state variables, and monetary expressions track bounds independently (if one factor in a product is already proven zero, complementarity holds automatically without introducing binary switches or Big-M bounds).
4. **Explicit failure attribution**: If a required finite bound cannot be proven, generation fails with `TASK-SOLVE-001` carrying `context.model_source` pinpointing the source relation and missing bound side rather than misreporting mathematical unboundedness or invalid input.

### Time Structure and Equivalent Optimization

- **Multi-axis isolation**: Operating steps (`step`), tax months (`tax_month`), and evaluation financial years (`financial_year`) retain exact indices and Gregorian calendar mappings; repeating inputs via `data_repeat` never authorizes unverified repetition of optimization decisions or tax periods.
- **Structural reuse and equivalence prerequisites**: Prefer expression template reuse and compact numeric storage to cut memory and modeling overhead; any decision compression or simplified formulation MUST be strictly equivalent mathematically to the full-horizon model (spanning state continuity, month boundaries, tax carry-forwards, and objective). For instance, when maximizing after-tax NPV and monotonicity/dependency certification holds, monthly demand peaks use a continuous upper envelope `peak >= flow[t]` instead of binary peak-step indicators, automatically falling back to exact maximum encoding whenever certification criteria do not hold.

### Structural Metadata Export for Solver Acceleration

Generators preserve pure in-memory deterministic compilation boundaries (never launching solver or search processes inside `generate`). When solver-embedded structural acceleration is active via `CalculationConfig.options` (enabled by default as `"on_demand_mutex"` in built-in `ies.generator.highs_milp@1.0.0`, opt-out via `"off"`), the generator exports verified structural metadata inside the Bundle so the solver executable can apply structural acceleration and verify original-model tolerances without breaking original column mappings or constraint fidelity (see [Computation Generation and Solving](engines.md#solver-embedded-structural-acceleration)).

### Cooperative Cancellation During Generation

Standard generation entry points retain pure input signatures; Providers supporting mid-generation cancellation implement `generate_with_stop(..., stop)`. When external task cancellation, timeout, or lease fencing occurs, the stop probe returns the stop reason and the generator aborts modeling and file emission promptly by raising `ComputationUnavailableError` with that cause, avoiding wasted work or half-built Bundles.

## Built-In Generator: `ies.generator.highs_milp@1.0.0`

| Protocol Aspect | Constraints and Semantics |
|---|---|
| Companion stack | Solver `ies.solver.highs@1.7.2`, executor `ies.executor.local_process@1.0.0`, adapter `ies.result-adapter.highs@1.0.0` |
| Computation support | `mode: planning`, `precision: medium`, `options.acceleration.mode` defaulting to `"on_demand_mutex"` (`"off"` to opt out), `outputs` supporting `solution` and `metrics`; `seed` `null` fixes `0` |
| Math expressiveness | Affine relations, exact Big-M complementarity `x * y = 0` under finite non-negative bounds, first-order `[t]`/`[t-1]` states, construction activation, and piecewise financial/tax rules (variable-times-variable bilinearity unsupported) |
| Units and coordinates | Physical quantities convert to internal SI; monetary values preserve the frozen finance currency; objective constants pass separately via `objective_offset` |
| Artifacts and timeouts | Streams `input/problem.mps` column-wise in chunks alongside `input/parameters.json` and `input/parameters.jsonl`; passes user `time_limit_seconds` verbatim to HiGHS `--time_limit` while adding fixed `+600` s outer process supervision grace for startup, model reading, and solution writing (bounded by deployment `max_timeout_seconds`) |
| Purity and determinism | Never accesses temp directories or environment variables; identical frozen inputs produce byte-identical Bundle contents |
