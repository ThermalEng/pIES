# Extension System

> Status: Effective Blueprint; Spec Version: 1.1.0; Governing Spec: [Architecture Constitution](ARCHITECTURE_CONSTITUTION.md)

pIES separates "declarative business content" from "executable computation capabilities": device models plug in as pure data documents, whereas computation generation, controlled execution, result adaptation, and storage register via their owning module's public `Protocol`.

## Division of Extension Roles

| Extension Type | Owning Module | Pinning and Versioning Mechanism | Discovery and Runtime Behavior |
|---|---|---|---|
| Device Technical Model | `devices` | Stable `device.id` + publication `revision` + unified `ies.device-model` schema (no per-device semver) | Published via declarative YAML parsing; all devices share one parser and equation AST |
| Computation Stages & Algorithm Plugins (`1.1.5` / `1.2.0` target) | `computation` / `customization` | Unified `ies.algorithm-plugin@1.0.0` (`plugin.yaml`) covering `predictor`, `generator`, `solver`, `evaluator`, and `algorithm`, pinned by ID + three-part semver | Both built-in and third-party plugins package into computation images launched by `backend` via `job_runner` as ephemeral network-disabled containers; never hot-loaded into control-plane processes |
| Storage Adapter | `storage` | Namespaced ID + three-part semantic version | Injected by composition root; handles byte-level I/O and health state only |

Official executable Providers are discovered, dependency-checked, and atomically published exclusively by the composition root at process startup; runtime hot-reloading and cross-module global registries are not supported.

## Boundary Split and Runtime Modularity Gates

- **Containerize at file handoffs and immutable compute nodes**: Containers wrap only compute nodes requiring isolation, resource governance, or replaceability; equation parsing, unit conversion, assembly validation, and pure math helpers remain reusable Python libraries without artificial container-to-container RPC.
- **Four runtime modularity gates**:
  1. **Independent build**: Computation plugins build and self-test completely detached from the full backend application;
  2. **Independent execution**: Plugins run solely against the public SDK and canonical `/work/in` inputs to emit valid outputs;
  3. **Replaceability**: Swapping any contract-compliant plugin requires zero changes to `backend` orchestration, API routes, or unrelated domain modules;
  4. **Dependency isolation**: The SDK and computation images MUST NOT depend on databases, ORMs, Redis, or backend internal services, verified by running a standalone reference plugin authored solely against public SDK documentation without touching main-app business logic.

## Device Content Integration

Device descriptors originate solely from [Device Model YAML](formats/device-model-yaml.md), defining only time-invariant technical `properties`, five classes of time-series `interfaces`, and restricted declarative `equations`.

- Device definitions MUST NOT carry costs, prices, taxes, calculation precision, or execution paths;
- `predefined` interfaces declare only slot metadata, units, and valid ranges inside the device definition; project-level data sources (`constant` / `data_repeat` / `data_full` / `data_predict`) bind on assembly instances;
- Adding new devices requires no device-ID special cases in the frontend, assembly, or generators.

## Equation Capability Extension

`modeling` maintains a unified restricted equation syntax, common AST, and math contribution contract—never a per-device command registry. Extending equation operators or functions MUST satisfy:

1. Uniform syntax, type, dimensional, and time-index rules across all devices;
2. Explicit complexity bounds and deterministic behavior for the operator;
3. Solver-independent output variables, constraints, and mapping metadata;
4. Coordinated updates to generic generator lowering and contract tests.

## Computation Provider Architecture

Computation decouples mathematical formulation from process execution across three independent roles:

- **`GeneratorProvider`**: Directly consumes issued `ValidatedAssemblyArtifact`, device math contributions, materialized full-horizon series, `EffectiveFinanceConfig`, `CalculationConfig`, and frozen evaluation confirmation; performs the sole business-to-solver-unit conversion; and deterministically produces a [Solver Bundle](formats/solver-bundle.md).
- **`ExecutorProvider`**: Maintains the trusted executable allowlist, argument and environment filtering rules, and resource/network isolation policies; executes structured commands inside the Bundle; and returns an `ExecutionReceipt`.
- **`ResultAdapterProvider`**: Consumes the read-only Bundle, receipt, and raw solver outputs, restoring solver status, objective, variables, and financial facts into a `ComputeResult` with canonical business units.

Each Provider declares supported schemas, modes, capability sets, an Options JSON Schema (Draft 2020-12), and companion dependency versions through its Descriptor.

## Storage and External Data Sources

- **Storage adapters**: Encapsulate only raw blob read/write/delete and existence/size queries; object identity, owner reference manifests, retention policies, and crash reconciliation belong to the `storage` domain service.
- **External time-series data**: Weather, load, and price data pass format and unit validation first to become immutable dataset versions or price objects before binding in assembly or finance files; data preparation preserves original source resolution and contiguous `step` indices without implicit interpolation or resampling.

## Standard Workflow for Developing a New Provider

1. Identify the target owning module and single Provider role, and assign a stable namespaced ID and semantic version;
2. Implement the module's public `Protocol` and Descriptor (including options schema and self-check method);
3. Verify valid inputs, invalid inputs, unit boundaries, and determinism using the module's contract test suite;
4. Register the candidate Provider in the composition root and verify dependency resolution and partial-failure rollback;
5. Prove via end-to-end tests that integration succeeds without modifying API, assembly, or frontend type maps.
