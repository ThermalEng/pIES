# Solver Bundle

> Contract ID: `ies.solver-bundle`; `schema_version`: `"1.0.0"`; Producer: `GeneratorProvider`; Consumers: `SolverRuntime` and `ResultAdapter`

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

`bundle.yaml` declares bundle metadata, the produced public bundle contract ID, relative input manifest paths, and allowed output declarations (`output/` starts empty during generation; any `solver` container declaring consumption of that contract can execute it without hardcoding solver or adapter IDs):

```yaml
schema: ies.solver-bundle
schema_version: "1.0.0"

bundle:
  generator: ies.generator.highs_milp@1.0.0
  contract: ies.solver-bundle.mps@1.0.0

inputs:
  - path: input/problem.mps
    media_type: application/mps
  - path: input/parameters.json
    media_type: application/json
  - path: input/parameters.jsonl
    media_type: application/x-ndjson

outputs:
  - path: output/primal_solution.json
    media_type: application/json
    required: true

extensions: {}
```

### Chunked Content and Lifecycle

All input and output files inside a Bundle adhere to a repeatable, read-only chunked content protocol (`Content.iter_chunks() -> Iterator[bytes]`) so large payloads never require full in-memory assembly during generation, checkpointing, runtime staging, or result adaptation:

| Stage | Ownership and Handoff Semantics |
|---|---|
| Generation | The generator emits deterministic read-only content; once the generation checkpoint and object references commit, the application layer replaces in-memory sources with persistent object references and releases modeling buffers |
| Runtime | Streams inputs in chunks from object storage into the isolated workdir; once the process exits, persists execution checkpoints through the storage receiver before cleaning temporary directories |
| Adaptation & Export | Reads solutions and receipts through the same read-only content protocol; final evidence publication reuses existing object references |

## Container Isolation and Responsibility Boundaries

- **Network-disabled containers & shell-free execution**: `solver` containers launch via `job_runner` as non-root with `--network=none --read-only --cap-drop=ALL`, prohibiting shell string composition, `..` traversal, and arbitrary command injection.
- **Execution receipt (`ExecutionReceipt`) & 0-based column order**: Regardless of success or failure, the runtime records attempt/solver plugin versions, start/end timestamps, exit code or signal, and cgroup resource metrics; exit code `0` indicates clean container exit only, and `primal_solution.json` column values MUST strictly follow the **0-based original column order** of `input/problem.mps`.
- **Distinguishing built-in HiGHS acceleration checks from universal restoration**: Built-in `ies.generator.highs_milp@1.0.0` enables `on_demand_mutex` structural acceleration by default (disable via [CalculationConfig](calculation-config.md) `options.generator.acceleration.mode = "off"`); **original-model constraint tolerance verification under relaxed mutexes belongs strictly to official built-in `ies.solver.highs` (`ies-highs-solver`) internal quality control** (see [Computation Generation and Solving](../modules/engines.md#solver-embedded-structural-acceleration)). The universal `ResultAdapter` inside `backend`'s bounded subprocess solely restores the canonical business-unit engineering solution from `variable_map` (`input/parameters.json` + `input/parameters.jsonl`) faithfully and **never re-solves or certifies third-party `solver` plugins**.

## Universal Variable Mapping and Parameter Records

`generator` plugins and `backend`'s bounded-subprocess restoration layer exchange 0-based solver column positions, explicit linear unit conversions, and variable/provenance declarations via a metadata header `input/parameters.json` (declaring `schema: ies.variable-map`, `schema_version: "1.0.0"`, assembly identity, objective `objective_scale` / `objective_offset` and unit, column count, requested outputs, precision, seed, and relative record file path; uniformly applying `business_value = scale * raw_value + offset` with finite non-zero `scale`) paired with an ordered NDJSON record stream `input/parameters.jsonl` (the `1.1.5` built-in chain retains its built-in mapping format until atomic cutover).

`input/parameters.jsonl` emits JSON array records strictly in the following order (all positions and expression IDs index from `0` by internal slot order; homogeneous record groups MUST NOT reorder):

| Record Tag and Elements | Meaning |
|---|---|
| `column_names, [name, ...]` / `row_names, [name, ...]` | Batched solver column/row name tables in actual matrix order |
| `epigraph, peak_position` / `epigraph_flows, [col, ...]` / `epigraph_end` | Emitted only when continuous demand upper envelopes are active; records monthly peak column and same-month flow columns for tightness verification |
| `expression, id, constant` / `terms, [[col, coeff], ...]` / `expression_end` | Frozen affine expressions (adapter evaluates terms in exact order, sharing one result across consumers) |
| `units, [[col, unit, scale, offset], ...]` | Solver column positions, business units, and explicit linear conversion coefficients (`business_value = scale * primal + offset`) for publishable variables |
| `component, identity, unit, caliber, expression_id` | Links financial components to shared expressions |
| `facts, object` | Structured financial facts skeleton (monetary nodes reference `{expression: id}`) |
| `series, object` / `positions, [col, ...]` / `series_end` | Exact device or system step-series metadata (including linear `scale` and `offset`) and column positions across contiguous `step` indices |
| `sources, object` / `source_group, ...` / `targets` / `indices` / `source_group_end` | Model source table and compressed row/column provenance mappings |

## Step Series Result Mapping

When creating variables, companion generators register exact device series metadata `{series_id, instance_id, variable_id, unit, scale, offset}` (only allowing optional `extensions` and rejecting unknown core fields) alongside solver column positions ordered across `step=0..n-1`. `series_id` is a unique opaque identifier within the Bundle; `ResultAdapter` restores `ComputeResult.outputs.series` in business units strictly from this metadata rather than guessing instances or steps from column names.

## System Series Mapping

When assembly declares `outputs.system_series`, the generator registers `{series_id, block, variable, unit, scale, offset}` (only allowing optional `extensions`) and column positions via the same record mechanism (sharing the unique `series_id` namespace with device series); the adapter restores `ComputeResult.outputs.system_series`, distinguished cleanly via `kind: "system"` in catalogs and exports (see [Step Series Reading](../contracts.md#step-series-reading)).

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
