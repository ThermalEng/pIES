# Finance YAML Contract

> Contract IDs: `ies.finance-profile`, `ies.finance-overrides`, `ies.effective-finance-config`; Target `schema_version`: `"1.0.0"`; Recommended Project Package Filenames: `finance_profile.yaml`, `finance_overrides.yaml`, `effective_finance.yaml`

This page is the sole field-level normative reference for the Finance Triplet (regional baseline `FinanceProfile`, sparse project overrides `FinanceOverrides`, and deterministic merged snapshot `EffectiveFinanceConfig`). Overall financial boundaries and planning/evaluation rules appear in [Finance Calculation](../modules/finance.md), and versioning rules in [Single Current Contract](../file-formats.md#single-current-contract-and-project-package-dependencies).

```text
FinanceProfile (regional baseline, hand-authorable)
FinanceOverrides (project overrides, hand-authorable)
        │
        ▼ Deterministic Merge + Domain Reference Validation
EffectiveFinanceConfig (immutable snapshot, machine-generated only)
        │ Exact Reference
        ▼
assembly / computation / Project Package
```

- **Money atoms**: Every cost and rate is written `{value, unit}`, where `value` is a decimal fixed-point string (`float`, `NaN`/`Infinity`, and `null` are prohibited) and `unit` comes from the common unit vocabulary. Cost components MUST be non-negative (`"0"` allowed); energy prices allow positive, zero, and negative finite decimal values.
- **Independent provenance rule**: Finance configurations can be exported and migrated with project packages; imports MUST carry source `FinanceProfile` and `FinanceOverrides` and re-merge/verify from those sources.

## Generic Amount Reference Target Semantics

Beyond base cost components, energy prices, and base tax rules, extended financial relations (named items, `amounts`, `cash_movements`, `assets`, and `derived_tax`) have their field definitions in [Named Amounts, Payments, and Assets](finance-relations.md) and obey these principles:

- **Separation of identity and occurrence**: Expense item identities remain separate from referenced scale/operating `driver` symbols, allowing multiple expense items to share one `driver` with independent rates, tax-inclusiveness bases, and deduction rules; pure numeric amount formulas (`amounts`) remain distinct from actual payment occurrences (`cash_movements`) and capitalized assets (`assets`), never double-deducting cash or tax bases merely due to formula references.
- **Explicit bases and periods**: Amount formulas combine unit-bearing constants and known coefficients times explicit sources over finite sums, retaining tax-inclusive, tax-exclusive, deductible-input, and net-basis flags while strictly separating scalar, financial-year, and tax-month axes.
- **Derived taxes and construction conditions**: Derived taxes compute from designated tax outputs multiplied by known rates rather than guessing tax bases from label names; costs gated on optional construction switches MUST link explicitly via [Cost Occurrence Conditions](assembly-yaml.md#cost-occurrence-conditions).

| Illustrative Semantic Case | Handling Rule |
|---|---|
| Same capacity `q` drives material cost `2 CNY/kW/a × q` and service cost `5 CNY/kW/a × q` | Retain separate item identities and independent input-tax rules without merging into an unattributed leaf |
| Working capital `9 CNY/kW × P` paid in year 1 and recovered in year 4 | Two separate occurrences reference the same amount recipe; recovery is declared explicitly rather than inferred from residual book value |
| Surtax equals `0.04 × paid tax of rule X` | Bind to the `paid` output rather than output tax or VAT credit carry-forward; payment lag shifts only the cash payment period |
| Asset pool combines net upfront sub-items and capitalized expenditures | Preserve each source occurrence, allocation share, and earliest depreciable operating year |

## `ies.finance-profile@1.0.0`

### Complete Example

```yaml
schema: ies.finance-profile
schema_version: "1.0.0"
profile:
  id: cn-north-demo
  region: CN-North
  currency: CNY
  base_year: 2025
  price_basis: tax_inclusive   # Price basis; whether taxes apply and tax rates are declared explicitly in tax_rules
  cost_method: fixed_plus_linear
finance_types:
  pv_system:
    upfront_capex:
      fixed: {value: "0", unit: CNY}
      linear:
        capacity_kw: {unit_cost: {value: "3500", unit: CNY/kW}}
    annual_fixed_om:
      fixed: {value: "1200", unit: CNY/a}
      linear:
        capacity_kw: {unit_cost: {value: "35", unit: CNY/kW/a}}
    period_variable_om:
      linear:
        generated_kwh: {unit_cost: {value: "0.01", unit: CNY/kWh}}
  battery_system:
    upfront_capex:
      linear:
        energy_kwh: {unit_cost: {value: "900", unit: CNY/kWh}}
    annual_fixed_om:
      linear:
        energy_kwh: {unit_cost: {value: "18", unit: CNY/kWh/a}}
    period_variable_om:
      linear:
        cycled_kwh: {unit_cost: {value: "0.002", unit: CNY/kWh}}
energy_prices:
  grid_import:                  # price_id: stable custom name, lower_snake_case, no __
    carrier: electricity        # carrier from common carrier vocabulary (shared with device interface carriers)
    direction: purchase         # Explicit direction: purchase | sale; never inferred from price_id/key name
    kind: constant
    value: {value: "0.7", unit: CNY/kWh}
  pv_export:
    carrier: electricity
    direction: sale
    kind: time_series
    ref: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    series_meta:
      resolution: 1h
      point_count: 8760  # Single-year example (operating years confirmed at submission, 1 here; not a default); actual count must match total operating steps
      unit: CNY/kWh
  heat_supply:                  # Second carrier example: heat purchase entry (heat is used by device interface examples)
    carrier: heat
    direction: purchase
    kind: constant
    value: {value: "0.35", unit: CNY/kWh}
demand_charges: {}
taxes:
  grid_import_vat:
    display_name: Grid Import VAT
    tax_type: value_added_tax
    applies_to: grid_import     # References a price_id (energy_prices key) or finance_types component path
credit_pools: {}
tax_rules: {}                  # Display labels only in this example; no tax calculation rules enabled
net_cashflows: {}              # No extra net cash items; never auto-generated from residual depreciation value
```

### Field Table

| Path | Required | Rules |
|---|---|---|
| `schema` / `schema_version` | Yes | Fixed `ies.finance-profile` / `"1.0.0"` |
| `profile.id` | Yes | Stable lowercase identifier (e.g., `cn-north-demo`) referenced by Overrides and Effective |
| `profile.region` | Yes | Regional display label; does not participate in numeric calculations |
| `profile.currency` | Yes | `CNY` / `USD` |
| `profile.base_year` | Yes | Integer financial base year (`1900–2999`), independent of operating `start_year` |
| `profile.price_basis` | Yes | `tax_inclusive` / `tax_exclusive`: specifies whether monetary values in the document include tax |
| `profile.cost_method` | Yes | Fixed to `fixed_plus_linear` |
| `finance_types` | Yes | Map `finance_type` → cost component blocks (at least one of `upfront_capex`, `annual_fixed_om`, `period_variable_om`) |
| `finance_types.*.upfront_capex` | Optional | One-time construction outlay (`new` only): `fixed` in `currency`, `linear.<driver>.unit_cost` in `currency / driver_unit` |
| `finance_types.*.annual_fixed_om` | Optional | Annual fixed O&M: `fixed` in `currency/year` (e.g., `CNY/a`), `linear.<driver>.unit_cost` in `currency / driver_unit / year` |
| `finance_types.*.period_variable_om` | Optional | Operating variable O&M: `linear.<driver>.unit_cost` in `currency / quantity_unit` (e.g., `CNY/kWh`) |
| `energy_prices` | Yes | Map `price_id` (`lower_snake_case`, no `__`) → `{carrier, direction, kind, ...}` |
| `energy_prices.*.carrier` / `direction` | Yes | `carrier` from common device carrier vocabulary; `direction` is `purchase` or `sale` |
| `energy_prices.*` pricing union | Yes | One of `{kind: constant, value: Money}`, `{kind: time_series, ref, series_meta}`, or `{kind: monthly_daily, months}` |
| `energy_prices.*.series_meta` | Required for series | `{resolution, point_count, unit}`; full-horizon series MUST match confirmed operating point count at submission |
| `demand_charges` | Yes | Demand charge map (`{}` when none); see "Calendar-Month Demand Charges" |
| `taxes` | Optional | Display tax label definitions `{display_name, tax_type, applies_to}`; does not directly enable computation |
| `credit_pools` / `tax_rules` | Yes | Credit pool and tax rule mappings (explicit `{}` when none); see "Tax Calculation Inputs" |
| `net_cashflows` | Yes | Extra after-tax net cash items (`{}` when none) |
| `finance_types.*.<comp>.items` / `amounts` / `cash_movements` / `assets` | Optional | Named financial relation sections; fields in [Named Amounts, Payments, and Assets](finance-relations.md) |

### Monthly Daily Prices

When `energy_prices.<id>` uses `kind: monthly_daily`, `months` MUST contain all 12 string keys `"1"` through `"12"`, each `{ref, series_meta}` referencing a validated full-day `ies.price-series@1.0.0` curve (`24` points for `1h`, `48` for `30min`, `96` for `15min`). Multiple months MAY reuse the same object ID; during execution, each operating calendar year-month selects its daily curve (Feb 29 in leap years reuses month `"2"`).

```yaml
carrier: electricity
direction: purchase
kind: monthly_daily
months:
  "1": {ref: "day-price-a", series_meta: {resolution: 1h, point_count: 24, unit: CNY/kWh}}
  "2": {ref: "day-price-a", series_meta: {resolution: 1h, point_count: 24, unit: CNY/kWh}}
  "3": {ref: "day-price-a", series_meta: {resolution: 1h, point_count: 24, unit: CNY/kWh}}
  "4": {ref: "day-price-a", series_meta: {resolution: 1h, point_count: 24, unit: CNY/kWh}}
  "5": {ref: "day-price-a", series_meta: {resolution: 1h, point_count: 24, unit: CNY/kWh}}
  "6": {ref: "day-price-b", series_meta: {resolution: 1h, point_count: 24, unit: CNY/kWh}}
  "7": {ref: "day-price-b", series_meta: {resolution: 1h, point_count: 24, unit: CNY/kWh}}
  "8": {ref: "day-price-b", series_meta: {resolution: 1h, point_count: 24, unit: CNY/kWh}}
  "9": {ref: "day-price-b", series_meta: {resolution: 1h, point_count: 24, unit: CNY/kWh}}
  "10": {ref: "day-price-a", series_meta: {resolution: 1h, point_count: 24, unit: CNY/kWh}}
  "11": {ref: "day-price-a", series_meta: {resolution: 1h, point_count: 24, unit: CNY/kWh}}
  "12": {ref: "day-price-a", series_meta: {resolution: 1h, point_count: 24, unit: CNY/kWh}}
```

### Tax Calculation Inputs

`credit_pools` and `tax_rules` declare tax mechanisms participating in planning solves and same-solve evaluation (rule IDs and pool IDs are `lower_snake_case`):

| Path / Kind | Structure and Rules |
|---|---|
| `credit_pools.<id>` | `{opening_credit: Money, receivable_policy: end_receivable, refunds: [{period, amount, arrival_period}]}`; `period` and `arrival_period` are positive integer tax months (`arrival_period >= period`); `end_receivable` keeps post-horizon refund receivables out of in-horizon cash |
| `kind: vat` | `{label_ref, settlement: monthly, in_tax, out_tax, om_input_tax, maintenance_input_tax, demand_input_tax?, credit_pool, confirmation: same_period}` |
| `in_tax[]` / `out_tax[]` | `{price, rate, deductible_ratio}` (input) and `{price, rate}` (output), referencing declared `energy_prices` |
| `om_input_tax[]` | `{for, rate, deductible_ratio}`, where `for` targets `<finance_type>.annual_fixed_om` / `period_variable_om` or `.fixed` / `.linear.<driver>` leaves |
| `maintenance_input_tax[]` | `{year, position, rate, deductible_ratio}`, referencing evaluation year and within-year ordinal of the frozen maintenance list |
| `kind: construction_input_vat` | `{label_ref, for, rate, deductible_ratio, credit_pool, confirmation}`; `for` targets `upfront_capex` or sub-items; `confirmation` is `{method: payment_period}` or `{method: explicit_schedule, schedule: [{period, ratio}]}` (`ratio` sums to `"1"`) |
| `kind: income_tax` | `{label_ref, rate, revenue, energy_cost, annual_om, variable_om, deductible_maintenance, depreciation, loss_carryforward_years, opening_losses, demand_cost?}` |
| `deductible_maintenance` | `{mode: explicit_items, items: [{year, position, deductible}]}` |
| `depreciation[]` | `{base_from, method: straight_line, life_years, residual: Money, first_year}`; `first_year` is operating year ordinal; `residual` is a fixed currency amount or a `0..1` ratio with `unit: "1"` |
| `opening_losses[]` | `{amount: Money, last_usable_year}` (positive integer evaluation year; preserves list order within identical expiry years); `loss_carryforward_years` is a non-negative integer |
| `kind: derived_tax` | Derived tax rules; fields in [Derived Taxes](finance-relations.md#derived-taxes) |

**Time axis and double-claim checks**: Tax month `period` spans `1..12E` across the entire evaluation horizon (evaluation year `y` closes at month `12y`; construction payment `t` maps to month `12t+1`). Duplicate claims of input VAT or depreciation over the same underlying leaf or instance basis are rejected; whole-component paths and sub-item paths are checked for overlap against their covered leaves.

```yaml
taxes:
  vat_label: {display_name: Synthetic VAT, tax_type: value_added_tax, applies_to: grid_import}
  income_label: {display_name: Synthetic Income Tax, tax_type: income_tax, applies_to: pv_export}
credit_pools:
  main_pool:
    opening_credit: {value: "0", unit: CNY}
    receivable_policy: end_receivable
    refunds: []
tax_rules:
  operating_vat:
    kind: vat
    label_ref: vat_label
    settlement: monthly
    in_tax:
      - {price: grid_import, rate: {value: "0.13", unit: "1"}, deductible_ratio: {value: "1", unit: "1"}}
    out_tax:
      - {price: pv_export, rate: {value: "0.13", unit: "1"}}
    om_input_tax:
      - {for: pv_system.annual_fixed_om, rate: {value: "0.06", unit: "1"}, deductible_ratio: {value: "1", unit: "1"}}
      - {for: pv_system.period_variable_om, rate: {value: "0.06", unit: "1"}, deductible_ratio: {value: "0.5", unit: "1"}}
    maintenance_input_tax: []
    credit_pool: main_pool
    confirmation: same_period
  building_vat:
    kind: construction_input_vat
    label_ref: vat_label
    for: pv_system.upfront_capex
    rate: {value: "0.09", unit: "1"}
    deductible_ratio: {value: "0.8", unit: "1"}
    credit_pool: main_pool
    confirmation: {method: payment_period}
  income:
    kind: income_tax
    label_ref: income_label
    rate: {value: "0.25", unit: "1"}
    revenue: [pv_export]
    energy_cost: [grid_import]
    annual_om: [pv_system.annual_fixed_om]
    variable_om: [pv_system.period_variable_om]
    deductible_maintenance: {mode: explicit_items, items: []}
    depreciation:
      - {base_from: {kind: upfront_capex_net, finance_type: pv_system, scope: {kind: all_new_instances}}, method: straight_line, life_years: 20, residual: {value: "0.04", unit: "1"}, first_year: 1}
    loss_carryforward_years: 5
    opening_losses: []
```

### Amount Sources and Depreciation Periods

`depreciation[].base_from` and `net_cashflows[].amount` share the following amount source definitions (named `{kind: asset, id}` and `{kind: amount_ref, id}` appear in [Named Amounts, Payments, and Assets](finance-relations.md)):

| `kind` | Structure and Meaning |
|---|---|
| `upfront_capex_net` | `{kind: upfront_capex_net, finance_type, scope}`, where `scope` is `{kind: all_new_instances}` or `{kind: instances, instances: [...]}`; equals actual construction payments of matching new instances minus deductible construction input VAT |
| `maintenance_expense_net` | `{kind: maintenance_expense_net, year, position}`; references the actual tax-inclusive maintenance payment at `(year, position)` minus its deductible input VAT |

- Straight-line annual depreciation equals `(B - R_effective) / life_years`: for ratio residuals `R_effective = residual * B`; fixed currency residuals apply `R` only when `B > 0` (`R <= B` required), yielding zero depreciation when `B = 0`.
- Initial construction depreciation MAY start in operating year 1, whereas renewal maintenance depreciation `first_year` MUST follow the payment's evaluation year; a maintenance item claimed as a depreciation base MUST NOT simultaneously set `deductible: true` for full current-period expense deduction.

### Explicit Net Cash Items

`net_cashflows.<id>` defines additional after-tax net cash inflows or outflows (never double-counted into energy, O&M, capex, or tax chains, nor taxed again; book residual values never convert automatically into cash):

| Field | Rules |
|---|---|
| `at` | `{kind: evaluation_end}` (terminal `CF[E]`) or `{kind: financial_year, year}` (integer index `0..E`) |
| `direction` | `inflow` / `outflow` |
| `amount` | `{kind: fixed, value: Money}` or `{kind: source_share, base_from, ratio: Money}` (also accepts `{kind: amount_ref, id}`) |

```yaml
net_cashflows:
  end_recovery:
    at: {kind: evaluation_end}
    direction: inflow
    amount:
      kind: source_share
      base_from: {kind: upfront_capex_net, finance_type: pv_system, scope: {kind: all_new_instances}}
      ratio: {value: "0.03", unit: "1"}
```

### Time Bases and Components

`FinanceProfile` preserves distinct time bases explicitly:

- `upfront_capex` (`currency`, one-time construction outlay), `annual_fixed_om` (`currency/year`, annual fixed O&M), and `period_variable_om`/`period_energy_*` (`currency`, operating window totals).
- `annual_fixed_om` MUST multiply explicitly by a time span (such as `1 year`) before summing with window totals; see [Financial Component Identifiers in Planning Expressions](assembly-yaml.md#financial-component-identifiers-in-planning-expressions).

### Calendar-Month Demand Charges

`demand_charges.<charge_id>` declares `{rate: Money}` in currency per power unit (e.g., `CNY/kW`, representing the monthly fee per unit of peak demand in each calendar month).

```yaml
demand_charges:
  import_demand:
    rate: {value: "20", unit: CNY/kW}  # Synthetic example; applies per calendar month, not a default rate
```

Catalog entries not referenced by `demand_bindings` in assembly stay inactive; Overrides MAY override only `rate` on existing `charge_id` entries and `demand_input_tax` rates on VAT rules, and MUST NOT modify `demand_cost` scope.

## `ies.finance-overrides@1.0.0`

### Complete Example

```yaml
schema: ies.finance-overrides
schema_version: "1.0.0"
profile_ref:
  id: cn-north-demo
finance_types:
  pv_system:
    upfront_capex:
      fixed: {value: "1500", unit: CNY}                    # Replaces Money atom at leaf path
      linear:
        capacity_kw: {unit_cost: {value: "3200", unit: CNY/kW}}
energy_prices:
  grid_import:                                             # Replaces pricing definition only (kind + value/series)
    kind: constant
    value: {value: "0.75", unit: CNY/kWh}
  pv_export:
    kind: time_series
    ref: "9999999999999999999999999999999999999999999999999999999999999999"
    series_meta:
      resolution: 1h
      point_count: 8760  # Single-year example (operating years confirmed at submission, 1 here; not a default); actual count must match total operating steps
      unit: CNY/kWh
```

### Field Table and Override Discipline

| Location | Allowed Override Scope and Restrictions |
|---|---|
| `schema` / `schema_version` / `profile_ref` | Fixed headers and `{id}`; an empty overrides document contains solely these three fields |
| `finance_types.*` | Replaces existing leaf `Money` atoms (`<component>.fixed` or `<component>.linear.<driver>.unit_cost`) in place with matching unit and non-negative value; adding/deleting types, components, or `driver` keys is prohibited |
| `energy_prices.*` | Replaces the entire pricing definition of an existing `price_id` (`constant` / `time_series` / complete 12-month `monthly_daily`) keeping unit intact while inheriting `carrier`/`direction` from Profile |
| `tax_rules.*` | Overrides rates/ratio atoms or `loss_carryforward_years` on existing rules, or replaces full tables for `depreciation`, `maintenance_input_tax`, `deductible_maintenance.items`, `opening_losses`, and construction `schedule`; adding/removing rules or changing pool bindings is prohibited |
| `net_cashflows.*` / `credit_pools.*` | Allows overriding timing/fixed amount/ratio on existing entries and `opening_credit`/`refunds` on existing pools; changing flow direction or `receivable_policy` is prohibited |
| Immutable fields | Top-level `currency`, `base_year`, `price_basis`, `cost_method`, `taxes`, etc. |

#### Example of Maintenance Tax Treatment and Capitalization Overrides

```yaml
schema: ies.finance-overrides
schema_version: "1.0.0"
profile_ref: {id: cn-north-demo}
tax_rules:
  operating_vat:
    maintenance_input_tax:
      - {year: 2, position: 1, rate: {value: "0.13", unit: "1"}, deductible_ratio: {value: "0", unit: "1"}}
      - {year: 2, position: 2, rate: {value: "0.13", unit: "1"}, deductible_ratio: {value: "1", unit: "1"}}
  income:
    deductible_maintenance:
      items:
        - {year: 2, position: 1, deductible: true}
        - {year: 2, position: 2, deductible: false}
```

Complete evaluation confirmation and Overrides example with capitalized renewal maintenance expenses:

```json
{
  "evaluation_years": 11,
  "construction_period": 2,
  "operation_years": 9,
  "discount_rate": 0.04,
  "construction_payments": [{"t": 2, "ratio": 1.0}],
  "maintenance_expenses": [
    {"year": 6, "currency": "CNY", "amount": "4500", "terms": []},
    {"year": 6, "currency": "CNY", "amount": "7000", "terms": []}
  ]
}
```

```yaml
schema: ies.finance-overrides
schema_version: "1.0.0"
profile_ref: {id: cn-north-demo}
tax_rules:
  operating_vat:
    maintenance_input_tax:
      - {year: 6, position: 1, rate: {value: "0.1", unit: "1"}, deductible_ratio: {value: "1", unit: "1"}}
      - {year: 6, position: 2, rate: {value: "0.1", unit: "1"}, deductible_ratio: {value: "0", unit: "1"}}
  income:
    deductible_maintenance:
      items:
        - {year: 6, position: 1, deductible: false}
        - {year: 6, position: 2, deductible: false}
    depreciation:
      - base_from: {kind: upfront_capex_net, finance_type: pv_system, scope: {kind: all_new_instances}}
        method: straight_line
        life_years: 20
        residual: {value: "0.04", unit: "1"}
        first_year: 1
      - base_from: {kind: maintenance_expense_net, year: 6, position: 1}
        method: straight_line
        life_years: 3
        residual: {value: "0", unit: CNY}
        first_year: 5
      - base_from: {kind: maintenance_expense_net, year: 6, position: 2}
        method: straight_line
        life_years: 4
        residual: {value: "0.05", unit: "1"}
        first_year: 5
```

## `ies.effective-finance-config@1.0.0`

Produced by deterministic merger `merge(FinanceProfile, FinanceOverrides | None)` (unoverridden entries pass through verbatim):

```yaml
schema: ies.effective-finance-config
schema_version: "1.0.0"
profile_id: cn-north-demo
currency: CNY
base_year: 2025
price_basis: tax_inclusive
cost_method: fixed_plus_linear
finance_types:
  pv_system:
    upfront_capex:                                     # Overridden: fixed 1500, linear 3200
      fixed: {value: "1500", unit: CNY}
      linear:
        capacity_kw: {unit_cost: {value: "3200", unit: CNY/kW}}
    annual_fixed_om:                                   # Unchanged from Profile
      fixed: {value: "1200", unit: CNY/a}
      linear:
        capacity_kw: {unit_cost: {value: "35", unit: CNY/kW/a}}
    period_variable_om:                                # Unchanged from Profile
      linear:
        generated_kwh: {unit_cost: {value: "0.01", unit: CNY/kWh}}
  battery_system:                                      # Unchanged from Profile
    upfront_capex:
      linear:
        energy_kwh: {unit_cost: {value: "900", unit: CNY/kWh}}
    annual_fixed_om:
      linear:
        energy_kwh: {unit_cost: {value: "18", unit: CNY/kWh/a}}
    period_variable_om:
      linear:
        cycled_kwh: {unit_cost: {value: "0.002", unit: CNY/kWh}}
energy_prices:
  grid_import:                                         # Overridden pricing: constant 0.75
    carrier: electricity
    direction: purchase
    kind: constant
    value: {value: "0.75", unit: CNY/kWh}
  pv_export:                                           # Overridden pricing: switched to another full-horizon series
    carrier: electricity
    direction: sale
    kind: time_series
    ref: "9999999999999999999999999999999999999999999999999999999999999999"
    series_meta:
      resolution: 1h
      point_count: 8760  # Single-year example (operating years confirmed at submission, 1 here; not a default); actual count must match total operating steps
      unit: CNY/kWh
  heat_supply:                                         # Unchanged from Profile
    carrier: heat
    direction: purchase
    kind: constant
    value: {value: "0.35", unit: CNY/kWh}
demand_charges: {}                                     # Inherited from Profile unchanged
taxes:                                                 # Inherited from Profile unchanged
  grid_import_vat:
    display_name: Grid Import VAT
    tax_type: value_added_tax
    applies_to: grid_import
credit_pools: {}
tax_rules: {}
net_cashflows: {}
```

## Canonicalization, Aggregation, and Billing Rules

When no overrides apply, submit the following empty overrides document:

```yaml
schema: ies.finance-overrides
schema_version: "1.0.0"
profile_ref:
  id: <profile-id>
```

### Aggregation and Billing Interface

| Aggregation Enum | Valid Target | Semantics |
|---|---|---|
| `scalar` | Time-invariant device `property` | Reads scalar property value directly; permitted only in `upfront_capex` and `annual_fixed_om` (invalid for `period_variable_om` or energy billing) |
| `integrate_positive` | Non-negative power/rate `interface` | Integrates over project step duration `e[t] = p[t] * step_duration` |
| `sum_positive` | Interval-cumulative energy/quantity `interface` | Used directly as step series `e[t]` without multiplying by `step_duration` |

- Each `tariff_binding` bills point-by-point along the non-negative energy series `e_b[t]` (multiplying total energy by an averaged price is prohibited):
  ```text
  raw_charge(b)             = Σ_t price_b[t] * e_b[t]
  period_energy_purchase(b) = +raw_charge(b)   # direction: purchase
  period_energy_sale(b)     = -raw_charge(b)   # direction: sale
  ```
- `period_energy_purchase` and `period_energy_sale` already carry accounting signs (negative prices reverse naturally), so planning expressions always combine components additively.
