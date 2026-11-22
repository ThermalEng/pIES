# Assembly and Validation

> Code Boundary: `backend/iesplan/assembly/`; File Contracts: [Assembly YAML](../formats/assembly-yaml.md), [Network Boundaries and System Equations](../formats/network-relations.md)

`assembly` is the synchronous validation gate bridging editable business state and mathematical solving. It accepts a project computation baseline, device instances, source declarations and dataset references, port connections, system equations, planning configuration, and an immutable `EffectiveFinanceConfig` snapshot, performs structural, model, network, and financial completeness checks, and issues an immutable `ValidatedAssemblyArtifact`.

## Responsibilities and Boundaries

- **Input sources**: Project exports (with pinned versions and configs) and hand-authored `ies.assembly` YAML share the exact same parsed model `AssemblySpec`, injected alongside the device catalog snapshot, equation contracts, and canonical dataset version references.
- **Output artifact**: `ValidatedAssemblyArtifact` (composed of canonicalized assembly bytes plus a validation receipt with full dependency locks), preserving exact business units; any blocking error produces an ordered diagnostic collection with no partial output.
- **Strict isolation**: Does not read ORMs or sessions, generate solver input files or commands, or choose `generator`/`solver`/precision (those live separately in `CalculationConfig`).
- **Validity vs. solvability**: Passing assembly indicates complete, domain-valid inputs—it does not guarantee a given generator supports every mathematical form or that an optimal mathematical solution exists.

## Four-Stage Validation and Canonicalization

1. **Structural checks**: YAML 1.2 safe-subset parsing, `schema` header, ID namespaces, and required fields.
2. **Model and data checks**: Verifies instance `properties`, `equations`, unique source bindings for every `predefined` interface (`constant` / `data_repeat` / `data_full` / `data_predict`), resolution consistency, and contiguous `step` rules (`data_full` and full-horizon price lengths undergo final step-count checks at task submission once operating years are confirmed).
3. **Graph and system checks**: Validates port connection directions, carrier and dimensional compatibility; rejects self-loops, duplicate edges, and wiring on `blind`/`predefined` ports; verifies `system_equations` and `planning.constraints` (including `step_relation` and `amount_relation`) references.
4. **Planning and finance completeness checks**: Confirms instance `asset_origin` (`existing` / `new`), explicit device cost bindings (`costed` / `none` and incremental `driver` completeness), `tariff_bindings` and `demand_bindings` validity, and financial identifiers and dimensions in planning objectives.

After all four stages pass, the validator sorts mappings and unordered sets by stable keys, strips non-semantic whitespace and comments, and issues the canonical artifact bundled with version locks for devices, equations, and effective finance.

### Port-Level Network Semantics

The assembly domain owns unified port-network partitioning consumed identically by both validation and generators; implicit device-level port short-circuiting or duplicate connectivity algorithms elsewhere are prohibited:

- **Ideal shared buses**: Partition connectable ports (`in` / `out` / `bidirectional`) via the transitive closure of `{from, to}` pairs in `connections`; separate ports on the same device never merge automatically simply by belonging to the same device or carrier. Each bus satisfies step-by-step flow conservation (`out` positive supply, `in` negative absorption, `bidirectional` signed net supply per model).
- **Closed semantics for unconnected ports**: Any connectable port participating in no connection forms an isolated closed boundary (zero flow constraint at every step), never fabricating edges, free external supply, or unmeasured emissions. Buses with only sources or only sinks are not rejected as syntax errors; supply-demand feasibility is determined by actual equations and solver execution.
- **Source allocation and transfer boundaries**: Physical wiring, directional allocation, and cross-carrier conversion are distinct concepts. Whenever a power source's destination must be restricted or a source energy account established, declare auxiliary flows and conservation equations explicitly via `system_equations` in [Network Boundaries and System Equations](../formats/network-relations.md); anti-reverse-flow semantics MUST NOT be inferred merely from omitting a direct edge.
