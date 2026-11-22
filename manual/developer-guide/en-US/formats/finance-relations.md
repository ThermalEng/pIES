# Named Amounts, Payments, and Asset Relations

> Owner: `finance`; Governing Spec: [Architecture Constitution](../ARCHITECTURE_CONSTITUTION.md)

This page specifies the field contracts and override discipline for **named cost items**, `amounts`, `cash_movements`, `assets`, and **derived taxes** inside the Finance Triplet `1.0.0`. Base cost components, energy prices, VAT, income tax, and direct sources appear in [Finance YAML Contract](finance-yaml.md), and evaluation baseline comparisons in [Public Contracts](../contracts.md#evaluation-scope-and-frozen-baseline).

## Three-Way Separation of Amounts, Occurrences, and Uses

1. **Amount definitions (`amounts`)**: Declare numeric formula relations without automatically creating cash flows, assets, or tax deductions.
2. **Economic occurrences (`cash_movements`, etc.)**: Carry distinct occurrence identities; even when reusing the same numeric recipe, separate occurrences represent separate economic transactions.
3. **Uses**: Payment schedules, input-VAT confirmations, capitalization, and depreciation reference occurrence identities individually; the platform detects and blocks duplicate payments or simultaneous full expensing and depreciation of the same occurrence.

## Named Cost Items

An optional `items` map (omitting equals `{}`) may be added under `finance_types.<type>.<component>`, summing **additively** with existing `fixed`/`linear` base leaves within that component:

| Field | Rules |
|---|---|
| `items.<item_id>` | Stable local expense ID (independent of `driver` and display label), containing at least one of `fixed` or `linear` |
| `items.<item_id>.fixed` | Optional `Money` (`currency` for `upfront_capex`, `currency/year` for `annual_fixed_om`; unsupported for `period_variable_om`) |
| `items.<item_id>.linear` | Optional `driver -> {unit_cost: Money}`, reusing assembly `driver` bindings and incremental/sunk validation |

```yaml
annual_fixed_om:
  items:
    materials:
      linear:
        capacity_kw: {unit_cost: {value: "2", unit: CNY/kW/a}}
    service:
      fixed: {value: "10", unit: CNY/a}
      linear:
        capacity_kw: {unit_cost: {value: "5", unit: CNY/kW/a}}
```

- **Tax base and occurrence condition paths**: Extend to `<type>.<component>.items.<item_id>` plus optional `.fixed` / `.linear.<driver>` (dropping the `<type>.` prefix inside instance `cost_occurrence`). A whole-component path covers all its active leaves including `items`; whole paths and sub-paths MUST NOT overlap.

## Generic Amount Definitions (`amounts`)

Both Profile and Effective support an optional `amounts` map (omitting normalizes to `{}`), expressing scalar or periodic amount relations of the form `constant + sum(coefficient * source)`:

| Field | Rules |
|---|---|
| `unit` | Required output unit (project currency amount or rate, e.g., `CNY`, `CNY/a`) |
| `axis` | Required: `scalar`, `financial_year`, or `tax_month` |
| `constant` | Required `Money` whose unit matches `unit` (use `"0"` if no constant term) |
| `terms` | Required map `term_id -> {coefficient: Money, source: Reference}` (`{}` when empty); `coefficient` MUST be a known finite decimal, never a decision variable |

`source` accepts 12 explicit reference variants (direct or indirect same-period self-reference is prohibited):

| `kind` | Required Fields and Semantics |
|---|---|
| `driver` | `finance_type, driver, scope, period`; bound instance property or aggregated operating driver |
| `system_quantity` | `block, variable, aggregation, period`; references a `system_equations` auxiliary variable (`aggregation` is `integrate_positive` or `sum_positive`) |
| `construction_unit` | `unit_ref, period: {kind: scalar}`; references the `0/1` activation state of an assembly construction unit (`unit: "1"`) |
| `cost` | `path, scope, basis, period`; references a device cost component or named leaf path |
| `tariff` / `demand` | `price`/`charge, bindings, basis, period`; references a non-empty `tariff_bindings` / `demand_bindings` set (reads positive `raw` amount; signs come explicitly from coefficients) |
| `maintenance` | `year, position, basis`; references an occurrence in the evaluation maintenance list |
| `amount` | `id, period`; references another declared entry in `amounts` |
| `movement` | `id, basis, period`; references an independent cash movement by recognition period |
| `asset` | `id, measure, period`; `measure` is `basis` (`scalar`), `depreciation`, or `book_value` (`financial_year`) |
| `tax` | `source, measure, period`; see "Tax Output References" below |
| `baseline` | `id, period`; references a frozen `evaluation.comparison.facts` alias |

- **`scope`**: `{kind: instances, instances: [...]}` or `{kind: all_new_instances}` (covers new instances only; evaluates to zero when no project instance matches).
- **`basis`**: `raw` (original source basis), `gross` (tax-inclusive cash movement), `deductible_input` (creditable input VAT), `net_basis` (outflow `gross - deductible_input`), or `net_revenue` (inflow `gross - output_tax`); inapplicable directions are rejected at input boundaries.
- **`period`**: Discriminated union supporting `{kind: scalar}`, `{kind: current}` (same period on matching axis), `{kind: financial_year, year}`, `{kind: tax_month, month}`, `{kind: year_months, year}` (sum over the 12 tax months of that year), and `{kind: range, axis, first, last}` (closed-interval sum).

### Tax Output References

| `source` | `measure` | Unit and Semantics |
|---|---|---|
| `{kind: rule, id}` (VAT) | `output_tax` / `deductible_input` | Output VAT or deductible input VAT recognized by that rule in the target tax month |
| `{kind: pool, id}` | `paid` / `refund_confirmed` / `refund_received` / `credit_balance` / `receivable` | Pool cash paid, confirmed refund, arrived refund, closing carry-forward, or closing receivable |
| `{kind: rule, id}` (Income Tax) | `taxable_base` / `loss_used` / `assessed` / `paid` | Pre-loss taxable base, loss carried forward used, assessed tax, or cash income tax paid |
| `{kind: rule, id}` (Derived Tax) | `assessed` / `paid` | Accrued tax in recognition period or cash paid in lagged payment period |

Corresponding sealed output fields for each `source`/`measure` appear in [Tax References and Sealed Fields](solver-bundle.md#tax-references-and-sealed-fields); entering planning constraints appears in [Amount Reference Constraints](assembly-yaml.md#amount-reference-constraints).

## Named Cash Movements (`cash_movements`)

`cash_movements.<movement_id>` defines an independent economic occurrence identified by `(movement_id, recognition_axis, period)`:

| Field | Rules |
|---|---|
| `amount` | Required `{kind: amount, id, period}` referencing a scalar or same-axis periodic `amount` whose output unit is project currency |
| `price_basis` | `tax_inclusive` or `tax_exclusive` |
| `direction` | `inflow` or `outflow` |
| `recognition` | `{kind: at, at: TimePoint}` or `{kind: each, axis: financial_year|tax_month, first, last}` (`financial_year` in `at` allows `0`) |
| `payments` | Required ordered list `[{offset, ratio}]`; `offset` is integer offset from recognition period (negative allowed for prepayments); `ratio` is non-negative decimal summing strictly to `"1"` |

- Recognition periods MUST lie inside the evaluation horizon, and shifted payment periods MUST satisfy evaluation year `>= 0` or tax month `>= 1`; scheduled payments falling past horizon end seal into `end_unpaid` without entering in-horizon cash flows.
- `net_cashflows.<id>.amount` supports `{kind: amount_ref, id}` referencing a non-negative scalar currency amount.

## Capitalization and Depreciation (`assets`)

`assets.<asset_id>` groups net bases of selected outlays into standalone depreciable assets referenced by `{kind: asset, id}` in income tax `depreciation[].base_from`:

| Field | Rules |
|---|---|
| `basis_from` | References `net_basis` of `cost` (restricted to `upfront_capex` and its sub-items), `maintenance`, or `movement`; or a pure net-basis linear combination `amount` (`constant` `"0"`, non-negative known coefficients) |
| `capitalized_at` | Required `TimePoint` (annual capitalization MUST precede `K + available_from`; monthly capitalization MUST be no later than `12*(K + available_from - 1)`) |
| `available_from` | Required positive integer operating year (earliest year eligible for depreciation) |

Total shares of any underlying occurrence claimed directly for depreciation plus allocated across `assets` MUST NOT exceed `1`; capitalized expenditures MUST NOT simultaneously appear in `movement_cost` as current-period deductible expenses. Example (`CNY`, `K=1`, `E>=2`):

```yaml
amounts:
  purchase_quote:
    unit: CNY
    axis: scalar
    constant: {value: "100", unit: CNY}
    terms: {}
cash_movements:
  purchase_invoice:
    amount: {kind: amount, id: purchase_quote, period: {kind: scalar}}
    price_basis: tax_inclusive
    direction: outflow
    recognition: {kind: at, at: {kind: financial_year, year: 0}}
    payments:
      - {offset: 0, ratio: "0.4"}
      - {offset: 2, ratio: "0.6"}
assets:
  asset_a:
    basis_from:
      kind: movement
      id: purchase_invoice
      basis: net_basis
      period: {kind: financial_year, year: 0}
    capitalized_at: {kind: financial_year, year: 1}
    available_from: 1
```

## Derived Taxes and Movement Tax Rules

### Derived Taxes

- **Derived taxes (`kind: derived_tax` under `tax_rules.<id>`)**: Declares `{label_ref, axis, base_from, rate, payment_lag}`; `axis` is `tax_month` or `financial_year`, `base_from` references `kind: tax` (period `current` or cross-axis `year_months`), accrued tax equals `base_from * rate`, and cash payment occurs in `period + payment_lag` (payments beyond the horizon stay outside in-period cash).
- **Income tax extension lists**: `income_tax` rules add optional `movement_revenue`, `movement_cost` (included in taxable base by recognition period), and `derived_tax_cost` (deducted by accrual period).
- **Movement VAT entries**: `vat` rules add optional `movement_input_tax: [{movement, rate, deductible_ratio, confirmation}]` and `movement_output_tax: [{movement, rate, confirmation}]`; `confirmation` is `{method: recognition_period}` or `{method: explicit_schedule, occurrences: [{at, schedule: [{period, ratio}]}]}` (each occurrence MAY be confirmed by at most one VAT rule):

```yaml
movement_input_tax:
  - movement: service
    rate: {value: "0.1", unit: "1"}
    deductible_ratio: {value: "1", unit: "1"}
    confirmation:
      method: explicit_schedule
      occurrences:
        - at: {kind: financial_year, year: 1}
          schedule: [{period: 13, ratio: "1"}]
        - at: {kind: financial_year, year: 2}
          schedule: [{period: 25, ratio: "1"}]
```

## Merging, Freezing, and Capability Boundaries

| Extension Section | Overrides Rules | Required Generator Capability |
|---|---|---|
| Named cost `items` | Replaces `fixed` / `linear` unit prices at leaf paths only; adding/deleting `item` keys prohibited | `finance.named_items` |
| `amounts` | Replaces existing `constant`/`coefficient`, instance/binding scopes, and fixed periods; changing unit, axis, or source kind prohibited | `finance.amount_references` |
| `cash_movements` | Replaces full `recognition` and `payments`; changing direction, price basis, or amount source prohibited | `finance.cash_movements` |
| `assets` | Replaces instance scopes, `capitalized_at`, and `available_from` | `finance.assets` |
| `derived_tax` & VAT/CIT movement lists | Replaces derived tax `rate`/`payment_lag` and rates/full `occurrences` on existing `movement` VAT entries | `finance.derived_tax` |

All active relations validate against required generator capabilities prior to solving (all depend on `finance.financial_facts`); corresponding same-solve output fields appear in [Relational Fact Mapping](solver-bundle.md#relational-fact-mapping).

## Minimal Hand-Calculated Reference Cases

| Test Input | Expected Accounting Fact |
|---|---|
| `q=3 kW`, `materials` 2 CNY/kW/a (10% VAT fully deductible), `service` 10 CNY/a + 5 CNY/kW/a (no input VAT) | Annual costs equal `6` and `25` CNY/a respectively (`31` total), splitting input VAT solely on `materials` |
| `100 CNY` tax-inclusive (no VAT) occurrence paid `40%/60%`, 5-year straight-line depreciation with `0` residual | Two cash outflows of `40` and `60`; constant annual depreciation of `20` |
| Base tax paid `10`, derived tax rate `0.04`, `payment_lag: 1` | Accrual period records `0.4`; next period cash flow records payment `0.4` |
| Terminal net cash recovery referencing `9 CNY/kW × 3 kW` | Recovery amount `27` scales with the solve; residual book value triggers no second inflow |
