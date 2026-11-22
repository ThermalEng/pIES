# Network Boundaries and System Equations

> Owners: `assembly` (port boundaries and references), `modeling` (equation AST and math contributions); Governing Spec: [Architecture Constitution](../ARCHITECTURE_CONSTITUTION.md)

This page defines **complete network boundary semantics** and the optional **`system_equations` block** contract inside Assembly YAML; basic connections and `step_relation` constraints appear in [Assembly YAML](assembly-yaml.md).

## Scope and Applicability

pIES strictly separates physical port connections, closed port boundaries, internal device conversions, and cross-instance allocation/source accounting:

- **Shared buses**: Form ideal mixing buses across connectable ports per [Assembly Connections](assembly-yaml.md#connections);
- **System equations (`system_equations`)**: Express cross-instance energy allocation, source tracing, auxiliary system accounts, and multi-device state couplings while reusing `modeling`'s unified equation AST and unit system without introducing prices or financial objectives (financial references to auxiliary quantities require explicit `system_quantity` declarations under `amounts`).

## Complete Network Boundaries

| Port / Variable Class | Network Semantics |
|---|---|
| Connected `in` / `out` / `bidirectional` | Joins its carrier's bus partition and obeys step-by-step flow conservation (`out` positive supply, `in` negative absorption, `bidirectional` signed net supply) |
| Unconnected `in` / `out` / `bidirectional` | Independent closed boundary forcing flow equal to `0` at every operating step without inventing external sources/sinks |
| `predefined` | Retains externally bound data-source series; excluded from bus conservation and not zeroed when unconnected |
| `blind` and internal device variables | Preserves internal device equation semantics; neither wired externally nor zeroed |

- **Closed-boundary semantics**: Leaving a port unconnected is an explicit mathematical boundary (zero-flow constraint), not an input validation error; conflicts with device or load equations are resolved by unified analysis or the solver. External energy inputs and exports MUST attach explicit source/sink devices.
- **Separated valid domains**: Different ports on the same ideal bus need not have pairwise intersecting valid intervals; feasibility follows from total bus conservation combined with each device's own equations.

## `system_equations` Field Specification

Optional top-level assembly field `system_equations` (omission equals `{}`) maps `lower_snake_case` block IDs to blocks defining local constants, references, auxiliary variables, and equation relations:

| Field | Rules and Types |
|---|---|
| `constants` | Map `name -> {value, unit}` of known local technical coefficients (cannot be optimization variables) |
| `references` | Map of local aliases pointing to existing assembly instance `interface`, `property`, or internal `variable` symbols (see below) |
| `variables` | Map `name -> {unit, valid_range, initial?}` of block-local auxiliary step variables; `valid_range` and `initial` follow device equation rules and may reference same-dimension local constants or `property` references |
| `relations` | Ordered list `[{id, expression}]`; reuses restricted device equation syntax supporting `[t]` and `[t-1]` for initialized state variables inside this block (`step_duration` alias collisions prohibited) |

Each entry in `references` is one of three discriminated union variants:

| `kind` | Complete Structure and Target |
|---|---|
| `interface` | `{kind: interface, instance, interface}`; references an actual instance interface (`predefined` becomes materialized input; others are model variables) |
| `property` | `{kind: property, instance, property}`; references an instance technical property (binds to the planning variable if controlled by planning) |
| `variable` | `{kind: variable, instance, variable}`; references an internal variable from `equations.variables` |

System equation blocks are mutually independent and cannot read each other's internal variables directly; complex cross-instance couplings SHOULD reside within the same block.

### Allocation and Source Accounting Example

System equations supply general primitives for directional allocation and source energy accounts; required conservation, efficiency, and mutual exclusivity relations MUST be written explicitly by the model (omitting a bus wire alone does not prove one source cannot indirectly displace another across a bus):

```yaml
system_equations:
  delivery_account:
    constants: {}
    references:
      supply: {kind: interface, instance: s, interface: power_out}
      demand: {kind: interface, instance: l, interface: power_in}
    variables:
      delivered:
        unit: kW
        valid_range: {minimum: 0, maximum: null}
    relations:
      - {id: source_total, expression: "supply[t] = delivered[t]"}
      - {id: sink_total, expression: "demand[t] = delivered[t]"}
```

## Saving, Freezing, and Public Outputs

- **Editing and freezing**: The workbench "System technical equations" editor and draft commands manage the complete `{system_equations, outputs}` fragment; drafts, project versions, canonical assembly, and `CalcSnapshot` preserve every block definition and serialized AST verbatim without filtering by form allowlists.
- **Auxiliary series output**: Declare auxiliary system curves to publish via `outputs.system_series: [{block, variable}]` (omitting equals `[]`); output identity, paginated reading, and Excel export contracts appear in [System Series Mapping](solver-bundle.md#system-series-mapping) and [Step Series Reading](../contracts.md#step-series-reading).
- **Capabilities and diagnostics**: Task submission checks `network.closed_ports`, `modeling.system_equations`, and `outputs.system_series` capabilities according to actual usage; invalid fields or references report block and relation IDs at the assembly boundary, while lowering gaps report by model provenance during generation.
