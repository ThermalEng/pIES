# Technical Equation Modeling

> Code Boundary: `backend/iesplan/modeling/`; Related Formats: [Device Model YAML](../formats/device-model-yaml.md), [Network Boundaries and System Equations](../formats/network-relations.md)

`modeling` parses restricted declarative expressions in device models and system equations into a unified common AST, performs lexical, semantic, and dimensional checks, and emits solver-independent declarative mathematical contributions (`ies.modeling.contribution@1.0.0`) for generators.

## Responsibilities and Boundaries

- **Owns**: Restricted equation lexing/parsing (`eval` and dynamic imports prohibited), common AST, identifier/unit/valid-range/time-index semantic checks, and canonical device- and system-level mathematical contributions.
- **Does not own**: Device catalog loading, project graph topology, planning objectives/constraints, financial parameters/prices, solver-private objects, or process execution.
- **No per-device command table**: All devices share the exact same equation syntax and lowering contract; per-device dispatch command registries MUST NOT exist.

## Conversion Pipeline

```text
DeviceDescriptor.equations / system_equations
  ↓ Safe lexing & parsing
Canonical Common AST (ies.equation.ast@1.0.0)
  ↓ Identifier, unit & time-index validation
Device / System Math Contribution Template
  ↓ Bind instance property overrides, interface refs & project baseline step_duration
Immutable Public Math Contribution (variables, relations, state transitions, initial_ast)
```

## Equation Language and Contribution Rules

1. **Restricted allowlist syntax**: Supports only numeric constants (converted to SI during evaluation), parentheses, basic arithmetic, comparison operators, and locally declared `properties`, `interfaces`, internal `variables`, plus the project's public `step_duration`.
2. **First-order time indexing**: Permits only `[t]` and `[t-1]`; `[t-k]` (`k > 1`) is rejected directly at the input validation boundary. At `t=0`, `[t-1]` substitutes the state variable's `initial` (`property_ref` or property expression `initial_ast`).
3. **Preserving links under planning takeover**: When a device property is controlled by a planning decision variable, the math contribution preserves symbolic links to that property/planning variable (including `property_ref` inside `valid_range` and `initial_ast`) so generators lower according to unified capabilities rather than baking in template defaults.
4. **System equations share the same kernel**: `system_equations` inside assembly reuses this module's exact common AST, dimensional reduction rules, and mathematical contribution outputs.

## Extension and Verification Requirements

Before adding equation operators or expression capabilities, confirm whether they can be composed from the existing language; any new operator MUST specify exact type, dimension, time-index, and complexity bounds, include valid/invalid/dimension-mismatch tests, and work seamlessly through generic generators without device-ID special casing.
