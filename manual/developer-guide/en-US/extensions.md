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

## Computation Stages and Plugin Architecture

Computation decouples mathematical formulation, solver execution, and multi-dimensional evaluation across isolated container stages and platform runtime restoration (see [Algorithm Plugin Package](formats/algorithm-plugin-package.md) and [Models and Algorithms](customization-center.md)):

- **Prediction & core optimization plugins (`predictor` / `generator` / `solver` / `algorithm`)**: `predictor` plugins group targets by selected plugin in one logical stage (supporting joint multi-target prediction); `generator` performs the sole forward conversion from business units to solver internal units and emits solver inputs plus `variable_map`; `solver` consumes model files only and emits a solver-neutral canonical primal solution; `algorithm` skips the `generator + solver` split to emit a business-unit canonical engineering solution directly.
- **Platform runtime solution restoration (`ResultAdapter`)**: Automatically combines `variable_map` and the execution receipt right after staged `solver` completion to execute the sole reverse unit conversion into the same canonical engineering solution schema emitted by `algorithm`.
- **Parallel evaluation plugins (`evaluator`)**: `0..N` independent evaluators run in parallel against the read-only canonical engineering solution and framework-hardlinked frozen context; the official built-in financial evaluator emits standard financial evaluation facts, while third-party evaluators output self-describing `metrics` / `charts` / `tables` blocks rendered generically by the frontend.

Each plugin declares public `consumes` / `produces` data contract IDs, supported modes/precisions, and its dedicated `parameters_schema` (Draft 2020-12) in `plugin.yaml`, allowing drop-in replacement without composition-root code edits or hardcoded mutual plugin IDs.

## Storage and External Data Sources

- **Storage adapters**: Encapsulate only raw blob read/write/delete and existence/size queries; object identity, owner reference manifests, retention policies, and crash reconciliation belong to the `storage` domain service.
- **External time-series data**: Weather, load, and price data pass format and unit validation first to become immutable dataset versions or price objects before binding in assembly or finance files; data preparation preserves original source resolution and contiguous `step` indices without implicit interpolation or resampling.

## Standard Workflow for Developing a New Extension

1. Choose the extension category (declarative `ies.device-model` YAML, one of the five `ies.algorithm-plugin` kinds, or an internal storage adapter) and assign a stable namespaced ID (plus three-part semver for plugins);
2. Author `plugin.yaml` (declaring `consumes`/`produces`, `parameters_schema`, and network-free `tests` fixtures) and implement the entrypoint against public `pies-sdk` only;
3. Run `pies-sdk validate` and `pies-sdk pack` to verify archive safety, I/O contracts, unit boundaries, and deterministic offline self-tests;
4. Import offline into a local instance or submit to the public market (passing `job_runner`'s controlled build and `--network=none` self-test gate);
5. Verify via end-to-end tasks that replacement or parallel evaluation works without modifying backend scheduling, API routes, or frontend UI code.
