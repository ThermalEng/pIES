# Assembly YAML

> Contract ID: `ies.assembly`; Target `schema_version`: `"1.0.0"`; Recommended Filename: `<assembly-id>.assembly.yaml`

Assembly YAML defines the complete verifiable system modeling and planning inputs: the project computation baseline, device instances and data source bindings, physical port connections, explicit device cost and metering bindings, an exact reference to the immutable effective finance snapshot, and planning configuration. It excludes solver options (`generator`/`solver`/precision, which belong to `CalculationConfig`), shell commands, and host paths. Versioning rules appear in [Single Current Contract and Project Package Dependencies](../file-formats.md#single-current-contract-and-project-package-dependencies).

## Minimal Structural Example

```yaml
schema: ies.assembly
schema_version: "1.0.0"

assembly:
  id: campus_demo
  name: Minimal Campus Demo

project_baseline:
  resolution: 1h
  scenario_mode: single
  start_year: 2025

devices:
  grid:
    definition:
      id: ies.device.grid_connection
    asset_origin: existing
    properties: {}
    predefined_interfaces: {}
  pv_1:
    definition:
      id: ies.device.pv
    asset_origin: new
    properties:
      rated_capacity_kwp:
        value: 100
        unit: kWp
    predefined_interfaces:
      solar_irradiance: {mode: constant, value: 800}
      ambient_temperature: {mode: constant, value: 25}
  load:
    definition:
      id: ies.device.electric_load
    asset_origin: existing
    properties: {}
    predefined_interfaces:
      electricity_demand:
        mode: data_repeat
        path: data/campus_load.data.csv

connections:
  grid_to_load:
    from: grid.electricity_import
    to: load.electricity_in
  pv_to_grid:
    from: pv_1.electric_out
    to: grid.electricity_export

finance:
  ref:
    kind: object                     # or relative_file inside packages
    object_id: "4444444444444444444444444444444444444444444444444444444444444444"
    media_type: application/yaml
  profile_id: cn-north-demo

finance_binding:
  instances:
    pv_1:
      type: costed
      finance_type: pv_system
      drivers:
        capacity_kw:
          property: rated_capacity_kwp
        generated_kwh:
          interface: electric_out
          aggregation: integrate_positive
    grid:
      type: none                     # Pure metering point; no device cost; energy fees in tariff_bindings
    load:
      type: none

tariff_bindings:
  import_meter:                      # binding_id: lower_snake_case, no __, distinct from instance_id
    price: grid_import
    instance: grid
    interface: electricity_import
    aggregation: integrate_positive
  export_meter:
    price: pv_export
    instance: grid
    interface: electricity_export
    aggregation: integrate_positive

demand_bindings: {}                  # Explicitly empty when no demand billing applies; independent of energy tariffs

planning:
  objective:
    sense: minimize
    expression: "1 year * pv_1__annual_fixed_om + pv_1__period_variable_om + import_meter__period_energy_purchase + export_meter__period_energy_sale"
  variables:
    pv_capacity:
      device_ref: pv_1
      parameter: rated_capacity_kwp
      lower_bound: "0"
      upper_bound: "1000"
      unit: kWp
  constraints:
    annual_purchase_cap:
      type: annual_purchase_cost_limit
      expression: "import_meter__period_energy_purchase <= 900000 CNY"
    export_step_cap:
      type: step_relation
      references:
        boundary: {instance: grid, interface: electricity_export}
        supply: {instance: pv_1, interface: electric_out}
      expression: "boundary[t] <= supply[t]"
      enabled: true

extensions: {}
```

## Top-Level Field Responsibilities

| Field | Purpose |
|---|---|
| `assembly` | Stable identifier `id` and human-readable `name` |
| `project_baseline` | Project-creation `resolution`, `scenario_mode` (`single`), and Gregorian `start_year` |
| `devices` | Device instances, referenced device types, asset origin (`existing`/`new`), property overrides, and `predefined_interfaces` source bindings |
| `connections` | Directed edges between real connectable `<device>.<interface>` ports |
| `system_equations` | Optional mapping of system equation blocks; full schema in [Network Boundaries and System Equations](network-relations.md) |
| `finance` | Exact reference to the `EffectiveFinanceConfig` merged from `FinanceProfile` + `FinanceOverrides` prior to assembly |
| `finance_binding` | Per-instance cost participation discriminated union (`costed` / `none`) and `driver` mappings |
| `tariff_bindings` | Energy price meter bindings: `binding_id -> {price, instance, interface, aggregation}` |
| `demand_bindings` | Calendar-month demand meter binding map (`{}` when inactive) |
| `planning` | Optimization objective, planning variables, optional `construction_units`, and `constraints` |
| `outputs.system_series` | Optional auxiliary system output selection list; see [Saving, Freezing, and Public Outputs](network-relations.md#saving-freezing-and-public-outputs) |
| `extensions` | Namespaced extension section |

## Device Instances and Predefined Sources

- **Instance rules**: `asset_origin` is required (`existing` or `new`); `properties` MAY override only properties declared on the device model while preserving explicit units; `blind` interfaces cannot be wired or source-bound.
- **Predefined interface bindings**: Every `predefined` slot on a device MUST declare exactly one of the following discriminated-union sources under `predefined_interfaces` (`path` is a project-relative CSV path; full operating-period row count for `data_full` is checked at task submission):
  - `{mode: constant, value}`
  - `{mode: data_repeat, path}`
  - `{mode: data_full, path}`
  - `{mode: data_predict, path, target_type}`

## Connections

Each `{from, to}` item in `connections` joins two real ports `<device-instance>.<interface-id>`:

- Transitive closures grouped by energy carrier form ideal shared buses satisfying step-by-step flow conservation (`out` positive supply, `in` negative absorption, `bidirectional` signed net supply). Distinct ports on the same device never short-circuit automatically.
- Connected ports MUST share the same energy carrier and compatible dimensions; self-loops, duplicate edges, and referencing `predefined` or `blind` ports are prohibited.
- Connectable ports participating in no connection act as isolated closed boundaries (zero-flow constraint); complete boundary and allocation rules appear in [Port-Level Network Semantics](../modules/assembly.md#port-level-network-semantics) and [Complete Network Boundaries](network-relations.md#complete-network-boundaries).

## Project Computation Baseline and Finance Reference

- **`project_baseline`**: `resolution` MUST divide one day evenly; `start_year` is the Gregorian operating start year (leap years derived from the Gregorian calendar with no `leap_year` flag), independent of the financial `base_year`; `step_duration` derives uniquely from `resolution`.
- **`finance` reference**: `assembly.finance` stores only an object-storage or relative-path reference to `EffectiveFinanceConfig` plus source `profile_id`—never inlining a full finance config inside assembly.

## Planning Configuration, Financial Identifiers, and Constraints

`planning.constraints.<id>` supports scalar financial/planning constraints, `step_relation`, and `amount_relation`; omitting `enabled` defaults to `true`.

### Financial Component Identifiers in Planning Expressions

Full-horizon after-tax optimal planning uses the reserved objective `{sense: maximize, expression: system.after_tax_npv}` (valid solely as the complete objective expression; see [Planning Objective and Same-Source Evaluation](../modules/finance.md#planning-objective-and-same-source-evaluation)).

Ordinary financial scalar expressions map components using the following unique identifiers (`instance_id`, `binding_id`, and `billing_id` share one `lower_snake_case` namespace free of `__` and MUST NOT collide):

```text
Device cost component:   <instance_id>__<upfront_capex | annual_fixed_om | period_variable_om>
Energy tariff component: <binding_id>__<period_energy_purchase | period_energy_sale>
Demand charge component: <billing_id>__period_demand_charge
```

- **Sign and suffix alignment**: Energy tariff suffixes MUST strictly match the referenced `energy_prices` entry's `direction` (`purchase|sale`); `purchase` records `+raw_charge` while `sale` records `-raw_charge` (`raw_charge = Σ price[t] * e[t]`, negative under positive export tariffs), so objective expressions always combine components additively (e.g., `+ export_meter__period_energy_sale`).
- **Incremental and sunk exclusion**: `existing` instances exclude and MAY NOT reference `upfront_capex` or `annual_fixed_om` components.
- **Dimensions and unit literals**: `annual_fixed_om` has dimension `currency/year` and MUST multiply explicitly by a time span literal (e.g., `1 year`) before summing with horizon totals (`currency`); dimensional constants MUST carry units (e.g., `900000 CNY`), whereas dimensionless weights MAY use bare numbers (e.g., `0.5`). Trade-offs between upfront investment and operating expenses MUST be explicitly written in the planning expression; finance Profiles perform no implicit annualization.

## Step Technical Constraints

Cross-instance step-level port relations are declared in `planning.constraints.<id>` with `type: step_relation`:

| Field | Semantics |
|---|---|
| `type` | Fixed to `step_relation` |
| `references` | Required non-empty map from local alias to `{instance, interface}` (restricted to non-scalar connectable ports) |
| `expression` | Required affine relation (`==` / `<=` / `>=`) referencing ports as `alias[t]`, planning variables without subscripts, plus optional unit-bearing constants and `step_duration` |
| `enabled` | Boolean, defaulting to `true`; `false` preserves the definition without imposing the constraint |

When enabled, the constraint applies at every operating step `t` (without cross-step state indexing or source isolation; use `system_equations` when auxiliary local variables or source accounts are needed). Requires generator capability `technical.step_constraints`.

## Amount Reference Constraints

`planning.constraints.<id>` MAY declare step-free scalar monetary/rate constraints `type: amount_relation`, binding explicit-period named amounts or frozen baseline values to local aliases:

| Field | Semantics |
|---|---|
| `type` | Fixed to `amount_relation` |
| `references` | Local alias → `{kind: amount, id, period}` or `{kind: baseline, id, period}` |
| `expression` | Affine `==` / `<=` / `>=` expression consuming local aliases and unit-bearing constants |
| `enabled` | Boolean, defaulting to `true` |

`period` permits only `scalar`, fixed `financial_year`/`tax_month`, `year_months`, and `range` (`current` is prohibited), and compared sides MUST share matching currency amount or rate dimensions. Example:

```yaml
planning:
  constraints:
    spend_cap:
      type: amount_relation
      references:
        spend: {kind: amount, id: additional_spend, period: {kind: financial_year, year: 2}}
        budget: {kind: baseline, id: budget_limit, period: {kind: financial_year, year: 2}}
      expression: "spend <= budget"
      enabled: true
```

Enabling this constraint requires generator capability `finance.amount_constraints` and the corresponding source capabilities.

## Construction Activation Target Semantics

Construction choices and multi-scale coupling for new devices are declared explicitly in `planning.construction_units` (omission equals `{}`; optional construction switches are never inferred from positive fixed fees or zero lower bounds):

| Field | Rules |
|---|---|
| `construction_units.<unit_id>` | Stable local ID of the construction unit |
| `device_ref` | Required reference to one `asset_origin: new` assembly instance (each instance belongs to at most one construction unit) |
| `mode` | Required: `mandatory` (always built, status fixed at `1`) or `optional` (binary decision `z ∈ {0, 1}`) |
| `scales.<var_id>.active_lower_bound` | Non-negative decimal string (defaults to `"0"`), active minimum scale enforced only when built |
| `scales.<var_id>.active_upper_bound` | Non-negative decimal string or `null` (defaults to `null`) |

When an `optional` unit is inactive (`z = 0`), associated planning variables equal zero; when active (`z = 1`), they obey the active bounds intersected with unconditional `lower_bound`/`upper_bound` and model valid ranges; any required active upper bound is derived by [Unified Bounds](../modules/generators.md#unified-bounds). Example:

```yaml
planning:
  construction_units:
    storage_build:
      device_ref: store
      mode: optional
      scales:
        store_energy: {active_lower_bound: "0", active_upper_bound: null}
        store_power: {active_lower_bound: "0", active_upper_bound: null}
```

### Cost Occurrence Conditions

`type: costed` instances MAY specify via `cost_occurrence` which costs trigger with the instance's construction state (undeclared or uncovered leaves default to unconditional `{kind: always}`):

| Key / Field | Syntax and Semantics |
|---|---|
| Path keys | `upfront_capex`, `annual_fixed_om`, `period_variable_om` as whole terms or `.fixed` / `.linear.<driver>` sub-paths (named item paths appear in [Named Cost Items](finance-relations.md#named-cost-items)); whole paths and sub-paths MUST NOT overlap |
| Condition values | `{kind: always}` or `{kind: construction_unit, unit_ref: <unit_id>}` (MAY reference only this instance's own construction unit) |

## Device Cost Bindings (`finance_binding`)

`finance_binding.instances` MUST cover every device instance explicitly (`type: costed` binds `finance_type` and `drivers`; `type: none` indicates zero device cost and MUST NOT carry `finance_type` or `drivers`):

- **Aggregation semantics**: `property` maps scalars (`aggregation` omitted); time-series `interface` mappings MUST specify `integrate_positive` (integrating non-negative power by `step_duration`) or `sum_positive` (summing interval energy directly); negative series are rejected, and ambiguous net/absolute modes are disallowed.
- **Driver completeness**: `new` instances MUST map every linear `driver` declared across `upfront_capex`, `annual_fixed_om`, and `period_variable_om` on their `finance_type`; `existing` instances map only `drivers` required by `period_variable_om`. Cost coefficients multiplied by canonical `driver` units MUST match `currency` or `currency/year`.

## Energy Tariff Bindings (`tariff_bindings`)

`tariff_bindings` explicitly maps `energy_prices` entries from the effective finance snapshot onto metering interfaces:

```yaml
tariff_bindings:
  import_meter:
    price: grid_import
    instance: grid            # may be type: costed or type: none
    interface: electricity_import
    aggregation: integrate_positive
```

- Metered ports MUST be unidirectional time-series `in` or `out` interfaces (`bidirectional` ports and scalar properties cannot be metered directly) and MUST match the price entry's carrier.
- Physical port direction (`in`/`out`) and price entry accounting direction (`purchase|sale`) are declared independently and never inferred from one another.

## Calendar-Month Demand Bindings (`demand_bindings`)

`demand_bindings` is required (explicit `{}` when no demand charges apply), formatted as `billing_id -> {charge, instance, interface}`:

- `charge` references a `demand_charges` entry in the effective snapshot; `instance.interface` MUST be a non-negative `out` electricity power time-series interface (neither energy-unit nor bidirectional).
- The same `(charge, instance, interface)` tuple MUST NOT bind twice; each binding exposes a currency-dimensioned horizon component `<billing_id>__period_demand_charge`, billed monthly from each operating calendar month's actual peak power times monthly rate inside the same solve and financial evaluation.

## Validation Flow and `ValidatedAssemblyArtifact`

1. **Four-stage validation**: Executes sequentially as `structural checks → model & data checks → graph & system checks → planning & finance completeness checks`; any blocking error suppresses artifact emission.
2. **Immutable two-part artifact**: Canonical assembly bytes (stable field ordering, non-semantic whitespace stripped, pinned resource refs, canonicalization ID `ies.assembly.canonical@1.0.0`) together with a zero-blocking validation receipt form `ValidatedAssemblyArtifact`, the sole input credential accepted by downstream generators.
