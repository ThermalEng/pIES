# Device Model YAML

> Contract ID: `ies.device-model`; Target `schema_version`: `"1.0.0"`; Recommended Filename: `<device-id>.device.yaml`

Device Model YAML defines a device's pure technical specification: time-invariant technical parameters (`properties`), external or data interfaces (`interfaces`), and declarative technical equations (`equations`). Device models carry no independent semantic version (pinned instead by stable ID, publication `revision`, and validation receipt), and MUST NOT contain prices, costs, financial assumptions, calculation precision, algorithm choices, or executable code paths.

## Complete Example

```yaml
schema: ies.device-model
schema_version: "1.0.0"

device:
  id: acme.device.heat_pump
  names:
    zh-CN: 热泵
    en-US: Heat Pump

properties:
  cop:
    value: 3.2
    unit: "1"
    valid_range:
      minimum: 1
      maximum: 10
interfaces:
  electricity_in:
    type: in
    carrier: electricity
    unit: kW
    valid_range:
      minimum: 0
      maximum: null
  heat_out:
    type: out
    carrier: heat
    unit: kW
    valid_range:
      minimum: 0
      maximum: null
equations:
  variables: {}
  relations:
    - id: heat_conversion
      expression: "heat_out[t] = electricity_in[t] * cop"
```

## Top-Level Fields

Published models require the following six fixed top-level fields (write `{}` or `[]` when empty); uninstantiated templates additionally allow top-level `inputs`, which is removed upon instantiation before full formal model validation.

| Field | Purpose |
|---|---|
| `schema` | Fixed to `ies.device-model` |
| `schema_version` | Fixed to `"1.0.0"` |
| `device` | Stable device identity `{id, names}`; `id` is a lowercase namespaced string (without per-device semver); `names` holds localized display labels |
| `properties` | Time-invariant technical constants |
| `interfaces` | Step-varying series interface definitions |
| `equations` | Restricted declarative relations across `properties`, interfaces, and internal variables |
| `inputs` | Uninstantiated templates only; declares isomorphic leaf paths users MUST supply upon instantiation |

## `properties`

Keys under `properties` are stable property IDs, each containing `value`, `unit`, and `valid_range`:

```yaml
properties:
  efficiency:
    value: 0.9
    unit: "1"
    valid_range:
      minimum: 0
      maximum: 1
```

- `value` MUST be a finite JSON scalar (type inferred from value; no extra type tag field).
- `unit` MUST be explicitly declared from the common unit vocabulary (`"1"` for dimensionless quantities).
- `valid_range` defines an optional closed interval `{minimum, maximum}` where `null` indicates unboundedness on that side.
- Optimization flags (such as `optimizable`, `stock_or_addition`), price/cost fields (such as `$price:`), and environment variable references are prohibited.

### Target Semantics of `valid_range` Under Planning Control

`valid_range` expresses the device's intrinsic technical valid domain rather than a default UI suggestion. When a property is taken over by a planning decision variable, the model's valid domain remains active and intersects with planning bounds (see [Unified Bounds](../modules/generators.md#unified-bounds)); default `value` numbers inside templates never act as planning upper bounds, and equation references to `property` stay symbolically linked rather than prematurely substituting constant defaults.

## Template `inputs`

Uninstantiated templates use `inputs` to declare required leaf fields pointing to existing structural paths, never to create new properties or interfaces:

- Each input leaf MUST declare `type` (`number` / `boolean` / `string` / `array` / `object` / `data_repeat` / `data_full` / `data_predict`); numeric inputs MUST declare `unit`.
- Data inputs (`data_repeat` / `data_full` / `data_predict`) hold `.csv` paths relative to the project directory (absolute paths, `..`, and URLs prohibited) and generate assembly data-source bindings during instantiation without entering the final pure technical model.
- Implicit fallbacks such as `default`, `source`, or `mode` inside template fields are prohibited; submission MUST cover every declared leaf and rejects extra fields.

## `interfaces`

All step-varying series are defined under `interfaces`, declaring `carrier`, `unit`, `valid_range`, and `type` (omitting `type` canonicalizes to `blind`):

| `type` | Semantics and Connection Rules |
|---|---|
| `in` | Receives series from compatible output ports of external devices |
| `out` | Supplies series to compatible input ports of external devices |
| `bidirectional` | Exchanges series in either direction on one port; participates in bus balance as signed net supply (positive supplying, negative absorbing) |
| `predefined` | Cannot wire on the bus; binds an external data source (`constant` / `data_repeat` / `data_full` / `data_predict`) explicitly under `predefined_interfaces` in assembly |
| `blind` | Neither connects externally nor accepts predefined data; determined solely by the device's `equations` |

- `valid_range` bounds on interfaces and internal variables accept numeric literals, `null`, or `{property_ref: <property_id>}` (dynamically resolved against the instance's overridden `property` value; canonical storage and reading MUST preserve the reference verbatim without expanding or nulling it).
- Device models themselves MUST NOT carry project-level source fields (`source`, `mode`, `value`, `data_ref`, `target_type`).

## `equations`

```yaml
equations:
  variables:
    stored_energy:
      unit: kWh
      valid_range:
        minimum: 0
        maximum: null
      initial:
        expression: "initial_soc * capacity_kwh"
  relations:
    - id: energy_transition
      expression: "stored_energy[t] = stored_energy[t-1] + (charge_in[t] * charge_efficiency - discharge_out[t] / discharge_efficiency) * step_duration"
```

- **Scope and units**: Expressions MAY reference only this device's declared `properties`, `interfaces`, `equations.variables`, the project baseline constant `step_duration`, and permitted mathematical operations; all quantities convert to SI for evaluation, and bare numbers represent SI constants (affine temperature units with offset such as `°C` SHOULD declare reference points via unit-bearing `property` definitions).
- **Time indexing**: Supports only `[t]` and `[t-1]` (`[t-k]` for `k > 1` is rejected). `[t-1]` at `t=0` substitutes the state variable's `initial` declaration.
- **Mutual exclusivity**: Supports non-negative complementarity equalities of the form `a[t] * b[t] = 0`.
- **Prohibitions**: Code/module paths, dynamic function calls, reflection, environment variables, and `eval` are strictly prohibited.

### State Initial Values

State variables declare their predecessor at `t=0` via `initial` using one of two mutually exclusive forms:

- `{property_ref: <property-id>}`: Directly references a scalar technical property;
- `{expression: "<expr>"}`: Combines non-indexed numeric `properties` of this device using basic arithmetic and parentheses (MUST NOT reference interfaces, internal variables, or `step_duration`), with resulting dimension matching the state variable.

When a referenced property is controlled by a planning decision variable, the initial value preserves its symbolic link to that planning variable (solvability depends on whether the chosen generator supports linear or bilinear expressions).

## Validation and Project Save Gate

Regardless of whether a candidate device originates from template instantiation, a structured form, or direct YAML editing, it passes through the exact same authoritative backend parser performing four-stage validation:

1. YAML 1.2 safe-subset parsing, duplicate-key check, `schema`, and unknown-field rejection;
2. Stable ID rules, `properties` scalar types, units, and valid ranges;
3. Five `interfaces` classes, energy carriers, and forbidden source-field checks;
4. Equation variables, symbol references, dimensions, and first-order time indexing.

When creating or saving a model inside a project, validation blocks invalid candidates inside the transaction (aggregating all independent diagnostics from that stage with YAML line/column numbers and field paths); upon passing, it atomically assigns an incrementing `_N` suffix and stores the canonical YAML alongside the validation receipt. Saving a device model does not handle or bundle data files directly; data binding happens during assembly.
