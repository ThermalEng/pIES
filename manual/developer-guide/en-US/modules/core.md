# Core Fundamentals

> Code Boundary: `backend/iesplan/core/`; Governing Spec: [Architecture Constitution](../ARCHITECTURE_CONSTITUTION.md)

`core` provides the stateless common foundation shared across all backend business modules, allowing modules to exchange semantically consistent primitive value objects and error structures without depending on one another. The criterion for whether a capability belongs in `core` is: **does it remain valid without any notion of projects, devices, tasks, or users?**

## Capability Scope

| Area | Public Capabilities | Key Invariants |
|---|---|---|
| Base value objects and errors | `AppError` hierarchy, stable ID types | Immutable; external IDs never participate in arithmetic |
| Structured diagnostics | `Diagnostic` and centrally registered codes | Carries only `code`, `message_key`, `params`, and location without hardcoded user prose; unregistered codes fail immediately |
| Units and dimensions | Unit normalization, dimensional checks, linear conversion | No implicit guessing; incompatible dimensions fail immediately |
| Sequence time axis | Contiguous `step` axis, resolutions, Gregorian calendar derivation | Rejects `NaN`/`Infinity`, non-contiguous `step`, and implicit time-zone shifts |
| Safe YAML & canonical JSON | YAML 1.2 safe-subset parsing and deterministic JSON encoding | Arbitrary object construction prohibited; identical semantics yield identical canonical bytes |
| Cryptographic primitives | Password hashing, CSPRNG tokens | Never stores or emits plaintext secrets |

## Constraints and Verification

- **Pure dependency constraint**: `core` MAY depend only on the Python standard library and explicitly approved foundational packages; importing any business module or reading environment variables, databases, or object storage is strictly prohibited.
- **Error representation**: Structured validation over batch inputs SHOULD aggregate a `Diagnostic` collection; programming contract violations or unrecoverable calls raise an explicit `AppError`.
- **Testing requirements**: All public pure functions MUST test valid boundaries, invalid inputs (non-finite numbers, invalid units, non-contiguous steps), and output determinism.
