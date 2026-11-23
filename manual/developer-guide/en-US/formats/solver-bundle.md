# Solver Bundle

> Contract ID: `ies.solver-bundle`; Target `schema_version`: `"1.0.0"`; Producer: `GeneratorProvider`; Consumers: `SolverRuntime` and `ResultAdapter`

Solver Bundle is the immutable handoff package bridging computation generation and isolated process execution: the generator compiles validated assembly, configuration, materialized series, and financial rules into solver input files and a structured execution command, while the runtime safely executes the command and collects outputs—decoupling "mathematical modeling" completely from "secure process control".

## Directory Layout and Manifest

```text
solver-bundle/
├── bundle.yaml
├── input/
│   ├── problem.mps
│   ├── parameters.json
│   └── parameters.jsonl
└── output/
```

`bundle.yaml` declares bundle metadata, relative input manifest paths, controlled command specification, allowed output declarations, and the matching result adapter ref (`output/` starts empty during generation).

```yaml
schema: ies.solver-bundle
schema_version: "1.0.0"

bundle:
  generator: ies.generator.highs_milp@1.0.0
  solver: ies.solver.highs@1.7.2

inputs:
  - path: input/problem.mps
    media_type: application/mps
  - path: input/parameters.json
    media_type: application/json
  - path: input/parameters.jsonl
    media_type: application/x-ndjson

command:
  executor: ies.executor.local_process@1.0.0
  executable: ies.solver.highs@1.7.2
  arguments:
    - input/problem.mps
    - --solution_file
    - output/solution.json
  working_directory: .
  environment: {}
  timeout_seconds: 43200
  memory_mib: 4096
  cpu_cores: 4
  network: false
  success_exit_codes:
    - 0

outputs:
  - path: output/solution.json
    media_type: application/json
    required: true

result_adapter: ies.result-adapter.highs@1.0.0
extensions: {}
```

### Chunked Content and Lifecycle

All input and output files inside a Bundle adhere to a repeatable, read-only chunked content protocol (`Content.iter_chunks() -> Iterator[bytes]`) so large payloads never require full in-memory assembly during generation, checkpointing, runtime staging, or result adaptation:

| Stage | Ownership and Handoff Semantics |
|---|---|
| Generation | The generator emits deterministic read-only content; once the generation checkpoint and object references commit, the application layer replaces in-memory sources with persistent object references and releases modeling buffers |
| Runtime | Streams inputs in chunks from object storage into the isolated workdir; once the process exits, persists execution checkpoints through the storage receiver before cleaning temporary directories |
| Adaptation & Export | Reads solutions and receipts through the same read-only content protocol; final evidence publication reuses existing object references |

## Structured Commands and Runtime Responsibilities

- **Command security**: `command` prohibits shell command strings and shell composition (`sh -c`, pipes, redirection, wildcards, `..` traversal); `executable` MUST match the deployment allowlist, and `network` defaults to `false`.
- **Execution receipt (`ExecutionReceipt`)**: Regardless of success or failure, the runtime records bundle/attempt/executor/solver versions, start/end timestamps, exit code or signal, applied resource limits, and captured `stdout`/`stderr`/declared outputs; exit code `0` reflects protocol completion only—mathematical optimality and feasibility are determined by `ResultAdapter` interpreting declared outputs.
- **Solver-embedded acceleration and structural metadata handoff**: Built-in `ies.generator.highs_milp@1.0.0` enables `on_demand_mutex` structural acceleration by default (explicitly disable via [CalculationConfig](calculation-config.md) `options.acceleration.mode = "off"`); when active, the Bundle delivers generator-verified structural metadata alongside the original model in one isolated workdir, the controlled `executable` enforces the single `time_limit_seconds` budget and preserves the best verified incumbent before cutoff, and `ResultAdapter` adjudicates `solved` vs. `feasible` against lossless optimality and original-model tolerance checks (see [Computation Generation and Solving](../modules/engines.md#solver-embedded-structural-acceleration)). When explicitly disabled, execution reads the original model directly without shell chaining or cross-Bundle assembly mutations.

## Built-In Companion Parameter Records

Built-in `ies.generator.highs_milp@1.0.0` and `ies.result-adapter.highs@1.0.0` transfer solver column positions and financial/provenance declarations via a lightweight metadata header `input/parameters.json` (holding assembly identity, objective offset and unit, column count, requested outputs, seed, and relative record file path) paired with an ordered NDJSON record stream `input/parameters.jsonl`.

`input/parameters.jsonl` emits JSON array records strictly in the following order (all positions and expression IDs index from `0` by internal slot order; homogeneous record groups MUST NOT reorder):

| Record Tag and Elements | Meaning |
|---|---|
| `column_names, [name, ...]` / `row_names, [name, ...]` | Batched solver column/row name tables in actual matrix order |
| `epigraph, peak_position` / `epigraph_flows, [col, ...]` / `epigraph_end` | Emitted only when continuous demand upper envelopes are active; records monthly peak column and same-month flow columns for tightness verification |
| `expression, id, constant` / `terms, [[col, coeff], ...]` / `expression_end` | Frozen affine expressions (adapter evaluates terms in exact order, sharing one result across consumers) |
| `units, [[col, unit], ...]` | Business units and solver column positions for publishable variables |
| `component, identity, unit, caliber, expression_id` | Links financial components to shared expressions |
| `facts, object` | Structured financial facts skeleton (monetary nodes reference `{expression: id}`) |
| `series, object` / `positions, [col, ...]` / `series_end` | Device or system step-series metadata and column positions across contiguous `step` indices |
| `sources, object` / `source_group, ...` / `targets` / `indices` / `source_group_end` | Model source table and compressed row/column provenance mappings |

## Step Series Result Mapping

When creating variables, companion generators register device series metadata `{series_id, instance_id, variable_id, unit}` alongside solver column positions ordered across `step=0..n-1`. `series_id` is a unique opaque identifier within the Bundle; `ResultAdapter` restores `ComputeResult.outputs.series` in business units strictly from this metadata rather than guessing instances or steps from column names.

## System Series Mapping

When assembly declares `outputs.system_series`, the generator registers `{series_id, block, variable, unit}` and column positions via the same record mechanism (sharing the unique `series_id` namespace with device series); the adapter restores `ComputeResult.outputs.system_series`, distinguished cleanly via `kind: "system"` in catalogs and exports (see [Step Series Reading](../contracts.md#step-series-reading)).

## Same-Solve Financial Fact Mapping

When `solution` output is enabled and a usable primal solution exists, the adapter evaluates frozen financial expressions to publish `ComputeResult.outputs.financial_facts` (current contract versioning rules appear in [Single Current Contract](../file-formats.md#single-current-contract-and-project-package-dependencies)):

| Field | Complete Structure and Business Semantics |
|---|---|
| `currency` | Frozen finance currency; all monetary amounts are decimal fixed-point strings |
| `construction_total` / `annual_fixed_om` | Actual tax-inclusive construction total and annual fixed O&M |
| `construction_units` | `[{unit_id, instance_id, mode, built, scales: [{planning_variable_id, value, unit}]}]`; `built` is same-solve boolean construction state; `scales[].value` is JSON number in business units |
| `payments_applied` | `[{t, ratio, amount}]` (preserves multiple payments at identical `t`) |
| `net_cashflows` | `[{id, t, direction, amount}]`, sorted by `(t, id)` |
| `annual_operation` | `[{financial_year, energy_net, variable_om, fixed_om, maintenance, demand_charge, net}]`; `net` deducts annual expenses once |
| `monthly_demand` | `[{billing_id, charge_id, calendar_year, calendar_month, financial_year, peak_power, peak_unit, rate, charge}]`, sorted by `(billing_id, calendar_year, calendar_month)` |
| `tax_cash.vat` | `[{pool, month, paid, refund_claimed, refund_arrived, carry, receivable}]` (recorded by pool and month `1..12E`) |
| `tax_cash.vat_rules` | `[{rule, pool, month, output_tax, deductible_input}]` (recognized rule amounts without splitting pool cash paid) |
| `tax_cash.refunds` | `[{pool, period, arrival_period, amount}]` |
| `tax_cash.income` | `[{rule, year, taxable_base, loss_used, loss_generated, loss_expired, assessed, tax}]` (one row per income-tax rule per year `1..E`) |
| `tax_cash.depreciation` | `[{rule, base_from, year, amount}]`; `base_from` preserves the source discriminated union |
| `tax_cash.loss_balances` | `[{rule, vintage_id, year, last_usable_year, amount}]` |
| `annual_net_flows` | Full after-tax cash flow list across `t=0..evaluation_years` |

### Linked Maintenance Amount Protocol

Both fixed amounts and scale/upfront-capex-linked terms in `maintenance_expenses` evaluate through frozen affine expression nodes into decimal strings inside `annual_operation[].maintenance`; `net_cashflows[].amount` likewise reuses frozen expression nodes to guarantee exact parity with the main planning model.

## Relational Fact Mapping

Extended financial relations and [evaluation comparison](../contracts.md#evaluation-scope-and-frozen-baseline) seal the following same-solve structures inside `financial_facts` (ordered by identity and period; inactive sections emit `[]`):

| Field | Structure and Semantics |
|---|---|
| `cost_items` | `[{instance_id, finance_type, component, item_id, axis, period, raw, gross, deductible_input, net_basis, unit}]` (already included in component totals; never double-deducted) |
| `amounts` | `[{id, axis, unit, values: [{period, value}]}]` (`scalar` uses `period = 0`) |
| `cash_movements` | `[{id, axis, period, direction, price_basis, raw, gross, deductible_input, net_basis, output_tax, net_revenue, payments: [{at, ratio, amount}], end_unpaid}]` (in-horizon payments enter `annual_net_flows`; out-of-horizon amounts seal into `end_unpaid`) |
| `assets` | `[{id, basis, capitalized_at, available_from, book_values: [{year, value}]}]` (depreciation entries live under `tax_cash.depreciation`) |
| `tax_cash.derived` | `[{rule, axis, period, assessed, payment_period, paid}]` (cash outflow enters at `payment_period`) |
| `comparison` | `{mode}`; `reference` attaches `{baseline_source, comparability, facts}`; `difference` additionally includes `baseline_net_flows` and `evaluation_net_flows` without overwriting candidate `annual_net_flows` |

### Tax References and Sealed Fields

Financial `kind: tax` references and baseline `kind: tax` selectors read sealed same-solve `financial_facts` strictly according to this mapping:

| `source` Kind | `measure` | Sealed Path and Field | Axis & Period |
|---|---|---|---|
| VAT Rule | `output_tax` / `deductible_input` | `tax_cash.vat_rules[rule=id, month=p].output_tax` / `.deductible_input` | `tax_month` (recognition month `p`) |
| VAT Credit Pool | `paid` / `refund_confirmed` / `refund_received` / `credit_balance` / `receivable` | `tax_cash.vat[pool=id, month=p].paid` / `.refund_claimed` / `.refund_arrived` / `.carry` / `.receivable` | `tax_month` (month `p`) |
| Income Tax Rule | `taxable_base` / `loss_used` / `assessed` / `paid` | `tax_cash.income[rule=id, year=y].taxable_base` / `.loss_used` / `.assessed` / `.tax` | `financial_year` (eval year `y`) |
| Derived Tax Rule | `assessed` | `tax_cash.derived[rule=id, period=p].assessed` | Rule's own axis (accrual `p`) |
| Derived Tax Rule | `paid` | `tax_cash.derived[rule=id, payment_period=p].paid` | Rule's own axis (payment `p`) |

## Model Source Mapping

Built-in generators and adapters pass modeling provenance via `schema: ies.model-sources` (`schema_version: "1.0.0"`):

| Field | Meaning |
|---|---|
| `sources` | Opaque source ID → `{kind, location, domain: {kind, count, index?}, inputs, outputs, depends_on}` |
| `groups` | `[{source, role, axis, targets, indices}]` (`role` distinguishes `row:` / `column:`; arithmetic index runs compress to `{start, stride, count}`) |
| `outputs` | `[{path, sources}]`, linking result paths to upstream source IDs |

Published as `ComputeResult.outputs.model_sources` when `solution` is requested and available; when generation hits expected bounds/conversion failures, `TASK-SOLVE-001` carries provenance context under `context.model_source`.
